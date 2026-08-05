# LLM Inference Autopilot MVP 开发计划

> 计划基线：2026-08-05  
> 目标环境：单台 Linux 主机、单张 NVIDIA RTX 5090 32 GB、Docker  
> 开发环境：Windows 仅执行不占用 GPU 的单元、Contract 和可用的集成测试

## 1. 完成标准

MVP 完成必须同时满足：

- REST API 能驱动环境检测、容量规划、vLLM 部署、EvalScope 压测、Optuna 搜索、Top 候选复测、Champion 审批和 Evidence Bundle 归档；
- 所有状态改变经过 Tool Gateway，并实施 Schema、阶段、预算、OPA、审批、幂等、资源锁和审计检查；
- API 不挂载 Docker Socket，Host Runner 仅接受带类型白名单动作；
- PostgreSQL 是 Graph、Job 和业务状态事实源，MLflow 只保存实验证据；
- 中断、取消、超时、Runner/API 重启和 Provider 失败都有确定的对账与清理路径；
- 单元、Golden、Adapter Contract、非 GPU 集成测试和获批的 RTX 5090 GPU E2E 均通过；
- Evidence Bundle 能回溯硬件、模型 Revision、镜像 Digest、Provider/Schema 版本、工作负载、原始结果和代码 Revision。

## 2. 实施原则

- 每个阶段生成一个可独立验收的 Git 提交；同一阶段包含实现、测试和必需文档。
- 不在 Windows 开发机上伪造 Linux、Docker Daemon、NVML 或 RTX 5090 测试结果。
- 真实 Provider 未锁定版本前默认不可用，测试通过 Fake Adapter 验证确定性流程。
- 任何阶段不得以任意 Shell、任意 Provider 参数、内存状态事实源或未审批的高成本动作换取进度。
- 每个阶段先运行最小相关测试，收尾前运行全量标准检查。

## 3. 分阶段路线

执行顺序固定为 `M0 → M1 → M2 → M3 → M4 → M5 → G0 → M6 → M7 → M8 → M9`。G0 依赖 Linux RTX 5090 主机，在它完成前只能开发不依赖真实 Provider 版本的 M0～M5。

### M0：架构收敛与工程基座

产物：

- 冻结 ADR-015 中的 MVP 实现选择；
- 统一 Agent Tool 命名，删除当前未使用的多 Provider 和 Pareto 预留；
- Python 3.12、`uv`、Ruff、mypy、pytest 工程；
- 根 README、`.gitignore`、局部 `AGENTS.md`、CI 检查入口。

验收：四项标准检查在空业务基座上通过，且 Git 工作树干净。

### M1：领域契约

产物：

- 稳定 ID、UTC 时间、版本 Enum、来源类型、错误 Envelope 和 Artifact Ref；
- Requirement、Hardware Passport、Model Profile、Candidate、Workload、SLO、Plan、Job、Approval、Trial、Champion 契约；
- Canonical JSON 与 Plan Hash；
- 数值范围、判别联合、单位和 `extra="forbid"` 测试。

验收：非法模式组合、TP 非 1、浮动 Revision、非 Digest 镜像和未知字段均在领域边界拒绝。

### M2：确定性能力核心

产物：

- vLLM 参数白名单 Compiler 和部署预览；
- EvalScope 四种流量模式 Compiler、预算计算和指标 Normalizer；
- Search Space 静态裁剪、约束评估和确定性 Champion/Fallback 选择；
- Golden Test 和能力包 Manifest 校验。

验收：四种 Benchmark 输入与 Golden 结果一致，所有 Provider 输出都在统一指标契约内。

### M3：持久化、Job 与 Artifact

产物：

- SQLAlchemy 2 实体、Repository Port 和 Alembic 初始 Revision；
- PostgreSQL Lease Queue、Job 状态机、幂等记录、Event 和追加式 Audit；
- PostgreSQL 权威时钟、Lease Fencing Token、`waiting_approval` 过期恢复和持久化取消请求；
- 同 Experiment Plan/Artifact 组合外键和 Provider Job 对账字段；
- 本地 Artifact Store，包括根目录约束、符号链接逃逸防护、SHA-256、原子发布、同内容幂等重放和数据库元数据；
- PostgreSQL 集成测试。

验收：进程重启后能恢复 Job 与租约，重复幂等请求不会创建第二个 Job。

### M4：Tool Gateway、OPA 与审批

前置：用户确定唯一身份认证来源和审批人隔离契约，并通过 ADR 冻结。

产物：

- ADR-016 固定 opaque Bearer Token、SHA-256 Token Hash、Human/Service 主体隔离和审批职责分离；
- 按 Phase、Role、Hardware、Provider、Feature Flag 和 Policy 求交集的动态 Tool 可见性；
- Tool Gateway 强制链、OPA Client、默认拒绝 Rego 和 Policy Golden Test；
- Approval v2 的不可变 Plan 绑定、PostgreSQL 权威过期复验和 human admin 非自审批；
- 追加式 Tool Set Snapshot、Job Authorization 和事务级幂等 Claim，供 Worker 执行前复核。

验收：伪造 Tool、不可见 Tool、无审批 L2、Hash 错误、超预算和全部 L3 动作均被拒绝并记录。

### M5：Host Runner

产物：

- Unix Domain Socket 上的带类型白名单 API；
- GPU 租约锁、路径安全、镜像 Digest、端口和参数白名单；
- Docker 容器启停、健康检查、日志脱敏、取消、超时、清理和对账；
- systemd 单元和 Runner 边界集成测试。

验收：Runner 无原始命令入口，失败和取消路径都回收容器、临时目录和 GPU 锁。

### M6：七环节 Adapter 与固定 Trial 闭环

前置：G0 已固定并验证 Provider 版本矩阵。

产物：

- NVML + Linux Host Collector、固定容器的 llm-d Planner Adapter；
- vLLM Docker Adapter、EvalScope 固定版本 Python API Adapter；
- MLflow Evidence Adapter 和每个 Adapter 的 Contract Test；
- 一次固定 Candidate 的 Environment → Capacity → Deploy → Benchmark → Evidence REST 闭环。

验收：Fake Adapter 端到端通过；未锁定的真实 Provider Profile 明确失败而不回退到未验证实现。

### M7：Optuna、复测与 Evidence Bundle

产物：

- 单 Objective + 硬可行性约束的 Optuna Controller；
- Trial 全状态、预算、收敛、失败分类和恢复；
- Top 3 每个复测两次，计算均值、变异系数和最差值；
- 确定性 Champion/Fallback 与完整 Evidence Bundle。

验收：至少 10 个 Fake Trial 完成，失败 Trial 不丢失，Champion 只来自证据完整的可行复测候选。

### M8：LangGraph 与 FastAPI

产物：

- 一个主 Graph、结构化 State、Checkpoint、Interrupt 和 Reconciliation；
- 远程 OpenAI-compatible `ModelProvider`，仅用于需求结构化、测试策略、失败解释和报告；
- Experiment、Plan、Job、Approval、Deployment、Artifact 和 SSE API；
- Fake Adapter 下的 Interrupt/Resume 与 API 重启恢复集成测试。

验收：对话无法绕过确定性节点，Graph State 不包含 Secret、大型报告或完整日志。

### M9：部署与非 GPU 集成验收

产物：

- 固定 Digest 的 Compose 控制面、OPA Policy Bundle、配置模板、备份和恢复文档；
- API 容器非 Root、只读根文件系统、无 Docker Socket；
- PostgreSQL、OPA、MLflow、API 和 Fake Runner 集成验收；
- 生成 JSON Schema、OpenAPI 和能力 Manifest 一致性检查。

验收：新 Linux 主机能按文档启动控制面，边界安全测试全部通过。

### G0：RTX 5090 Phase 0 与 GPU 入场门禁

前置：用户明确批准 GPU 0 测试，GPU 空闲，已配置时长、请求和 Token 预算。

产物：

- 驱动、NVIDIA Container Toolkit、vLLM 镜像 Digest、Planner Commit/Image、EvalScope 版本和目标模型 Revision 矩阵；
- NVML、vLLM Smoke、Baseline、Closed Loop、Open Loop、2～3 Trial 与 Evidence GPU E2E；
- OOM、取消、超时、Runner 重启和清理验证；
- 真机性能数据与容量估算误差记录。

验收：固定的 Provider Profile 可以进入 M6 开发，且完成文档第 08 章与 Provider 版本相关的入场检查。M6～M9 完成后还必须在同一版本矩阵上执行完整 GPU E2E。

## 4. 标准检查

每个阶段收尾执行：

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/autopilot runner
uv run pytest tests/unit tests/contract
```

涉及 API、数据库、Gateway、Runner 或 Adapter 边界时增加对应 `tests/integration` 测试。`tests/gpu` 只能在 G0 前置条件满足后执行。

## 5. 当前状态

- Git 和架构文档基线已建立；
- M0 已完成；
- M1 已完成；
- M2 已完成：vLLM/EvalScope Compiler、Benchmark Normalizer、封闭 Search Space、确定性
  Champion/Fallback Policy、Golden Test、公共 Schema 和能力包 Manifest 已落地，标准检查已通过；
- M3 已完成实现和非 PostgreSQL 验证：SQLAlchemy/Alembic、Lease Queue、Fencing、幂等、
  Event/Audit、取消请求及 Artifact Store 已落地；Artifact 重启恢复测试已通过；
- M3 的 6 项真实 PostgreSQL 集成测试已实现，当前 Windows 环境未配置
  `AUTOPILOT_TEST_POSTGRES_URL`，因此数据库执行验收仍待可用 PostgreSQL 测试库；
- M4 已完成实现和非 PostgreSQL 验证：opaque Bearer Token 认证、动态 Tool 可见性、
  Gateway 强制链、OPA fail-closed Client/Rego、Approval v2、Tool Set Snapshot、Job
  Authorization 及资源预留前的事务级幂等 Claim 已落地；
- M4 的真实 Rego Golden Test 需要本机 `opa` 可执行文件，当前环境缺失，因此 14 项跳过；
- M3/M4 的 13 项真实 PostgreSQL 集成测试已实现，当前 Windows 环境未配置
  `AUTOPILOT_TEST_POSTGRES_URL`，因此数据库执行验收仍待可用 PostgreSQL 测试库；
- M5～M9 未开始；
- G0 等待 Linux RTX 5090 主机、固定版本矩阵和明确 GPU 测试批准。
