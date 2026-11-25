"""claude-sessiond: an HTTP daemon that hosts Claude Code sessions through the Claude Agent SDK.

Each hosted session is one long-lived `ClaudeSDKClient` (one `claude` subprocess). Messages are sent as ordinary user messages, exactly as if typed in the CLI. Permission prompts and AskUserQuestion calls are held open and answered over HTTP, or declined after a timeout. Idle sessions with no background tasks or scheduled wakeups are put to sleep (subprocess exits, event buffer dropped) and resumed transparently from their transcript on the next message. Archived sessions are hidden from the session list until used again. When a turn fails on a usage or billing limit, the session moves to the next available account and continues.
"""

import argparse
import asyncio
import dataclasses
import json
import os
import re
import secrets
import shutil
import signal
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import warnings
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    EffortLevel,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    delete_session,
    fork_session,
    get_session_info,
    get_session_messages,
    list_sessions,
    rename_session,
)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel

EVENT_BUFFER = 5000
# Event numbers handed out ahead of the state file: it always holds a `seq` no event has reached, so numbering after a crash resumes above every event a client saw.
SEQ_RESERVE = 1000
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
# Sent after moving a session to another account, worded like the CLI's own note after it restarts a crashed session.
CONTINUE_PROMPT = 'Continue from where you left off. Note: the previous turn stopped because the account ran into a usage or billing limit, and the session was moved to another account; the user has not sent a new message. The new account is not limited, so do not wait for the limit to reset. Agents you started that stopped because of the limit or of the restart keep their transcripts: resume each of them now by sending it a message with SendMessage. Background commands and monitors were terminated by the restart and will not report back; start again any that are still needed.'
# How long a sign-in waits for the browser to come back.
LOGIN_TIMEOUT = 900.0
# Account names that can sign in: each gets a directory of that name.
LOGIN_NAME = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._-]*')
# Tools that change the session's scheduled tasks, followed through their results between Stop hooks.
CRON_TOOLS = ('CronCreate', 'CronDelete', 'ScheduleWakeup')
# How long a one-shot scheduled task past its time still keeps the session awake: the CLI fires it once no turn is under way.
CRON_GRACE = 120.0
# Credentials in the daemon's environment take precedence over a sign-in stored in the config directory.
CREDENTIAL_ENV = ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN')

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

def config_dir() -> Path:
    """The Claude Code configuration directory, `CLAUDE_CONFIG_DIR` or `~/.claude`, as the SDK's session functions resolve it."""
    return Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')

def session_registry() -> Path:
    """The directory where running Claude Code processes register their open session."""
    return config_dir() / 'sessions'

def purge_session_files(session_id: str, cwd: str) -> None:
    """Delete a session's transcript and what the CLI keeps for it besides: its `session-env/` and `file-history/` directories, and the project directory when nothing but an empty `memory/` is left in it."""
    projects = {p.parent for p in (config_dir() / 'projects').glob(f'*/{session_id}.jsonl')}
    if get_session_info(session_id, directory=cwd) is not None:
        delete_session(session_id, directory=cwd)
    for name in ('session-env', 'file-history'):
        shutil.rmtree(config_dir() / name / session_id, ignore_errors=True)
    # `memory/` belongs to the project and may hold other sessions' memories; only empty directories are removed.
    for project in projects:
        for d in (project / 'memory', project):
            try:
                d.rmdir()
            except OSError:
                pass

def process_start(pid: int) -> str | None:
    """The start time of a process as the CLI records it (`procStart`): field 22 of `/proc/<pid>/stat`. None where there is no `/proc`."""
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except OSError:
        return None

def same_process(pid: int, start: str | None) -> bool:
    """Whether a process with this pid is running and, where its start time can be read, is the one that started at `start`, not a later one that reuses the pid."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return start is None or process_start(pid) in (None, start)

def holder(session_id: str) -> tuple[int, str | None] | None:
    """The pid and start time of a running Claude Code process that has this session open, if any. Registrations whose process has exited are left behind when it is killed, and their pid may since belong to another process."""
    for f in session_registry().glob('*.json'):
        try:
            d = json.loads(f.read_text())
            if d.get('sessionId') == session_id and same_process(d['pid'], d.get('procStart')):
                return d['pid'], d.get('procStart')
        except (OSError, ValueError, KeyError):
            continue
    return None

def restorable(crons: list[dict[str, Any]], now: float) -> list[dict[str, Any]]:
    """The scheduled tasks a new process restores from the transcript: CronCreate tasks that are recurring or not yet due. Wakeups are not restored, one-shot tasks due by now are dropped, and a one-shot task whose time is unknown is left out rather than keep a session awake for a task that may be gone."""
    return [c for c in crons if not c['wakeup'] and (c['recurring'] or (c['fires_at'] is not None and c['fires_at'] > now))]

def write_private(path: Path, text: str) -> None:
    """Atomically replace a file with one only its owner can read."""
    tmp = path.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(text)
    tmp.replace(path)

async def control(client: ClaudeSDKClient, request: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
    """Send a raw control request and return the CLI's response.

    The Python SDK's `rewind_files()` drops the response and has no dry run, and it wraps neither `cancel_async_message` nor sign-in; the TypeScript SDK sends these same requests and returns the response.
    """
    return await client._query._send_control_request(request, timeout)

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
    effort: EffortLevel | None = None
    append_system_prompt: str | None = None
    allowed_tools: list[str] = []
    disallowed_tools: list[str] = []
    add_dirs: list[str] = []
    max_budget_usd: float | None = None
    partial_messages: bool = False
    allow_bypass: bool = True
    checkpointing: bool = True
    account: str | None = None

class NewSession(SessionConfig):
    cwd: str | None = None
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
    effort: EffortLevel | None = None
    account: str | None = None

class NewAccount(BaseModel):
    name: str
    oauth_token: str | None = None
    api_key: str | None = None
    env: dict[str, str] = {}

class Account(NewAccount):
    """Credentials a session's `claude` process runs under. `default` has none and uses whatever the daemon inherited.

    An account signed in over the API keeps the CLI's own credentials, refresh token included, in `storage`, which the CLI is pointed to with `CLAUDE_SECURESTORAGE_CONFIG_DIR`; everything else stays in the shared config directory.
    """
    storage: str | None = None
    limited: str | None = None
    limited_until: float | None = None

    def available(self) -> bool:
        """A billing limit has no reset time and lasts until the account is reset over the API."""
        return self.limited is None or (self.limited_until is not None and self.limited_until <= time.time())

    @property
    def kind(self) -> str:
        return 'login' if self.storage is not None else 'oauth_token' if self.oauth_token is not None else 'api_key' if self.api_key is not None else 'inherited'

    def environment(self) -> dict[str, str]:
        """Variables to set for the `claude` process. Credential variables not used are set to the empty string, which the CLI treats as unset, so an inherited one cannot take precedence."""
        blank = {name: '' for name in CREDENTIAL_ENV}
        match self.kind:
            case 'login':
                base = blank | {'CLAUDE_SECURESTORAGE_CONFIG_DIR': self.storage}
            case 'oauth_token':
                base = blank | {'CLAUDE_CODE_OAUTH_TOKEN': self.oauth_token}
            case 'api_key':
                base = blank | {'ANTHROPIC_API_KEY': self.api_key}
            case _:
                base = {}
        return base | self.env

    def public(self, sessions: int) -> dict[str, Any]:
        """What the API shows: no secrets, and only the names of extra variables."""
        return {'name': self.name, 'kind': self.kind, 'env': sorted(self.env), 'available': self.available(), 'limited': self.limited, 'limited_until': self.limited_until, 'sessions': sessions}

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

class Removed(Exception):
    pass

class Session:
    def __init__(self, daemon: 'Daemon', session_id: str, config: SessionConfig, created: float, last_active: float, seq: int = 0, archived: bool = False, process: tuple[int, str | None] | None = None, crons: list[dict[str, Any]] | None = None) -> None:
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
        self.seq_limit = seq
        self.archived = archived
        # The pid and start time of the `claude` process last started for this session; kept in the state file until the process stops, so that one left behind by a crashed daemon can be recognized.
        self.process = process
        self.start_lock = asyncio.Lock()
        self.tick = asyncio.Event()
        self.prompts: dict[str, Prompt] = {}
        self.outstanding: dict[str, asyncio.Future] = {}
        self.sent: dict[str, dict[str, Any]] = {}
        # A turn of the main thread is under way: from its `init` system message, which the CLI sends at the start of every turn, including those it starts by itself, to its result.
        self.turn_active = False
        # That turn has produced output; a message it takes in from then on did not start it.
        self.turn_answering = False
        # The CLI's latest `background_tasks_changed` list: the background shells, monitors and agents still running.
        self.background_tasks: list[dict[str, Any]] = []
        # The session's scheduled tasks (CronCreate, ScheduleWakeup), which live in the process too: the list of the last Stop hook, updated by the cron tools' results since. `fires_at` is the fire time of a one-shot task, known when a result announced it; `wakeup` marks those made by ScheduleWakeup. Kept in the state file, so that a session started again without a message stays awake for the tasks its new process restores.
        self.crons = crons or []
        # Cron tool calls awaiting their results, by tool use id.
        self.cron_calls: dict[str, ToolUseBlock] = {}
        self.last_result: dict[str, Any] | None = None
        self.cost_usd = 0.0
        self.applied_name: str | None = None
        # The transcript's title, for lists: the custom title (set by renaming) or the one the CLI generates, else the first prompt. Read again when a turn takes in or ends a message.
        self.title: str | None = None
        self.refresh_title()
        self.limit_error: str | None = None
        self.limit_resets: float | None = None
        self.relayed: list[asyncio.Future] = []
        self.switching = False

    # Event log
    def emit(self, kind: str, data: Any) -> None:
        self.seq += 1
        if self.seq > self.seq_limit:
            self.seq_limit = self.seq + SEQ_RESERVE
            self.daemon.save()
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

    def keeps_awake(self) -> bool:
        """Whether stopping the process would lose work: a background task, whose result would never come back, or a scheduled wakeup, which would never fire. The CLI marks tasks that are not activity (`ambient`) and tasks that run outside the process (`off_host`)."""
        if any(c['recurring'] or c['fires_at'] is None or c['fires_at'] > time.time() - CRON_GRACE for c in self.crons):
            return True
        return any(not t.get('ambient') and not t.get('off_host') for t in self.background_tasks)

    def refresh_title(self) -> None:
        info = get_session_info(self.id, directory=self.config.cwd)
        self.title = info and (info.custom_title or info.first_prompt)

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
            'title': self.title,
            'pending_prompts': [p.info for p in self.prompts.values()],
            'background_tasks': self.background_tasks,
            'crons': self.crons if self.client is not None else restorable(self.crons, time.time()),
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
            hooks={'Stop': [HookMatcher(hooks=[self.on_stop])]},
            env=account.environment(),
            resume=self.id if exists else None,
            session_id=None if exists else self.id,
        )

    async def ensure_started(self, force: bool = False) -> None:
        # Starts and stops are serialized, so a request arriving while the process exits waits for it and starts a new one, and a stop or archive arriving while a leftover process is ended takes effect after the start.
        async with self.start_lock:
            if self.status == 'dormant':
                other = holder(self.id)
                if other is not None and other == self.process and other[1] is not None:
                    await self.kill_leftover(*other)
                elif other is not None and not force:
                    raise Conflict(f'session {self.id} is open in another Claude Code process (pid {other[0]}); pass force=true to open it anyway')
            # Checked after every wait: the request may have looked the session up before it was deleted, and starting it then would leave a process no one stops.
            if self.daemon.sessions.get(self.id) is not self:
                raise Removed(f'unknown session {self.id}')
            if self.status == 'dormant':
                # One-shot tasks that came due while the session slept are gone.
                self.crons = restorable(self.crons, time.time())
                self.unarchive()
                self.ready = asyncio.get_running_loop().create_future()
                self.stop_event = asyncio.Event()
                self.error = None
                self.set_status('starting')
                self.task = asyncio.create_task(self.run())
        await asyncio.shield(self.ready)

    async def kill_leftover(self, pid: int, start: str) -> None:
        """Stop the `claude` process a crashed daemon left running for this session (one with background tasks outlives its input), so that two processes do not write the same transcript. It is recognized by its pid and start time, so a process that reuses the pid is never signalled."""
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            # It exited by itself after it was found.
            return
        # It unregisters the session before it exits, so wait for the process itself.
        for _ in range(100):
            await asyncio.sleep(0.1)
            if not same_process(pid, start):
                return
        os.kill(pid, signal.SIGKILL)

    async def run(self) -> None:
        try:
            async with ClaudeSDKClient(self.options()) as client:
                self.client = client
                pid = client._transport._process.pid
                self.process = pid, process_start(pid)
                self.daemon.save()
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
            self.process = None
            for p in list(self.prompts.values()):
                p.resolve(PermissionResultDeny(message='Session stopped', interrupt=True), 'stopped')
            for u, f in self.outstanding.items():
                if not f.done():
                    f.set_result(None)
                if self.sent.get(u, {}).get('state') in ('queued', 'taken'):
                    self.sent[u]['state'] = 'dropped'
            self.outstanding.clear()
            if not self.switching:
                self.release_relayed()
            self.turn_active = self.turn_answering = False
            self.background_tasks = []
            # Kept for the next process, which restores them before any Stop hook lists them.
            self.crons = restorable(self.crons, time.time())
            self.cron_calls.clear()
            self.limit_error = self.limit_resets = None
            # A dormant session keeps nothing but its config in memory; its full history stays readable from the transcript (`/history`). `seq` keeps counting, so event cursors stay valid.
            self.events.clear()
            self.set_status()
            self.daemon.save()

    async def stop(self) -> None:
        async with self.start_lock:
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
                # Subagents run alongside the turns of the main thread and are tracked as background tasks; a limit they run into still counts.
                if message.parent_tool_use_id is None:
                    self.turn_active = self.turn_answering = True
                if isinstance(message, AssistantMessage) and message.error in LIMIT_ERRORS:
                    self.limit_error = message.error
                if isinstance(message, AssistantMessage):
                    self.cron_calls |= {b.id: b for b in message.content if isinstance(b, ToolUseBlock) and b.name in CRON_TOOLS}
            elif isinstance(message, RateLimitEvent) and message.rate_limit_info.status == 'rejected':
                self.limit_resets = message.rate_limit_info.resets_at
            elif isinstance(message, UserMessage) and message.uuid in self.sent:
                self.on_taken(message.uuid)
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for b in message.content:
                    if isinstance(b, ToolResultBlock) and b.tool_use_id in self.cron_calls:
                        self.on_cron_result(self.cron_calls.pop(b.tool_use_id), message.tool_use_result if not b.is_error else None)
            elif isinstance(message, SystemMessage) and message.subtype == 'init':
                self.turn_active = True
            elif isinstance(message, SystemMessage) and message.subtype == 'background_tasks_changed':
                # The full set after each change; the CLI sends nothing at startup, when the set is empty.
                self.background_tasks = message.data['tasks']
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
            await self.stop()
            # Announced after stopping, which clears the event buffer.
            self.announce_switch(previous, target.name, reason)
            # Our own process registered the session too; force skips that stale entry.
            await self.ensure_started(force=True)
            _, future = await self.send(CONTINUE_PROMPT)
        except Exception as e:
            self.emit('error', {'error': f'switching to account {target.name} failed: {e!r}'})
            self.release_relayed()
            return
        finally:
            self.switching = False
        relayed, self.relayed = self.relayed, []

        def forward(done: asyncio.Future) -> None:
            for f in relayed:
                if not f.done():
                    f.set_result(done.result())

        future.add_done_callback(forward)

    def release_relayed(self) -> None:
        """Wake callers relayed to a continuing turn that will not come."""
        for f in self.relayed:
            if not f.done():
                f.set_result(None)
        self.relayed.clear()

    def on_taken(self, u: str) -> None:
        """`--replay-user-messages` echoes a sent message when a turn takes it in. A message that starts a turn is a file checkpoint; one taken in mid-turn is not."""
        record = self.sent[u]
        record['state'] = 'taken'
        record['checkpoint'] = not self.turn_answering
        self.emit('taken', {'uuid': u, 'checkpoint': record['checkpoint']})
        if self.title is None:
            self.refresh_title()

    def on_result(self, message: ResultMessage, relay: bool = False) -> None:
        self.turn_active = self.turn_answering = False
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
        self.refresh_title()
        # A turn ends the messages it took in. Queued ones go on to a later turn even when this one was interrupted, and a turn the CLI started by itself (a finished background task, a scheduled wakeup) lists none.
        listed = set(message.user_message_uuids or ())
        for u in [u for u in self.outstanding if u in listed or self.sent.get(u, {}).get('state') == 'taken']:
            f = self.outstanding.pop(u)
            if not f.done():
                if relay:
                    self.relayed.append(f)
                else:
                    f.set_result(self.last_result)
            if u in self.sent:
                self.sent[u]['state'] = 'done'

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

    async def cancel(self, u: str) -> bool:
        """Withdraw a message that is still queued (sent mid-turn, not yet taken in)."""
        record = self.sent.get(u)
        if record is None or record['state'] != 'queued':
            return False
        r = await control(self.client, {'subtype': 'cancel_async_message', 'message_uuid': u})
        if r.get('cancelled'):
            record['state'] = 'cancelled'
            f = self.outstanding.pop(u, None)
            if f is not None and not f.done():
                f.set_result(None)
            self.emit('cancelled', {'uuid': u})
            self.set_status()
        return bool(r.get('cancelled'))

    async def rewind(self, u: str, dry_run: bool) -> dict[str, Any]:
        """The CLI reports the files and line counts only for a dry run; an actual rewind is preceded by one so that its response and the `rewound` event carry them too."""
        r = await control(self.client, {'subtype': 'rewind_files', 'user_message_id': u, 'dry_run': True})
        if dry_run or not r.get('canRewind'):
            return r
        r |= await control(self.client, {'subtype': 'rewind_files', 'user_message_id': u, 'dry_run': False})
        if r.get('canRewind'):
            self.emit('rewound', {'uuid': u, **r})
        return r

    async def interrupt(self) -> None:
        for p in list(self.prompts.values()):
            p.resolve(PermissionResultDeny(message='Interrupted by user', interrupt=True), 'interrupted')
        # Without `spare_background_tasks` the CLI also stops every background agent; those are stopped one by one through `/tasks/{task_id}/stop`. A process being stopped no longer takes requests, and stopping ends the turn anyway.
        if self.client is not None and not self.stop_event.is_set():
            await control(self.client, {'subtype': 'interrupt', 'spare_background_tasks': True})

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

    async def on_stop(self, data: HookInput, tool_use_id: str | None, context: HookContext) -> HookJSONOutput:
        """The Stop hook, run when a turn of the main thread ends normally, is where the CLI lists the session's scheduled tasks; older CLIs leave `session_crons` out. What only the tools' results tell (the fire time of a one-shot task, and which task is a ScheduleWakeup) is kept: by id, or for a ScheduleWakeup, whose result has no id, by prompt."""
        if 'session_crons' in data:
            known = {c['id'] or c['prompt']: c for c in self.crons}
            crons = []
            for c in data['session_crons']:
                previous = known.get(c['id']) or known.get(c['prompt']) or {}
                crons.append(c | {'fires_at': previous.get('fires_at'), 'wakeup': previous.get('wakeup', False)})
            if crons != self.crons:
                self.crons = crons
                self.daemon.save()
        return {}

    def on_cron_result(self, call: ToolUseBlock, result: Any) -> None:
        """Follow a change to the scheduled tasks made in a turn that may end without a Stop hook (interrupted or failed). Durable tasks are kept in the project, not in the process, and the Stop hook does not list them either."""
        if not isinstance(result, dict):
            return
        if call.name == 'CronCreate' and not result['durable']:
            # Only a one-shot task's fire time matters, and a recurring one's would go stale.
            fires_at = None if result['recurring'] else result['nextFireAt'] / 1000
            self.crons.append({'id': result['id'], 'schedule': call.input['cron'], 'recurring': result['recurring'], 'prompt': call.input['prompt'], 'fires_at': fires_at, 'wakeup': False})
        elif call.name == 'ScheduleWakeup':
            # A wakeup supersedes the one pending, and `stop` cancels it.
            self.crons = [c for c in self.crons if not c['wakeup']]
            if not result.get('stopped'):
                self.crons.append({'id': None, 'schedule': None, 'recurring': False, 'prompt': call.input['prompt'], 'fires_at': result['scheduledFor'] / 1000, 'wakeup': True})
        elif call.name == 'CronDelete':
            self.crons = [c for c in self.crons if c['id'] != result['id']]
        self.daemon.save()

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

class Login:
    """A sign-in of one account through the browser, driven through a dedicated `claude` process that runs no session.

    The `claude_authenticate` control request returns the authorization URL (`automaticUrl`), which redirects to a listener of that process on the daemon's machine (`http://localhost:<port>/callback`); it is opened in a browser on that machine, or with that port forwarded to the browser's machine. `claude_oauth_wait_for_completion` answers once the CLI has stored the credentials, as `claude auth login` does: for `default` in the config directory, for any other account in its own directory under the state directory.
    """
    def __init__(self, daemon: 'Daemon', name: str) -> None:
        self.daemon = daemon
        self.name = name
        self.status = 'starting'
        self.url: str | None = None
        self.profile: dict[str, Any] | None = None
        self.error: str | None = None
        self.created = time.time()
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.run())

    def summary(self) -> dict[str, Any]:
        return {
            'account': self.name,
            'status': self.status,
            'url': self.url,
            'created': self.created,
            'expires': self.created + LOGIN_TIMEOUT,
            'profile': self.profile,
            'error': self.error,
        }

    async def run(self) -> None:
        try:
            await asyncio.wait_for(self.sign_in(), LOGIN_TIMEOUT)
            self.status = 'done'
        except asyncio.CancelledError:
            self.status = 'cancelled'
        except Exception as e:
            self.status = 'failed'
            self.error = f'the browser did not come back within {LOGIN_TIMEOUT:g} seconds' if isinstance(e, TimeoutError) else str(e)
        if not self.ready.done():
            self.ready.set_exception(RuntimeError(self.error or f'the sign-in was {self.status}'))

    async def sign_in(self) -> None:
        # Empty credential variables make the CLI use, and store into, its secure storage.
        env = {name: '' for name in CREDENTIAL_ENV}
        if self.name == DEFAULT_ACCOUNT:
            r = await self.authenticate(env)
            self.daemon.reset_account(DEFAULT_ACCOUNT)
        else:
            # Signing in again replaces the credentials in the same directory, which sessions already running under the account then pick up.
            storage = self.daemon.logins_dir / self.name
            storage.mkdir(mode=0o700, parents=True, exist_ok=True)
            # A sign-in also records the account's identity (`oauthAccount`) in the config directory's `.claude.json`; a throwaway config directory keeps the shared one describing the default account.
            with tempfile.TemporaryDirectory(prefix='claude-sessiond-login-') as config:
                r = await self.authenticate(env | {'CLAUDE_SECURESTORAGE_CONFIG_DIR': str(storage), 'CLAUDE_CONFIG_DIR': config})
            self.daemon.set_storage(self.name, storage)
        self.profile = r.get('account')

    async def authenticate(self, env: dict[str, str]) -> dict[str, Any]:
        options = ClaudeAgentOptions(cwd=str(Path.home()), setting_sources=[], cli_path=self.daemon.cli_path, env=env)
        async with ClaudeSDKClient(options) as client:
            urls = await control(client, {'subtype': 'claude_authenticate', 'loginWithClaudeAi': True})
            self.url = urls['automaticUrl']
            self.status = 'pending'
            self.ready.set_result(None)
            return await control(client, {'subtype': 'claude_oauth_wait_for_completion'}, LOGIN_TIMEOUT)

    async def cancel(self) -> None:
        self.task.cancel()
        await asyncio.wait({self.task})

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
        self.logins: dict[str, Login] = {}
        # Each account signed in over the API keeps its credentials in a directory here.
        self.logins_dir = state_path.with_name('logins')

    def load(self) -> None:
        if self.accounts_path.exists():
            self.accounts = [Account(**a) for a in json.loads(self.accounts_path.read_text())]
        if not self.state_path.exists():
            return
        for item in json.loads(self.state_path.read_text()):
            self.sessions[item['id']] = Session(self, item['id'], SessionConfig(**item['config']), item['created'], item['last_active'], item['seq'], item['archived'], tuple(item['process']) if item.get('process') else None, restorable(item.get('crons', []), time.time()))

    def save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        items = [{'id': s.id, 'config': s.config.model_dump(), 'created': s.created, 'last_active': s.last_active, 'seq': s.seq_limit, 'archived': s.archived, 'process': s.process, 'crons': s.crons} for s in self.sessions.values()]
        write_private(self.state_path, json.dumps(items, indent=1, ensure_ascii=False))

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

    def reset_account(self, name: str) -> None:
        account = self.account(name)
        account.limited = account.limited_until = None
        self.save_accounts()

    def set_storage(self, name: str, storage: Path) -> None:
        """Give an account the credentials of a sign-in, registering it if it does not exist yet; a token or API key it had is dropped."""
        account = self.account(name)
        if account is None:
            account = Account(name=name)
            self.accounts.append(account)
        account.storage = str(storage)
        account.oauth_token = account.api_key = account.limited = account.limited_until = None
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
                if s.status == 'idle' and not s.keeps_awake() and now - s.last_active > self.idle_timeout:
                    await s.stop()

    async def shutdown(self) -> None:
        await asyncio.gather(*(login.cancel() for login in self.logins.values()))
        await asyncio.gather(*(s.stop() for s in self.sessions.values()))
        # After a clean shutdown numbering resumes where it stopped.
        for s in self.sessions.values():
            s.seq_limit = s.seq
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

    # No Swagger UI or ReDoc pages; `/openapi.json` stays for clients.
    app = FastAPI(title='claude-sessiond', lifespan=lifespan, docs_url=None, redoc_url=None)

    @app.middleware('http')
    async def guard(request: Request, call_next: Any) -> Any:
        host = request.headers.get('host', '')
        hostname = host[1:host.index(']')] if host.startswith('[') else host.rsplit(':', 1)[0]
        if hostname not in allowed_hosts:
            return JSONResponse({'detail': f'host {host!r} not allowed'}, status_code=403)
        # A cross-site page can send a POST without a body (e.g. /interrupt, /accounts/default/login) with no preflight; browsers mark it with the page's Origin.
        origin = request.headers.get('origin')
        if origin is not None and request.method not in ('GET', 'HEAD') and urllib.parse.urlsplit(origin).hostname not in allowed_hosts:
            return JSONResponse({'detail': f'origin {origin!r} not allowed'}, status_code=403)
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
        except Removed as e:
            raise HTTPException(404, str(e))
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
        if body.cwd is not None:
            fields['cwd'] = str(Path(body.cwd).expanduser().resolve())
        check_account(body.account)
        if body.resume is not None:
            info = get_session_info(body.resume, directory=fields['cwd'])
            if info is None:
                raise HTTPException(404, f'no transcript for session {body.resume}')
            fields['cwd'] = fields['cwd'] or info.cwd
            session_id = fork_session(body.resume, directory=fields['cwd'], title=body.name).session_id if body.fork else body.resume
            if session_id in daemon.sessions:
                hint = ' (archived; send it a message or unarchive it)' if daemon.sessions[session_id].archived else ''
                raise HTTPException(409, f'session {session_id} is already hosted{hint}')
        else:
            if fields['cwd'] is None or not Path(fields['cwd']).is_dir():
                raise HTTPException(400, 'cwd must be an existing directory')
            session_id = str(uuid.uuid4())
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
        check_account(body.account)
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
        # Removed first, so that a request arriving meanwhile does not start the session again.
        del daemon.sessions[session_id]
        await s.stop()
        if purge:
            purge_session_files(s.id, s.config.cwd)
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
        batch = await s.wait_events(after, min(wait, 60.0))
        return {'events': batch, 'seq': s.seq, 'status': s.status}

    @app.get('/sessions/{session_id}/history')
    async def history(session_id: str, limit: int | None = None, offset: int = 0, before: str | None = None) -> list[Any]:
        """Page through a transcript that may still be growing. A negative `offset` counts from the end, and `before` takes the messages that precede the one with that uuid, so a client can read the latest messages and then earlier ones without knowing the length."""
        s = daemon.get(session_id)
        if before is None and offset >= 0:
            return to_jsonable(get_session_messages(s.id, directory=s.config.cwd, limit=limit, offset=offset))
        messages = get_session_messages(s.id, directory=s.config.cwd)
        if before is not None:
            end = next((i for i, m in enumerate(messages) if m.uuid == before), None)
            if end is None:
                raise HTTPException(404, f'no message {before} in the transcript')
            messages = messages[:end]
            return to_jsonable(messages if limit is None else messages[max(0, len(messages) - limit):])
        messages = messages[offset:]
        return to_jsonable(messages if limit is None else messages[:limit])

    @app.get('/sessions/{session_id}/context')
    async def context(session_id: str) -> Any:
        s = daemon.get(session_id)
        if s.client is None:
            raise HTTPException(409, 'session is dormant')
        return to_jsonable(await s.client.get_context_usage())

    @app.get('/sessions/{session_id}/info')
    async def info(session_id: str) -> Any:
        s = daemon.get(session_id)
        if s.client is None:
            raise HTTPException(409, 'session is dormant')
        return to_jsonable(await s.client.get_server_info())

    @app.post('/sessions/{session_id}/tasks/{task_id}/stop')
    async def stop_task(session_id: str, task_id: str) -> dict[str, Any]:
        s = daemon.get(session_id)
        if s.client is None:
            raise HTTPException(409, 'session is dormant')
        await s.client.stop_task(task_id)
        return {'task_id': task_id}

    @app.get('/prompts')
    async def prompts() -> list[dict[str, Any]]:
        return [p.info for s in daemon.sessions.values() for p in s.prompts.values()]

    @app.post('/sessions/{session_id}/prompts/{prompt_id}')
    async def answer(session_id: str, prompt_id: str, body: PromptAnswer) -> dict[str, Any]:
        daemon.get(session_id).answer(prompt_id, body)
        return {'id': prompt_id, 'allow': body.allow}

    def public_account(account: Account) -> dict[str, Any]:
        return account.public(sum(s.config.account == account.name for s in daemon.sessions.values()))

    def check_account(name: str | None) -> None:
        if name is not None and daemon.account(name) is None:
            raise HTTPException(400, f'unknown account {name}')

    @app.get('/accounts')
    async def accounts() -> list[dict[str, Any]]:
        return [public_account(a) for a in daemon.accounts]

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
        account = get_account(name)
        daemon.accounts.remove(account)
        daemon.save_accounts()
        if account.storage is not None:
            shutil.rmtree(account.storage, ignore_errors=True)
        return {'name': name}

    @app.post('/accounts/{name}/reset')
    async def reset_account(name: str) -> dict[str, Any]:
        account = get_account(name)
        daemon.reset_account(name)
        return public_account(account)

    def get_login(name: str) -> Login:
        login = daemon.logins.get(name)
        if login is None:
            raise HTTPException(404, f'no sign-in has been started for account {name}')
        return login

    @app.post('/accounts/{name}/login')
    async def start_login(name: str) -> dict[str, Any]:
        overriding = [v for v in CREDENTIAL_ENV if os.environ.get(v)]
        if name == DEFAULT_ACCOUNT and overriding:
            raise HTTPException(409, f'the daemon was started with {overriding[0]}, which sessions under the default account use instead of a sign-in')
        if not LOGIN_NAME.fullmatch(name):
            raise HTTPException(400, 'an account that signs in is named with letters, digits, ".", "_" and "-", not starting with "."')
        if name in daemon.logins:
            await daemon.logins[name].cancel()
        login = daemon.logins[name] = Login(daemon, name)
        try:
            await login.ready
        except Exception as e:
            raise HTTPException(502, f'failed to start the sign-in: {e}')
        return login.summary()

    @app.get('/accounts/{name}/login')
    async def login_status(name: str) -> dict[str, Any]:
        return get_login(name).summary()

    @app.get('/accounts/{name}/login/open')
    async def open_login(name: str) -> RedirectResponse:
        """A short address that redirects a browser to the authorization URL, which is too long to copy or click reliably."""
        login = get_login(name)
        if login.status != 'pending':
            raise HTTPException(409, f'the sign-in for account {name} is {login.status}')
        return RedirectResponse(login.url, status_code=302)

    @app.delete('/accounts/{name}/login')
    async def cancel_login(name: str) -> dict[str, Any]:
        login = get_login(name)
        await login.cancel()
        return login.summary()

    @app.get('/transcripts')
    async def transcripts(cwd: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        out = []
        for info in list_sessions(directory=cwd, limit=limit):
            d = to_jsonable(info)
            d['hosted'] = info.session_id in daemon.sessions
            d['archived'] = d['hosted'] and daemon.sessions[info.session_id].archived
            d['holder_pid'] = (holder(info.session_id) or (None,))[0]
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
    # Event streams never end on their own, so without a timeout a SIGTERM waits on them forever and the sessions are never stopped.
    uvicorn.run(create_app(daemon, args.token, LOCAL_HOSTS | set(args.allow_host)), host=args.host, port=args.port, log_level='warning', timeout_graceful_shutdown=5)

if __name__ == '__main__':
    main()
