# ADR-017：Agent TUI、流式交互与未来领域 Agent 边界

- 状态：Accepted
- 日期：2026-08-06
- 范围：MVP Agent 运行时、终端交互、事件流和后续领域扩展

## 背景

MVP 已确定使用 Python、FastAPI、LangGraph 和 LangChain。此前 `autopilot chat` 使用
`prompt_toolkit + Rich` 拼装连续输入和输出，但该组合只覆盖交互式 Prompt，不提供完整的应用布局、
后台 Worker、Command Palette、响应式状态、Headless UI 测试和终端尺寸适配。继续在其上增加消息列表、
流式 Token、Tool 状态、审批面板和会话状态会形成自研 TUI 框架。

公开实现的共同模式是复用成熟终端框架：Codex 的 Rust TUI 使用 Ratatui/Crossterm，Gemini CLI 的
TypeScript TUI 使用 React/Ink，Python 项目 Toad 使用 Textual。Claude Code 官方资料只公开产品界面和
安装方式，不公开可核实的 TUI 框架，因此不将其内部实现猜测作为选型依据。

LangChain 官方将 `create_agent` 定义为标准 Agent harness，并提供 Checkpointer、Middleware、
Human-in-the-loop 和 `stream(..., stream_mode=["messages", "updates"], version="v2")`。官方多 Agent
文档将 supervisor 调用包装为 Tool 的 subagent 作为独立领域扩展方式，而不是要求所有产品从多 Agent
开始。

## 决策

1. `langchain.agents.create_agent` 是唯一用户会话 Agent loop；LangGraph 继续提供持久化、Interrupt、
   恢复和流式事件运行时。不得新增手写 ReAct loop、第二套 Workflow Engine 或 Deep Agents 的
   Shell/Filesystem 能力。
2. MVP 使用一个 `bittune-autopilot` Agent。当前不实现 supervisor、自治 Agent 团队或并行子 Agent。
3. Agent Tool 只包装既有领域 Tool，并且只能调用 Tool Gateway。Tool Gateway、OPA、独立管理员审批、
   PostgreSQL Job、Capability Service、Adapter 和 Host Runner 的强制路径保持不变。
4. `thread_id = experiment_id` 是连续会话标识。可信的用户、角色、硬件能力、Provider、Feature Flag
   和预算只通过 `context_schema/runtime.context` 传入，不写入消息或 Checkpoint State。
5. Agent 事件使用 LangChain/LangGraph v2 stream，至少传输 assistant token、完成消息、Tool Call、
   Tool Result、Interrupt、错误和完成事件。SSE 是 API 到 TUI 的唯一实时传输；数据库事件和 Checkpoint
   仍是事实源。
6. L2 的 `HumanInTheLoopMiddleware` 只负责暂停当前 Agent Tool Call。真正执行前仍必须通过
   ADR-016 的 Approval v2：请求人与审批人不同，且只有 human admin 可以审批。TUI 的 `/approve`
   不能把 operator 自身变成审批人。
7. Click 保留为安装后的命令入口和非交互控制面命令。`autopilot chat` 启动 Textual 应用；不引入
   Node.js、React、Ink、Rust 或第二套包管理器。
8. Textual 负责消息滚动、Markdown、输入、Command Palette、状态栏、异步 HTTP/SSE Worker、取消和
   Headless Pilot 测试。TUI 只调用 FastAPI，不导入 Graph、Gateway、Capability、Provider 或 Runner。
9. Slash Command 固定为 `/approve`、`/reject`、`/status`、`/cancel`、`/new` 和 `/quit`。同一动作同时
   注册到 Textual Command Palette，不自行实现模糊搜索或补全引擎。
10. 后续出现三个以上边界清晰、上下文独立且工具集合稳定的领域时，新增 ADR 后引入中央 supervisor。
    每个领域 Agent 被包装成一个受控 Tool，内部仍只能调用该领域的 Gateway Tool；长任务继续使用
    `start -> job_id -> status/result/cancel`，不得把 Provider 或 Host Runner 直接暴露给子 Agent。

## 唯一数据流

```text
Textual TUI
  -> FastAPI session run SSE
  -> create_agent stream v2
  -> dynamic Gateway tools
  -> Tool Gateway
  -> PostgreSQL Plan / Approval / Job / Audit
  -> Worker
  -> Capability Service / Provider Adapter
  -> Host Runner（仅高权限动作）
```

## 交付界面

`autopilot chat` 的首屏直接是可操作会话，不是说明页。界面固定包含：

- 可滚动的用户、Agent、Tool 和审批事件时间线；
- 底部多轮输入；
- 会话 ID、Experiment Phase、连接和运行状态；
- Textual Command Palette；
- Markdown 结果和结构化 Tool 详情；
- 流式 Token、请求取消、错误恢复和重新连接。

## 结果

- 删除 `prompt_toolkit` 直接依赖，固定 Textual 版本并由 `uv` 管理。
- 现有 Click 的 `create/status/events/resume/cancel` 继续作为自动化入口。
- M8/M9 的验收增加真实 token/tool/interrupt SSE 和 Textual Pilot 测试。
- 本 ADR 不改变既有 Plan、Approval、Job、Artifact、Evidence 或 Provider Schema。

## 核查来源

- LangChain Agents: https://docs.langchain.com/oss/python/langchain/agents
- LangChain Streaming: https://docs.langchain.com/oss/python/langchain/streaming
- LangChain Subagents: https://docs.langchain.com/oss/python/langchain/multi-agent/subagents
- Textual: https://textual.textualize.io/
- Textual Workers: https://textual.textualize.io/guide/workers/
- Textual Command Palette: https://textual.textualize.io/guide/command_palette/
- Textual Testing: https://textual.textualize.io/guide/testing/
- Codex TUI: https://github.com/openai/codex/tree/main/codex-rs/tui
- Gemini CLI: https://github.com/google-gemini/gemini-cli
- Toad: https://github.com/batrachianai/toad
- Claude Code Overview: https://code.claude.com/docs/en/overview
