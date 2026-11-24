# claude-sessiond

claude-sessiond 是一个常驻的 HTTP 守护进程，通过 Web API 托管 Claude Code 会话：新建会话、以用户本人的身份发送消息、跟随实时输出、中断回合，以及回答权限确认与 AskUserQuestion。它基于 Claude Agent SDK（Python），每个会话对应一个常驻的 `claude` 进程，不模拟终端。

经由 claude-sessiond 发送的消息是普通的用户消息，与在 CLI 中输入的消息相同：回合进行中发出的消息由当前回合吸收并执行，斜杠命令照常生效。会话使用 Claude Code 自身的系统提示词，加载用户、项目与本地三层设置（CLAUDE.md、skills、hooks、MCP 与权限规则），会话记录照常写入 `~/.claude/projects`，随时可以用 `claude --resume <id>` 在 CLI 中继续。这一点与 channel 不同：channel 把外部消息注入一个已打开的会话，回合进行中到达的内容被视为不可信的外部数据，也无法新建会话或回答权限确认。

## 安装与启动

需要 [uv](https://docs.astral.sh/uv/)，并且 Claude Code 已经登录。

```sh
git clone https://github.com/ayaka14732/claude-sessiond
cd claude-sessiond
uv run claude-sessiond    # 监听 http://127.0.0.1:8790/，/docs 为交互式 API 文档
```

新建会话并等待第一条消息的结果，随后继续对话、跟随输出、查看待回答的提示：

```sh
B=http://127.0.0.1:8790
curl -s $B/sessions -H 'Content-Type: application/json' -d '{"cwd": "/path/to/project", "prompt": "总结最近 5 个提交", "wait": true}'
curl -s $B/sessions/$ID/messages -H 'Content-Type: application/json' -d '{"text": "再检查一下测试"}'
curl -sN $B/sessions/$ID/events
curl -s $B/prompts
```

缺省只监听本机。从其他机器访问之前，请先阅读[部署与账号](docs/guide/deployment.md#访问控制)中的访问控制说明。

## 文档

- [会话与消息](docs/guide/sessions.md)：会话的状态、休眠与归档、消息的状态、权限确认与提问、文件回滚。
- [部署与账号](docs/guide/deployment.md)：登录凭据、多个账号与额度用完时的自动切换、访问控制。
- [参考](docs/reference.md)：命令行参数、HTTP API、事件格式与 webhook。
- [实现说明](docs/design.md)：与 CLI 的交互方式、直接发送的控制请求、内存与持久化、验证记录。

文档站的构建方法：

```sh
uv run --no-project --with-requirements docs/requirements.txt python -m sphinx -n -W --keep-going -b html docs docs/_build
```
