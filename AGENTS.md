# claude-sessiond 仓库规范

## 文风

文档使用正式的书面中文，面向一般用户，不用口语表达，不写只对某一位用户或某台机器成立的内容（个人路径、内部项目、个人设置）。正文尽量不用第二人称。

只写读者无法从接口本身、代码或通行的技术常识中直接得到的内容。不为显而易见的操作开页面或举例，新增内容优先并入已有页面。

`docs/guide/` 是使用指南，面向用户，应保持精简，只写用户需要知道的：怎样使用、会看到什么结果、需要注意什么。实现方式、内部的判断规则、边界情况的处理与竞态的细节等读代码就能看出的内容不写入使用指南；维护者需要知道的写在 `docs/design.md`。

Markdown 段内不折行。

## 文档站

- `README.md` 是概述，文档站首页原样包含它；详细内容写在 `docs/` 中，README 只链接过去。
- `docs/` 是中文站，用 Sphinx 与 MyST 构建。`docs/en/` 预留给以后的英文站：届时与 `docs/` 逐文件对应，并有自己的 `conf.py`，导入 `docs/conf.py` 后只修改 `language` 与 `exclude_patterns`；`docs/conf.py` 已把 `en` 排除在中文站之外。
- 新增页面时在 `docs/index.md` 的相应 toctree 中登记。
- 安装说明只用 pip 与 `requirements.txt`，不写 uv 等其他工具。
- 推送到 `main` 后，`.github/workflows/docs.yml` 以同样的命令构建文档站并部署到 GitHub Pages。文档应以下面的命令构建通过，不允许任何警告：

```sh
pip install -r docs/requirements.txt
python -m sphinx -n -W --keep-going -b html docs docs/_build
```

## 修改与验证

- 修改行为时，在同一次修改中更新相应的文档页面。命令行参数、端点、事件与 webhook 的变化同时更新 `docs/reference.md`。
- 避免冗余与过度防御性编程：只在确有必要时编写校验，不为不发生的错误增加代码，不重复实现 SDK、CLI 或 FastAPI 已经提供的行为。
- 新功能用真实会话验证，测试用的会话与会话记录在验证后删除；文档中不写验证的过程与结论。
- 依赖 SDK 私有接口或 CLI 未公开行为的部分记录在 `docs/design.md` 中，升级 SDK 后按其中的说明重新确认。
- 有待上游改动才能解决的问题登记在 `docs/issues.md` 中，写明现象、原因、确认方法与解决之后要做的修改；升级 SDK 后逐条确认，解决后删去该条。
- 提交说明使用英文，标题一行，不超过 72 个字符，只说明改动了什么。

## 验证时容易忽略的事实

- 验证不能碰正在使用的守护进程与登录：账号与登录凭据（`accounts.json`、`logins/`）在 `--state` 旁边；`default` 的登录写入配置目录，会替换本机真实的登录。
- 不设 `CLAUDE_CONFIG_DIR` 时，测试会话的记录写入真实的 `~/.claude/projects`，CLI 还在 `$TMPDIR/claude-<uid>/` 下留下后台任务输出；`purge` 只删除会话记录（含子智能体）。
- 用户设置中的 `defaultMode` 可能是 `bypassPermissions`，此时不会出现权限确认。
- 会话停止、休眠或切换账号时事件缓冲被清空；`/history` 不含子智能体的记录。
- `POST /sessions` 响应中的 `config` 是创建时的快照，`wait` 期间切换了账号时 `config.account` 仍是原来的账号。
- 账号 `env` 中的 `ANTHROPIC_BASE_URL` 可以把该账号的请求引向模拟服务器，不带凭据的账号也会把本机登录的 OAuth token 发过去。`api_key` 账号收到普通 429 时，CLI 重试约 3 分钟才放弃；带订阅额度响应头（`anthropic-ratelimit-unified-status: rejected` 等）时不重试。
- 真实登录要人在守护进程所在机器的浏览器中授权。授权地址过长，点击时可能被截断（授权页报 `Missing state parameter`），应给出 `/accounts/{name}/login/open`。登录进行中访问回调端口会使登录失败。
- `claude auth logout` 撤销凭据的同时删除配置目录中的 `oauthAccount`；撤销 `default` 以外的账号时不能用共用的配置目录，并且要在删除账号（同时删除其凭据目录）之前进行。
- `claude auth status` 显示的身份不代表实际使用的凭据（见 `docs/design.md` 的“登录”）。输出与日志中不打印邮箱、token 等个人信息。
- CLI 未公开的行为可以在 SDK 自带的 `claude_agent_sdk/_bundled/claude`（打包的 JS）中查到。
