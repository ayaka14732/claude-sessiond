# 部署与账号

claude-sessiond 以启动它的用户的身份运行会话：会话中的命令拥有该用户的全部权限，使用该用户的 Claude Code 设置。

## 登录凭据

守护进程启动时清除继承来的 `CLAUDECODE` 与 `CLAUDE_*` 环境变量，只保留 `CLAUDE_CONFIG_DIR`、`CLAUDE_CODE_OAUTH_TOKEN` 与 `--keep-env` 指定的变量；`ANTHROPIC_*` 不受影响。因此，在没有浏览器的服务器上可以把 `claude setup-token` 生成的 token 作为 `CLAUDE_CODE_OAUTH_TOKEN` 提供给守护进程，也可以使用 `ANTHROPIC_API_KEY` 或已有的登录凭据。若在某个 Claude Code 会话内启动守护进程，被清除的变量会使新会话被当作那个会话的子会话，不保存会话记录。

`CLAUDE_CONFIG_DIR` 只应在确实使用独立的配置目录时设置。设置之后，CLI 在该目录中查找 `.claude.json`，不再读取 `~/.claude.json`；即使其值为 `~/.claude`，也会被当作一份全新的配置。

## 多个账号

登记的账号必须都属于同一个用户本人，不得与他人共享。自动切换的作用与在官方客户端中切换到另一个账号、继续原来的对话相同，只是在额度用完时自动进行。

守护进程自身的凭据构成账号 `default`。`POST /accounts` 可以登记更多账号，每个账号给出 `oauth_token`（`claude setup-token` 生成）或 `api_key` 之一，并可附加 `env`（例如 `ANTHROPIC_BASE_URL`）。账号按登记的顺序排列，`default` 始终在最前。账号保存在状态文件旁的 `accounts.json` 中，文件权限为 600，API 不返回其中的凭据。

每个会话在一个账号下运行，即会话选项中的 `account`；新建时未指定的会话使用第一个可用的账号。凭据只在进程启动时读取，用 `PATCH` 修改的账号从下次启动起生效。

某个回合因订阅额度用完（`rate_limit`）或 API 余额不足（`billing_error`）而失败时：

1. 该账号被标记为不可用。订阅额度不可用到 CLI 报告的重置时间为止，没有重置时间的速率限制为 5 分钟，余额不足则持续到 `POST /accounts/{name}/reset`。
2. 若还有可用的账号，会话切换到其中顺序最前的一个：以新的凭据从会话记录重启进程，并发送一条 `Continue from where you left off.` 继续未完成的工作。以 `wait` 等待原回合的请求得到继续之后那个回合的结果。切换后提示缓存失效，继续的第一个回合按完整上下文计费。
3. 若没有可用的账号，原回合的错误结果照常返回，同时发出 `accounts_exhausted` 事件与通知。

休眠的会话启动时，若它的账号不可用，同样改用第一个可用的账号。切换时 CLI 中尚未被回合吸收的消息记为 `dropped`，需要重新发送。

## 访问控制

守护进程缺省只监听 `127.0.0.1`，并且只接受 Host 头为 `127.0.0.1`、`localhost` 或 `::1` 的请求，以防 DNS rebinding；经反向代理或以其他地址访问时，需要用 `--allow-host` 加入相应的主机名。对本机以外监听时必须设置 `--token`。

通过认证的客户端可以以该用户的身份执行任何操作：回答权限确认即可批准任意命令，`allow_bypass` 只是同一权限的便捷形式，不构成额外的防线。

token 应放在 `Authorization` 请求头中。查询参数 `?token=` 仅供无法设置请求头的浏览器 EventSource 使用，它会出现在反向代理的访问日志中；守护进程本身不记录访问日志。
