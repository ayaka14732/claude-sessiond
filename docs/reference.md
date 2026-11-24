# 参考

运行中的守护进程在 `/openapi.json` 提供机器可读的接口描述，包括各端点的请求与响应字段。本页说明其中没有体现的语义。

## 命令行参数

| 参数 | 缺省 | 说明 |
| --- | --- | --- |
| `--host`、`--port` | `127.0.0.1`、`8790` | 监听地址 |
| `--token` | `$SESSIOND_TOKEN` | 设置后，所有请求必须带 `Authorization: Bearer <token>` 或查询参数 `?token=` |
| `--allow-host` | 无 | 额外接受的 Host 头，可重复指定 |
| `--idle-timeout` | `1800` | 会话空闲多少秒后进入休眠 |
| `--prompt-timeout` | `3600` | 提示最多等待多少秒，超时按拒绝处理；`0` 表示一直等待 |
| `--webhook`、`--webhook-token` | `$SESSIOND_WEBHOOK`、`$SESSIOND_WEBHOOK_TOKEN` | 通知的目标地址与随通知发送的 bearer token |
| `--state` | `~/.local/state/claude-sessiond/sessions.json` | 状态文件：托管的会话、会话选项、事件序号与归档状态；账号保存在同一目录的 `accounts.json` 中，经 API 登录的账号的凭据保存在同一目录的 `logins/<账号名>/` 中 |
| `--cli-path` | SDK 自带的 `claude` | 改用指定的 Claude Code 可执行文件 |
| `--keep-env` | 无 | 额外保留并传给会话的 `CLAUDE_*` 环境变量，可重复指定 |

## HTTP API

| 方法与路径 | 作用 |
| --- | --- |
| `GET /health` | 托管的会话数与活跃进程数 |
| `GET /sessions` | 托管的会话；`?include_archived=true` 时包括已归档的会话 |
| `POST /sessions` | 新建会话，或用 `resume` 托管已有的会话记录（`fork: true` 时先复制）；可以用 `prompt` 同时发送第一条消息 |
| `GET /sessions/{id}` | 状态、选项、待回答的提示、上一回合的结果，以及本次守护进程运行期间的累计费用 |
| `PATCH /sessions/{id}` | 修改 `name`（同时写入会话记录的标题）、`model` 与 `permission_mode`（活跃时立即生效）、`effort` 与 `account`（下次启动时生效） |
| `DELETE /sessions/{id}` | 停止并不再托管；`?purge=true` 时同时删除会话记录 |
| `POST /sessions/{id}/messages` | 发送消息：`text`、`wait`、`timeout`、`interrupt` |
| `GET /sessions/{id}/messages` | 已发消息的状态，最多保留最近 1000 条 |
| `DELETE /sessions/{id}/messages/{uuid}` | 撤回尚未被吸收的消息；其他状态返回 409 |
| `POST /sessions/{id}/rewind` | 把文件回滚到 `message_uuid` 之前；`dry_run` |
| `POST /sessions/{id}/interrupt` | 中断进行中的回合 |
| `POST /sessions/{id}/start`、`/stop` | 启动进程或使会话休眠 |
| `POST /sessions/{id}/archive`、`/unarchive` | 归档或取消归档 |
| `GET /sessions/{id}/events` | SSE 事件流；`?after=<seq>` 或 `Last-Event-ID` 从指定位置之后续传 |
| `GET /sessions/{id}/log` | JSON 形式的同一事件流；`?after=<seq>&wait=<秒>` 长轮询，最长 60 秒 |
| `GET /sessions/{id}/history` | 会话记录中的完整历史，休眠的会话同样可读 |
| `GET /sessions/{id}/context` | 上下文用量，仅限活跃的会话 |
| `GET /prompts` | 所有会话中待回答的提示 |
| `POST /sessions/{id}/prompts/{prompt_id}` | 回答提示，格式见[权限确认与提问](guide/sessions.md#权限确认与提问) |
| `GET /transcripts` | 本机的会话记录（`?cwd=` 筛选），标明是否已托管、是否已归档、是否正被某个进程打开 |
| `GET /accounts` | 账号及其状态：`kind`（`inherited`、`login`、`oauth_token` 或 `api_key`）、`env` 中的变量名、`available`、`limited`（`rate_limit` 或 `billing_error`）、`limited_until`、使用它的会话数 |
| `POST /accounts` | 登记账号：`name`，`oauth_token` 或 `api_key`，`env` |
| `DELETE /accounts/{name}` | 删除账号（`default` 除外）及其登录凭据；使用它的会话下次启动时改用第一个可用的账号 |
| `POST /accounts/{name}/reset` | 清除账号的不可用标记 |
| `POST /accounts/{name}/login` | 开始在浏览器中登录该账号，取消它此前未完成的登录；返回授权地址 `url` 与截止时间 `expires`。`default` 登录守护进程自身的凭据，其他账号在登录成功后登记或更新为 `login` 账号；登录的账号名只能由字母、数字、`.`、`_` 与 `-` 组成，且不以 `.` 开头 |
| `GET /accounts/{name}/login` | 该账号最近一次登录：`status`（`pending`、`done`、`failed` 或 `cancelled`）、`url`、`expires`、`profile`（登录的账号信息，例如 `email`、`subscriptionType`）、`error` |
| `GET /accounts/{name}/login/open` | 302 重定向到进行中的登录的授权地址，供浏览器直接打开；需要 token 时用查询参数 `?token=` |
| `DELETE /accounts/{name}/login` | 取消进行中的登录 |

新建会话时，除 `cwd`（新建时必填）、`resume`、`fork`、`force`、`prompt`、`wait`、`timeout` 外，以下字段决定 `claude` 进程的启动方式，并保存在状态文件中：`name`、`model`、`permission_mode`、`effort`、`append_system_prompt`（追加到 Claude Code 系统提示词之后）、`allowed_tools`、`disallowed_tools`、`add_dirs`、`max_budget_usd`、`partial_messages`（输出逐 token 的 `StreamEvent`）、`allow_bypass`（缺省为真）、`checkpointing`（缺省为真）、`account`（缺省为第一个可用的账号）。

常见错误：400 表示参数无效，例如 `cwd` 不是已存在的目录或账号不存在；404 表示会话、消息或提示不存在；409 表示状态冲突，例如会话正被其他进程打开、消息已被吸收或目标消息不是检查点；502 表示 `claude` 进程无法启动。

## 事件

`/events` 与 `/log` 中的每个事件形如 `{"seq": …, "time": …, "kind": …, "data": …}`：

| `kind` | `data` |
| --- | --- |
| `message` | 一条 SDK 消息，`_type` 为类名，例如 `AssistantMessage`、`UserMessage`、`SystemMessage`、`ResultMessage`、`StreamEvent`。CLI 回送的用户消息也以 `UserMessage` 出现 |
| `sent` | 守护进程发出的消息：`uuid`、`text` |
| `taken` | 消息被回合吸收：`uuid`、`checkpoint` |
| `cancelled` | 消息已撤回：`uuid` |
| `rewound` | 文件已回滚：`uuid` 与 CLI 返回的改动统计 |
| `status` | 会话状态改变：`status` |
| `prompt` | 新的提示，内容与 `/prompts` 中的相同，包括 `id`、`kind`（`permission` 或 `question`）、`tool_name`、`input`、`expires` |
| `prompt_resolved` | 提示了结：`id`、`reason`（`answered`、`timeout`、`interrupted`、`stopped`，或 CLI 自行撤销时的 `cancelled`） |
| `archived`、`unarchived` | 归档状态改变 |
| `account_switched` | 会话改用另一个账号：`from`、`to`、`reason`（`rate_limit`、`billing_error`，或启动时原账号不可用的 `unavailable`）、`until`（原账号恢复可用的时间） |
| `accounts_exhausted` | 回合因额度失败且没有可用的账号：`account`、`reason`、`until` |
| `error` | `claude` 进程出错：`error` |

活跃会话在内存中保留最近 5000 个事件，休眠或归档时清空。

## Webhook

设置 `--webhook` 后，守护进程在以下时刻向该地址 POST 一个 JSON 对象；设置了 `--webhook-token` 时附带 `Authorization: Bearer <token>`：

```json
{
  "event": "prompt",
  "time": 1795510348.5,
  "session": {"id": "…", "name": "review", "cwd": "/path/to/project", "status": "waiting"},
  "data": {}
}
```

| `event` | 时刻 | `data` |
| --- | --- | --- |
| `prompt` | 出现需要回答的提示 | 与 `prompt` 事件相同 |
| `prompt_resolved` | 提示了结 | `id`、`reason` |
| `result` | 回合结束 | 与 `GET /sessions/{id}` 的 `last_result` 相同 |
| `error` | `claude` 进程出错 | `error` |
| `account_switched`、`accounts_exhausted` | 见上一节的同名事件 | 与事件相同 |

通知按发生的顺序逐条发送，每条的超时为 10 秒；发送失败时在标准错误输出中记录，不重试。
