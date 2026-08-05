# 02. Agent 工作流与能力编排设计

## 1. 设计目标

Agent 层要解决的是：

- 理解用户目标；
- 将自然语言转换为结构化要求；
- 在已允许的领域能力之间制定实验策略；
- 在需要时向用户解释、询问或请求审批；
- 根据结构化结果选择下一步；
- 生成可理解的报告。

Agent 层不负责：

- 拼 Shell；
- 直接操作 Docker；
- 自己给自己授权；
- 计算资源锁；
- 实现 Optuna 采样；
- 解析非结构化日志作为唯一事实；
- 绕过状态机。

---

## 2. 为什么使用一个主 Graph，而不是多个自由 Agent

MVP 中的步骤高度依赖、资源稀缺且具有明确顺序：

```text
环境事实 → 容量候选 → 部署 → 测试 → 调优 → 复测 → 选择
```

多 Agent 会带来：

- 上下文复制；
- 责任边界模糊；
- 工具重复暴露；
- 更复杂的恢复；
- 对单卡 MVP 没有直接收益。

因此首版采用：

- 一个主 LangGraph；
- 多个确定性 Node；
- 4 类 LLM Node；
- 可选 Subgraph，但不是多 Agent 自治。

---

## 3. LLM Node 和确定性 Node

### 3.1 LLM Node

| Node | 作用 | 输出必须结构化 |
|---|---|---|
| `parse_requirements` | 从用户描述提取场景、长度、SLO 和优先级 | `RequirementSpec` |
| `propose_test_strategy` | 在支持的测试模式中建议测试组合 | `BenchmarkIntent` |
| `analyze_failure` | 根据错误分类和日志摘要给出可解释建议 | `FailureAnalysis` |
| `write_report` | 将已验证证据转换成报告 | Markdown，不得改变指标 |

### 3.2 确定性 Node

- `inspect_environment`
- `validate_environment`
- `inspect_model`
- `estimate_capacity`
- `generate_candidates`
- `authorize_action`
- `deploy_candidate`
- `health_check`
- `compile_benchmark_plan`
- `run_benchmark`
- `normalize_metrics`
- `create_optuna_study`
- `execute_trial`
- `check_convergence`
- `verify_top_candidates`
- `select_champion`
- `archive_evidence`

原则：

> 任何会改变宿主机状态、消耗大量资源或改变最终指标的步骤，都必须是确定性 Node。

---

## 4. Graph 状态设计

```python
from typing import TypedDict, Literal

class AutopilotState(TypedDict, total=False):
    schema_version: str
    thread_id: str
    experiment_id: str

    phase: Literal[
        "requirements",
        "environment",
        "planning",
        "approval",
        "deployment",
        "benchmark",
        "optimization",
        "verification",
        "report",
        "completed",
        "failed",
        "cancelled",
    ]

    requirements: dict
    hardware_passport_ref: str
    model_profile_ref: str
    workload_spec: dict
    slo_spec: dict

    candidates: list[dict]
    active_candidate_id: str | None
    active_deployment_id: str | None
    active_job_id: str | None
    active_study_id: str | None

    approval_request: dict | None
    approval_decision: dict | None

    benchmark_summary_refs: list[str]
    trial_refs: list[str]
    champion_ref: str | None

    retry_count: int
    last_error: dict | None
    warnings: list[dict]
```

### 4.1 State 中应保存什么

保存：

- 对后续步骤必要的结构化事实；
- Artifact 引用；
- ID；
- 当前阶段；
- 关键决策；
- 重试和错误信息。

不保存：

- 大型原始报告全文；
- 模型权重；
- Secret；
- 全部日志；
- 可通过 ID 查询的大对象。

---

## 5. Graph 主流程

```text
START
  ↓
parse_requirements
  ↓
requirements_gate
  ├─ 缺少关键字段 → ask_user / interrupt
  └─ 完整
       ↓
inspect_environment
       ↓
environment_gate
  ├─ 阻断风险 → explain_and_end
  └─ 通过
       ↓
estimate_capacity
       ↓
generate_candidates
       ↓
deployment_approval / interrupt
  ├─ reject → revise_or_end
  └─ approve
       ↓
deploy_and_smoke_test
       ↓
benchmark_baseline
       ↓
benchmark_strategy
       ↓
optimization_loop
       ↓
verification
       ↓
champion_approval / interrupt
       ↓
write_report
       ↓
END
```

---

## 6. 条件边设计

### 6.1 环境 Gate

```python
def route_environment(state):
    severity = state["environment_assessment"]["max_severity"]
    if severity == "blocker":
        return "environment_blocked"
    if severity == "warning":
        return "environment_review"
    return "capacity_planning"
```

阻断示例：

- GPU 不可见；
- Docker GPU Test 失败；
- 显存被其他进程长期占用；
- 模型存储不足；
- 驱动与固定镜像不兼容；
- 目标模型配置无法解析。

### 6.2 Trial Gate

```text
Candidate Schema 校验
  ↓
静态显存/参数约束
  ↓
OPA 和预算
  ↓
部署
  ↓
健康检查
  ↓
Benchmark
  ↓
有效结果？
  ├─ 否：失败分类并记录
  └─ 是：提交给 Optuna
```

---

## 7. 长任务协议

所有长任务统一为异步 Job。

### 7.1 启动

```json
{
  "job_id": "bench_01J...",
  "kind": "benchmark",
  "status": "queued",
  "submitted_at": "2026-08-05T10:00:00Z"
}
```

### 7.2 查询

```json
{
  "job_id": "bench_01J...",
  "status": "running",
  "stage": "closed_loop",
  "progress": {
    "completed_units": 3,
    "total_units": 6
  },
  "latest_message": "parallel=8"
}
```

### 7.3 完成

```json
{
  "job_id": "bench_01J...",
  "status": "succeeded",
  "result_ref": "artifact://experiments/e1/benchmarks/b1/summary.json"
}
```

统一状态：

```text
queued → validating → waiting_approval → running
      → succeeded | failed | cancelled | timed_out
```

---

## 8. Interrupt 和人工审批

必须使用审批的动作：

- 首次下载大型模型；
- 启动或替换 vLLM；
- 停止当前模型服务；
- 开始超过阈值的压测；
- 开始完整调优；
- 将 Champion 标记为推荐配置；
- 删除缓存或工件。

Interrupt Payload：

```json
{
  "approval_id": "apr_01J...",
  "action": "start_deployment",
  "risk_level": "L2",
  "summary": "将停止当前 vLLM 容器并以新参数启动",
  "resource_impact": {
    "gpu": "GPU-0 exclusive",
    "estimated_vram_gb": 28.4,
    "estimated_disk_download_gb": 16.2
  },
  "expires_at": "2026-08-05T12:00:00Z"
}
```

审批结果：

```json
{
  "decision": "approve",
  "actor": "user-id",
  "comment": "允许本次部署",
  "approved_plan_hash": "sha256:..."
}
```

审批绑定 Plan Hash，计划改变后必须重新审批。

---

## 9. 动态工具可见性

Tool 可见性由当前 State、用户角色、环境能力和策略共同决定。

```python
visible_tools = registry.resolve(
    phase=state["phase"],
    role=user.role,
    capabilities=hardware.capabilities,
    policy_context=policy_context,
)
```

### 9.1 示例

| 阶段 | AI 可见 Tool |
|---|---|
| `requirements` | `get_mvp_capabilities_result`、`create_experiment_plan` |
| `environment` | `create_environment_plan`、`start_environment_inspection`、`get_environment_status`、`get_environment_result`、`cancel_environment_inspection` |
| `planning` | `create_capacity_plan` |
| `approval` | `start_approval_request`、`get_approval_status` |
| `deployment` | `create_deployment_plan`、`start_deployment`、`get_deployment_status`、`get_deployment_result`、`cancel_deployment` |
| `benchmark` | `create_benchmark_plan`、`start_benchmark`、`get_benchmark_status`、`get_benchmark_result`、`cancel_benchmark` |
| `optimization` | `create_optimization_plan`、`start_optimization`、`get_optimization_status`、`get_optimization_result`、`cancel_optimization` |
| `verification` | `get_trial_comparison_result`、`create_champion_plan` |
| `report` | `get_evidence_result` |

AI 永远看不到：

- `docker_run_raw`
- `execute_shell`
- `opa_allow`
- `acquire_gpu_lock`
- `compile_evalscope_cli`
- `optuna_suggest`
- `mlflow_log_raw`

---

## 10. AI 自由度的三级模式

### 10.1 Preset

AI 选择领域模式，系统补齐全部参数。

```json
{
  "mode": "closed_loop_sweep",
  "profile": "balanced"
}
```

### 10.2 Guided

AI 可设置白名单参数和受限范围。

```json
{
  "mode": "closed_loop_sweep",
  "concurrency_levels": [1, 2, 4, 8],
  "duration_seconds": 120,
  "advanced": {
    "warmup_ratio": 0.1
  }
}
```

### 10.3 Expert Override

只向内部角色开放，且必须说明理由。

```json
{
  "plan_id": "plan_01J...",
  "overrides": {
    "log_every_n_query": 10
  },
  "reason": "定位失败请求发生位置"
}
```

Expert Override 仍然：

- 只允许注册字段；
- 有类型和范围；
- 不允许任意键；
- 不允许 Shell；
- 记录审计日志；
- 可能触发额外审批。

---

## 11. 失败处理

错误分为：

| 分类 | 示例 | 默认行为 |
|---|---|---|
| `validation_error` | 参数非法 | 不执行，返回字段级错误 |
| `policy_denied` | 高风险操作 | 停止并解释 |
| `resource_busy` | GPU 被占用 | 等待或取消 |
| `deployment_error` | 容器启动失败 | 收集日志并回收 |
| `model_incompatible` | Kernel/量化不兼容 | 标记 Candidate 无效 |
| `benchmark_error` | API 超时、数据集错误 | 可有限重试 |
| `oom` | CUDA OOM | 停止容器，缩小搜索边界 |
| `quality_gate_failed` | 输出异常 | Candidate 淘汰 |
| `infrastructure_error` | Docker/磁盘故障 | 阻断 Experiment |
| `unknown_error` | 未分类 | 不自动无限重试 |

每类错误必须包含：

```json
{
  "code": "BENCHMARK_OPEN_LOOP_RATE_INVALID",
  "category": "validation_error",
  "retryable": false,
  "user_message": "...",
  "technical_detail_ref": "artifact://...",
  "suggested_actions": []
}
```

---

## 12. 重试和幂等

### 12.1 幂等键

```text
idempotency_key =
sha256(action + experiment_id + normalized_input + tool_version)
```

### 12.2 不能盲目重试的动作

- 模型下载；
- 部署替换；
- 长压测；
- Optuna Trial；
- 删除操作。

调用方重复提交时先查现有 Job。

### 12.3 重试策略

- API 查询：指数退避；
- 健康检查：固定窗口；
- Benchmark 单请求网络错误：由 EvalScope/Adapter 有界重试；
- OOM：不原参数重试；
- 版本不兼容：不重试。

---

## 13. 可恢复执行

LangGraph Checkpoint 保存：

- 当前 Node；
- State 引用；
- 审批等待；
- active job ID；
- 上一步结果引用。

恢复时必须先做 Reconciliation：

1. 查询 Job 是否仍运行；
2. 查询 Docker 容器状态；
3. 查询 GPU 锁持有者；
4. 查询 MLflow Run；
5. 对齐数据库状态；
6. 再决定继续、补偿或失败。

不能只根据 Graph State 假设外部世界未变化。

---

## 14. 模型输出约束

LLM 生成的计划必须满足：

- Pydantic Schema；
- 禁止未知字段；
- 枚举使用固定值；
- 单位明确；
- 所有时间使用秒；
- 所有 Token 使用整数；
- 指标名称使用统一字典；
- 估算和实测字段分离。

LLM 不能生成最终性能数值，只能引用已有 Artifact。

---

## 15. 参考资料

- https://docs.langchain.com/oss/python/langgraph/overview
- https://docs.langchain.com/oss/python/langgraph/persistence
- https://docs.langchain.com/oss/python/langgraph/interrupts
- https://docs.langchain.com/oss/python/langgraph/use-subgraphs
- https://openai.github.io/openai-agents-python/human_in_the_loop/
