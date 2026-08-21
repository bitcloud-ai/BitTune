# Bittune 产品方向

> 状态：方向已收敛，待以架构 ADR 和实施计划落地。

本目录定义 Bittune 的产品定位、首发切片和演进路线。它不描述已经实现的 Tool 行为，也不替代工程文档。

`docs/` 与本目录职责不同：

- `vision/`：回答为什么做、服务谁、先验证什么、阶段如何收敛；
- `docs/architecture/`、`docs/capabilities/`：记录已接受架构与当前实现事实；
- `docs/decisions/`、`docs/plans/`：把本目录已确认的方向转为长期技术决策和当前实施工作。

产品方向不能回写尚未实现的能力到 Tool Contract；实现也不能在没有产品决策的情况下偏离这里的首发切片。

## 阅读顺序

1. [`current-state.md`](current-state.md)：当前工程资产、证据冻结状态和迁移约束。
2. [`product-brief.md`](product-brief.md)：首发用户、问题、价值承诺和非目标。
3. [`strategy.md`](strategy.md)：首个集群切片、成功标准和决策边界。
4. [`roadmap.md`](roadmap.md)：阶段顺序、进入条件和明确不做事项。

## 一句话定位

> **面向既有算力环境的、可追溯的推理运维智能体**：用户用自然语言提出推理服务目标；Bittune 在被授权的边界内复用现有 Docker、Kubernetes、SSH 或 GPUStack 能力完成操作，并交付带证据的结果。
