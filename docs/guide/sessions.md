# 会话与消息

每个托管的会话对应一个常驻的 `claude` 进程。会话 ID 由守护进程在创建时分配，即 Claude Code 的会话 ID，因此同一个 ID 既可用于 API，也可用于 `claude --resume`。

## 会话的状态

| 状态 | 含义 |
| --- | --- |
| `starting` | `claude` 进程正在启动，通常约 0.5 秒 |
| `idle` | 进程在运行，没有进行中的回合 |
| `running` | 回合进行中，或者有已发出但尚未被回合吸收的消息 |
| `waiting` | 有待回答的权限确认或提问 |
| `dormant` | 没有进程；会话记录保留在磁盘上 |

会话空闲超过 `--idle-timeout` 秒（缺省 30 分钟）、收到 `/stop` 或进程异常退出后进入休眠：进程退出，内存中的事件缓冲同时清空。每个活跃进程约占 280 MB 内存，休眠的会话只保留配置。之后发送消息、调用 `/start` 或 `/rewind` 时，守护进程从会话记录恢复进程，上下文保持不变。

事件序号 `seq` 在休眠和守护进程重启之后继续递增，客户端保存的 `after` 或 `Last-Event-ID` 始终有效。已清空的早期事件可以通过 `/history` 从会话记录读取。

恢复之前，守护进程检查该会话是否正被另一个 Claude Code 进程打开（例如 VS Code 或另一个守护进程）。若是，请求返回 409，以免两个进程同时写同一份会话记录；确需打开时传入 `force: true`。

### 归档与删除

不再使用的会话可以归档或删除，两者都会中止进行中的回合：

- **归档**（`POST /sessions/{id}/archive`）：停止进程，清空事件缓冲与已发消息的记录，并从 `GET /sessions` 的缺省结果中隐藏（`?include_archived=true` 时列出）。再次发送消息、调用 `/start` 或 `/rewind` 时自动取消归档；`/unarchive` 只取消归档，不启动进程。
- **删除**（`DELETE /sessions/{id}`）：不再托管该会话，会话记录保留，以后可以用 `POST /sessions` 的 `resume` 字段重新托管；加上 `?purge=true` 时同时删除会话记录。

`POST /sessions` 的 `resume` 字段也可以托管在 CLI 或 IDE 中创建的会话，`GET /transcripts` 列出本机所有会话记录；同时指定 `fork: true` 时先复制一份再托管。

## 消息

`POST /sessions/{id}/messages` 发送一条消息。`wait: true` 使请求等到吸收这条消息的回合结束，并返回该回合的结果；`timeout` 秒内没有结束时先返回 `timed_out`。`interrupt: true` 先中断进行中的回合。

回合进行中发出的消息不会等到下一个回合，而是在当前工具调用结束时被当前回合吸收。守护进程根据 CLI 的回送记录每条消息的状态，可以通过 `GET /sessions/{id}/messages` 查询：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已交给 CLI，尚未被回合吸收；此时可以用 `DELETE /sessions/{id}/messages/{uuid}` 撤回，撤回的消息不会交给模型 |
| `taken` | 已被回合吸收；`checkpoint` 为真表示这条消息开启了该回合 |
| `done` | 吸收它的回合已经结束 |
| `cancelled` | 已撤回 |
| `dropped` | 回合被中断或会话停止时尚未被吸收 |

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

## 智能体团队

会话可以使用 Claude Code 的智能体团队（agent teams）。在设置的 `env` 中开启 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 后，主会话用 Agent 工具派生的 teammate 由同一个 `claude` 进程运行。守护进程没有终端，因此应把 `teammateMode` 设为 `in-process`，以下行为均在该模式下验证。若改用环境变量开启，需要用 `--keep-env CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 保留它，否则守护进程启动时会将其清除（见[部署与账号](deployment.md#登录凭据)）。

- teammate 的消息与工具调用照常出现在事件流中，`parent_tool_use_id` 为派生它的 Agent 调用；teammate 的启动、进度与结束另有 `task_started`、`task_progress`、`task_notification` 等系统消息。
- teammate 需要确认的工具调用与主会话的一样挂起为提示，提示的 `agent_id` 标明发起调用的 teammate。
- 主会话派生 teammate 后通常先结束当前回合，`wait` 随该回合返回，此时 teammate 可能仍在运行，会话状态为 `running`。teammate 用 SendMessage 回复后，CLI 不经用户消息自行开启新的回合。这个回合同样产生 `result` 事件与 webhook 通知并更新 `last_result`，但不对应任何已发消息，`wait` 等不到它，其结果需要从事件流或 webhook 获取。
- teammate 与主会话使用同一个账号，额度用完时一同受影响。teammate 遇到限制后提前结束，主会话随即开启的回合在同一个账号下同样失败，于是触发[账号切换](deployment.md#多个账号)。切换会重启进程，仍在运行的 teammate 随之中断，其中正在执行的命令被终止。继续的消息要求主会话用 SendMessage 恢复因额度或重启而停下的 teammate；CLI 从 teammate 自己的会话记录恢复它，它从中断处继续，必要时重新执行被终止的命令。

## 文件回滚

会话缺省开启文件检查点（`checkpointing`）：Write、Edit 与 NotebookEdit 修改文件之前，CLI 先备份原文件。每条开启回合的消息（状态记录中 `checkpoint` 为真）是一个检查点，`POST /sessions/{id}/rewind` 把文件恢复到该消息被处理之前的状态，`dry_run: true` 只列出将要改动的文件与增删行数。

回滚只作用于文件，对话与上下文保持不变。Bash 命令与子智能体对文件的修改不被跟踪（主对话此前已用 Write 或 Edit 修改过的同一文件除外），目录的创建、移动与删除也不会被撤销；需要完整撤销时应使用版本控制。
