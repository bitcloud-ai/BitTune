# Bittune 路线图

> 状态：方向基线。阶段以可验证的用户结果定义，而不是以 Tool 数量或代码量定义。

## 阶段 0：冻结单机基础与方向

**状态：Runtime 完成，真实闭环证据待冻结。** 本阶段的 Runtime、Tool 和 Linux PTY/fake-server 回归已具备；真实单机部署、探测、压测和证据化容量结论必须先以环境指纹、Run ID 和 Artifact 形式冻结在 `docs/releases/`。

本阶段不再新增单机功能。下一阶段的工作以 [`product-brief.md`](product-brief.md) 和 [`strategy.md`](strategy.md) 为准。

## 阶段 1：已有 Kubernetes 的受限纳管

**用户结果：** 用户在完成初始授权后，仅通过 Bittune 完成指定模型服务的部署、验证、压测和可复核容量结论。

**边界：** 一个已有 Kubernetes GPU 集群；一个专属 namespace；最小 RBAC；只管理 Bittune 创建并记录所有权的资源。

**进入条件：** [ADR 0009](../docs/decisions/0009-cluster-target-ownership-and-tool-catalog.md) 已接受；集群扩展架构明确状态模型、证据规则和 Tool Capability Catalog。

**退出条件：** `product-brief.md` 的 P1 成功标准在至少三个独立真实部署中满足，并有 release record。

## 阶段 2：多 Target 与执行通道扩展

**用户结果：** 相同推理服务目标可在多个已授权 Target 上以一致的所有权、证据和回滚语义执行。

**范围候选：** 多 Kubernetes Target、受限 SSH 单机 Target、更多 Runtime Adapter。具体顺序由 P1 用户需求和操作证据决定。

**不包含：** 自建调度器、自动接管外部资源或裸机集群安装。

## 阶段 3：高影响环境生命周期

**用户结果：** Bittune 能在明确授权和可回滚策略下，对裸机环境执行 SSH bootstrap、GPUStack 或既有控制面安装，并将创建的环境纳入受管状态。

**进入条件：** 已验证 Target 所有权、凭据隔离、破坏性操作确认和回滚证据；无法由 P1 成功直接推导。

## 阶段 4：持续观测与受控自愈

**用户结果：** 对 Bittune 受管服务持续采集指标、识别漂移，并按明确策略告警或执行受控恢复。

**进入条件：** 时序指标模型、任务调度、人工接管、审计和回滚策略均已验证。

## 所有阶段的长期约束

- Bittune 不替代 Kubernetes、Docker、SSH、GPUStack 或既有调度器。
- 用户不需要日常操作控制面，但初始授权、权限边界与高影响确认必须显式。
- 外部资源默认只读；资源所有权来自 Bittune 的创建记录，而不是名称匹配或环境发现。
- Tool 是可独立审计的领域操作；Agent 决定顺序，代码不隐藏固定部署流水线。
- Tool Catalog 可以按需暴露能力，但只能暴露 Bittune 审核的静态可信 Tool。
- 每阶段先在真实环境验证用户结果，再扩大执行权限或环境范围。
