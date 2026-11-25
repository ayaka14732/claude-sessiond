# 部署与账号

claude-sessiond 以启动它的用户的身份运行会话：会话中的命令拥有该用户的全部权限，使用该用户的 Claude Code 设置。

## 常驻运行

在 SSH 会话的前台或编辑器的远程终端中启动的守护进程，会随 SSH 连接或编辑器的远程服务一同结束，它托管的会话也随之停止。需要长期运行时，应以 systemd 用户服务运行守护进程，并为该用户开启 lingering，使用户的 systemd 实例在没有任何登录会话时也保持运行：

```sh
loginctl enable-linger
```

服务文件 `~/.config/systemd/user/claude-sessiond.service` 的示例如下，路径按实际的安装位置修改，命令行参数附加在 `ExecStart` 之后：

```ini
[Unit]
Description=claude-sessiond

[Service]
ExecStart=%h/claude-sessiond/.venv/bin/python %h/claude-sessiond/claude_sessiond.py
EnvironmentFile=-%h/.config/claude-sessiond/env
KillMode=mixed
Restart=on-failure

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now claude-sessiond
journalctl --user -u claude-sessiond -f
```

- `KillMode=mixed` 使 SIGTERM 只发给守护进程，由它逐个停止会话的 `claude` 进程，systemd 随后才结束残留的进程。缺省的 `control-group` 同时向服务中的所有进程发送 SIGTERM，`claude` 进程先于守护进程退出，会话以进程异常退出告终。守护进程异常退出时，systemd 以 SIGKILL 结束服务中残留的 `claude` 进程与后台命令，再按 `Restart=on-failure` 重新启动守护进程。
- 收到 SIGTERM 后，守护进程最多等待 5 秒，让进行中的 HTTP 请求结束，随后断开事件流与仍在等待的请求，再停止所有会话。日志中先记一行 `Cancel N running task(s), timeout graceful shutdown exceeded`，每个被断开的请求再留下一段 `Exception in ASGI application` 与以 `CancelledError` 结尾的 traceback，这些都是预期的。进行中的回合随之中止，会话以休眠状态保留，服务重新启动后照常恢复。
- `EnvironmentFile` 中可以给出 `SESSIOND_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN` 等变量，每行一个 `名称=值`，文件权限应为 600；路径前的 `-` 表示文件不存在时忽略。

会话中的命令继承服务的环境，而不是登录 shell 的环境：`~/.profile`、`~/.zprofile` 等登录时读取的文件中设置的变量都不存在，SSH 登录时转发的 `SSH_AUTH_SOCK` 也不存在。Claude Code 为 Bash 工具生成 shell 快照时会读取用户的 shell 配置文件（如 `~/.zshrc`、`~/.bashrc`），但只保留其中的 `PATH`、函数、别名与 shell 选项，配置文件中导出的其他变量不会出现在命令的环境中。会话需要的变量应在服务文件中用 `Environment=` 设置，或写入 `EnvironmentFile`；其中的 `CLAUDE_*` 变量还需要用 `--keep-env` 保留（见下一节）。

## 登录凭据

守护进程启动时清除继承来的 `CLAUDECODE` 与 `CLAUDE_*` 环境变量，只保留 `CLAUDE_CONFIG_DIR`、`CLAUDE_CODE_OAUTH_TOKEN` 与 `--keep-env` 指定的变量；`ANTHROPIC_*` 不受影响。因此，在没有浏览器的服务器上可以把 `claude setup-token` 生成的 token 作为 `CLAUDE_CODE_OAUTH_TOKEN` 提供给守护进程，也可以使用 `ANTHROPIC_API_KEY` 或已有的登录凭据。若在某个 Claude Code 会话内启动守护进程，被清除的变量会使新会话被当作那个会话的子会话，不保存会话记录。

也可以经 API 在浏览器中登录。`POST /accounts/{name}/login` 返回授权地址，`GET /accounts/{name}/login/open` 把浏览器重定向到这个地址，以免复制或点击过长的链接时被截断。授权之后，浏览器跳回 `http://localhost:<端口>/callback`，由守护进程所在机器上的 `claude` 进程接收，因此授权地址必须在这台机器的浏览器中打开；浏览器在另一台机器上时，先转发授权地址中的端口，例如 `ssh -L <端口>:localhost:<端口>`。浏览器跳回即完成登录，结果通过 `GET /accounts/{name}/login` 查询。一次登录在 15 分钟内有效，失败或超时之后需要重新开始；该端口收到不是由授权页面重定向而来的请求时，登录同样失败。

每个账号都得到与 `claude auth login` 相同的完整登录，CLI 用其中的 refresh token 自行续期：

- `default`：凭据写入守护进程所用的配置目录，替换其中已有的登录，并清除 `default` 的不可用标记；`default` 下已经在运行的会话无需重启，下一个请求即使用新的凭据。守护进程的环境中有 `ANTHROPIC_API_KEY` 或 `CLAUDE_CODE_OAUTH_TOKEN` 时，`default` 的会话使用这些变量而不是登录，登录因此返回 409。
- 其他账号：凭据写入状态文件旁的 `logins/<账号名>/`（权限 700），会话记录、设置、skills 等其余内容仍与 `default` 共用同一个配置目录，因此会话切换账号之后照常恢复。不存在的账号在登录成功后登记为 `login` 账号；已存在的账号改用这次登录，原有的 `oauth_token` 或 `api_key` 被替换，不可用标记被清除。再次登录时凭据写回同一目录。删除账号时一并删除该目录。需要附加 `env` 时，先用 `POST /accounts` 登记不带凭据的账号，再登录。

配置目录的 `.claude.json` 中记录着登录的身份（`oauthAccount`），它只有一份，始终描述 `default`：其他账号的登录不改动它，其他账号的会话也照常运行，`claude auth status` 显示的身份只对 `default` 成立。

`CLAUDE_CONFIG_DIR` 只应在确实使用独立的配置目录时设置。设置之后，CLI 在该目录中查找 `.claude.json`，不再读取 `~/.claude.json`；即使其值为 `~/.claude`，也会被当作一份全新的配置。

## 多个账号

登记的账号必须都属于同一个用户本人，不得与他人共享。自动切换的作用与在官方客户端中切换到另一个账号、继续原来的对话相同，只是在额度用完时自动进行。

守护进程自身的凭据构成账号 `default`。`POST /accounts` 可以登记更多账号，每个账号给出 `oauth_token`（`claude setup-token` 生成）或 `api_key` 之一，并可附加 `env`（例如 `ANTHROPIC_BASE_URL`）；也可以经 API 在浏览器中登录（见上一节）。账号按登记的顺序排列，`default` 始终在最前。账号保存在状态文件旁的 `accounts.json` 中，文件权限为 600，API 不返回其中的凭据。

每个会话在一个账号下运行，即会话选项中的 `account`；新建时未指定的会话使用第一个可用的账号。凭据只在进程启动时读取，用 `PATCH` 修改的账号从下次启动起生效。

某个回合因订阅额度用完（`rate_limit`）或 API 余额不足（`billing_error`）而失败时：

1. 该账号被标记为不可用。订阅额度不可用到 CLI 报告的重置时间为止，没有重置时间的速率限制为 5 分钟，余额不足则持续到 `POST /accounts/{name}/reset`。
2. 若还有可用的账号，会话切换到其中顺序最前的一个：以新的凭据从会话记录重启进程，并发送一条以 `Continue from where you left off.` 开头的消息继续未完成的工作；消息中说明上一个回合因额度而中止、会话已换到另一个账号且新账号没有受限、用户并未发送新消息，并要求恢复因额度或重启而停下的子智能体（见[子智能体](sessions.md#子智能体)）。以 `wait` 等待原回合的请求得到继续之后那个回合的结果。切换后提示缓存失效，继续的第一个回合按完整上下文计费。
3. 若没有可用的账号，原回合的错误结果照常返回，同时发出 `accounts_exhausted` 事件与通知。

休眠的会话启动时，若它的账号不可用，同样改用第一个可用的账号。切换时 CLI 中尚未被回合吸收的消息记为 `dropped`，需要重新发送。

## 访问控制

守护进程缺省只监听 `127.0.0.1`，并且只接受 Host 头为 `127.0.0.1`、`localhost` 或 `::1` 的请求，以防 DNS rebinding；经反向代理或以其他地址访问时，需要用 `--allow-host` 加入相应的主机名。带 `Origin` 头的非 GET 请求，`Origin` 的主机名同样必须属于这些主机：浏览器可以不经预检、从任意网站发出不带请求体的 POST（例如 `/interrupt`），据此拒绝。对本机以外监听时必须设置 `--token`。

通过认证的客户端可以以该用户的身份执行任何操作：回答权限确认即可批准任意命令，`allow_bypass` 只是同一权限的便捷形式，不构成额外的防线。

token 应放在 `Authorization` 请求头中。查询参数 `?token=` 仅供无法设置请求头的浏览器 EventSource 使用，它会出现在反向代理的访问日志中；守护进程本身不记录访问日志。
