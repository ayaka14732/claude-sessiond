# 会话与消息

每个托管的会话对应一个常驻的 `claude` 进程。会话 ID 由守护进程在创建时分配，即 Claude Code 的会话 ID，因此同一个 ID 既可用于 API，也可用于 `claude --resume`。

## 会话的状态

| 状态 | 含义 |
| --- | --- |
| `starting` | `claude` 进程正在启动，通常约 0.5 秒 |
| `idle` | 进程在运行，主会话没有进行中的回合；后台任务与子智能体可能仍在运行 |
| `running` | 回合进行中（包括 CLI 自行开启的回合），或者有已发出但尚未被回合吸收的消息 |
| `waiting` | 有待回答的权限确认或提问 |
| `dormant` | 没有进程；会话记录保留在磁盘上 |

会话空闲超过 `--idle-timeout` 秒（缺省 30 分钟）、收到 `/stop` 或进程异常退出后进入休眠：进程退出，内存中的事件缓冲同时清空。每个活跃进程约占 280 MB 内存，休眠的会话只保留配置。之后发送消息、调用 `/start` 或 `/rewind` 时，守护进程从会话记录恢复进程，上下文保持不变；进程正在退出时到达的这些请求等它退出后再恢复。

进程退出时，其中的后台任务随之终止，结果不会再返回；会话内的定时任务也只存在于进程中，进程退出后不再按时触发。因此有这两类工作时，空闲的会话不会休眠：

- 后台任务包括 `run_in_background` 的 Bash 命令、Monitor 与后台子智能体，列在 `GET /sessions/{id}` 的 `background_tasks` 中。任务结束后，CLI 自行开启一个回合报告结果，空闲时间从这个回合结束时起算。不会自行结束的任务（例如开发服务器或 `tail -f`）使会话一直保持活跃，可以用 `POST /sessions/{id}/tasks/{task_id}/stop` 停止任务。
- 定时任务由 ScheduleWakeup 或 CronCreate 创建，列在 `crons` 中；`fires_at` 为一次性定时任务的触发时间（已知时），`wakeup` 标明由 ScheduleWakeup 创建。守护进程跟随这些工具的结果更新列表，并在每个正常结束的回合以 CLI 给出的列表校准。一次性的定时任务（包括 ScheduleWakeup）触发后即被移除，触发时间过去两分钟后也不再使会话保持活跃；周期性的定时任务使会话一直保持活跃，直到被删除或到期（CLI 缺省为 30 天）。`durable` 的定时任务保存在项目的 `.claude/scheduled_tasks.json` 中，不列在 `crons` 中，也不使会话保持活跃，会话休眠期间不会触发。

`/stop`、归档与删除不受影响，照常停止进程，后台任务随之消失。CronCreate 创建的定时任务在会话休眠期间不触发；再次启动时，CLI 从会话记录恢复尚未到期的那些（已过触发时间的一次性任务被丢弃），守护进程也把触发时间已知的那些保存在状态文件中，因此只启动而不发消息的会话同样等到它们触发，守护进程重启之后也是如此。ScheduleWakeup 安排的唤醒不会恢复。

事件序号 `seq` 在休眠和守护进程重启之后继续递增，守护进程异常退出后也不会回退，客户端保存的 `after` 或 `Last-Event-ID` 始终有效；异常退出之后的序号可能跳过一段。已清空的早期事件可以通过 `/history` 从会话记录读取。

恢复之前，守护进程检查该会话是否正被另一个 Claude Code 进程打开（例如 VS Code 或另一个守护进程）。若是，请求返回 409，以免两个进程同时写同一份会话记录；确需打开时传入 `force: true`。登记该会话的进程已经退出、或其 pid 已被其他进程复用时，不算占用。例外是守护进程自己此前启动的进程：守护进程被强制结束后，仍有后台任务的 `claude` 进程不随之退出，恢复时守护进程先结束这个进程（其中的后台任务一同终止），再照常恢复。这两种判断都依赖 `/proc` 中的进程启动时间，只在 Linux 上进行；其他系统上，pid 已被其他进程复用的过期登记与守护进程自己留下的进程都使请求返回 409；pid 已不存在的登记照常不算占用。

### 归档与删除

不再使用的会话可以归档或删除，两者都会中止进行中的回合：

- **归档**（`POST /sessions/{id}/archive`）：停止进程，清空事件缓冲与已发消息的记录，并从 `GET /sessions` 的缺省结果中隐藏（`?include_archived=true` 时列出）。再次发送消息、调用 `/start` 或 `/rewind` 时自动取消归档；`/unarchive` 只取消归档，不启动进程。
- **删除**（`DELETE /sessions/{id}`）：不再托管该会话，删除期间到达的请求返回 404。会话记录保留，以后可以用 `POST /sessions` 的 `resume` 字段重新托管；加上 `?purge=true` 时同时删除会话记录。

`POST /sessions` 的 `resume` 字段也可以托管在 CLI 或 IDE 中创建的会话，`GET /transcripts` 列出本机所有会话记录；同时指定 `fork: true` 时先复制一份再托管。

## 消息

`POST /sessions/{id}/messages` 发送一条消息。`wait: true` 使请求等到吸收这条消息的回合结束，并返回该回合的结果；`timeout` 秒内没有结束时先返回 `timed_out`。`interrupt: true` 先中断进行中的回合。中断（包括 `POST /sessions/{id}/interrupt`）只结束主会话的回合，后台任务与子智能体继续运行；尚未被吸收的消息也不受影响，由之后的回合照常处理。

回合进行中发出的消息不会等到下一个回合，而是在当前工具调用结束时被当前回合吸收；被 CLI 自行开启的回合（例如报告后台任务结果的回合）吸收时，`wait` 返回的是那个回合的结果。守护进程根据 CLI 的回送记录每条消息的状态，可以通过 `GET /sessions/{id}/messages` 查询：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已交给 CLI，尚未被回合吸收；此时可以用 `DELETE /sessions/{id}/messages/{uuid}` 撤回，撤回的消息不会交给模型 |
| `taken` | 已被回合吸收；`checkpoint` 为真表示这条消息开启了该回合 |
| `done` | 吸收它的回合已经结束 |
| `cancelled` | 已撤回 |
| `dropped` | 会话停止时尚未被吸收，或吸收它的回合尚未结束 |

## 权限确认与提问

工具调用先经过权限模式以及设置中的允许、拒绝规则；仍需确认的调用挂起为一个提示。AskUserQuestion 在任何权限模式下都会挂起为提示，包括 `bypassPermissions`。待回答的提示可以通过 `GET /prompts` 查询，此时会话的状态为 `waiting`。回答的格式如下：

```javascript
{"allow": true}                                     // 批准
{"allow": true, "remember": true}                   // 批准，并应用 CLI 给出的“不再询问”规则
{"allow": true, "updated_input": {...}}             // 修改参数后批准
{"allow": false, "message": "不要删除文件"}            // 拒绝，message 交给 Claude
{"allow": false, "interrupt": true}                 // 拒绝并结束回合
{"allow": true, "answers": {"<问题原文>": "Blue"}}    // 回答 AskUserQuestion，多选时给出列表
{"allow": true, "response": "换一种做法"}              // 不选择任何选项，直接回复一段文字
```

提示最多等待 `--prompt-timeout` 秒（缺省 1 小时），截止时间见提示的 `expires` 字段。超时的提示按拒绝处理，Claude 会被告知用户没有回答、不应重试，并在结束回合时说明需要什么；会话随后回到 `idle`，可以照常休眠。中断或停止会话时，未回答的提示同样按拒绝处理。

会话缺省以 `allow_bypass` 启动，运行中可以用 `PATCH /sessions/{id}` 把权限模式切换为 `bypassPermissions`。未指定 `permission_mode` 时，会话使用设置中的 `defaultMode`；若该值为 `bypassPermissions`，普通工具调用不会产生权限确认。

## 子智能体

主会话用 Agent 工具派生的子智能体由同一个 `claude` 进程运行。守护进程托管的会话不能使用智能体团队（agent teams）：即使在设置中开启了 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS`，带 `name` 的 Agent 调用派生的也是普通的后台子智能体（`task_type` 为 `local_agent`），而不是 teammate，团队特有的协作功能不可用（见[未解决的问题](../issues.md#sdk-会话不能使用智能体团队)）。带 `name` 的子智能体可以用 SendMessage 联系，能满足让多个有名字的子智能体并行工作、随后继续交代任务的用法。

- 子智能体的消息与工具调用照常出现在事件流中，`parent_tool_use_id` 为派生它的 Agent 调用；子智能体的启动、进度与结束另有 `task_started`、`task_progress`、`task_updated`、`task_notification` 等系统消息。子智能体的消息不改变会话的状态。
- 子智能体需要确认的工具调用与主会话的一样挂起为提示，提示的 `agent_id` 标明发起调用的子智能体，会话的状态随之变为 `waiting`。
- 主会话派生后台子智能体后通常先结束当前回合，`wait` 随该回合返回，此时子智能体仍在运行，会话不会休眠（见[会话的状态](#会话的状态)）。子智能体结束后，CLI 不经用户消息自行开启新的回合报告结果。这个回合同样产生 `result` 事件与 webhook 通知并更新 `last_result`，但不对应任何已发消息，`wait` 等不到它，其结果需要从事件流或 webhook 获取。
- 主会话可以用 SendMessage 给带 `name` 的子智能体发消息。已经结束的子智能体由 CLI 从它自己的会话记录恢复，保留此前的上下文，作为新的后台任务继续运行。
- 运行中的子智能体与后台任务可以用 `POST /sessions/{id}/tasks/{task_id}/stop` 单独停止，主会话的回合不受影响。
- 子智能体与主会话使用同一个账号，额度用完时一同受影响。子智能体遇到限制后提前结束，主会话随即开启的回合在同一个账号下同样失败，于是触发[账号切换](deployment.md#多个账号)。切换会重启进程，仍在运行的子智能体随之中断，其中正在执行的命令被终止。继续的消息要求主会话用 SendMessage 恢复因额度或重启而停下的子智能体；CLI 从子智能体自己的会话记录恢复它，它从中断处继续，必要时重新执行被终止的命令。

## 文件回滚

会话缺省开启文件检查点（`checkpointing`）：Write、Edit 与 NotebookEdit 修改文件之前，CLI 先备份原文件。每条开启回合的消息（状态记录中 `checkpoint` 为真）是一个检查点，`POST /sessions/{id}/rewind` 把文件恢复到该消息被处理之前的状态，`dry_run: true` 只列出将要改动的文件与增删行数。

回滚只作用于文件，对话与上下文保持不变。Bash 命令与子智能体对文件的修改不被跟踪（主对话此前已用 Write 或 Edit 修改过的同一文件除外），目录的创建、移动与删除也不会被撤销；需要完整撤销时应使用版本控制。
