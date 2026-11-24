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

Python SDK 的 `rewind_files()` 丢弃 CLI 的响应，不支持 dry run，SDK 也没有包装 `cancel_async_message` 与登录。守护进程通过 SDK 的私有方法 `_query._send_control_request` 直接发送这些控制请求，格式与 TypeScript SDK 相同：

| 请求 | 响应中使用的字段 |
| --- | --- |
| `{"subtype": "cancel_async_message", "message_uuid": …}` | `cancelled` |
| `{"subtype": "rewind_files", "user_message_id": …, "dry_run": …}` | `canRewind`、`error`、`filesChanged`、`insertions`、`deletions` |
| `{"subtype": "claude_authenticate", "loginWithClaudeAi": true}` | `automaticUrl` |
| `{"subtype": "claude_oauth_wait_for_completion"}` | `account` |

`requirements.txt` 把 SDK 固定在已验证的版本上。升级 SDK 之后，应首先确认 `_query._send_control_request` 仍然存在，并重新验证撤回、回滚与登录。

## 登录

登录由一个不运行会话的专用 `claude` 进程完成，两个凭据变量设为空字符串，使 CLI 使用并写入自己的凭据存储。`claude_authenticate` 返回的 `automaticUrl` 重定向到该进程在本机监听的端口，`claude_oauth_wait_for_completion` 在 CLI 收到重定向、换得并保存凭据之后响应。`claude_authenticate` 同时返回的 `manualUrl` 以一个显示 code 的页面结束，需要把 code 经 `claude_oauth_callback` 交回 CLI；守护进程不采用这种方式。监听端口收到不是由授权页面重定向而来的请求时，这次登录以 `No authorization code received` 失败。

CLI 的凭据存储目录（Linux 上的 `.credentials.json`；macOS 钥匙串条目的名称也由它派生）在设置了 `CLAUDE_SECURESTORAGE_CONFIG_DIR` 时取该变量，否则取配置目录。守护进程据此为 `default` 以外的每个账号单独设置凭据存储目录，配置目录保持共用：若为每个账号单独设置 `CLAUDE_CONFIG_DIR`，会话记录与用户设置也会随之分开，切换账号后会话无法恢复。CLI 把这个变量传给自己启动的子进程（例如 teammate）。

登录还把账号的身份（`oauthAccount`）写入配置目录的 `.claude.json`。为了不让其他账号的登录覆盖 `default` 的身份，这些登录的进程使用临时的 `CLAUDE_CONFIG_DIR`，登录结束后删除。`oauthAccount` 与会话实际使用的凭据属于不同账号时，会话照常以凭据所属的账号运行，CLI 也不改写 `oauthAccount`；`claude auth status` 显示的身份取自 `oauthAccount`，与凭据无关，因此要确认某份凭据属于哪个账号，应以它的 access token 查询 `/api/oauth/profile`。

## 账号切换

额度相关的信号来自 CLI 的输出，已用返回固定错误的模拟 API 确认：

| 情形 | CLI 的输出 |
| --- | --- |
| 订阅额度用完（OAuth token，HTTP 429） | 先输出 `rate_limit_event`，`status` 为 `rejected`，`resetsAt` 为重置时间；随后 `AssistantMessage` 的 `error` 为 `rate_limit`，结果 `is_error` 为真，`terminal_reason` 为 `api_error` |
| API key 的速率限制（HTTP 429） | 没有 `rate_limit_event`；`error` 为 `rate_limit` |
| API 余额不足（HTTP 400） | `error` 为 `billing_error` |

CLI 在订阅额度用完时还带有 `apiError: "usage_limit_reached"`，但 Python SDK 的 `AssistantMessage` 不保留这个字段，因此守护进程无法区分额度用完与短暂的速率限制，两者都会切换。只有结果 `is_error` 为真时才切换并继续；若只有子智能体遇到限制而回合本身成功，账号仍被标记为不可用，但不发送继续的消息；子智能体失败的通知随即开启的回合在同一个账号下同样失败，由那个回合触发切换。继续的消息明确说明新账号没有受限：只说会话换了账号时，主会话会看到上一条错误中的重置时间，打算等到那时再恢复停下的子智能体。

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
