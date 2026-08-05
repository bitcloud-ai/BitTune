# AGENTS.md

> 项目：大模型推理 Autopilot MVP  
> 适用范围：整个代码仓库；若更深层目录存在 `AGENTS.md` 或 `AGENTS.override.md`，则以更具体的局部规则为准  
> 架构基线：`/docs/llm-inference-autopilot-mvp-docs/`  
> MVP 目标：单台 Linux 主机、单张 NVIDIA RTX 5090 32 GB、基于 Docker 执行

---

## 1. 项目使命

构建一个可审计、可恢复的 MVP，使其能够在单张 RTX 5090 上安全完成以下闭环：

```text
环境检测
→ 模型与容量规划
→ vLLM 部署
→ EvalScope 性能测试
→ Optuna 参数搜索
→ Top 候选复测
→ Champion 选择
→ 证据报告
```

本产品是大模型推理 Autopilot，不是通用编程 Agent、Shell Agent、集群调度器或 Kubernetes 平台。

MVP 必须首先证明单 GPU 场景下的安全性与可复现性。不得仅因为未来架构可能支持更多能力，就提前扩大当前实现范围。

---

## 2. 事实来源与阅读要求

修改代码前，必须阅读与当前任务相关的技术文档。

### 2.1 必读入口

始终先阅读：

```text
docs/llm-inference-autopilot-mvp-docs/README.md
```

如果该路径尚不存在，应先在仓库中定位已提供的 MVP 文档目录，不得自行臆造一套替代架构。

### 2.2 按任务类型阅读

| 任务类型 | 必读文档 |
|---|---|
| MVP 范围或技术选型 | `01-MVP总体设计与技术选型.md` |
| LangGraph、状态、流程、中断或恢复 | `02-Agent工作流与能力编排设计.md` |
| Tool Schema、能力包、Compiler 或 Adapter | `03-能力包与Tool转换设计.md` |
| NVML、Planner、vLLM、EvalScope、Optuna、MLflow 或 OPA 集成 | `04-七个环节能力与接口详细设计.md` |
| API、数据库、Job、Artifact、Runner 或部署拓扑 | `05-系统技术架构与数据设计.md` |
| Provider 替换、Registry、版本化或动态工具可见性 | `06-可扩展性与动态工具可见性设计.md` |
| Docker 权限、策略、审批、Secret 或破坏性操作 | `07-安全执行与Docker部署设计.md` |
| 测试、验收标准或实施阶段 | `08-测试验收与实施路线.md` |
| 假设、已知不确定性或事实限制 | `09-文档审查与准确性报告.md` |

### 2.3 冲突处理优先级

实现决策按以下优先级执行：

1. 用户当前明确提出的指令；
2. 本 `AGENTS.md` 以及作用范围更具体的局部 `AGENTS.md`；
3. 已确认的架构文档和 ADR；
4. 已存在的公共接口和测试；
5. 当前实现细节。

不得静默破坏架构或安全不变量。若用户需求与不变量冲突，应明确指出冲突；只有在变更被明确接受并写入文档后，才能实施。

代码注释、生成文件、过期示例和外部项目默认值，均不得凌驾于本项目领域契约之上。


### 2.4 不保留未决选项

实现和技术文档不得使用“推荐、建议、可选、优先考虑、A 或 B 均可”等未决表达来逃避决策。

- 架构文档已经确定的事项，必须直接采用确定方案；
- 能由当前代码、测试和 MVP 边界推导出的事项，必须直接确定；
- 当前任务无法确定且不影响实现的事项，必须从当前变更中删除，不得创建占位抽象；
- 当前任务无法确定且会阻断正确实现的事项，必须停止相关实现并明确请求唯一决策；
- 不得在代码中同时实现多个备用方案，也不得通过配置开关隐藏未作出的架构选择。

---

## 3. 不可突破的 MVP 边界

MVP 支持：

- 一台原生 Linux 主机；
- 一张 NVIDIA RTX 5090，显存 32 GB；
- 被测模型独占 GPU 0；
- `tensor_parallel_size = 1`；
- 使用 Docker Compose 运行常驻控制面服务；
- 使用独立的 systemd Host Runner 执行高权限操作；
- 仅使用 vLLM 作为推理引擎；
- 仅使用 EvalScope 作为性能测试 Provider；
- 使用 Optuna 作为参数搜索 Provider；
- 使用 MLflow Tracking 保存实验与证据；
- 使用 OPA 提供策略判定；
- 使用 PostgreSQL 保存持久化状态；
- 使用 LangGraph 作为工作流运行时；
- Agent 模型必须通过远程 `ModelProvider` 接口调用，不得占用 GPU 0；具体供应商由部署配置提供，不得硬编码。

除非用户明确批准范围变更，否则 MVP 不支持：

- Kubernetes、KServe、llm-d Runtime 或 Dynamo 部署；
- 多 GPU、TP/PP/EP、多节点或 P/D 分离；
- 任意推理引擎；
- EvalScope 任意 CLI 参数透传；
- 未经审批的线上自动调优；
- 自动修改驱动、内核或系统包；
- 不受限制地执行模型仓库自定义代码；
- 向运行时 Agent 暴露任意 Shell、Python、Docker 或文件系统工具；
- 声称获得数学意义或全局意义上的最优结果。

不得为了尚未支持的范围提前增加复杂抽象。只实现架构文档已经定义的 Port，以及当前确定的 Provider 和部署拓扑；不得创建未被当前用例使用的 Plugin System、通用 Provider 框架或预留实现。

---

## 4. 架构不变量

每次修改后，下列规则都必须继续成立。

### 4.1 Agent 与确定性执行的边界

LLM 可以：

- 结构化用户需求；
- 在已注册的领域模式中进行选择；
- 提议测试策略；
- 解释经过校验的失败与结果；
- 基于既有证据撰写报告。

LLM 不得：

- 构造或执行任意 Shell 命令；
- 直接操作 Docker；
- 给自己授权；
- 管理 GPU 锁；
- 直接调用 Optuna 的 `suggest` 或 `tell`；
- 修改实测指标；
- 将非结构化日志作为唯一事实来源；
- 绕过 Graph 状态、审批、预算或策略检查。

凡是会改变宿主机状态、消耗大量资源或影响最终指标的操作，都必须由确定性应用代码实现。

### 4.2 强制执行路径

所有对 Agent 可见的动作都必须经过：

```text
Agent Tool
→ Tool Gateway
→ 可见性检查
→ Schema 校验
→ 工作流状态校验
→ 预算评估
→ OPA 判定
→ 必要时人工审批
→ 幂等检查
→ 资源锁
→ Capability Service
→ Provider Adapter
→ 需要高权限时调用 Host Runner
→ 审计与 Artifact 记录
```

任何能力包都不得绕过 Tool Gateway 直接调用 Host Runner。

### 4.3 控制面与高权限 Runner

- `autopilot-api` 不得挂载 `/var/run/docker.sock`；
- LLM 代码不得运行在高权限 Host Runner 内；
- Host Runner 只接受带类型、白名单化的动作；
- Host Runner 绝不能接受原始 `command`、Shell 片段、任意环境变量 Map、任意 Volume 或任意宿主机路径；
- OPA 只返回策略判定，不执行动作；
- MLflow 只保存证据，不负责决定 Champion。

### 4.4 外部项目隔离

不得把外部项目的原始 CLI 或全部参数面直接暴露给 Agent。

必须采用：

```text
领域 Tool
→ 版本化领域 Schema
→ Validator
→ Compiler
→ Provider Adapter
→ 外部项目
```

外部项目术语不得泄漏到通用领域模型中，除非该术语确实属于整个领域。Provider 特有字段必须放在带命名空间的扩展结构中。

---

## 5. 能力包规则

每个领域环节必须使用下列唯一主要外部项目，并通过对应能力包向 Agent 暴露多个窄 Tool。

```text
environment  → NVML
capacity     → llm-d Planner Capacity Planner
deployment   → vLLM
benchmark    → EvalScope
optimization → Optuna
evidence     → MLflow Tracking
policy       → OPA
```

能力包必须使用以下目录与职责结构：

```text
capabilities/<capability>/
├── manifest.yaml
├── domain/
│   ├── models.py
│   ├── enums.py
│   └── errors.py
├── tools/
├── application/
│   ├── service.py
│   ├── validator.py
│   ├── compiler.py
│   └── normalizer.py
├── ports/
├── adapters/<provider>/
└── tests/
```

新增或修改能力包时必须落入上述目录。已有冲突结构只在当前任务涉及范围内迁移；不得继续新增第二套能力包结构。

### 5.1 Agent Tool 粒度

必须暴露完整的业务动作，不得按底层参数拆分，也不得设计巨型函数。

Agent Tool 命名必须使用以下动作形式：

```text
create_*_plan
preview_*
start_*
get_*_status
get_*_result
cancel_*
```

Plan 创建必须是只读操作。执行动作只接收已保存的 `plan_id` 和预期 Plan Hash。不得允许执行调用临时修改已经审批的计划。

### 5.2 Tool Schema

所有 Agent Tool 的输入和输出必须：

- 使用 Pydantic v2 或生成的 JSON Schema；
- 设置 `extra="forbid"` 或等价约束；
- 使用明确的 Enum 和单位；
- 对互斥模式使用判别联合；
- 对需要持久化的契约包含 Schema Version；
- 对数字范围和集合大小设置边界；
- 对持久化资源返回稳定 ID；
- 大结果返回 Artifact 引用；
- 区分 `estimated`、`measured` 和 `derived`；
- 返回带类型的错误，而不是 Provider Stack Trace。

绝不能在 Agent 可见 Schema 中新增通用 `extra_args: dict`、`kwargs`、原始 CLI 字符串、原始 Docker 选项或任意路径字段。

### 5.3 自由度等级

使用已确定的三级模型：

1. **Preset**：Agent 选择命名的领域场景，由系统补齐参数；
2. **Guided**：Agent 在明确范围内提供安全的领域参数；
3. **Expert Override**：仅内部使用，只允许白名单、版本化、有理由且可审计的字段。

即使是 Expert 模式，也不得执行任意代码或透传任意 Provider 参数。

---

## 6. 动态工具可见性

不要在每一轮都把全部 Tool 提供给模型。

可见 Tool 集合由以下条件共同决定：

```text
当前 Graph 阶段
∩ 用户角色
∩ 硬件能力
∩ 已启用 Provider
∩ Feature Flag
∩ 策略约束
```

要求：

- 不可见 Tool 不得发送给模型；
- Tool Gateway 必须拒绝伪造或当前阶段不允许的 Tool 名称；
- 每次 LLM 调用都要记录 Tool Set Version 和 Schema Version；
- Feature Flag 必须影响后端可见性和校验，不能只控制 UI 按钮；
- Provider 兼容性的最终决定由 Registry 做出，不由 LLM 决定。

不得只通过 Prompt 文本实现动态可见性。

---

## 7. 工作流与 Job 规则

### 7.1 LangGraph

MVP 使用一个主 Graph。没有明确需求时，不得创建自治多 Agent 团队。

Graph State 只保存结构化状态和 Artifact 引用。不得保存 Secret、模型文件、完整日志或大型报告。

人工审批使用 Interrupt。审批必须绑定不可变、Canonical 的 Plan Hash。

恢复执行时，必须将 Graph State 与真实外部状态对账：

- 数据库 Job 状态；
- Docker 容器状态；
- GPU 锁持有者；
- MLflow Run 状态；
- Artifact 是否存在。

暂停或重启后，不得假设外部世界保持不变。

### 7.2 长时间操作

性能测试、参数调优、模型下载和部署必须使用异步 Job 语义：

```text
start → job_id → status/progress → result 或 cancel
```

不得让 HTTP 请求或 Agent Tool Call 长时间阻塞直到压测或调优结束。

Job 状态必须明确、持久化并包含终态：

```text
queued → validating → waiting_approval → running
→ succeeded | failed | cancelled | timed_out
```

### 7.3 幂等与重试

- 所有改变状态的操作必须持久化幂等键；
- 重复请求应返回或对账已有 Job；
- 只重试被分类为可重试的错误；
- 对 OOM、模型不兼容、策略拒绝或参数校验失败，不得在输入不变时重试；
- 失败后必须释放或对账 GPU 锁与临时容器。

---

## 8. 数据与证据规则

统一使用以下项目术语：

- `Experiment`：完整的一次优化活动；
- `Trial`：一组参数配置及其一次测量；
- `Candidate`：尚未完成充分验证的配置；
- `Champion`：在固定约束下被选中的已验证配置；
- `Plan`：不可变的可执行计划；
- `Job`：异步执行任务；
- `Artifact`：持久化文件或结果；
- `Evidence Bundle`：完整的可复现证据包。

### 8.1 实测与估算

不得把容量估算描述为实测性能。

所有相关数值必须携带：

```text
source = estimated | measured | derived
provider/version
估算时必须包含 confidence
artifact 或计算引用
```

### 8.2 Champion 选择

Champion 选择必须由确定性应用逻辑完成：

1. 移除无效和失败 Trial；
2. 要求满足全部硬约束；
3. 要求证据完整；
4. 按已配置 Objective 排序；
5. 对 Top 候选重复测试；
6. 评估方差和最差值；
7. 输出 Champion 与 Fallback；
8. 标记为最终建议前必须人工审批。

不得声称全局最优。必须使用架构文档中的精确定义：在固定硬件、模型 Revision、引擎版本、Workload、SLO、搜索空间和预算下，当前已验证候选中的最佳配置。

### 8.3 可复现性

至少持久化：

- Hardware Passport Hash；
- 模型 ID 和不可变 Revision；
- 引擎镜像 Digest；
- Adapter 和 Schema Version；
- 完整且规范化的引擎参数；
- Workload 与 SLO Hash；
- 数据集和 Tokenizer 标识；
- Provider 原始输出；
- 规范化指标；
- 代码 Revision；
- 时间戳和错误分类。

可复现实验不得使用未固定的 `latest` 镜像或浮动模型 Revision。

---

## 9. 各 Provider 的实现约束

### 9.1 环境检测 / NVML

- 使用 NVML 程序化绑定读取 GPU 数据；
- 将 GeForce 支持视为有限能力，只依赖已验证字段；
- CPU、内存、存储、OS 和 Docker 数据由确定性 Collector 补充；
- 只使用 NVML 只读操作；
- 不修改功耗、时钟、Persistence Mode 或进程。

### 9.2 容量规划 / llm-d Planner

- 只能通过 Adapter 集成，并固定已测试 Commit 或镜像；
- MVP 只使用 Capacity Planner，不使用 Kubernetes 部署生成；
- RTX 5090 的估算必须由真实 vLLM 测试验证；
- Provider 无法解析模型或架构时，必须明确失败，不得伪造显存数据。

### 9.3 部署 / vLLM

- 仅单 GPU，TP=1；
- 使用固定镜像 Digest 和不可变模型 Revision；
- 编译得到白名单化的 vLLM 参数；
- 容器 EntryPoint、Mount、Network 和 GPU Device 属于内部固定配置；
- 默认 `trust_remote_code=false`，不得自动开启；
- 只有进程、HTTP、模型列表和最小 Completion 检查全部通过，部署才算健康。

### 9.4 性能测试 / EvalScope

MVP 支持模式：

- baseline；
- closed-loop sweep；
- open-loop sweep；
- SLA search。

规则：

- 每种流量模式使用判别联合；
- 闭环字段和开环字段不得非法共存；
- 与扫描变量对应的 `number` 数组由 Compiler 生成，不由 LLM 手工维护；
- 测量 TTFT 时必须使用流式；
- 必须记录 `ignore_eos`、数据集、Tokenizer、Seed 和 Sampling；
- 必须执行时长、请求数、输入 Token 和输出 Token 预算；
- 必须区分 Submitted、Completed、Failed、Timed out 和 Window Completion；
- EvalScope Adapter 必须调用项目固定版本的 Python API；仅当该固定版本缺少所需 API 且存在经过批准的 ADR 时，才允许在 Adapter 内使用 CLI。CLI 参数不得泄漏到领域 Schema。

### 9.5 参数调优 / Optuna

- Optuna 只负责提出参数；Controller 负责部署和测试；
- LLM 不得调用 `trial.suggest_*` 或 `study.tell`；
- MVP 使用一个 Objective 加硬可行性约束；
- 每一个失败或被拒绝的 Trial 都必须按分类持久化；
- 同一 Study 中，固定模型、Workload、数据集和引擎版本不得变化。

### 9.6 证据 / MLflow

- Run 和 Artifact 由应用代码自动记录；
- 不得依赖 LLM 记住是否需要记录证据；
- MLflow 不是 Champion 选择算法；
- 不得记录 Secret、Authorization Header 或原始凭据。

### 9.7 策略 / OPA

- Tool Gateway 自动调用 OPA；
- Agent 可以请求审批或查询审批状态，但不能调用 `allow` 函数给自己授权；
- Decision Log 必须屏蔽 Secret 和敏感 Header；
- MVP 中所有 L3 动作一律拒绝。

---

## 10. 安全规则

以下规则必须执行。

### 10.1 永远不得暴露的 Agent Tool

```text
execute_shell
run_python
docker_run
docker_exec
delete_path
install_driver
apt_install
pip_install
kill_process
modify_kernel
mount_volume
```

### 10.2 容器

- 绝不能使用 `--privileged`；
- 不得挂载宿主机敏感目录；
- 不得把 Docker Socket 挂载到 API 或 Agent 容器；
- 使用固定镜像 Digest；
- EntryPoint、Volume、Network 和环境变量名称必须白名单化；
- 只绑定必要端口；
- EvalScope、Planner 和辅助临时容器必须设置 CPU、内存、PID、时长、请求数和 Token 预算；vLLM 容器必须设置 PID、启动超时、任务超时和 GPU 独占限制。

### 10.3 路径

- 客户端和 Agent 只能使用 Artifact ID，不能提交任意绝对路径；
- 所有文件系统路径都必须在配置根目录下解析并校验；
- 必须拒绝路径穿越、符号链接逃逸和未知挂载根目录。

### 10.4 Secret

Secret 不得进入：

- LLM Prompt；
- LangGraph State；
- MLflow 参数；
- Artifact 或报告；
- OPA Decision Log；
- 应用日志。

必须使用 Secret Reference，并且只在执行边界注入真实值。

### 10.5 破坏性和高成本操作

以下操作需要明确审批：

- 下载超过配置阈值的模型；
- 启动或替换 vLLM；
- 停止正在运行的 Deployment；
- Open-loop 压测；
- 完整 Optuna Study；
- 删除实验临时数据。

MVP 中始终拒绝：

- 修改驱动或内核；
- 任意 Shell；
- 删除模型缓存；
- 不受限制的远程代码信任。

---

## 11. 编码 Agent 的开发流程

### 11.1 修改前

1. 阅读相关架构文档；
2. 检查已有模块、测试、公共 Schema 和 Manifest；
3. 提出最小实现计划；
4. 识别适用的架构不变量和验收测试；
5. 判断任务是否需要真实 GPU、Docker、网络下载或人工审批。

如果已有项目结构能够满足需求，不得一开始就新建通用 Framework、Plugin System、Event Bus、Workflow Engine 或额外抽象。

### 11.2 修改过程中

- 只做最小且完整的改动；
- 除非任务明确要求，否则保持公共契约不变；
- 业务/领域逻辑不得依赖外部 Provider Library；
- Compiler、Validator、Normalizer 和 Policy 的确定性转换逻辑必须实现为纯函数；I/O、数据库和 Provider 调用必须放在独立服务或 Adapter 中；
- Provider 特有行为必须放在 Port/Adapter 之后；
- 使用带类型错误和明确状态迁移；
- 没有 Contract Test 或当前使用场景时，不得提前加入推测性的多 Provider 代码；
- 能从代码生成 Schema 时，不得在代码与文档中维护多份重复定义；
- 不得重写无关文件或进行大范围纯格式化修改。

### 11.3 存在不确定性时

只允许使用本 `AGENTS.md`、架构文档、公共 Schema 或测试中已经定义的默认值。不得自行创造“保守默认值”。

无法由现有事实确定时：

- 不影响当前实现的内容必须删除；
- 会阻断正确实现的内容必须停止对应实现并请求唯一决策。

以下歧义始终属于阻断项：

- 安全边界；
- 破坏性操作；
- 公共 Schema 兼容性；
- 持久化格式；
- 架构不变量；
- 高成本 GPU 执行；
- 超出 MVP 的范围。

### 11.4 修改完成后

1. 先运行最小相关测试；
2. 运行所需单元测试和 Contract Test；
3. 对修改过的 Python 代码运行静态检查；
4. 仅在依赖可用时运行集成测试；
5. 未获得明确批准且未确认 GPU 空闲时，不得运行 GPU 或高成本性能测试；
6. 重新检查 Diff，确认没有架构和安全回归；
7. 汇报已运行测试、跳过测试、假设和剩余风险。

没有实际执行的测试，不得声称通过。

### 11.5 Git 与分阶段提交

- 仓库必须使用 Git 管理，默认开发分支为 `main`；
- 开发计划必须拆分为可独立验收的阶段，每个阶段完成实现、相关测试和 Diff 复查后立即提交；
- 每个提交只包含一个明确的架构或业务关注点，不得混入无关格式化、重构或用户未授权的现有改动；
- 提交信息统一使用 `type(scope): description` 格式，`type` 仅使用 `feat`、`fix`、`test`、`docs`、`refactor`、`build`、`ci` 或 `chore`；
- 提交前必须运行当前改动要求的最小相关测试；阶段收尾提交前还必须运行第 13.3 节定义的全量标准检查；
- 不得提交 Secret、本地环境文件、模型权重、测试原始大文件、数据库数据或可再生成的构建产物；
- 提交后必须记录提交 Hash 和已执行的验证；未经用户明确授权，不得改写已共享历史或强制推送。

---

## 12. 仓库与代码规范

本项目必须使用以下目录基线。已有代码与该结构冲突时，只在当前任务涉及的范围内迁移，不得进行无关的大规模重构：

```text
src/autopilot/
├── api/
├── graph/
├── gateway/
├── domain/
├── capabilities/
├── jobs/
├── evidence/
├── policy/
└── infrastructure/

runner/
policies/
tests/
├── unit/
├── contract/
├── integration/
└── gpu/
docs/
```

Python 技术基线固定为：

- Python 3.12，并在 `pyproject.toml` 中固定 `requires-python = ">=3.12,<3.13"`；
- `uv` 作为唯一依赖管理器和命令入口；
- FastAPI；
- Pydantic v2；
- LangGraph；
- SQLAlchemy 2；
- Alembic；
- psycopg 3；
- pytest；
- Ruff，统一承担 Lint 和 Format；
- mypy，作为唯一 Python 静态类型检查器。

不得引入第二套包管理器、ORM、数据库迁移工具、格式化器、Lint 工具、类型检查器或测试运行器。

### 12.1 Python 风格

- 公共接口必须有完整类型标注；
- 领域边界不得使用无类型字典；
- Domain 和 Application 代码必须依赖 `Protocol`/Port，不得直接导入 Provider 实现；
- 使用带时区的 UTC 时间；
- 持久化状态使用稳定字符串 Enum；
- 存储大小统一使用 Byte；延迟统一使用毫秒；持续时间统一使用秒；Token 数量统一使用整数；
- 禁止无分类、无清理、无重新抛出或映射的宽泛 `except Exception`；
- Job、Lock 和 Provider Client 不得依赖隐藏的全局可变状态。

### 12.2 数据库与迁移

- 数据库初始结构和每次 Schema 变化都必须通过 Alembic Revision 创建；不得使用 `create_all` 作为正式建库或迁移机制；
- 已提交或已执行的 Migration 不得修改，只能新增 Migration；
- 发出成功事件前必须先持久化状态；
- Audit Event 只追加，不更新；
- 大结果保存为 Artifact 和引用，不得无明确理由存为数据库 Blob。

---

## 13. 测试策略

### 13.1 必需测试层级

每项修改必须按照以下映射增加测试：

- 修改 Domain Rule、Schema、Validator 或 Policy：增加单元测试；
- 修改 Compiler 或指标归一化：增加 Golden Test；
- 修改或新增 Adapter：增加 Adapter Contract Test；
- 修改 API、Database、Gateway、Job 或 Runner 边界：增加集成测试；
- 修改真实 GPU 部署或 Benchmark 路径：在获得批准后增加并运行 GPU 端到端测试。

### 13.2 Fake Adapter

Graph 和工作流测试必须使用 Fake Adapter。不得为了测试可确定的状态迁移而占用 RTX 5090。

### 13.3 必须执行的检查命令

代码修改完成后必须运行：

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/autopilot runner
uv run pytest tests/unit tests/contract
```

涉及 API、数据库、Gateway、Runner 或 Adapter 边界的修改，必须运行对应集成测试；若依赖服务无法启动，必须在最终报告中明确列出阻断原因。`tests/gpu` 只有在用户明确批准、GPU 0 空闲且预算已配置后才能运行。

上述命令是本项目唯一标准入口。发现旧命令或并行工具链时，必须在当前任务涉及范围内迁移到该入口，并删除对应的重复配置；不得保留并行命令体系。

### 13.4 GPU 测试安全

运行 GPU 测试前：

- 确认用户已批准；
- 确认 GPU 0 空闲或由当前 Experiment 持有；
- 记录驱动、镜像 Digest 和模型 Revision；
- 设置超时和 Token Budget；
- 确保所有退出路径都会清理容器并释放锁。

---

## 14. 完成定义

一项改动只有同时满足以下条件才算完成：

- 实现了用户要求的行为；
- 架构不变量仍然成立；
- 输入输出有类型，持久化契约有版本；
- 具备失败、取消和清理路径；
- 需要时正确执行授权与审批；
- 需要时记录审计与证据；
- 相关单元测试和 Contract Test 通过；
- 集成测试和 GPU 测试已运行，或明确说明跳过；
- 公共行为变化时已更新文档或生成 Schema；
- 未意外引入不受支持的 MVP 范围；
- 最终总结列明修改文件、测试、假设和剩余风险。

涉及安全或 GPU 执行的改动，还必须验证回滚或清理路径。

---

## 15. 禁止的实现捷径

不得：

- 把整个 MVP 写成一个超大 Agent Prompt；
- 实现一个巨型 `run_everything()`；
- 把外部项目全部 CLI Flag 暴露为 Tool 输入；
- 为所谓“灵活性”增加任意命令 Tool；
- 在存在稳定程序化 API 时解析人类可读 CLI 输出；
- 只在进程内存中保存全部状态；
- 把 MLflow 当成工作流事实源；
- 把 OPA 当成可选建议调用；
- 让 LLM 决定实测指标或 Pass/Fail 约束；
- 静默吞掉失败 Trial；
- 在可复现实验中使用 `latest` 镜像；
- 自动开启 `trust_remote_code`；
- 在性能测量期间让 Agent 模型占用被测 GPU；
- 在单 GPU 契约尚未完成前加入 Kubernetes 或多 GPU 代码；
- 在没有固定条件下 Champion 定义时声称“最优”。

---

## 16. 文档与变更汇报

当修改架构、公共 Schema、安全策略、Provider 行为或验收标准时：

- 更新 `docs/llm-inference-autopilot-mvp-docs/` 中对应文档；
- 若变更改变既有不变量，新增或更新 ADR；
- 文档中的公共接口示例必须由代码生成 Schema 校验，二者不得不一致；
- 说明变更是否向后兼容；
- 说明已有已审批 Plan 或既有 Evidence 是否仍然有效。

实现类任务的最终回复格式：

```text
已实现
- ...

关键设计决策
- ...

验证
- command: result
- command: result

已跳过
- 测试及原因

风险 / 后续事项
- ...
```

汇报必须基于事实。不得声称未验证的完成度、兼容性、性能或安全性。

---

## 17. 未来的局部 AGENTS.md

根文件只保留全局不变量。以下目录首次加入业务代码时，必须同时创建对应的局部 `AGENTS.md`：

```text
runner/AGENTS.md
    高权限执行、Docker 白名单、锁与清理规则

src/autopilot/capabilities/AGENTS.md
    能力包、Port、Adapter 与 Schema 契约

src/autopilot/graph/AGENTS.md
    LangGraph State、Checkpoint、Interrupt 与 Reconciliation 规则

src/autopilot/gateway/AGENTS.md
    可见性、OPA、审批、预算与幂等规则

tests/gpu/AGENTS.md
    明确审批、预算、模型 Fixture 与清理要求

policies/AGENTS.md
    Rego 风格、默认拒绝和 Policy Golden Test
```

更深层目录的文件只能增加更严格的局部规则，不得削弱全局安全和架构不变量。需要改变全局不变量时，必须先修改根 `AGENTS.md` 和对应 ADR。
