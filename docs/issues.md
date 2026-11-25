# 未解决的问题

本页记录有待 Claude Code CLI 或 SDK 上游改动才能解决的问题。升级 SDK 后按“确认方法”检查，解决后更新代码与文档并删去该条。

## SDK 会话不能使用智能体团队

开启 `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS` 后，带 `name` 的 Agent 调用派生的仍是 `local_agent`，不是 teammate（替代用法见[子智能体](guide/sessions.md#子智能体)）。CLI 2.1.331 只在交互式启动时调用 `initializeSessionTeam` 建立会话的团队，而 SDK 启动的 CLI 不是交互式会话；派生 teammate 又依赖这个团队，否则报错 `session team not initialized`。`--agent-teams` 只是功能开关，会话中途也没有创建团队的工具。CLI 自带的后台会话（`claude --bg`）是交互式会话，可以使用团队，但不经守护进程托管。

确认方法：在打包的 CLI 中查看 `initializeSessionTeam` 的调用条件，或实测带 `name` 的 Agent 产生的 `task_type` 是否为 `in_process_teammate`。

解决之后：恢复使用指南中的团队说明（`teammateMode` 设为 `in-process`；以环境变量开启时需要 `--keep-env`）；确认 teammate 出现在 `background_tasks_changed` 中，并处理空闲的 teammate（CLI 中仍为 `running`，只另有 `isIdle` 标记）使会话永不休眠的问题。
