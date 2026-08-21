# Bittune 产品简报

> 状态：方向基线。后续架构和实施计划必须满足本文件；未实现内容不得写入当前 Tool Contract。

## 1. 要解决的问题

部署和运营大模型推理服务，需要跨越模型与镜像版本、GPU 资源、Runtime 参数、服务健康、压测指标和容量结论。现有 Docker、Kubernetes、GPUStack 等系统能执行这些动作，但工程团队仍需要把多个控制面、命令和实验结果拼接成一次可信决策。

Bittune 不替代这些基础设施。它将用户目标翻译为受约束、可追溯的推理运维动作，并把结论和证据保留下来。

## 2. 首发用户

首发用户是已经拥有 Kubernetes GPU 集群、但没有专职推理平台团队的工程团队。它们能够提供管理员预先配置的访问凭据和专属 namespace，却不希望日常工作依赖 `kubectl`、Helm 参数和分散的压测脚本。

这不是面向所有算力环境的首发承诺。裸机集群安装、GPUStack 生命周期接管、多租户平台和自主调度属于后续阶段。

## 3. 首个 Job To Be Done

当团队希望将一个指定模型以 OpenAI-compatible API 形式提供给内部调用方时，用户应能用自然语言给出目标和约束；Bittune 在预授权 namespace 内完成受管部署、可用性验证和性能测试，并返回带 Run Record 的容量运行点。

用户不需要进行日常 Kubernetes 操作，但以下事项不能被隐藏：初始凭据授予、namespace/RBAC 范围、模型与镜像来源、预算或高影响操作确认，以及紧急接管。

## 4. 产品承诺

1. **目标导向**：用户表达要交付的推理服务，而不是拼接底层命令。
2. **受限执行**：Bittune 只在被授权的 Target 和资源边界内行动。
3. **归属明确**：Bittune 创建的资源可由 Bittune 管理；外部资源默认只读。
4. **结论可复核**：部署、探测、压测和容量结论都关联不可变 Run Record 与 Artifact。
5. **复用基础设施**：Bittune 复用 Kubernetes、Docker、SSH、GPUStack 等执行通道，不构建替代性的集群调度系统。

## 5. P1 成功标准

P1 在真实、预授权的 Kubernetes GPU 集群中达成以下结果才算有效：

- 用户完成初始 Target 授权后，无需手工执行 Kubernetes 运维命令；
- Bittune 只在专属 namespace 中创建、读取和删除自己记录为受管的资源；
- Bittune 成功交付一个可访问的受管 vLLM 服务，并完成 Ready、端点探测和性能测试；
- 产出至少一个可复核的、带目标环境与工作负载指纹的 CapacityBaseline；
- 不修改集群级配置、节点、其他 namespace 或非受管 workload；
- 在至少三个独立的真实部署运行中复现上述闭环，并记录人工介入原因。

## 6. 非目标

- 通用 Kubernetes 管理平台或 Web 控制台；
- 自动接管既有服务、namespace、节点或集群；
- 裸机安装 Kubernetes/GPUStack、节点调度或跨集群迁移；
- 多租户、计费、配额平台；
- 无边界的 Shell、任意 Helm values、任意 Kubernetes Manifest 或任意 vLLM 参数；
- 在没有实测证据时宣称性能、容量或调优收益。
