# LLM Inference Autopilot MVP 开发计划

> 计划基线：2026-08-05  
> 目标环境：单台 Linux 主机、单张 NVIDIA RTX 5090 32 GB、Docker  
> 开发环境：Windows 仅执行不占用 GPU 的单元、Contract 和可用的集成测试

## M8 Agent 重构决策（当前执行基线）

M8 的主交付由官方 LangChain `create_agent` 实现持续会话，不再以固定阶段 DAG 驱动用户交互。LangGraph 仍作为底层状态运行时，PostgresSaver 以 `thread_id = experiment_id` 持久化消息和 Interrupt。所有 Agent 工具通过既有 Tool Registry/Tool Gateway，动态可见性由 Middleware 在每次模型调用前解析，L2 由官方 `HumanInTheLoopMiddleware` 触发审批，L3 永远不可见。

交互入口按 ADR-017 确定为：

```text
autopilot chat -> Click -> Textual -> FastAPI SSE -> create_agent stream v2
```

不新增自定义 Agent 循环、通用 Plugin System、任意 Shell 工具或 Web UI。Textual 只负责终端交互，
不导入 Graph、Gateway、Capability、Provider 或 Runner。原有领域能力、异步 Job、OPA、Approval、
Runner、MLflow 和 Evidence 保持不变，Agent 只负责自然语言理解、工具选择、结果解释和审批请求。

### M8.0：Agent/TUI 调研与冻结决策

- 官方 Agent Runtime：`langchain.agents.create_agent`、Checkpointer、Middleware、
  `HumanInTheLoopMiddleware` 和 v2 streaming；
- Python TUI：Textual，使用内建 Worker、Command Palette、Markdown、Input 和 Pilot；
- Click 保留为命令入口及非交互自动化命令，删除 `prompt_toolkit`；
- 当前只实现一个 Agent。未来只有在多个独立领域成熟后，才按 ADR 新增中央 supervisor，并把领域
  Agent 包装为受控 Tool；
- Claude Code 未公开可核实的内部 TUI 框架，不基于猜测复制实现；Codex/Ratatui、Gemini/Ink 和
  Toad/Textual 共同证明应复用成熟 TUI 框架。

验收：ADR-017、Agent 设计、开发路线和依赖选择一致，不存在第二套 Agent loop、TUI 框架或包管理器。

### M8.1：领域 Tool 装配（本次执行项）

M8.1 只完成已有能力包到标准 Agent Runtime 的装配，不改变能力包的 Provider 边界：

- 根据 `capabilities/*/manifest.yaml` 注册固定的窄 Tool 和 Pydantic 输入 Schema；
- `StructuredTool` 只负责把输入转换成 `ToolCallRequest`，执行仍由既有 `ToolGateway` 完成；
- 增加只读 `get_mvp_capabilities_result`，返回固定 MVP 范围、Provider 和当前可用性，不执行外部操作；
- 生产装配使用 PostgreSQL 的 Experiment、Tool Set、Plan、Approval、Idempotency 和 Audit Port；
- 没有 G0 验证的 Provider Profile、OPA 或 Runner 时，相关工具后端默认拒绝，不回退到 Fake Adapter；
- 资源预留和异步 Job 仍由现有 Gateway/Job Port 负责，M8.1 不新建 Worker 或第二套队列。

验收：Agent 在 `autopilot chat` 中只能看到当前阶段和策略允许的已注册领域 Tool；能力查询可以通过 Gateway 返回；所有部署、压测、调优和删除类动作在 Provider/Runner 未固定时得到分类拒绝并留下审计事件。

### M8.2：实时会话与 Textual TUI

- API 使用 LangChain/LangGraph v2 stream 输出 Token、Tool Call、Tool Result、Interrupt、错误和完成事件；
- `autopilot chat` 使用 Textual 消费 SSE，提供滚动消息、Markdown、状态栏、Command Palette、
  `/approve`、`/reject`、`/status`、`/cancel`、`/new` 和 `/quit`；
- L2 Agent Interrupt 与 ADR-016 独立管理员 Approval v2 分层处理，operator 不能通过 TUI 自审批；
- TUI 的网络调用使用 Textual Worker，界面线程不被模型或网络请求阻塞；
- 使用 Textual Pilot 覆盖消息提交、命令执行和 Interrupt 展示，不建立第二套 UI 测试框架。

验收：终端能连续多轮对话并实时显示 Agent/Tool/Interrupt 事件；API、TUI 或网络错误不会丢失已持久化
会话；TUI 不包含任何领域执行逻辑。

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
- 基于 Click 的 `autopilot` CLI，覆盖 Experiment 创建、状态查询、SSE 事件、审批恢复和取消；
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
- M5 已完成实现和 Windows/Fake 验证：FastAPI + Uvicorn UDS、类型化白名单请求、
  Docker SDK Adapter、单 GPU Lease、路径/Secret/日志边界、vLLM 分层健康检查、取消、超时、
  磁盘预算、清理和权威快照约束的 Reconciliation 已落地；真实 Linux UDS、Docker、systemd
  和 GPU 验收仍待目标主机；
- G0 等待 Linux RTX 5090 主机、固定 Provider 版本矩阵和明确 GPU 测试批准；
- M6 已落地环境、容量、部署、Benchmark 和 MLflow Evidence 的版本化 Port、Fake 生命周期、
  Runner/Tracking Adapter 及边界 Contract Test；真实 llm-d Planner、vLLM 和 EvalScope
  Provider 版本尚未通过 G0，因此这些高成本入口继续 fail-closed；固定 Candidate 的
  Environment → Capacity → Deploy → Benchmark → Evidence worker 闭环已通过 Fake 端到端测试，
  包含 approved Plan 绑定、静态拒绝、部署/Benchmark/OOM/约束失败分类、取消、异步 pending、
  MLflow 自动记录及 Benchmark/Deployment 清理；M6 的 REST 入口和 G0 真机验收仍待目标环境。
- M7 已完成非 GPU 实现：Optuna 4.9.0 Study/Trial 持久化与恢复、预算/收敛、固定 Trial
  Controller、失败分类、Top 3 候选各 2 次复测、Verification Summary、确定性
  Champion/Fallback 选择及 Evidence Bundle manifest/artifact 已落地；Fake 单元路径和
  Optuna Contract Test 已通过。复测状态使用 Job progress 结构化快照，真实 PostgreSQL、
  MLflow 线上和 GPU 验收仍待对应环境。
- M8 Agent/TUI 已完成非 GPU 实现：单主 LangGraph、结构化 State、官方 InMemorySaver 测试恢复、
  官方 PostgresSaver 生产生命周期、两处 Plan Hash 绑定的 Interrupt、远程
  OpenAI-compatible ModelProvider、FastAPI REST/SSE/OpenAPI 路由、PostgreSQL 实验与
  Deployment 投影、Artifact 查询、Bearer Token 配置装配、fail-closed Provider/外部状态
  默认值、LangChain `create_agent`、v2 Streaming、Textual TUI，以及 51 个由代码生成的公共
  JSON Schema。`create_experiment_plan` 已通过 Gateway 将认证主体、模型、Workload、SLO、预算和
  权限写入 PostgreSQL Experiment 投影与 Graph State；Agent Interrupt 已接入独立 Approval v2，
  同步和 SSE 恢复都会校验 Plan ID/Hash/Action，并在模型恢复失败时回到可重试的审批状态。
  Graph/API/TUI Fake 测试已通过；真实
  PostgreSQL、OPA/MLflow 线上与 Linux/RTX 5090 验收仍待对应环境。
- M9 已完成部署模板的非 GPU 部分：固定 Digest 变量的 Compose 控制面、API 非 Root/只读
  根文件系统/无 Docker Socket 安全约束、OPA Policy Bundle 挂载、Secret 文件边界、
  配置模板、迁移入口、备份恢复文档、基于 Click 的 REST/SSE CLI、Compose 安全契约测试，
  以及代码生成的 OpenAPI。
  M9 的真实 Docker Compose、PostgreSQL、OPA、MLflow 和 Linux 边界验收仍待对应环境。
- 整体 MVP 尚未完成：领域 Plan 持久化、Gateway Job Dispatcher、PostgreSQL Lease Worker 和
  M6 REST 执行路径尚未贯通；生产 Dispatcher 当前只执行能力查询和 Experiment 需求计划，
  其余动作分类拒绝。不得将当前状态描述为完整闭环。
- 未创建虚假的 Worker 进程。后续只把已实现的 Capability Service/Adapter 接入既有 PostgreSQL
  Lease Queue 和 Host Runner；真实 Provider Profile 未固定时继续 fail-closed，不回退 Fake。

下一执行顺序固定为：领域 `create_*_plan` 持久化 → `start_*` 入队 Dispatcher → Lease Worker
执行前复核授权 → Capability Service/Adapter → Host Runner → Job/Artifact/Graph 对账 →
REST/Agent/Textual 主路径验收。未来多领域子 Agent 只按 ADR-017 的 supervisor-as-tool 边界扩展，
不改变上述确定性执行链。
