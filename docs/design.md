# 实现说明

本页面向维护者，说明 claude-sessiond 与 Claude Agent SDK、Claude Code CLI 之间依赖的行为，以及升级 SDK 时需要重新确认的部分。实现集中在 `claude_sessiond.py` 一个文件中。

## 进程与选项

每个会话是一个 `ClaudeSDKClient`，以流式输入模式运行，进程在会话活跃期间一直存在。启动选项中与缺省值不同的部分及其原因如下：

| 选项 | 原因 |
| --- | --- |
| `system_prompt={'type': 'preset', 'preset': 'claude_code'}` | SDK 缺省的系统提示词为空，会话的行为与 CLI 不同 |
| `setting_sources=['user', 'project', 'local']` | 与 CLI 一样加载 CLAUDE.md、skills、hooks、MCP 与权限规则 |
| `--replay-user-messages` | CLI 在吸收用户消息时把它回送到输出流，用于判断消息的状态（见下一节） |
| `--allow-dangerously-skip-permissions`（`allow_bypass`） | 没有这个参数时，CLI 拒绝在运行中切换到 `bypassPermissions` |
| `enable_file_checkpointing`（`checkpointing`） | 开启文件检查点，SDK 为此设置 `CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING` |
| `session_id` 或 `resume` | 会话记录不存在时以预先分配的 ID 新建，存在时恢复 |
| `can_use_tool` | 需要确认的工具调用与 AskUserQuestion 在此挂起，等待 API 回答或超时 |
| `env` | 会话所用账号的凭据（见“账号切换”） |

`bypassPermissions` 下 SDK 会警告 `can_use_tool` 不再被调用，但 AskUserQuestion 仍经过这个回调，因此守护进程屏蔽了这条警告。

守护进程启动时清除 `CLAUDECODE` 与 `CLAUDE_*` 环境变量（保留项见[部署与账号](guide/deployment.md#登录凭据)）。从 Claude Code 会话内启动时，父会话的 `CLAUDECODE`、`CLAUDE_CODE_SESSION_ID`、`CLAUDE_CODE_CHILD_SESSION`、`CLAUDE_CODE_ENTRYPOINT` 等变量会被子进程继承；新会话因此被当作父会话的子会话，不保存会话记录，`resume` 随之失效。清除后，会话记录中的 `entrypoint` 为 `sdk-py`。

## 消息何时被吸收

CLI 在某个回合实际吸收一条用户消息时，以同一个 `uuid` 回送这条消息；回送发生在吸收的时刻，而不是收到的时刻。回合中途发出的消息要等到当前工具调用结束、被回合吸收时才回送。守护进程据此维护消息的状态：

- 发出时记为 `queued`。
- 收到回送时记为 `taken`。若此时没有进行中的回合（自上一个 `ResultMessage` 之后尚未收到 `AssistantMessage` 或 `StreamEvent`），这条消息开启了新回合，`checkpoint` 记为真。
- 收到 `ResultMessage` 时，`user_message_uuids` 中的消息记为 `done`，并唤醒以 `wait` 等待它们的请求。回合被中断（`terminal_reason` 为 `aborted_streaming` 或 `aborted_tools`）或进程停止时，仍未被吸收的消息记为 `dropped`。

## 直接发送的控制请求

Python SDK 的 `rewind_files()` 丢弃 CLI 的响应，不支持 dry run，SDK 也没有包装 `cancel_async_message`。守护进程通过 SDK 的私有方法 `_query._send_control_request` 直接发送这两个控制请求，格式与 TypeScript SDK 相同：

| 请求 | 响应中使用的字段 |
| --- | --- |
| `{"subtype": "cancel_async_message", "message_uuid": …}` | `cancelled` |
| `{"subtype": "rewind_files", "user_message_id": …, "dry_run": …}` | `canRewind`、`error`、`filesChanged`、`insertions`、`deletions` |

升级 SDK 之后，应首先确认这个私有方法仍然存在，并重新验证撤回与回滚。

## 账号切换

额度相关的信号来自 CLI 的输出，已用返回固定错误的模拟 API 确认：

| 情形 | CLI 的输出 |
| --- | --- |
| 订阅额度用完（OAuth token，HTTP 429） | 先输出 `rate_limit_event`，`status` 为 `rejected`，`resetsAt` 为重置时间；随后 `AssistantMessage` 的 `error` 为 `rate_limit`，结果 `is_error` 为真，`terminal_reason` 为 `api_error` |
| API key 的速率限制（HTTP 429） | 没有 `rate_limit_event`；`error` 为 `rate_limit` |
| API 余额不足（HTTP 400） | `error` 为 `billing_error` |

CLI 在订阅额度用完时还带有 `apiError: "usage_limit_reached"`，但 Python SDK 的 `AssistantMessage` 不保留这个字段，因此守护进程无法区分额度用完与短暂的速率限制，两者都会切换。只有结果 `is_error` 为真时才切换并继续；若只有子智能体遇到限制而回合本身成功，账号仍被标记为不可用，但不发送继续的消息。

凭据通过 `ClaudeAgentOptions.env` 传给进程。SDK 把它覆盖在继承的环境之上，无法删除继承的变量，因此守护进程把另一个凭据变量设为空字符串：实测 CLI 把空字符串视为未设置。`ANTHROPIC_API_KEY` 优先于已有的登录凭据；两个变量同时设置时，`CLAUDE_CODE_OAUTH_TOKEN` 优先。

切换时，以 `wait` 等待原回合的请求不随原回合的结果返回，而是转接到继续之后那个回合；若新账号也遇到限制，再次转接，直到某个回合成功或没有可用的账号。`account_switched` 事件在停止旧进程之后发出，因为停止会清空事件缓冲。

## 内存与持久化

状态文件只保存托管的会话及其选项（包括当前的账号）、创建与最近活动时间、事件序号 `seq` 与归档状态，每当会话列表或这些字段变化时整体重写。账号及其不可用标记保存在同一目录的 `accounts.json` 中，以权限 600 写入。以下内容只存在于内存中：

| 内容 | 上限 | 清空时机 |
| --- | --- | --- |
| 事件缓冲 | 每个会话 5000 个事件 | 休眠、归档 |
| 已发消息的状态 | 每个会话 1000 条 | 归档 |
| 待回答的提示、等待中的 `wait` 请求 | 无 | 提示了结、回合结束 |
| 上一回合的结果、累计费用 | 无 | 归档（结果）、守护进程重启 |

休眠时保留已发消息的状态，是为了在进程异常退出后仍能查到哪些消息被 `dropped`。守护进程重启时，进行中的回合中止，会话以休眠状态载入；会话记录不受影响。

## 验证

以 Claude Agent SDK 0.2.188（自带 Claude Code 2.1.331）与 Haiku 5.5，通过 HTTP 驱动真实会话，以下各项均已通过。测试环境的用户设置中 `defaultMode` 为 `bypassPermissions`，因此涉及权限确认的用例显式传入 `permission_mode: "default"`。

- **对话**：新建会话时附带第一条消息，`wait` 返回结果，后续消息保留上下文；`/compact` 正常压缩上下文（出现 `compact_boundary`）；会话记录中经守护进程发出的消息是普通的用户记录（没有 `isMeta` 与 `origin`），系统提示词为 Claude Code 的提示词。
- **回合中途的消息**：Bash 运行 12 秒期间发出的消息被同一回合执行，`user_message_uuids` 包含两条消息。同样期间发出的两条消息先处于 `queued`；撤回其中一条后，它没有交给模型，重复撤回返回 409；另一条在工具调用结束时被吸收，`checkpoint` 为假。
- **中断**：中断正在执行 90 秒命令的回合，0.5 秒内结束，`terminal_reason` 为 `aborted_tools` 或 `aborted_streaming`。
- **权限确认与提问**：Write 的权限确认出现在 `/prompts`，状态为 `waiting`，批准后文件写入；被拒绝的 `rm` 没有执行，Claude 收到了拒绝理由。AskUserQuestion 以 `question` 提示出现，回答后 Claude 使用了所选的选项，`bypassPermissions` 下同样挂起。`PATCH` 切换到 `bypassPermissions` 立即生效。
- **提示超时**：`--prompt-timeout 8` 时，Write 的权限确认在 8 秒后按拒绝处理，`reason` 为 `timeout`，文件没有写入；Claude 没有重试，回合结束，状态回到 `idle`。下一回合经 API 批准，`reason` 为 `answered`，文件写入。
- **Webhook**：按顺序收到 `prompt`（会话状态为 `waiting`）、`prompt_resolved`、`result`，均附带 bearer token；目标地址格式错误时，发送失败被记录到标准错误输出，后续通知照常处理。
- **休眠与恢复**：`/stop` 与空闲超时后进程退出，下一条消息自动恢复，上下文保留。有 35 个事件的会话休眠后，`/log?after=0` 只剩 `seq` 为 36 的 `dormant` 事件，`/messages` 的三条记录保留，`/history` 完整。守护进程重启后 `seq` 仍为 36，恢复后的新事件从 37 开始。
- **归档**：归档的会话不出现在 `GET /sessions` 的缺省结果中，`?include_archived=true` 时列出，`/log` 只剩 `archived` 事件，`/messages` 为空，`/transcripts` 标为已归档；对它再次 `resume` 返回 409；直接发送消息后自动取消归档，上下文保留；`/unarchive` 后会话仍为 `dormant`。归档状态与 `seq` 写入了状态文件。
- **托管已有会话**：`fork` 得到带有原上下文的新会话；`DELETE ?purge=true` 删除会话记录；第二个守护进程 `resume` 已被第一个打开的会话时返回 409。
- **文件回滚**：Write 新建 a.txt（v1）、下一回合 Edit 为 v2 之后，dry run 列出 a.txt 且不改动文件；回滚到第二条消息恢复为 v1；回滚到回合中途吸收的消息返回 409；会话休眠后回滚到第一条消息，守护进程自动恢复进程并删除 a.txt，此后对话照常继续。
- **事件流**：SSE 收到各类事件，`Last-Event-ID` 续传只补发之后的事件；`/log` 长轮询在没有新事件时按时返回。
- **账号切换**：在两个本地模拟 API 上登记账号：一个以 OAuth token 访问、返回订阅的 5 小时额度用完，一个以 API key 访问、返回余额不足。在前者下新建会话并以 `wait` 发送第一条消息，会话切换到 `default`，`wait` 返回继续之后那个回合的正确回答；该账号标为 `rate_limit`，恢复时间约为 1 小时后（即模拟的重置时间），`account_switched` 事件与通知的 `reason` 为 `rate_limit`。后者标为 `billing_error`，没有恢复时间，`reset` 后恢复可用。把休眠会话的账号改回不可用的账号后再发消息，会话以 `unavailable` 为由改用 `default`，上下文保留。其他账号都不可用时，原回合的错误结果原样返回，并发出 `accounts_exhausted`。未知账号返回 400，`default` 不能删除，`accounts.json` 的权限为 600，`GET /accounts` 不包含凭据。模拟 API 对每个请求都返回错误，因此回合进行到中途才遇到限制的情形没有单独验证，其处理与回合开始时相同。
- **环境与认证**：从 Claude Code 会话内启动时，`claude` 子进程的环境中没有 `CLAUDECODE`、`CLAUDE_CODE_SESSION_ID` 等变量，`CLAUDE_CODE_ENTRYPOINT` 为 `sdk-py`，`CLAUDE_CODE_OAUTH_TOKEN` 与 `CLAUDE_CONFIG_DIR` 原样传入。不带 token 或 token 错误（包括含非 ASCII 字符的 token）返回 401，Host 头不在白名单中时返回 403。
