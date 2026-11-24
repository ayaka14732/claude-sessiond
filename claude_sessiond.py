"""claude-sessiond: an HTTP daemon that hosts Claude Code sessions through the Claude Agent SDK.

Each hosted session is one long-lived `ClaudeSDKClient` (one `claude` subprocess). Messages are sent as ordinary user messages, exactly as if typed in the CLI. Permission prompts and AskUserQuestion calls are held open and answered over HTTP, or declined after a timeout. Idle sessions are put to sleep (subprocess exits, event buffer dropped) and resumed transparently from their transcript on the next message. Archived sessions are hidden from the session list until used again. When a turn fails on a usage or billing limit, the session moves to the next available account and continues.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import secrets
import sys
import time
import urllib.request
import uuid
import warnings
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

import uvicorn
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    ToolPermissionContext,
    UserMessage,
    delete_session,
    fork_session,
    get_session_info,
    get_session_messages,
    list_sessions,
    rename_session,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

PermissionMode = Literal['default', 'acceptEdits', 'plan', 'bypassPermissions', 'dontAsk', 'auto']
Effort = Literal['low', 'medium', 'high', 'xhigh', 'max']
EVENT_BUFFER = 5000
SENT_HISTORY = 1000
SSE_KEEPALIVE = 15.0
LOCAL_HOSTS = {'127.0.0.1', 'localhost', '::1'}
KEEP_ENV = {'CLAUDE_CONFIG_DIR', 'CLAUDE_CODE_OAUTH_TOKEN'}
WEBHOOK_TIMEOUT = 10.0
DEFAULT_ACCOUNT = 'default'
# `AssistantMessage.error` values that mean the account cannot serve requests for now.
LIMIT_ERRORS = ('rate_limit', 'billing_error')
# How long an account stays unavailable after a rate limit that came without a reset time (API keys get no `rate_limit_event`).
RATE_LIMIT_COOLDOWN = 300.0
CONTINUE_PROMPT = 'Continue from where you left off.'

def to_jsonable(o: Any) -> Any:
    """Serialize SDK dataclasses losslessly, tagging each with its class name under `_type`."""
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        d: dict[str, Any] = {'_type': type(o).__name__}
        for f in dataclasses.fields(o):
            d[f.name] = to_jsonable(getattr(o, f.name))
        return d
    if isinstance(o, dict):
        return {str(k): to_jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [to_jsonable(v) for v in o]
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    return repr(o)

def session_registry() -> Path:
    """The directory where running Claude Code processes register their open session, under `CLAUDE_CONFIG_DIR` like the SDK's session functions."""
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'sessions'

def holder_pid(session_id: str) -> int | None:
    """Return the pid of a running Claude Code process that has this session open, if any."""
    for f in session_registry().glob('*.json'):
        try:
            d = json.loads(f.read_text())
            if d.get('sessionId') != session_id:
                continue
            os.kill(d['pid'], 0)
            return d['pid']
        except (OSError, ValueError, KeyError):
            continue
    return None

def write_private(path: Path, text: str) -> None:
    """Atomically replace a file with one only its owner can read."""
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    tmp.replace(path)

def scrub_environment(keep: list[str]) -> None:
    """Drop CLAUDE* variables inherited from a parent Claude Code session.

    Without this, a daemon started from inside Claude Code would spawn sessions marked as child sessions (transcript saving off, wrong entrypoint). The config directory and the `claude setup-token` credential are kept.
    """
    for name in list(os.environ):
        if (name == 'CLAUDECODE' or name.startswith('CLAUDE_')) and name not in KEEP_ENV and name not in keep:
            del os.environ[name]

class SessionConfig(BaseModel):
    """Options that define how a session's `claude` process is started. Persisted across daemon restarts."""
    cwd: str
    name: str | None = None
    model: str | None = None
    permission_mode: PermissionMode | None = None
    effort: Effort | None = None
    append_system_prompt: str | None = None
    allowed_tools: list[str] = []
    disallowed_tools: list[str] = []
    add_dirs: list[str] = []
    max_budget_usd: float | None = None
    partial_messages: bool = False
    allow_bypass: bool = True
    checkpointing: bool = True
    account: str | None = None

class NewSession(BaseModel):
    cwd: str | None = None
    name: str | None = None
    model: str | None = None
    permission_mode: PermissionMode | None = None
    effort: Effort | None = None
    append_system_prompt: str | None = None
    allowed_tools: list[str] = []
    disallowed_tools: list[str] = []
    add_dirs: list[str] = []
    max_budget_usd: float | None = None
    partial_messages: bool = False
    allow_bypass: bool = True
    checkpointing: bool = True
    account: str | None = None
    resume: str | None = None
    fork: bool = False
    force: bool = False
    prompt: str | None = None
    wait: bool = False
    timeout: float | None = None

class PatchSession(BaseModel):
    name: str | None = None
    model: str | None = None
    permission_mode: PermissionMode | None = None
    effort: Effort | None = None
    account: str | None = None

class NewAccount(BaseModel):
    name: str
    oauth_token: str | None = None
    api_key: str | None = None
    env: dict[str, str] = {}

class Account(NewAccount):
    """Credentials a session's `claude` process runs under. `default` has none and uses whatever the daemon inherited."""
    limited: str | None = None
    limited_until: float | None = None

    def available(self) -> bool:
        """A billing limit has no reset time and lasts until the account is reset over the API."""
        return self.limited is None or (self.limited_until is not None and self.limited_until <= time.time())

    def environment(self) -> dict[str, str]:
        """Variables to set for the `claude` process. The other credential variable is set to the empty string, which the CLI treats as unset, so an inherited one cannot take precedence."""
        if self.oauth_token is not None:
            base = {'CLAUDE_CODE_OAUTH_TOKEN': self.oauth_token, 'ANTHROPIC_API_KEY': ''}
        elif self.api_key is not None:
            base = {'ANTHROPIC_API_KEY': self.api_key, 'CLAUDE_CODE_OAUTH_TOKEN': ''}
        else:
            base = {}
        return base | self.env

    def public(self, sessions: int) -> dict[str, Any]:
        """What the API shows: no secrets, and only the names of extra variables."""
        kind = 'oauth_token' if self.oauth_token is not None else 'api_key' if self.api_key is not None else 'inherited'
        return {'name': self.name, 'kind': kind, 'env': sorted(self.env), 'available': self.available(), 'limited': self.limited, 'limited_until': self.limited_until, 'sessions': sessions}

class SendMessage(BaseModel):
    text: str
    wait: bool = False
    timeout: float | None = None
    interrupt: bool = False
    force: bool = False

class Rewind(BaseModel):
    message_uuid: str
    dry_run: bool = False
    force: bool = False

class PromptAnswer(BaseModel):
    allow: bool
    message: str = ''
    updated_input: dict[str, Any] | None = None
    answers: dict[str, str | list[str]] | None = None
    response: str | None = None
    remember: bool = False
    interrupt: bool = False

@dataclasses.dataclass
class Prompt:
    info: dict[str, Any]
    tool_input: dict[str, Any]
    suggestions: list[Any]
    future: asyncio.Future
    reason: str = 'cancelled'

    def resolve(self, result: PermissionResultAllow | PermissionResultDeny, reason: str) -> None:
        if not self.future.done():
            self.reason = reason
            self.future.set_result(result)

class Conflict(Exception):
    pass

class Session:
    def __init__(self, daemon: 'Daemon', session_id: str, config: SessionConfig, created: float, last_active: float, seq: int = 0, archived: bool = False) -> None:
        self.daemon = daemon
        self.id = session_id
        self.config = config
        self.created = created
        self.last_active = last_active
        self.status = 'dormant'
        self.error: str | None = None
        self.client: ClaudeSDKClient | None = None
        self.task: asyncio.Task | None = None
        self.ready: asyncio.Future | None = None
        self.stop_event = asyncio.Event()
        self.events: deque[dict[str, Any]] = deque(maxlen=EVENT_BUFFER)
        self.seq = seq
        self.archived = archived
        self.tick = asyncio.Event()
        self.prompts: dict[str, Prompt] = {}
        self.outstanding: dict[str, asyncio.Future] = {}
        self.sent: dict[str, dict[str, Any]] = {}
        self.turn_active = False
        self.last_result: dict[str, Any] | None = None
        self.cost_usd = 0.0
        self.applied_name: str | None = None
        self.limit_error: str | None = None
        self.limit_resets: float | None = None
        self.relayed: list[asyncio.Future] = []
        self.switching = False

    # Event log
    def emit(self, kind: str, data: Any) -> None:
        self.seq += 1
        self.last_active = time.time()
        self.events.append({'seq': self.seq, 'time': self.last_active, 'kind': kind, 'data': data})
        self.tick.set()
        self.tick = asyncio.Event()

    def events_after(self, cursor: int) -> list[dict[str, Any]]:
        return [e for e in self.events if e['seq'] > cursor]

    async def wait_events(self, cursor: int, timeout: float) -> list[dict[str, Any]]:
        tick = self.tick
        batch = self.events_after(cursor)
        if batch:
            return batch
        try:
            await asyncio.wait_for(tick.wait(), timeout)
        except TimeoutError:
            pass
        return self.events_after(cursor)

    def set_status(self, status: str | None = None) -> None:
        if status is None:
            if self.client is None:
                status = 'dormant'
            elif self.prompts:
                status = 'waiting'
            elif self.turn_active or self.outstanding:
                status = 'running'
            else:
                status = 'idle'
        if status != self.status:
            self.status = status
            self.emit('status', {'status': status})

    def brief(self) -> dict[str, Any]:
        return {'id': self.id, 'name': self.config.name, 'cwd': self.config.cwd, 'status': self.status}

    def summary(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'status': self.status,
            'archived': self.archived,
            'created': self.created,
            'last_active': self.last_active,
            'config': self.config.model_dump(),
            'pending_prompts': [p.info for p in self.prompts.values()],
            'last_result': self.last_result,
            'cost_usd': self.cost_usd,
            'seq': self.seq,
            'error': self.error,
        }

    def announce_switch(self, previous: str | None, target: str, reason: str) -> None:
        account = self.daemon.account(previous)
        until = account.limited_until if account is not None else None
        data = {'from': previous, 'to': target, 'reason': reason, 'until': until}
        self.emit('account_switched', data)
        self.daemon.notify('account_switched', self, data)

    # Process lifecycle
    def options(self) -> ClaudeAgentOptions:
        c = self.config
        account = self.daemon.choose_account(c.account)
        if account.name != c.account:
            if c.account is not None:
                self.announce_switch(c.account, account.name, 'unavailable')
            c.account = account.name
        system_prompt: dict[str, Any] = {'type': 'preset', 'preset': 'claude_code'}
        if c.append_system_prompt:
            system_prompt['append'] = c.append_system_prompt
        exists = get_session_info(self.id, directory=c.cwd) is not None
        return ClaudeAgentOptions(
            cwd=c.cwd,
            model=c.model,
            permission_mode=c.permission_mode,
            effort=c.effort,
            system_prompt=system_prompt,
            allowed_tools=c.allowed_tools,
            disallowed_tools=c.disallowed_tools,
            add_dirs=c.add_dirs,
            max_budget_usd=c.max_budget_usd,
            include_partial_messages=c.partial_messages,
            setting_sources=['user', 'project', 'local'],
            extra_args={'replay-user-messages': None} | ({'allow-dangerously-skip-permissions': None} if c.allow_bypass else {}),
            enable_file_checkpointing=c.checkpointing,
            cli_path=self.daemon.cli_path,
            can_use_tool=self.can_use_tool,
            env=account.environment(),
            resume=self.id if exists else None,
            session_id=None if exists else self.id,
        )

    async def ensure_started(self, force: bool = False) -> None:
        if self.status == 'dormant':
            pid = holder_pid(self.id)
            if pid is not None and not force:
                raise Conflict(f'session {self.id} is open in another Claude Code process (pid {pid}); pass force=true to open it anyway')
            self.unarchive()
            self.ready = asyncio.get_running_loop().create_future()
            self.stop_event = asyncio.Event()
            self.error = None
            self.set_status('starting')
            self.task = asyncio.create_task(self.run())
        await asyncio.shield(self.ready)

    async def run(self) -> None:
        try:
            async with ClaudeSDKClient(self.options()) as client:
                self.client = client
                self.ready.set_result(None)
                self.set_status()
                reader = asyncio.create_task(self.read(client))
                stopper = asyncio.create_task(self.stop_event.wait())
                await asyncio.wait({reader, stopper}, return_when=asyncio.FIRST_COMPLETED)
                stopper.cancel()
                if reader.done():
                    reader.result()
                    raise RuntimeError('claude process closed its output')
                reader.cancel()
        except Exception as e:
            self.error = repr(e)
            self.emit('error', {'error': self.error})
            self.daemon.notify('error', self, {'error': self.error})
            if not self.ready.done():
                self.ready.set_exception(e)
        finally:
            self.client = None
            for p in list(self.prompts.values()):
                p.resolve(PermissionResultDeny(message='Session stopped', interrupt=True), 'stopped')
            for u, f in self.outstanding.items():
                if not f.done():
                    f.set_result(None)
                if self.sent.get(u, {}).get('state') in ('queued', 'taken'):
                    self.sent[u]['state'] = 'dropped'
            self.outstanding.clear()
            if not self.switching:
                for f in self.relayed:
                    if not f.done():
                        f.set_result(None)
                self.relayed.clear()
            self.turn_active = False
            self.limit_error = self.limit_resets = None
            # A dormant session keeps nothing but its config in memory; its full history stays readable from the transcript (`/history`). `seq` keeps counting, so event cursors stay valid.
            self.events.clear()
            self.set_status()
            self.daemon.save()

    async def stop(self) -> None:
        if self.task is not None and not self.task.done():
            self.stop_event.set()
            await self.task

    async def archive(self) -> None:
        """Mark the session as no longer in use: stop it, drop what it holds in memory and hide it from the session list. Using it again unarchives it."""
        await self.stop()
        self.archived = True
        self.events.clear()
        self.sent.clear()
        self.last_result = None
        self.emit('archived', {})
        self.daemon.save()

    def unarchive(self) -> None:
        if self.archived:
            self.archived = False
            self.emit('unarchived', {})
            self.daemon.save()

    async def read(self, client: ClaudeSDKClient) -> None:
        async for message in client.receive_messages():
            self.emit('message', to_jsonable(message))
            target = None
            if isinstance(message, ResultMessage):
                target = self.on_limit(message.is_error) if self.limit_error is not None else None
                self.on_result(message, relay=self.switching or target is not None)
            elif isinstance(message, (AssistantMessage, StreamEvent)):
                self.turn_active = True
                if isinstance(message, AssistantMessage) and message.error in LIMIT_ERRORS:
                    self.limit_error = message.error
            elif isinstance(message, RateLimitEvent) and message.rate_limit_info.status == 'rejected':
                self.limit_resets = message.rate_limit_info.resets_at
            elif isinstance(message, UserMessage) and message.uuid in self.sent:
                self.on_taken(message.uuid)
            self.set_status()
            if isinstance(message, ResultMessage):
                self.daemon.notify('result', self, self.last_result)
            if target is not None:
                asyncio.create_task(self.switch_account(*target))

    def on_limit(self, failed: bool) -> tuple[Account, str] | None:
        """After a turn ran into a usage or billing limit, mark the current account as limited; if the turn failed because of it, return the account to continue under, if any is available.

        A turn can still succeed when only a subagent ran into the limit; the account is marked all the same, but nothing is continued. A subscription's `rate_limit_event` gives the reset time; a rate limit without one keeps the account out for `RATE_LIMIT_COOLDOWN`, and a billing limit until it is reset over the API.
        """
        reason, resets = self.limit_error, self.limit_resets
        self.limit_error = self.limit_resets = None
        until = None if reason == 'billing_error' else resets or time.time() + RATE_LIMIT_COOLDOWN
        self.daemon.limit_account(self.config.account, reason, until)
        if self.switching or not failed:
            return None
        target = self.daemon.choose_account(self.config.account)
        if target.name != self.config.account:
            return target, reason
        data = {'account': self.config.account, 'reason': reason, 'until': until}
        self.emit('accounts_exhausted', data)
        self.daemon.notify('accounts_exhausted', self, data)
        return None

    async def switch_account(self, target: Account, reason: str) -> None:
        """Restart the session under another account and continue the turn that hit the limit. Callers waiting on that turn get the result of the continuing turn.

        Messages still queued in the CLI when the process is stopped are dropped and have to be sent again.
        """
        self.switching = True
        try:
            previous = self.config.account
            self.config.account = target.name
            for u in [u for u in self.outstanding if self.sent.get(u, {}).get('state') == 'taken']:
                self.relayed.append(self.outstanding.pop(u))
            await self.stop()
            # Announced after stopping, which clears the event buffer.
            self.announce_switch(previous, target.name, reason)
            # Our own process registered the session too; force skips that stale entry.
            await self.ensure_started(force=True)
            _, future = await self.send(CONTINUE_PROMPT)
        except Exception as e:
            self.emit('error', {'error': f'switching to account {target.name} failed: {e!r}'})
            for f in self.relayed:
                if not f.done():
                    f.set_result(None)
            self.relayed.clear()
            return
        finally:
            self.switching = False
        relayed, self.relayed = self.relayed, []

        def forward(done: asyncio.Future) -> None:
            for f in relayed:
                if not f.done():
                    f.set_result(done.result())

        future.add_done_callback(forward)

    def on_taken(self, u: str) -> None:
        """`--replay-user-messages` echoes a sent message when a turn takes it in. A message that starts a turn is a file checkpoint; one taken in mid-turn is not."""
        record = self.sent[u]
        record['state'] = 'taken'
        record['checkpoint'] = not self.turn_active
        self.emit('taken', {'uuid': u, 'checkpoint': record['checkpoint']})

    def on_result(self, message: ResultMessage, relay: bool = False) -> None:
        self.turn_active = False
        self.cost_usd += message.total_cost_usd or 0.0
        self.last_result = {
            'subtype': message.subtype,
            'is_error': message.is_error,
            'result': message.result,
            'terminal_reason': message.terminal_reason,
            'total_cost_usd': message.total_cost_usd,
            'num_turns': message.num_turns,
            'user_message_uuids': message.user_message_uuids,
        }
        if self.config.name is not None and self.applied_name != self.config.name:
            rename_session(self.id, self.config.name, directory=self.config.cwd)
            self.applied_name = self.config.name
        aborted = message.terminal_reason in ('aborted_streaming', 'aborted_tools')
        taken = list(self.outstanding) if aborted or message.user_message_uuids is None else message.user_message_uuids
        for u in taken:
            f = self.outstanding.pop(u, None)
            if f is not None and not f.done():
                if relay:
                    self.relayed.append(f)
                else:
                    f.set_result(self.last_result)
            if u in self.sent:
                self.sent[u]['state'] = 'done' if self.sent[u]['state'] == 'taken' else 'dropped'

    # User actions
    async def send(self, text: str) -> tuple[str, asyncio.Future]:
        u = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.outstanding[u] = future
        self.sent[u] = {'uuid': u, 'text': text, 'time': time.time(), 'state': 'queued', 'checkpoint': None}
        while len(self.sent) > SENT_HISTORY:
            del self.sent[next(iter(self.sent))]
        msg = {'type': 'user', 'message': {'role': 'user', 'content': text}, 'parent_tool_use_id': None, 'uuid': u}

        async def one() -> AsyncIterator[dict[str, Any]]:
            yield msg

        self.emit('sent', {'uuid': u, 'text': text})
        self.set_status()
        await self.client.query(one(), session_id=self.id)
        return u, future

    async def control(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send a raw control request and return the CLI's response.

        The Python SDK's `rewind_files()` drops the response and has no dry run, and it has no wrapper for `cancel_async_message`; the TypeScript SDK sends these same requests (`dry_run`, `cancel_async_message`) and returns the response.
        """
        return await self.client._query._send_control_request(request)

    async def cancel(self, u: str) -> bool:
        """Withdraw a message that is still queued (sent mid-turn, not yet taken in)."""
        record = self.sent.get(u)
        if record is None or record['state'] != 'queued' or self.client is None:
            return False
        r = await self.control({'subtype': 'cancel_async_message', 'message_uuid': u})
        if r.get('cancelled'):
            record['state'] = 'cancelled'
            f = self.outstanding.pop(u, None)
            if f is not None and not f.done():
                f.set_result(None)
            self.emit('cancelled', {'uuid': u})
            self.set_status()
        return bool(r.get('cancelled'))

    async def rewind(self, u: str, dry_run: bool) -> dict[str, Any]:
        r = await self.control({'subtype': 'rewind_files', 'user_message_id': u, 'dry_run': dry_run})
        if not dry_run and r.get('canRewind'):
            self.emit('rewound', {'uuid': u, **r})
        return r

    async def interrupt(self) -> None:
        for p in list(self.prompts.values()):
            p.resolve(PermissionResultDeny(message='Interrupted by user', interrupt=True), 'interrupted')
        if self.client is not None:
            await self.client.interrupt()

    async def can_use_tool(self, tool_name: str, tool_input: dict[str, Any], ctx: ToolPermissionContext) -> PermissionResultAllow | PermissionResultDeny:
        prompt_id = secrets.token_hex(4)
        timeout = self.daemon.prompt_timeout
        created = time.time()
        info = {
            'id': prompt_id,
            'session_id': self.id,
            'kind': 'question' if tool_name == 'AskUserQuestion' else 'permission',
            'tool_name': tool_name,
            'input': tool_input,
            'title': ctx.title,
            'display_name': ctx.display_name,
            'description': ctx.description,
            'decision_reason': ctx.decision_reason,
            'blocked_path': ctx.blocked_path,
            'agent_id': ctx.agent_id,
            'tool_use_id': ctx.tool_use_id,
            'suggestions': [s.to_dict() for s in ctx.suggestions],
            'created': created,
            'expires': None if timeout is None else created + timeout,
        }
        prompt = Prompt(info, tool_input, ctx.suggestions, asyncio.get_running_loop().create_future())
        self.prompts[prompt_id] = prompt
        self.emit('prompt', info)
        self.set_status()
        self.daemon.notify('prompt', self, info)
        try:
            try:
                return await asyncio.wait_for(asyncio.shield(prompt.future), timeout)
            except TimeoutError:
                prompt.resolve(PermissionResultDeny(message=f'The user did not answer within {timeout:g} seconds. Do not ask again or retry this; end your turn and say what you need from the user.'), 'timeout')
                return prompt.future.result()
        finally:
            self.prompts.pop(prompt_id, None)
            self.emit('prompt_resolved', {'id': prompt_id, 'reason': prompt.reason})
            self.set_status()
            self.daemon.notify('prompt_resolved', self, {'id': prompt_id, 'reason': prompt.reason})

    def answer(self, prompt_id: str, a: PromptAnswer) -> None:
        prompt = self.prompts.get(prompt_id)
        if prompt is None or prompt.future.done():
            raise HTTPException(404, f'no pending prompt {prompt_id}')
        if not a.allow:
            prompt.resolve(PermissionResultDeny(message=a.message or 'User denied this action', interrupt=a.interrupt), 'answered')
            return
        updated = dict(a.updated_input if a.updated_input is not None else prompt.tool_input)
        if a.answers is not None:
            updated['answers'] = a.answers
        if a.response is not None:
            updated['response'] = a.response
        prompt.resolve(PermissionResultAllow(updated_input=updated, updated_permissions=prompt.suggestions if a.remember else None), 'answered')

def post_json(url: str, payload: dict[str, Any], token: str | None) -> None:
    headers = {'Content-Type': 'application/json'} | ({'Authorization': f'Bearer {token}'} if token else {})
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode(), headers=headers)
    with urllib.request.urlopen(request, timeout=WEBHOOK_TIMEOUT) as response:
        response.read()

class Daemon:
    def __init__(self, state_path: Path, idle_timeout: float, cli_path: str | None, prompt_timeout: float | None, webhook: str | None, webhook_token: str | None) -> None:
        self.state_path = state_path
        self.idle_timeout = idle_timeout
        self.cli_path = cli_path
        self.prompt_timeout = prompt_timeout
        self.webhook = webhook
        self.webhook_token = webhook_token
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sessions: dict[str, Session] = {}
        # Holds credentials, so it is kept apart from the state file and readable only by its owner. The order is the order in which accounts are tried.
        self.accounts_path = state_path.with_name('accounts.json')
        self.accounts = [Account(name=DEFAULT_ACCOUNT)]

    def load(self) -> None:
        if self.accounts_path.exists():
            self.accounts = [Account(**a) for a in json.loads(self.accounts_path.read_text())]
        if not self.state_path.exists():
            return
        for item in json.loads(self.state_path.read_text()):
            # `seq` and `archived` are absent from state files written before they existed.
            self.sessions[item['id']] = Session(self, item['id'], SessionConfig(**item['config']), item['created'], item['last_active'], item.get('seq', 0), item.get('archived', False))

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        items = [{'id': s.id, 'config': s.config.model_dump(), 'created': s.created, 'last_active': s.last_active, 'seq': s.seq, 'archived': s.archived} for s in self.sessions.values()]
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(items, indent=1, ensure_ascii=False))
        tmp.replace(self.state_path)

    def save_accounts(self) -> None:
        self.accounts_path.parent.mkdir(parents=True, exist_ok=True)
        write_private(self.accounts_path, json.dumps([a.model_dump() for a in self.accounts], indent=1, ensure_ascii=False))

    def account(self, name: str | None) -> Account | None:
        return next((a for a in self.accounts if a.name == name), None)

    def choose_account(self, preferred: str | None) -> Account:
        """The preferred account if it is available, else the first available one in order; when none is, the preferred one (or the default) all the same."""
        current = self.account(preferred)
        if current is not None and current.available():
            return current
        return next((a for a in self.accounts if a.available()), current or self.accounts[0])

    def limit_account(self, name: str | None, reason: str, until: float | None) -> None:
        account = self.account(name)
        if account is not None:
            account.limited = reason
            account.limited_until = until
            self.save_accounts()

    def get(self, session_id: str) -> Session:
        s = self.sessions.get(session_id)
        if s is None:
            raise HTTPException(404, f'unknown session {session_id}')
        return s

    def notify(self, event: str, session: Session, data: Any) -> None:
        if self.webhook is not None:
            self.outbox.put_nowait({'event': event, 'time': time.time(), 'session': session.brief(), 'data': data})

    async def deliver_webhooks(self) -> None:
        """POST notifications one at a time, in order. A failed delivery is reported on stderr and not retried.

        Any exception is caught: besides `OSError`, urllib raises `ValueError` for a malformed URL and `http.client.HTTPException` for a malformed response, and a dead worker would leave the queue growing forever.
        """
        while True:
            payload = await self.outbox.get()
            try:
                await asyncio.to_thread(post_json, self.webhook, payload, self.webhook_token)
            except Exception as e:
                print(f"claude-sessiond: webhook {payload['event']} for session {payload['session']['id']} failed: {e!r}", file=sys.stderr, flush=True)

    async def reap(self) -> None:
        while True:
            await asyncio.sleep(min(30.0, self.idle_timeout))
            now = time.time()
            for s in list(self.sessions.values()):
                if s.status == 'idle' and now - s.last_active > self.idle_timeout:
                    await s.stop()

    async def shutdown(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.sessions.values()))
        self.save()

def create_app(daemon: Daemon, token: str | None, allowed_hosts: set[str]) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        daemon.load()
        tasks = [asyncio.create_task(daemon.reap())] + ([asyncio.create_task(daemon.deliver_webhooks())] if daemon.webhook is not None else [])
        yield
        for t in tasks:
            t.cancel()
        await daemon.shutdown()

    app = FastAPI(title='claude-sessiond', lifespan=lifespan)

    @app.middleware('http')
    async def guard(request: Request, call_next: Any) -> Any:
        host = request.headers.get('host', '')
        hostname = host[1:host.index(']')] if host.startswith('[') else host.rsplit(':', 1)[0]
        if hostname not in allowed_hosts:
            return JSONResponse({'detail': f'host {host!r} not allowed'}, status_code=403)
        if token is not None:
            given = request.headers.get('authorization', '').removeprefix('Bearer ') or request.query_params.get('token', '')
            if not secrets.compare_digest(given.encode(), token.encode()):
                return JSONResponse({'detail': 'unauthorized'}, status_code=401)
        return await call_next(request)

    async def start_or_409(s: Session, force: bool) -> None:
        try:
            await s.ensure_started(force)
        except Conflict as e:
            raise HTTPException(409, str(e))
        except Exception as e:
            raise HTTPException(502, f'failed to start claude: {e!r}')

    async def deliver(s: Session, text: str, wait: bool, timeout: float | None) -> dict[str, Any]:
        u, future = await s.send(text)
        if not wait:
            return {'uuid': u}
        try:
            return {'uuid': u, 'result': await asyncio.wait_for(asyncio.shield(future), timeout)}
        except TimeoutError:
            return {'uuid': u, 'result': None, 'timed_out': True}

    @app.get('/health')
    async def health() -> dict[str, Any]:
        return {'ok': True, 'sessions': len(daemon.sessions), 'live': sum(s.client is not None for s in daemon.sessions.values())}

    @app.get('/sessions')
    async def sessions(include_archived: bool = False) -> list[dict[str, Any]]:
        return [s.summary() for s in daemon.sessions.values() if include_archived or not s.archived]

    @app.post('/sessions', status_code=201)
    async def new_session(body: NewSession) -> dict[str, Any]:
        fields = body.model_dump(include=set(SessionConfig.model_fields))
        if body.account is not None and daemon.account(body.account) is None:
            raise HTTPException(400, f'unknown account {body.account}')
        if body.resume is not None:
            info = get_session_info(body.resume, directory=body.cwd)
            if info is None:
                raise HTTPException(404, f'no transcript for session {body.resume}')
            fields['cwd'] = body.cwd or info.cwd
            session_id = fork_session(body.resume, directory=fields['cwd'], title=body.name).session_id if body.fork else body.resume
            if session_id in daemon.sessions:
                hint = ' (archived; send it a message or unarchive it)' if daemon.sessions[session_id].archived else ''
                raise HTTPException(409, f'session {session_id} is already hosted{hint}')
        else:
            if body.cwd is None or not Path(body.cwd).is_dir():
                raise HTTPException(400, 'cwd must be an existing directory')
            session_id = str(uuid.uuid4())
        fields['cwd'] = str(Path(fields['cwd']).expanduser().resolve())
        now = time.time()
        s = Session(daemon, session_id, SessionConfig(**fields), now, now)
        daemon.sessions[session_id] = s
        try:
            await start_or_409(s, body.force)
        except HTTPException:
            del daemon.sessions[session_id]
            raise
        daemon.save()
        out = s.summary()
        if body.prompt is not None:
            out['message'] = await deliver(s, body.prompt, body.wait, body.timeout)
        return out

    @app.get('/sessions/{session_id}')
    async def session(session_id: str) -> dict[str, Any]:
        return daemon.get(session_id).summary()

    @app.patch('/sessions/{session_id}')
    async def patch_session(session_id: str, body: PatchSession) -> dict[str, Any]:
        s = daemon.get(session_id)
        if body.account is not None and daemon.account(body.account) is None:
            raise HTTPException(400, f'unknown account {body.account}')
        if body.name is not None:
            s.config.name = body.name
            if get_session_info(s.id, directory=s.config.cwd) is not None:
                rename_session(s.id, body.name, directory=s.config.cwd)
                s.applied_name = body.name
        try:
            if body.model is not None and s.client is not None:
                await s.client.set_model(body.model)
            if body.permission_mode is not None and s.client is not None:
                await s.client.set_permission_mode(body.permission_mode)
        except Exception as e:
            raise HTTPException(400, str(e))
        if body.model is not None:
            s.config.model = body.model
        if body.permission_mode is not None:
            s.config.permission_mode = body.permission_mode
        if body.effort is not None:
            s.config.effort = body.effort
        if body.account is not None:
            s.config.account = body.account
        daemon.save()
        return s.summary()

    @app.delete('/sessions/{session_id}')
    async def remove_session(session_id: str, purge: bool = False) -> dict[str, Any]:
        s = daemon.get(session_id)
        await s.stop()
        del daemon.sessions[session_id]
        if purge and get_session_info(s.id, directory=s.config.cwd) is not None:
            delete_session(s.id, directory=s.config.cwd)
        daemon.save()
        return {'id': session_id, 'purged': purge}

    @app.post('/sessions/{session_id}/start')
    async def start_session(session_id: str, force: bool = False) -> dict[str, Any]:
        s = daemon.get(session_id)
        await start_or_409(s, force)
        return s.summary()

    @app.post('/sessions/{session_id}/stop')
    async def stop_session(session_id: str) -> dict[str, Any]:
        s = daemon.get(session_id)
        await s.stop()
        return s.summary()

    @app.post('/sessions/{session_id}/archive')
    async def archive_session(session_id: str) -> dict[str, Any]:
        s = daemon.get(session_id)
        await s.archive()
        return s.summary()

    @app.post('/sessions/{session_id}/unarchive')
    async def unarchive_session(session_id: str) -> dict[str, Any]:
        s = daemon.get(session_id)
        s.unarchive()
        return s.summary()

    @app.post('/sessions/{session_id}/messages')
    async def send_message(session_id: str, body: SendMessage) -> dict[str, Any]:
        s = daemon.get(session_id)
        await start_or_409(s, body.force)
        if body.interrupt and s.status in ('running', 'waiting'):
            await s.interrupt()
        return await deliver(s, body.text, body.wait, body.timeout)

    @app.get('/sessions/{session_id}/messages')
    async def sent_messages(session_id: str) -> list[dict[str, Any]]:
        return list(daemon.get(session_id).sent.values())

    @app.delete('/sessions/{session_id}/messages/{message_uuid}')
    async def cancel_message(session_id: str, message_uuid: str) -> dict[str, Any]:
        s = daemon.get(session_id)
        if message_uuid not in s.sent:
            raise HTTPException(404, f'unknown message {message_uuid}')
        if not await s.cancel(message_uuid):
            raise HTTPException(409, f"message is {s.sent[message_uuid]['state']}, only a queued message can be withdrawn")
        return s.sent[message_uuid]

    @app.post('/sessions/{session_id}/rewind')
    async def rewind(session_id: str, body: Rewind) -> dict[str, Any]:
        s = daemon.get(session_id)
        if not s.config.checkpointing:
            raise HTTPException(409, 'session was created with checkpointing disabled')
        await start_or_409(s, body.force)
        r = await s.rewind(body.message_uuid, body.dry_run)
        if not r.get('canRewind'):
            raise HTTPException(409, r.get('error') or 'cannot rewind to this message')
        return r

    @app.post('/sessions/{session_id}/interrupt')
    async def interrupt(session_id: str) -> dict[str, Any]:
        s = daemon.get(session_id)
        await s.interrupt()
        return s.summary()

    @app.get('/sessions/{session_id}/events')
    async def events(session_id: str, request: Request, after: int | None = None) -> StreamingResponse:
        s = daemon.get(session_id)
        cursor = after if after is not None else int(request.headers.get('last-event-id', 0))

        async def stream() -> AsyncIterator[str]:
            nonlocal cursor
            yield ': connected\n\n'
            while daemon.sessions.get(session_id) is s and not await request.is_disconnected():
                batch = await s.wait_events(cursor, SSE_KEEPALIVE)
                if not batch:
                    yield ': keepalive\n\n'
                for e in batch:
                    yield f"id: {e['seq']}\nevent: {e['kind']}\ndata: {json.dumps(e, ensure_ascii=False)}\n\n"
                    cursor = e['seq']

        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache'})

    @app.get('/sessions/{session_id}/log')
    async def log(session_id: str, after: int = 0, wait: float = 0.0) -> dict[str, Any]:
        s = daemon.get(session_id)
        batch = await s.wait_events(after, min(wait, 60.0)) if wait > 0 else s.events_after(after)
        return {'events': batch, 'seq': s.seq, 'status': s.status}

    @app.get('/sessions/{session_id}/history')
    async def history(session_id: str, limit: int | None = None, offset: int = 0) -> list[Any]:
        s = daemon.get(session_id)
        return to_jsonable(get_session_messages(s.id, directory=s.config.cwd, limit=limit, offset=offset))

    @app.get('/sessions/{session_id}/context')
    async def context(session_id: str) -> Any:
        s = daemon.get(session_id)
        if s.client is None:
            raise HTTPException(409, 'session is dormant')
        return to_jsonable(await s.client.get_context_usage())

    @app.get('/prompts')
    async def prompts() -> list[dict[str, Any]]:
        return [p.info for s in daemon.sessions.values() for p in s.prompts.values()]

    @app.post('/sessions/{session_id}/prompts/{prompt_id}')
    async def answer(session_id: str, prompt_id: str, body: PromptAnswer) -> dict[str, Any]:
        daemon.get(session_id).answer(prompt_id, body)
        return {'id': prompt_id, 'allow': body.allow}

    @app.get('/accounts')
    async def accounts() -> list[dict[str, Any]]:
        return [a.public(sum(s.config.account == a.name for s in daemon.sessions.values())) for a in daemon.accounts]

    @app.post('/accounts', status_code=201)
    async def add_account(body: NewAccount) -> dict[str, Any]:
        if daemon.account(body.name) is not None:
            raise HTTPException(409, f'account {body.name} already exists')
        if body.oauth_token is not None and body.api_key is not None:
            raise HTTPException(400, 'give either oauth_token or api_key, not both')
        account = Account(**body.model_dump())
        daemon.accounts.append(account)
        daemon.save_accounts()
        return account.public(0)

    def get_account(name: str) -> Account:
        account = daemon.account(name)
        if account is None:
            raise HTTPException(404, f'unknown account {name}')
        return account

    @app.delete('/accounts/{name}')
    async def remove_account(name: str) -> dict[str, Any]:
        if name == DEFAULT_ACCOUNT:
            raise HTTPException(400, 'the default account cannot be removed')
        daemon.accounts.remove(get_account(name))
        daemon.save_accounts()
        return {'name': name}

    @app.post('/accounts/{name}/reset')
    async def reset_account(name: str) -> dict[str, Any]:
        account = get_account(name)
        account.limited = account.limited_until = None
        daemon.save_accounts()
        return account.public(sum(s.config.account == name for s in daemon.sessions.values()))

    @app.get('/transcripts')
    async def transcripts(cwd: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for info in list_sessions(directory=cwd, limit=limit):
            d = to_jsonable(info)
            d['hosted'] = info.session_id in daemon.sessions
            d['archived'] = d['hosted'] and daemon.sessions[info.session_id].archived
            d['holder_pid'] = holder_pid(info.session_id)
            out.append(d)
        return out

    return app

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument('--host', default='127.0.0.1')
    p.add_argument('--port', type=int, default=8790)
    p.add_argument('--token', default=os.environ.get('SESSIOND_TOKEN'), help='require "Authorization: Bearer <token>" (default: $SESSIOND_TOKEN)')
    p.add_argument('--allow-host', action='append', default=[], help='extra Host header value to accept (DNS-rebinding guard)')
    p.add_argument('--idle-timeout', type=float, default=1800.0, help='seconds an idle session keeps its claude process')
    p.add_argument('--prompt-timeout', type=float, default=3600.0, help='seconds a permission prompt or question waits for an answer before it is declined (0: wait forever)')
    p.add_argument('--webhook', default=os.environ.get('SESSIOND_WEBHOOK'), help='URL to POST prompt, prompt_resolved, result and error notifications to (default: $SESSIOND_WEBHOOK)')
    p.add_argument('--webhook-token', default=os.environ.get('SESSIOND_WEBHOOK_TOKEN'), help='bearer token sent with webhook requests (default: $SESSIOND_WEBHOOK_TOKEN)')
    p.add_argument('--state', type=Path, default=Path.home() / '.local' / 'state' / 'claude-sessiond' / 'sessions.json')
    p.add_argument('--keep-env', action='append', default=[], help='CLAUDE_* variable to pass through to sessions')
    p.add_argument('--cli-path', help='claude executable to run (default: the one bundled with claude-agent-sdk)')
    args = p.parse_args()
    # The callback still receives AskUserQuestion in bypassPermissions mode, so the SDK's warning does not apply here.
    warnings.filterwarnings('ignore', message='can_use_tool will not be invoked')
    scrub_environment(args.keep_env)
    daemon = Daemon(args.state, args.idle_timeout, args.cli_path, args.prompt_timeout or None, args.webhook, args.webhook_token)
    uvicorn.run(create_app(daemon, args.token, LOCAL_HOSTS | set(args.allow_host)), host=args.host, port=args.port, log_level='warning')

if __name__ == '__main__':
    main()
