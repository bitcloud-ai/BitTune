# 01. MVP 总体设计与技术选型

## 1. 背景

目标产品是一个进入客户环境后，能够协助完成以下工作的推理 Autopilot：

1. 环境检测；
2. 模型与容量规划；
3. 模型部署；
4. 性能测试；
5. 参数调优；
6. 选出满足约束的最佳已验证配置；
7. 保存证据并安全执行。

长期产品需要覆盖多厂商、多 GPU、多引擎和 Kubernetes，但当前资源只有一台机器和一张 RTX 5090，因此 MVP 必须先验证最核心的闭环，而不是提前建设集群平台。

---

## 2. MVP 硬件和环境基线

### 2.1 已知硬件

- GPU：NVIDIA GeForce RTX 5090；
- 显存：32 GB；
- GPU 数量：1；
- GPU 资源模型：独占；
- 推理并行：`tensor_parallel_size = 1`；
- Agent 模型：默认远程 API，不能与被测模型争用 GPU。

### 2.2 推荐宿主机环境

- 原生 Linux，优先 Ubuntu 24.04 LTS；
- NVIDIA 驱动与 RTX 5090、目标 CUDA/vLLM 镜像兼容；
- Docker Engine + Docker Compose Plugin；
- NVIDIA Container Toolkit；
- 至少 64 GB 系统内存，推荐 128 GB；
- 模型缓存和实验工件建议预留至少 500 GB NVMe；
- 固定一个经过验证的 vLLM 容器镜像 Digest。

其中系统内存和磁盘属于工程建议，不是硬性产品规格；应根据目标模型和本地模型缓存规模调整。

### 2.3 为什么不以 Windows 原生为 MVP 基线

推理部署工具通常首先针对 Linux 和容器环境验证。即使 Windows/WSL2 可以用于开发，客户环境执行器仍建议以 Linux 为正式基线，以减少 GPU Runtime、文件系统、网络和进程控制差异。

---

## 3. MVP 产品目标

### 3.1 输入

用户提供：

- 模型 ID 或本地模型路径；
- 主要业务场景；
- 典型输入和输出 Token 长度；
- 希望的 TTFT、TPOT、成功率或吞吐目标；
- 调优时间或 Trial 预算；
- 是否允许下载模型和启动容器。

### 3.2 输出

系统生成：

1. `hardware-passport.json`；
2. 模型结构与容量分析；
3. 2～5 个初始 Candidate；
4. 可审阅的 vLLM 部署计划；
5. 基线、闭环、开环或 SLA 测试结果；
6. Optuna Trial 列表；
7. Top 候选重复验证结果；
8. Champion、备选方案和未满足约束说明；
9. Evidence Bundle；
10. 可复制的最终启动配置。

### 3.3 MVP 成功定义

满足以下条件即视为闭环跑通：

- 环境检测结果可重复；
- 模型能根据规划结果成功启动；
- EvalScope 测试参数来自结构化计划而不是人工 Shell；
- 至少完成 10 个有效 Trial；
- 非法 Trial 能在部署前被拒绝或在失败后自动回收；
- Top 3 候选至少重复测试两次；
- Champion 选择可追溯到明确 SLO 与原始数据；
- Agent/API 重启后能够恢复 Experiment；
- AI 无法绕过审批直接执行高风险动作。

---

## 4. MVP 非目标

第一版明确不支持：

- 多 GPU TP/PP/EP；
- 多节点；
- Prefill/Decode 分离；
- Kubernetes、KServe、llm-d Runtime、Dynamo；
- 多副本自动扩缩容；
- 在线无审批修改生产配置；
- 任意推理引擎；
- 任意 EvalScope CLI 参数透传；
- 驱动、内核和系统包自动修改；
- 复杂 MoE 和实验性量化作为首条支持路径；
- 证明全局数学最优。

---

## 5. Agent 框架选型

### 5.1 候选

| 方案 | 优点 | 主要问题 | MVP 结论 |
|---|---|---|---|
| Codex CLI | 代码理解、修改、执行和开发效率高 | 核心定位是 Coding Agent；实验状态、资源锁和产品 API 需重建 | 用于开发，不作产品运行时 |
| OpenCode | 开源、可扩展、具备工具和权限机制 | 仍围绕代码、Shell、文件；不应让其直接拥有宿主机执行权 | 不作核心运行时 |
| LangChain | 组件生态丰富 | 工作流本身主要由 LangGraph 承担 | 按需使用组件 |
| LangGraph | 显式状态、持久化、中断、恢复、条件边、子图 | 需要自行设计领域状态和节点 | **选择** |
| OpenAI Agents SDK | 工具、Session、Tracing、HITL 开发简单 | 自由 Agent Loop 不等于实验状态机；仍需外围工作流 | 备选 |
| Google ADK | 多 Agent、工作流、工具与部署生态完整 | 当前单机 MVP 不需要其多 Agent/Google Cloud 方向 | 暂不选择 |

### 5.2 最终选择

- 产品运行时：LangGraph；
- Agent/LLM 接口：抽象为 `ModelProvider`，首版可接 OpenAI-compatible API；
- 不以多 Agent 为 MVP 目标；
- 一个主 Graph + 若干确定性节点 + 少量 LLM 节点。

### 5.3 选择 LangGraph 的直接原因

本业务需要：

- 长时间任务；
- 测试和调优循环；
- 人工审批；
- 故障后恢复；
- 根据结果走不同分支；
- 保存每一步结构化状态；
- 动态控制可见工具；
- 将 LLM 节点和确定性节点混合。

LangGraph 的 Checkpoint、Interrupt 和 StateGraph 与这些要求直接匹配。

---

## 6. 七个环节的主要项目

“一个环节一个工具”指每个环节选择一个主要外部项目，不表示只向 AI 暴露一个函数。

| 环节 | 主要外部项目 | 你们自研的核心 |
|---|---|---|
| 1. 环境检测 | NVIDIA NVML | Hardware Passport、OS/CPU/磁盘采集、风险规则 |
| 2. 容量规划 | llm-d Planner 的 Capacity Planner | 统一 Candidate、置信度、适配 RTX 5090 的约束与验证 |
| 3. 模型部署 | vLLM | 配置编译、容器生命周期、GPU 锁、回滚 |
| 4. 性能测试 | EvalScope | Workload 编译、模式约束、结果归一化 |
| 5. 参数调优 | Optuna | 搜索空间裁剪、Trial 生命周期、失败分类 |
| 6. 收敛和证据 | MLflow Tracking | Champion Policy、复测策略、Evidence Bundle |
| 7. 安全控制 | Open Policy Agent | 风险分级、审批流、Tool Gateway 强制执行 |

---

## 7. 为什么这些外部项目不能直接暴露给 AI

直接把 CLI 或全部参数给 AI 会造成：

- 参数组合不合法；
- 版本变化导致 Prompt 失效；
- AI 生成任意 Shell；
- 测试规模失控；
- 结果不可复现；
- 授权逻辑可被绕过；
- Agent 上下文被大量底层参数占满；
- 外部工具更换时上层全部重写。

因此必须增加领域转换层：

```text
用户意图
  ↓
LLM 生成领域计划
  ↓
Pydantic/JSON Schema 校验
  ↓
Tool Gateway：状态、权限、预算、资源锁
  ↓
Capability Package
  ↓
Adapter 编译为具体 API/CLI/容器参数
  ↓
外部项目
```

---

## 8. 整体架构

```text
┌──────────────────────────────────────────┐
│ Web UI / CLI                             │
│ 对话、计划审阅、审批、状态、结果对比       │
└───────────────────┬──────────────────────┘
                    │ HTTP / SSE
┌───────────────────▼──────────────────────┐
│ Autopilot API                            │
│ FastAPI + LangGraph + Pydantic           │
│                                          │
│ LLM 节点：需求结构化、解释、失败归因、报告  │
│ 确定性节点：检测、部署、测试、Trial、选择   │
└───────────────────┬──────────────────────┘
                    │ Tool Call
┌───────────────────▼──────────────────────┐
│ Tool Gateway                             │
│ Schema / State / OPA / Approval / Budget │
│ Idempotency / Audit / Dynamic Visibility │
└───────┬────────┬────────┬────────┬───────┘
        │        │        │        │
┌───────▼──┐ ┌───▼────┐ ┌─▼──────┐ ┌▼────────┐
│Capacity  │ │Deploy  │ │Benchmark│ │Optimize │
│Package   │ │Package │ │Package  │ │Package  │
└───────┬──┘ └───┬────┘ └─┬──────┘ └┬────────┘
        │        │        │        │
  llm-d Planner  vLLM   EvalScope  Optuna
                    │
             Host Runner / Docker
                    │
              RTX 5090 独占
```

MVP 的用户入口固定为 Click + Textual TUI。Click 提供安装入口和非交互命令，Textual 提供连续对话、
消息滚动、Markdown、Command Palette、状态栏和 SSE 实时事件。TUI 仅调用 FastAPI，不包含领域执行逻辑。
该决策见 `docs/adr/ADR-017-agent-tui-and-streaming.md`。

---

## 9. MVP 部署形态

### 9.1 Docker Compose 常驻服务

- `autopilot-api`：FastAPI + LangGraph；
- `autopilot-worker`：单 GPU PostgreSQL Lease Queue Worker，是唯一连接 Host Runner 的控制面服务；
- `postgres`：Graph Checkpoint、业务元数据、Optuna Storage、MLflow Backend；
- `mlflow`：实验查询和工件元数据；
- `opa`：策略判定；

MVP 不部署 Web UI；交互入口固定为本机 Click + Textual 客户端，通过 FastAPI REST/SSE 访问控制面。

### 9.2 宿主机服务

- `autopilot-runner.service`：systemd 管理；
- 只接受结构化任务；
- 管理 Docker 容器；
- 获取 GPU 独占锁；
- 采集 NVML；
- 不接受任意 Shell 字符串。

### 9.3 临时容器

- vLLM 服务容器；
- EvalScope Job 容器；
- 固定 Digest 的 llm-d Planner 一次性容器；
- 每个 Trial 使用明确的配置和输出目录。

### 9.4 为什么 Host Runner 不放进 Agent 容器

如果将 `/var/run/docker.sock` 挂载给 Agent 容器，Agent 实际上可获得接近宿主机 Root 的能力。分离 Runner 后，Agent 只能提交白名单动作，Runner 再做二次校验和执行。

---

## 10. 端到端工作流

```text
START
  ↓
collect_requirements                LLM + Schema
  ↓
inspect_environment                 deterministic
  ↓
inspect_model_and_plan_capacity     deterministic
  ↓
generate_initial_candidates         rules + optional LLM explanation
  ↓
review_plan                         interrupt / human approval
  ↓
deploy_candidate                    deterministic
  ↓
run_smoke_test                      deterministic
  ↓
run_baseline_and_sweep              deterministic
  ↓
create_optimization_study           deterministic
  ↓
trial_loop
  ├─ suggest parameters             Optuna
  ├─ validate candidate             rules
  ├─ deploy                         Runner
  ├─ benchmark                      EvalScope
  ├─ log evidence                   MLflow
  └─ continue / stop                convergence policy
  ↓
verify_top_candidates               repeated benchmark
  ↓
select_champion                     deterministic policy
  ↓
human_approval
  ↓
generate_report                     LLM reads structured evidence
  ↓
END
```

---

## 11. AI 自由度边界

### 11.1 AI 可以决定

- 把用户语言转换为 WorkloadSpec/SLOSpec；
- 在允许的模式中选择基线、闭环、开环、SLA 搜索；
- 建议测试顺序；
- 解释为什么需要补充测试；
- 基于结构化结果提出下一步建议；
- 生成面向用户的结论和风险说明。

### 11.2 AI 不能决定

- 任意 Shell；
- 任意 Docker 参数；
- 任意文件路径；
- 任意 EvalScope `extra_args`；
- 资源锁；
- OPA 结果；
- 自动跳过审批；
- 修改驱动或系统；
- 伪造测试结果；
- 直接把估算结果标记为实测结果。

---

## 12. MVP 搜索空间

首版只搜索：

| 参数 | 初始范围 |
|---|---|
| `gpu_memory_utilization` | 0.80～0.94 |
| `max_num_seqs` | 4、8、16、32 |
| `max_num_batched_tokens` | 2048、4096、8192、16384 |
| `enable_chunked_prefill` | true / false |

固定：

- 模型；
- 模型 Revision；
- 量化格式；
- vLLM 版本和镜像 Digest；
- `tensor_parallel_size=1`；
- 工作负载；
- 采样参数；
- 测试数据集；
- GPU 功耗和系统环境。

这样才能确保 Trial 之间可比。

---

## 13. Champion 定义

本系统不声称找到全局最优。

Champion 定义为：

> 在固定硬件快照、模型 Revision、推理引擎版本、工作负载、SLO、搜索空间和 Trial 预算下，已完成真机验证且满足全部硬约束的候选中，目标指标最佳并通过重复测试稳定性门槛的配置。

首版目标：

- 最大化成功输出 Token/s；
- 硬约束：TTFT P95、TPOT P95、成功率、无 OOM；
- Top 3 重复两次；
- 改进低于测试噪声时不宣称显著更优。

---

## 14. 主要风险

| 风险 | 处理 |
|---|---|
| RTX 5090 在部分新量化/Kernel 上兼容性快速变化 | 固定镜像 Digest；首版使用成熟 Dense/GQA 路径 |
| llm-d Planner 属于 incubating 项目 | 固定 Commit；只使用 Capacity Planner；估算必须实测 |
| 单卡无法验证多 GPU 能力 | 文档和 UI 明确标注非支持范围 |
| 远程 Agent 模型不可用于完全离线客户 | 保留 ModelProvider 接口；未来支持外部控制机或 CPU 小模型 |
| 压测污染被测环境 | Agent 不占 GPU；运行前检查其他 GPU 进程；独占锁 |
| AI 参数幻觉 | 判别联合 Schema、Compiler、Validator、Preset |
| 调优耗时和 Token 成本不可控 | Trial、时长、请求数、Token 总量四重预算 |
| 结果波动 | Warmup、固定数据、重复测试、噪声阈值 |

---

## 15. 参考资料

- LangGraph Overview: https://docs.langchain.com/oss/python/langgraph/overview
- LangGraph Persistence: https://docs.langchain.com/oss/python/langgraph/persistence
- LangGraph Interrupts: https://docs.langchain.com/oss/python/langgraph/interrupts
- Codex CLI: https://developers.openai.com/codex/cli
- OpenAI Agents SDK: https://openai.github.io/openai-agents-python/
- Google ADK: https://google.github.io/adk-docs/
- NVIDIA NVML: https://docs.nvidia.com/deploy/nvml-api/nvml-api-reference.html
- llm-d Planner: https://github.com/llm-d-incubation/llm-d-planner
- vLLM Docker: https://docs.vllm.ai/en/latest/deployment/docker/
- EvalScope Parameters: https://evalscope.readthedocs.io/en/latest/user_guides/stress_test/parameters.html
- Optuna: https://optuna.readthedocs.io/en/stable/
- MLflow Tracking: https://mlflow.org/docs/latest/tracking
- OPA: https://www.openpolicyagent.org/docs
