# 会话与消息

每个托管的会话对应一个常驻的 `claude` 进程。会话 ID 即 Claude Code 的会话 ID，同一个 ID 也可用于 `claude --resume`。

## 会话的状态

| 状态 | 含义 |
| --- | --- |
| `starting` | `claude` 进程正在启动 |
| `idle` | 进程在运行，没有进行中的回合；后台任务与子智能体可能仍在运行 |
| `running` | 回合进行中，或者有已发出但尚未被回合吸收的消息 |
| `waiting` | 有待回答的权限确认或提问 |
| `dormant` | 没有进程；会话记录保留在磁盘上 |

会话空闲超过 `--idle-timeout` 秒（缺省 30 分钟）、收到 `/stop` 或进程异常退出后进入休眠：进程退出，内存中的事件缓冲清空。每个活跃进程约占 280 MB 内存。之后发送消息、调用 `/start` 或 `/rewind` 时，守护进程从会话记录恢复进程，上下文保持不变。

进程退出会终止其中的后台任务与会话内的定时任务，因此有这两类工作时，空闲的会话不会休眠：

- 后台任务：`run_in_background` 的 Bash 命令、Monitor 与后台子智能体，列在 `GET /sessions/{id}` 的 `background_tasks` 中。不会自行结束的任务（例如开发服务器）使会话一直保持活跃，可以用 `POST /sessions/{id}/tasks/{task_id}/stop` 停止。
- 定时任务：ScheduleWakeup 与 CronCreate 创建的任务，列在 `crons` 中。周期性的定时任务使会话一直保持活跃，直到被删除。`durable` 的定时任务不使会话保持活跃。

`/stop`、归档与删除照常停止进程。会话休眠期间定时任务不触发；再次启动后，CronCreate 创建、尚未到期的定时任务恢复，ScheduleWakeup 的唤醒不恢复。

事件序号 `seq` 在休眠与守护进程重启之后继续递增，客户端保存的 `after` 或 `Last-Event-ID` 始终有效。已清空的早期事件可以通过 `/history` 从会话记录读取。

会话正被另一个 Claude Code 进程（例如 VS Code）打开时，启动请求返回 409，以免两个进程同时写同一份会话记录；确需打开时传入 `force: true`。

### 归档与删除

不再使用的会话可以归档或删除，两者都会中止进行中的回合：

- **归档**（`POST /sessions/{id}/archive`）：停止进程，并从 `GET /sessions` 的缺省结果中隐藏（`?include_archived=true` 时列出）。再次发送消息、调用 `/start` 或 `/rewind` 时自动取消归档；`/unarchive` 只取消归档，不启动进程。
- **删除**（`DELETE /sessions/{id}`）：不再托管该会话，会话记录保留，以后可以用 `POST /sessions` 的 `resume` 字段重新托管；加上 `?purge=true` 时同时删除会话记录（含子智能体），以及配置目录中该会话的 `session-env/` 与 `file-history/`（文件检查点）；项目目录中的 `memory/` 属于整个项目，只在它为空时删除，项目目录也只在删空之后删除。

`POST /sessions` 的 `resume` 字段也可以托管在 CLI 或 IDE 中创建的会话，`GET /transcripts` 列出本机所有会话记录；同时指定 `fork: true` 时先复制一份再托管。

## 消息

`POST /sessions/{id}/messages` 发送一条消息。`wait: true` 使请求等到吸收这条消息的回合结束，并返回该回合的结果；`timeout` 秒内没有结束时先返回 `timed_out`。`interrupt: true` 先中断进行中的回合。中断只结束主会话的回合，后台任务、子智能体与尚未被吸收的消息不受影响。

回合进行中发出的消息在当前工具调用结束时被当前回合吸收。每条消息的状态可以通过 `GET /sessions/{id}/messages` 查询：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已交给 CLI，尚未被回合吸收；此时可以用 `DELETE /sessions/{id}/messages/{uuid}` 撤回 |
| `taken` | 已被回合吸收；`checkpoint` 为真表示这条消息开启了该回合 |
| `done` | 吸收它的回合已经结束 |
| `cancelled` | 已撤回 |
| `dropped` | 会话停止时尚未处理完 |

## 权限确认与提问

工具调用先经过权限模式以及设置中的允许、拒绝规则；仍需确认的调用挂起为一个提示。AskUserQuestion 在任何权限模式下都会挂起为提示，包括 `bypassPermissions`。待回答的提示可以通过 `GET /prompts` 查询，此时会话的状态为 `waiting`。回答的格式如下：

```javascript
{"allow": true}                                     // 批准
{"allow": true, "remember": true}                   // 批准，并应用 CLI 给出的“不再询问”规则
{"allow": true, "updated_input": {...}}             // 修改参数后批准
{"allow": false, "message": "不要删除文件"}         // 拒绝，message 交给 Claude
{"allow": false, "interrupt": true}                 // 拒绝并结束回合
{"allow": true, "answers": {"<问题原文>": "Blue"}}  // 回答 AskUserQuestion，多选时给出列表
{"allow": true, "response": "换一种做法"}           // 不选择任何选项，直接回复一段文字
```

提示最多等待 `--prompt-timeout` 秒（缺省 1 小时），截止时间见提示的 `expires` 字段。超时的提示按拒绝处理，Claude 会被告知用户没有回答，并在结束回合时说明需要什么。中断或停止会话时，未回答的提示同样按拒绝处理。

会话缺省以 `allow_bypass` 启动，运行中可以用 `PATCH /sessions/{id}` 把权限模式切换为 `bypassPermissions`。未指定 `permission_mode` 时，会话使用设置中的 `defaultMode`。

## 子智能体

主会话用 Agent 工具派生的子智能体在同一个 `claude` 进程中运行。守护进程托管的会话不能使用智能体团队（见[未解决的问题](../issues.md#sdk-会话不能使用智能体团队)）：开启 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 后，带 `name` 的 Agent 调用派生的仍是普通的后台子智能体。主会话可以用 SendMessage 给它发消息，已经结束的子智能体收到消息后从自己的会话记录恢复，保留此前的上下文。

- 子智能体的消息出现在事件流中，`parent_tool_use_id` 为派生它的 Agent 调用，不改变会话的状态。`/history` 只含主会话；子智能体的会话记录另存，用 `GET /sessions/{id}/agents` 列出，用 `/agents/{agent_id}/history` 读取。它需要确认的工具调用同样挂起为提示，提示的 `agent_id` 标明发起调用的子智能体。
- 后台子智能体结束后，CLI 自行开启一个回合报告结果。这个回合产生 `result` 事件与 webhook 通知，但不对应任何已发消息，`wait` 等不到它。
- 子智能体与主会话使用同一个账号。[切换账号](deployment.md#多个账号)时进程重启，仍在运行的子智能体被中断，继续的消息会要求主会话恢复它们。

## 文件回滚

会话缺省开启文件检查点（`checkpointing`）。每条开启回合的消息（`checkpoint` 为真）是一个检查点，`POST /sessions/{id}/rewind` 把文件恢复到该消息被处理之前的状态，`dry_run: true` 只列出将要改动的文件与增删行数。

回滚只作用于 Write、Edit 与 NotebookEdit 修改的文件，对话与上下文保持不变。Bash 命令与子智能体对文件的修改不被跟踪，目录的创建、移动与删除也不会被撤销；需要完整撤销时应使用版本控制。
