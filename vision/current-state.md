# 当前项目现状

> Status: v0.3 交付后的现状基线。本文不再描述产品价值文档缺失；产品定位见 [`product-brief.md`](product-brief.md)，首发策略见 [`strategy.md`](strategy.md)。

## 一句话

Bittune 目前是一个基于受控 Pi 0.84.1 fork 的**单机推理工程智能体**。工程架构已经形成，但真实单机推理闭环的外部证据尚未冻结在本仓库；当前主要问题是：**单机实现边界与下一阶段集群控制面方向不一致**，需要按 P1 设计扩展，而不是继续堆叠单机 Tool。

## 已经做了什么

- **Runtime**：受控 Pi 0.84.1 源码 fork，复用上游 TUI、Agent Loop、Session、Compaction、重试、取消、Tool 生命周期；产品入口是 `bittune`，不依赖系统 `pi`。
- **静态能力层**：约 44 个 Tool，覆盖环境探测、Discovery、部署预设、vLLM 资产与服务、性能压测、容量基线、实验（Spec/Trial/Comparison）、Run Record/Artifact 证据。
- **架构三原则**（已落代码，是这套工程里最扎实的部分）：
  1. `Tool → 防腐层 → Adapter` 单向依赖，Provider 细节不泄漏到 Tool 契约；
  2. `发现(Discovery)` 与 `受管状态(Managed State)` 分离，外部对象默认只读；
  3. `estimated / measured / derived / stored` 证据等级化，结论必须可追溯。
- **证据链**：File Store 持久化 + Run Record/Artifact，每次 Tool Call 写 Run Record，跨 Session 可通过稳定 ID 重新发现。

## 当前边界（这些正是与「集群控制面」方向冲突的地方）

- 单机、单用户、受信任主机；
- 外部服务默认只读、不接管；
- 无常驻守护进程，agent 只在对话会话里运行；
- 无多租户；
- 暂不做 GPUStack 生命周期接管；
- 无 GUI。

## 卡在哪里

1. **单机验证不是集群验证**：仓库已覆盖 Runtime、Tool Contract 和 Linux PTY/fake-server 回归，但真实 GPU + vLLM + EvalScope 的外部闭环证据尚未冻结在 release record 中。即使补齐该证据，它也不证明 Kubernetes、SSH、集群权限或持续观测能力已具备。
2. **方向错位风险**：若继续在单机 Tool 上堆功能，会延后集群目标所需的 Target、所有权、Adapter 和证据模型重构。

## 结论

当前最该做的不是继续加单机 Tool，而是按 [`product-brief.md`](product-brief.md)、[`strategy.md`](strategy.md) 和已完成的集群架构设计进入 Kubernetes MVP 实施计划，先冻结真实单机外部证据，再实现 Target、所有权、受管实例和集群证据模型。
