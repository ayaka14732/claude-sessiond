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
- 收到回送时记为 `taken`。若此时当前回合尚未输出（自上一个 `ResultMessage` 之后尚未收到主会话的 `AssistantMessage` 或 `StreamEvent`），这条消息开启了新回合，`checkpoint` 记为真。
- 收到 `ResultMessage` 时，状态为 `taken` 的消息与 `user_message_uuids` 中的消息记为 `done`，并唤醒以 `wait` 等待它们的请求。仍为 `queued` 的消息保持不变：普通的中断不丢弃排队的消息（CLI 在中断请求的响应中以 `still_queued` 列出它们），它们由之后的回合吸收；CLI 自行开启的回合（后台任务结束、定时任务触发）的 `user_message_uuids` 为 null，但可能在中途吸收消息。进程停止时，尚未 `done` 的消息记为 `dropped`。

会话的状态只反映主会话的回合。CLI 在每个回合开始时输出 `init` 系统消息，包括它自行开启的回合；守护进程以它为回合的开始，以 `ResultMessage` 为结束。`init` 先于开启回合的那条消息的回送，因此 `checkpoint` 不以 `init` 为准，而以主会话的首个输出为准。子智能体的 `AssistantMessage` 与 `StreamEvent` 带有 `parent_tool_use_id`，不计入回合：它们可以在主会话的回合之外运行，而且被中断时不一定产生 `ResultMessage`；它们是否仍在运行由后台任务的列表反映（见下一节）。

SDK 读取 CLI 的 `session_state_changed`（经 `CLAUDE_CODE_SDK_READS_SESSION_STATE` 请求，不交给调用方，最新值存于私有的 `_query._session_state`）。守护进程不采用它：后台子智能体运行期间它一直是 `running`，表示之后是否还会有回合，而不是主会话的回合是否在进行。

## 后台任务

CLI 在后台任务的集合变化时（启动、结束、被停止、前台的子智能体转入后台）输出 `background_tasks_changed` 系统消息，其中的 `tasks` 是变化之后仍在运行的全部后台任务，每项含 `task_id`、`task_type`、`description` 等字段。`run_in_background` 的 Bash 命令与 Monitor 的 `task_type` 都是 `local_bash`，后台子智能体是 `local_agent`。任务结束时，CLI 先输出 `task_updated` 与 `task_notification`，再输出不含该任务的列表，随后自行开启报告结果的回合。CLI 启动时不输出这条消息，此时集合为空。

守护进程保存最近一次的列表，进程停止时清空。列表中有任务时，空闲的会话不被休眠；`ambient` 为真的任务（CLI 说明不算活动，例如自动启动的监视器）与 `off_host` 为真的任务（在进程之外运行）除外。守护进程不以 `task_started` 与 `task_notification` 配对判断，因为 CLI 说明两者与列表的先后顺序不固定，也不是每个结束的任务都有 `task_notification`。

会话内的定时任务（CronCreate；ScheduleWakeup 实际上也创建一个 `recurring` 为假的定时任务）不在列表中，CLI 也不为它们输出系统消息。它们只存在于进程中，进程停止后不再按时触发。CLI 把会话内的 CronCreate 定时任务记入会话记录（`session_cron_carry` 附件），恢复会话时在进程启动时就恢复其中尚未到期的定时任务，不需要新的回合；已过触发时间的一次性任务被丢弃，ScheduleWakeup 的唤醒不恢复。守护进程从两处得到它们：

- Stop hook 的输入中的 `session_crons`，每项含 `id`、`schedule`（cron 表达式）、`recurring` 与 `prompt`。守护进程为每个会话注册一个 Stop hook 回调（`ClaudeAgentOptions.hooks`，与设置中的 hooks 并存），以它替换整个列表。Stop hook 只在回合正常结束时运行：被中断的回合不运行，出错的回合运行 StopFailure，不带这个字段；设置了 `disableAllHooks` 或只允许托管 hooks 时也不运行。`session_crons` 在 CLI 的 schema 中是可选的，没有时保留原来的列表。
- 工具的结果，用于两次 Stop hook 之间。守护进程记录 CronCreate、CronDelete 与 ScheduleWakeup 的调用，在对应的 `UserMessage` 中读取结构化的 `tool_use_result`：CronCreate 给出 `id`、`recurring`、`durable` 与下次触发的 `nextFireAt`（毫秒），CronDelete 给出 `id`，ScheduleWakeup 给出 `scheduledFor`（毫秒）而没有 `id`。周期性定时任务的 `nextFireAt` 很快过时，守护进程不保留。新的 ScheduleWakeup 取代仍在等待的那一个，`stop: true` 取消它（结果中 `stopped` 为真）；守护进程以 `wakeup` 标记 ScheduleWakeup 的条目，取代或取消时按标记移除。`durable` 为真的定时任务写入项目的 `.claude/scheduled_tasks.json`，Stop hook 也不列出它们，守护进程不跟踪。

以 Stop hook 替换列表时，已知的触发时间与 `wakeup` 标记按 `id` 保留，ScheduleWakeup 的则按 `prompt` 对应（Stop hook 中的 `prompt` 截断为 1000 个字符，过长时对应不上）。进程停止时，守护进程只保留 CLI 会恢复的条目：`wakeup` 为假，并且是周期性的，或触发时间已知且尚未到。触发时间未知的一次性条目不保留，因为它可能在休眠期间到期并被 CLI 丢弃，保留它会使只启动不发消息的会话一直保持活跃。启动进程、载入状态文件与显示休眠的会话时，再剔除已过触发时间的条目。这样，下一次启动后、第一个 Stop hook 之前，会话也为恢复的定时任务保持活跃。

## 直接发送的控制请求

Python SDK 的 `rewind_files()` 丢弃 CLI 的响应，不支持 dry run，`interrupt()` 不能带参数，SDK 也没有包装 `cancel_async_message` 与登录。守护进程通过 SDK 的私有方法 `_query._send_control_request` 直接发送这些控制请求，格式与 TypeScript SDK 相同：

| 请求 | 响应中使用的字段 |
| --- | --- |
| `{"subtype": "interrupt", "spare_background_tasks": true}` | 无 |
| `{"subtype": "cancel_async_message", "message_uuid": …}` | `cancelled` |
| `{"subtype": "rewind_files", "user_message_id": …, "dry_run": …}` | `canRewind`、`error`、`filesChanged`、`insertions`、`deletions` |
| `{"subtype": "claude_authenticate", "loginWithClaudeAi": true}` | `automaticUrl` |
| `{"subtype": "claude_oauth_wait_for_completion"}` | `account` |

不带 `spare_background_tasks` 的中断（SDK 的 `interrupt()` 发送的就是这种）除了结束回合，还停止所有后台子智能体与 workflow；后台的 Bash 命令不受影响。这个字段是 CLI 标为内部的字段，供自己能逐个停止任务的客户端使用，守护进程的 `/tasks/{task_id}/stop` 正是这样。

CLI 只在 dry run 的响应中给出 `filesChanged`、`insertions` 与 `deletions`，实际回滚的响应只有 `canRewind`。因此实际回滚之前，守护进程先做一次 dry run，把其中的统计并入响应与 `rewound` 事件。

`requirements.txt` 把 SDK 固定在已验证的版本上。升级 SDK 之后，应首先确认以下依赖仍然成立，并重新验证撤回、回滚、登录、中断、后台任务与定时任务：

- 私有方法 `_query._send_control_request` 与私有属性 `client._transport._process.pid`（见“内存与持久化”）。
- `GET /sessions/{id}/agents` 用 SDK 的私有函数 `_resolve_subagents_dir`、`_collect_agent_files` 与 `_read_agent_metadata_sidecar` 找到子智能体的会话记录并读取旁边的 `agent-<id>.meta.json`；SDK 的公开接口只给出子智能体的 ID。`meta` 的字段由 CLI 决定，守护进程原样转发。
- 带 `spare_background_tasks` 的 `interrupt` 控制请求，这个字段是 CLI 标为内部的字段。
- `background_tasks_changed` 不被 SDK 解析为专门的类型，守护进程从通用 `SystemMessage` 的 `data['tasks']` 读取列表；每个回合开始时的 `init` 系统消息。
- Stop hook 输入中的 `session_crons`，以及 CronCreate、CronDelete、ScheduleWakeup 结果的结构。
- 会话注册文件中的 `procStart` 与 `/proc/<pid>/stat` 第 22 个字段相同。
- [未解决的问题](issues.md)中各条的“确认方法”。

## 登录

登录由一个不运行会话的专用 `claude` 进程完成，两个凭据变量设为空字符串，使 CLI 使用并写入自己的凭据存储。`claude_authenticate` 返回的 `automaticUrl` 重定向到该进程在本机监听的端口，`claude_oauth_wait_for_completion` 在 CLI 收到重定向、换得并保存凭据之后响应。`claude_authenticate` 同时返回的 `manualUrl` 以一个显示 code 的页面结束，需要把 code 经 `claude_oauth_callback` 交回 CLI；守护进程不采用这种方式。监听端口收到不是由授权页面重定向而来的请求时，这次登录以 `No authorization code received` 失败。

CLI 的凭据存储目录（Linux 上的 `.credentials.json`；macOS 钥匙串条目的名称也由它派生）在设置了 `CLAUDE_SECURESTORAGE_CONFIG_DIR` 时取该变量，否则取配置目录。守护进程据此为 `default` 以外的每个账号单独设置凭据存储目录，配置目录保持共用：若为每个账号单独设置 `CLAUDE_CONFIG_DIR`，会话记录与用户设置也会随之分开，切换账号后会话无法恢复。CLI 把这个变量传给自己启动的子进程。

登录还把账号的身份（`oauthAccount`）写入配置目录的 `.claude.json`。为了不让其他账号的登录覆盖 `default` 的身份，这些登录的进程使用临时的 `CLAUDE_CONFIG_DIR`，登录结束后删除。`oauthAccount` 与会话实际使用的凭据属于不同账号时，会话照常以凭据所属的账号运行，CLI 也不改写 `oauthAccount`；`claude auth status` 显示的身份取自 `oauthAccount`，与凭据无关，因此要确认某份凭据属于哪个账号，应以它的 access token 查询 `/api/oauth/profile`。

## 账号切换

额度相关的信号来自 CLI 的输出，已用返回固定错误的模拟 API 确认：

| 情形 | CLI 的输出 |
| --- | --- |
| 订阅额度用完（OAuth token，HTTP 429） | 先输出 `rate_limit_event`，`status` 为 `rejected`，`resetsAt` 为重置时间；随后 `AssistantMessage` 的 `error` 为 `rate_limit`，结果 `is_error` 为真，`terminal_reason` 为 `api_error` |
| API key 的速率限制（HTTP 429） | 没有 `rate_limit_event`；`error` 为 `rate_limit` |
| API 余额不足（HTTP 400） | `error` 为 `billing_error` |

CLI 在订阅额度用完时还带有 `apiError: "usage_limit_reached"`，但 Python SDK 的 `AssistantMessage` 不保留这个字段，因此守护进程无法区分额度用完与短暂的速率限制，两者都会切换。只有结果 `is_error` 为真时才切换并继续；若只有子智能体遇到限制而回合本身成功，账号仍被标记为不可用，但不发送继续的消息；子智能体失败的通知随即开启的回合在同一个账号下同样失败，由那个回合触发切换。继续的消息明确说明新账号没有受限：只说会话换了账号时，主会话会看到上一条错误中的重置时间，打算等到那时再恢复停下的子智能体。继续的消息还说明后台命令与 Monitor 已随重启终止、不会再报告结果，否则主会话可能一直等待它们的通知。会话内的 CronCreate 定时任务由新进程恢复，继续的消息不提它们。

凭据通过 `ClaudeAgentOptions.env` 传给进程。SDK 把它覆盖在继承的环境之上，无法删除继承的变量，因此守护进程把另一个凭据变量设为空字符串：实测 CLI 把空字符串视为未设置。`ANTHROPIC_API_KEY` 优先于已有的登录凭据；两个变量同时设置时，`CLAUDE_CODE_OAUTH_TOKEN` 优先。

切换时，以 `wait` 等待原回合的请求不随原回合的结果返回，而是转接到继续之后那个回合；若新账号也遇到限制，再次转接，直到某个回合成功或没有可用的账号。`account_switched` 事件在停止旧进程之后发出，因为停止会清空事件缓冲。

## 内存与持久化

状态文件只保存托管的会话及其选项（包括当前的账号）、创建与最近活动时间、事件序号 `seq` 的上限、归档状态、`claude` 进程的 pid 与启动时间，以及定时任务的列表（见“后台任务”），每当会话列表或这些字段变化时整体重写。事件序号即将超过文件中的上限时，守护进程先把上限提高 1000 并写入文件，因此文件中的值总不小于已发出的序号；守护进程异常退出后，编号从这个上限继续，跳过的序号不对应任何事件。正常退出时写入实际的序号，编号不跳过。账号及其不可用标记保存在同一目录的 `accounts.json` 中，以权限 600 写入。以下内容只存在于内存中：

| 内容 | 上限 | 清空时机 |
| --- | --- | --- |
| 事件缓冲 | 每个会话 5000 个事件 | 休眠、归档 |
| 已发消息的状态 | 每个会话 1000 条 | 归档 |
| 待回答的提示、等待中的 `wait` 请求 | 无 | 提示了结、回合结束 |
| 后台任务的列表 | 无 | 进程停止（休眠、归档、切换账号） |
| 上一回合的结果、累计费用 | 无 | 归档（结果）、守护进程重启 |

休眠时保留已发消息的状态，是为了在进程异常退出后仍能查到哪些消息被 `dropped`。守护进程重启时，进行中的回合中止，会话以休眠状态载入；会话记录不受影响。

停止进程时，CLI 退出前的约 1 秒（有后台任务时更久）内 `ClaudeSDKClient` 仍然存在，但已不能写入。此时到达的消息与 `/start`、`/rewind` 等待进程退出，再按休眠的会话重新启动；`/interrupt` 不再发送中断请求，停止本身会结束回合。

pid 取自 SDK 的私有属性 `client._transport._process.pid`，启动时间取自 `/proc/<pid>/stat` 的第 22 个字段，即 CLI 在会话注册文件中记作 `procStart` 的值；两者在进程运行期间写入状态文件，进程停止后清除。CLI 的输入关闭后，若还有后台任务在运行，它不退出，而是继续运行并在会话注册目录中登记该会话；守护进程被 SIGKILL 等方式结束时就会留下这样的进程。被 SIGKILL 结束的进程（例如 systemd 在守护进程异常退出后结束的进程）则留下过期的注册文件，其中的 pid 日后可能属于其他进程，因此判断会话是否被占用时，除了确认 pid 存在，还在能读取启动时间的系统上比较 `procStart`。恢复会话时，若登记该会话的进程的 pid 与启动时间都与状态文件中记录的相同，守护进程向它发送 SIGTERM，等它退出（10 秒后改发 SIGKILL）再启动新进程，而不是返回 409；其他进程登记的会话仍返回 409。不能读取启动时间时（非 Linux 系统），或 `--cli-path` 指向包装脚本、记录的 pid 与登记的不同时，留下的进程只能手动结束。

同一个会话的启动与停止（包括归档、删除、休眠与切换账号中的停止）依次进行：等待进程退出与结束留下的进程都需要时间，期间会话仍显示为 `dormant`。所有等待结束、创建进程之前，若会话已被删除，请求返回 404，不会为不再托管的会话启动进程；删除会话时先从会话列表中移除，再停止进程。这期间到达的 `/stop` 与归档在启动完成后生效。

uvicorn 收到 SIGTERM 后，先等待所有连接结束，再执行 lifespan 中停止会话的清理。SSE 事件流不会自行结束，因此守护进程把 `timeout_graceful_shutdown` 设为 5 秒，超时后取消剩余的请求；否则只要还有客户端连着，守护进程就不会退出，最终被 systemd 以 SIGKILL 结束，会话的进程也不经停止就被杀死。
