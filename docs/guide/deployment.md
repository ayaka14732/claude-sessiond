# 部署与账号

claude-sessiond 以启动它的用户的身份运行会话：会话中的命令拥有该用户的全部权限，使用该用户的 Claude Code 设置。

## 常驻运行

在 SSH 会话或编辑器的远程终端中启动的守护进程，会随连接一同结束，它托管的会话也随之停止。需要长期运行时，应以 systemd 用户服务运行守护进程，并开启 lingering，使服务在用户退出登录后继续运行：

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

- `KillMode=mixed` 使守护进程先逐个停止会话，systemd 再结束残留的进程；缺省的 `control-group` 会使 `claude` 进程先于守护进程退出。
- `EnvironmentFile` 中可以给出 `SESSIOND_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN` 等变量，每行一个 `名称=值`，文件权限应为 600。
- 停止服务时，进行中的回合中止，会话以休眠状态保留，重新启动后照常恢复。日志中的 `timeout graceful shutdown exceeded` 与 `CancelledError` traceback 来自被断开的事件流，属于正常现象。

会话中的命令继承服务的环境，而不是登录 shell 的环境：登录时设置的变量（包括 SSH 转发的 `SSH_AUTH_SOCK`）都不存在，shell 配置文件（如 `~/.zshrc`）中导出的变量也只有 `PATH` 生效。会话需要的变量应在服务文件中用 `Environment=` 设置或写入 `EnvironmentFile`，其中的 `CLAUDE_*` 变量还需要用 `--keep-env` 保留（见下一节）。

## 登录凭据

守护进程启动时清除继承来的 `CLAUDECODE` 与 `CLAUDE_*` 环境变量，只保留 `CLAUDE_CONFIG_DIR`、`CLAUDE_CODE_OAUTH_TOKEN` 与 `--keep-env` 指定的变量；`ANTHROPIC_*` 不受影响。在没有浏览器的服务器上，可以把 `claude setup-token` 生成的 token 作为 `CLAUDE_CODE_OAUTH_TOKEN` 提供给守护进程，也可以使用 `ANTHROPIC_API_KEY` 或已有的登录凭据。

也可以经 API 在浏览器中登录：`POST /accounts/{name}/login` 开始登录，在守护进程所在机器的浏览器中打开 `GET /accounts/{name}/login/open`，授权后浏览器跳回 `http://localhost:<端口>/callback` 即完成登录，结果通过 `GET /accounts/{name}/login` 查询。浏览器在另一台机器上时，先转发授权地址中的端口，例如 `ssh -L <端口>:localhost:<端口>`。一次登录在 15 分钟内有效。

- `default` 的登录替换守护进程所用配置目录中已有的登录，已在运行的会话无需重启。守护进程的环境中有 `ANTHROPIC_API_KEY` 或 `CLAUDE_CODE_OAUTH_TOKEN` 时，`default` 使用这些变量，登录返回 409。
- 其他账号的凭据保存在状态文件旁的 `logins/<账号名>/` 中，其余内容与 `default` 共用同一个配置目录，因此切换账号后会话照常恢复。不存在的账号在登录成功后自动登记。需要附加 `env` 时，先用 `POST /accounts` 登记不带凭据的账号，再登录。
- `claude auth status` 显示的身份只对 `default` 成立。

`CLAUDE_CONFIG_DIR` 只应在确实使用独立的配置目录时设置：设置之后，CLI 不再读取 `~/.claude.json`，即使其值为 `~/.claude`，也会被当作一份全新的配置。

## 多个账号

登记的账号必须都属于同一个用户本人，不得与他人共享。自动切换的作用与在官方客户端中切换到另一个账号、继续原来的对话相同，只是在额度用完时自动进行。

守护进程自身的凭据构成账号 `default`。`POST /accounts` 可以登记更多账号，每个账号给出 `oauth_token`（`claude setup-token` 生成）或 `api_key` 之一，并可附加 `env`（例如 `ANTHROPIC_BASE_URL`）；也可以经 API 在浏览器中登录。账号按登记的顺序排列，`default` 始终在最前，保存在状态文件旁的 `accounts.json` 中，API 不返回其中的凭据。每个会话在一个账号下运行（会话选项中的 `account`），修改的账号从进程下次启动起生效。

某个回合因订阅额度用完或 API 余额不足而失败时：

1. 该账号被标记为不可用：订阅额度到 CLI 报告的重置时间为止，没有重置时间的速率限制为 5 分钟，余额不足则持续到 `POST /accounts/{name}/reset`。
2. 若还有可用的账号，会话切换到其中顺序最前的一个，从会话记录重启进程，并发送一条以 `Continue from where you left off.` 开头的消息继续未完成的工作。以 `wait` 等待原回合的请求得到继续之后那个回合的结果。切换后提示缓存失效，继续的第一个回合按完整上下文计费。
3. 若没有可用的账号，原回合的错误结果照常返回，同时发出 `accounts_exhausted` 事件与通知。

休眠的会话启动时，若它的账号不可用，同样改用第一个可用的账号。切换时尚未被回合吸收的消息记为 `dropped`，需要重新发送。

## 访问控制

守护进程缺省只监听 `127.0.0.1`，并且只接受 Host 头为 `127.0.0.1`、`localhost` 或 `::1` 的请求，以防 DNS rebinding；经反向代理或以其他地址访问时，需要用 `--allow-host` 加入相应的主机名。带 `Origin` 头的非 GET 请求，`Origin` 的主机名同样必须属于这些主机。对本机以外监听时必须设置 `--token`。

通过认证的客户端可以以该用户的身份执行任何操作：回答权限确认即可批准任意命令，`allow_bypass` 只是同一权限的便捷形式，不构成额外的防线。

token 应放在 `Authorization` 请求头中。查询参数 `?token=` 仅供无法设置请求头的浏览器 EventSource 使用，它会出现在反向代理的访问日志中。
