# Bittune 首发战略

> 状态：方向基线。本文决定集群架构和实施计划的范围，不是当前实现说明。

## 1. 阶段前提与证据状态

本路线以“真实单机闭环需要先完成外部证据冻结”为前提：仓库已具备受管 vLLM 部署、可用性验证、性能测试和证据记录能力，但当前 release record 尚未保存真实环境指纹、Run ID 或 Artifact 引用。

在真实单机证据冻结前，不能把该闭环称为已验证；即使冻结完成，也不自动证明集群能力已实现。

## 2. P1 决策：已有 Kubernetes 的受限纳管

P1 选择**已有 Kubernetes GPU 集群 + Bittune 专属 namespace**，而不是裸机 self-provision，也不是继续扩展本机 Docker 功能。

选择理由：

- 它首次验证“集群推理运维 Agent”的产品价值；
- 它避开节点安装、网络、GPU 驱动与集群 bootstrap 的高噪声问题；
- 它能够直接继承现有的 Discovery/Managed State、最小操作、证据链和容量运行点原则；
- 它将风险限制在 namespace 和 Bittune 创建资源内，便于真实用户验证和回滚。

## 3. P1 允许与禁止

允许：

- 读取已授权 Target 的集群与 namespace 必要事实；
- 在专属 namespace 内创建、查询、探测、压测和删除 Bittune 受管 vLLM 服务及其直接依赖；
- 对 Bittune 创建资源记录所有权、Provider 资源引用、环境证据和 Run Record；
- 对同一 Target 内的受管服务建立可复核容量运行点。

禁止：

- 修改 Node、CRD、集群级 RBAC、存储类、网络插件、GPU Operator 或其他 namespace；
- 自动接管已有 workload、endpoint 或模型服务；
- 把环境发现结果自动升级为受管资源；
- 把“模型成功启动”写成“集群容量已验证”；
- 在 P1 引入常驻自愈、定时任务、裸机安装或多租户。

## 4. 下一份架构 ADR 必须拍板的事项

在实现任何 Kubernetes Tool 前，集群 Target 与所有权 ADR（[ADR 0009](../docs/decisions/0009-cluster-target-ownership-and-tool-catalog.md)）已经确定以下事项：

1. Target 的稳定身份、认证来源和凭据存放边界；
2. 受管资源身份如何对应 Kubernetes 资源引用；
3. namespace、RBAC、删除和高影响操作的策略；
4. DeploymentPreset、DeploymentIntent、ServiceInstance 的职责边界；
5. CapacityBaseline 的目标环境、实际 placement 与工作负载可比性规则；
6. Tool Capability Catalog 如何按任务逐步暴露可信 Tool，避免所有 Tool 常驻模型上下文。

这些是承重接口与边界；此时不定义具体 TypeScript 字段、Kubernetes Manifest 或 Tool 参数。

## 5. 后续阶段的解锁条件

| 阶段 | 解锁内容 | 前提 |
|---|---|---|
| P1 | 已有 K8s 的专属 namespace 受限纳管 | 本文件与 [ADR 0009](../docs/decisions/0009-cluster-target-ownership-and-tool-catalog.md) 已接受 |
| P2 | 多 Target、更多受管 Runtime/执行通道 | P1 的所有权、证据和回滚在真实环境稳定 |
| P3 | 裸机 SSH self-provision / GPUStack 安装 | 已证明 Target 生命周期和权限模型可承受高影响操作 |
| P4 | 连续观测、漂移检测、定时任务和受控自愈 | 已存在稳定的时序指标、策略和人工接管机制 |

P2 不是自动等价于多节点调度；是否需要调度能力必须由真实 P1 用户场景与证据决定。Bittune 始终优先复用既有调度器，而不是自建调度器。
