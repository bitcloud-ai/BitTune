# OPA Policy 局部规则

> 作用范围：`policies/`；本文件只能收紧根 `AGENTS.md` 规则。

1. Rego 默认拒绝，允许规则必须列出明确 Action、Role、Risk、Phase、Budget 和审批条件。
2. L3、任意 Shell、驱动/内核修改和模型缓存删除在 MVP 中无条件拒绝。
3. L2 只在审批未过期、审批 Plan Hash 与请求完全一致且预算未超限时允许。
4. Policy 输入只接收结构化摘要和 SecretRef，不接收 Secret、Authorization Header 或原始 Provider 日志。
5. Decision Log 配置必须屏蔽 SecretRef 定位信息、敏感 Header 和凭据值。
6. 每次 Policy 变更都必须增加 Golden Test，至少覆盖 L0 允许、L2 无审批拒绝、L2 Hash 匹配允许、超预算拒绝和 L3 拒绝。
