# LangGraph 局部规则

> 作用范围：`src/autopilot/graph/`；本文件只能收紧根 `AGENTS.md` 规则。

1. MVP 只有一个主 Graph，不增加自治多 Agent 团队。
2. State 只保存小型结构化事实、稳定 ID、Artifact Ref、阶段、审批和错误分类；禁止 Secret、原始日志、大型报告和模型文件。
3. 会改变宿主机状态、消耗高成本资源或改变最终指标的节点必须是确定性节点。
4. 人工审批使用 Interrupt，并绑定不可变 Plan Hash；任何执行规格变化都必须新建 Plan 和重新审批。
5. 从 Checkpoint 恢复时先对账 PostgreSQL Job、容器、GPU 锁、MLflow Run 和 Artifact，再选择继续、补偿或失败。
6. Graph 测试使用 Fake Adapter 和持久化 Checkpoint，不占用 GPU。

## M8 Agent runtime

- 用户可见的持续会话必须使用官方 `langchain.agents.create_agent`；不得在本目录新增手写 ReAct、ToolNode 编排或第二套 Agent loop。
- `AgentRuntime` 使用 `messages`、`thread_id` 和 `PostgresSaver`/`InMemorySaver`，并通过 `AgentMiddleware` 在每次模型调用前解析 Tool Gateway 可见性。
- Agent Tool 适配器只能提交 `ToolCallRequest` 给 Tool Gateway；不得直接导入 Capability、Provider、Docker 或 Host Runner 实现。
- L2 工具使用官方 `HumanInTheLoopMiddleware` 产生 Interrupt；恢复输入必须是结构化 `approve` 或 `reject`，不能通过自然语言绕过审批。
- Agent context 是单次调用的临时可信上下文，不进入 Checkpoint State；消息进入 State 前必须进行 Secret 脱敏。
