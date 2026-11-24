# claude-sessiond 仓库规范

## 文风

文档使用正式的书面中文，面向一般用户，不用口语表达，不写只对某一位用户或某台机器成立的内容（个人路径、内部项目、个人设置）。正文尽量不用第二人称。

只写读者无法从接口本身、代码或通行的技术常识中直接得到的内容。不为显而易见的操作开页面或举例，新增内容优先并入已有页面。

Markdown 段内不折行。

## 文档站

- `README.md` 是概述，文档站首页原样包含它；详细内容写在 `docs/` 中，README 只链接过去。
- `docs/` 是中文站，用 Sphinx 与 MyST 构建。`docs/en/` 预留给以后的英文站：届时与 `docs/` 逐文件对应，并有自己的 `conf.py`，导入 `docs/conf.py` 后只修改 `language` 与 `exclude_patterns`；`docs/conf.py` 已把 `en` 排除在中文站之外。
- 新增页面时在 `docs/index.md` 的相应 toctree 中登记。
- 文档应以下面的命令构建通过，不允许任何警告：

```sh
uv run --no-project --with-requirements docs/requirements.txt python -m sphinx -n -W --keep-going -b html docs docs/_build
```

## 修改与验证

- 修改行为时，在同一次修改中更新相应的文档页面。命令行参数、端点、事件与 webhook 的变化同时更新 `docs/reference.md`。
- 新功能用真实会话验证，验证结论写入 `docs/design.md` 的“验证”一节；测试用的会话与会话记录在验证后删除。
- 依赖 SDK 私有接口或 CLI 未公开行为的部分记录在 `docs/design.md` 中，升级 SDK 后按其中的说明重新确认。
- 提交说明使用英文，标题一行，不超过 72 个字符，只说明改动了什么。
