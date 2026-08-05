# Tool Gateway 局部规则

> 作用范围：`src/autopilot/gateway/`；本文件只能收紧根 `AGENTS.md` 规则。

1. 所有 Agent Tool 调用按固定顺序执行：可见性、Schema、工作流状态、预算、OPA、审批、幂等、资源锁、能力服务、审计。
2. 不可见、未注册、重名、阶段不允许或命名不符合公共动作格式的 Tool 一律拒绝。
3. Tool 可见性是 Phase、Role、Hardware、Provider、Feature Flag 和 Policy 的交集，并在每次 LLM 调用中记录 Tool Set/Schema Version。
4. OPA 超时、不可用、返回格式非法或缺少 Decision ID 时必须失败关闭；MVP 中 L3 一律拒绝。
5. L2 执行前必须验证审批未过期、审批主体合法、Plan Hash 匹配且完整执行规格仍在预算内。
6. 幂等键与规范化请求 Hash 共同持久化；同 Key 的不同请求必须返回冲突。
7. Gateway 不得直接调用 Runner；执行请求先持久化 Job，由 Worker 在执行前重新实施强制检查。
