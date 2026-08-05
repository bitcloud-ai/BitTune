# 03. 能力包与 Tool 转换设计

## 1. 核心问题

一个外部项目通常包含几十到几百个参数和子命令。例如 EvalScope 同时支持：

- 不同 API；
- 闭环和开环；
- 并发扫描；
- Rate 扫描；
- SLA Auto-Tune；
- 多种数据集；
- 多轮；
- Trace Replay；
- 超时、Warmup、请求参数和输出存储。

以下两种设计都错误。

### 1.1 一个超级 Tool

```python
run_evalscope(mode, parallel, number, rate, dataset, extra_args, ...)
```

问题：

- 模式相关参数混在一起；
- AI 容易生成非法组合；
- Schema 太大；
- 版本变更影响 Prompt；
- 不适合异步 Job；
- 无法清晰区分计划、执行和查询。

### 1.2 每个参数一个 Tool

```text
set_parallel
set_rate
set_number
enable_open_loop
start
```

问题：

- Tool Call 数量过多；
- 形成半配置状态；
- 中途失败难恢复；
- 参数之间的原子性不足。

### 1.3 正确做法

> 一个环节选择一个主要外部项目；围绕该环节建设一个能力包；能力包对 AI 暴露多个按业务动作划分的窄 Tool；底层参数由 Compiler 和 Adapter 生成。

---

## 2. 四层转换架构

```text
Layer 1  Agent Tool / 领域动作
         create_benchmark_plan

Layer 2  Domain Model / 领域计划
         BenchmarkPlan + WorkloadSpec + SLOSpec

Layer 3  Compiler + Validator
         EvalScopeArgumentCompiler

Layer 4  Adapter / Executor
         EvalScope Python API、CLI 或容器任务
```

每层职责必须单一。

---

## 3. 能力包标准结构

```text
capabilities/
└── benchmark/
    ├── manifest.yaml
    ├── domain/
    │   ├── models.py
    │   ├── enums.py
    │   └── errors.py
    ├── tools/
    │   ├── create_plan.py
    │   ├── start_job.py
    │   ├── get_status.py
    │   ├── get_result.py
    │   └── cancel_job.py
    ├── application/
    │   ├── planner.py
    │   ├── validator.py
    │   ├── compiler.py
    │   └── result_normalizer.py
    ├── adapters/
    │   └── evalscope/
    │       ├── adapter.py
    │       ├── version_profile.py
    │       └── parser.py
    └── tests/
```

---

## 4. Capability Manifest

```yaml
api_version: autopilot/v1
kind: CapabilityPackage

metadata:
  name: benchmark
  package_version: 0.1.0

provider:
  name: evalscope
  version_constraint: "pinned-by-image"
  adapter_version: 0.1.0

requires:
  phases: [benchmark, optimization, verification]
  resources:
    cpu: true
    network: true
    gpu: false
  environment_capabilities:
    - openai_compatible_endpoint

tools:
  - name: create_benchmark_plan
    visibility: dynamic
    risk_level: L0
  - name: start_benchmark
    visibility: dynamic
    risk_level: L2
  - name: get_benchmark_status
    visibility: dynamic
    risk_level: L0
  - name: get_benchmark_result
    visibility: dynamic
    risk_level: L0
  - name: cancel_benchmark
    visibility: dynamic
    risk_level: L2

supports:
  modes:
    - baseline
    - closed_loop_sweep
    - open_loop_sweep
    - sla_search
```

---

## 5. Tool 设计原则

### 5.1 Tool 粒度

一个 Tool 应对应一个完整业务动作：

- 创建完整计划；
- 启动一个 Job；
- 获取一个 Job 状态；
- 获取一个标准化结果；
- 取消一个 Job。

### 5.2 Tool 输入

必须：

- 字段少且语义明确；
- 使用 Enum；
- 使用判别联合；
- 禁止未知字段；
- 有默认值和范围；
- 具有 `schema_version`；
- 不出现容器命令。

### 5.3 Tool 输出

必须：

- 返回结构化对象；
- 返回稳定 ID；
- 长结果返回 Artifact Ref；
- 明确 `source=estimated|measured|derived`；
- 明确 `status`；
- 错误使用统一 Error Envelope。

### 5.4 Tool 描述

描述应告诉 AI：

- 何时调用；
- 何时不能调用；
- 前置条件；
- 产生什么；
- 是否改变环境；
- 是否需要审批。

不要把完整 CLI 帮助文档塞进 Tool Description。

---

## 6. 计划和执行分离

### 6.1 Plan Tool

特点：

- 只读；
- 不消耗大量资源；
- 可反复修改；
- 返回完整计划和预计影响；
- 可以做人审。

### 6.2 Execute Tool

特点：

- 输入必须是已保存的 `plan_id`；
- 不允许 AI 在执行时临时增加任意参数；
- Tool Gateway 验证 Plan Hash；
- 需要时请求审批；
- 返回 Job ID。

```json
{
  "plan_id": "bench_plan_01J...",
  "expected_plan_hash": "sha256:..."
}
```

这样可避免“用户审批 A，执行时 AI 改成 B”。

---

## 7. 判别联合

以 Benchmark 为例：

```python
from typing import Annotated, Literal, Union
from pydantic import BaseModel, Field, ConfigDict

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

class BaselineTraffic(StrictModel):
    mode: Literal["baseline"]
    requests: int = Field(default=5, ge=1, le=20)

class ClosedLoopTraffic(StrictModel):
    mode: Literal["closed_loop_sweep"]
    concurrency_levels: list[int]
    requests_per_worker: int = Field(default=5, ge=2, le=100)
    pacing_rps: float | None = Field(default=None, gt=0)

class OpenLoopTraffic(StrictModel):
    mode: Literal["open_loop_sweep"]
    request_rates: list[float]
    duration_seconds: int = Field(ge=30, le=1800)
    arrival_pattern: Literal["poisson"] = "poisson"

class SlaSearchTraffic(StrictModel):
    mode: Literal["sla_search"]
    search_variable: Literal["parallel", "rate"]
    lower_bound: int = Field(ge=1)
    upper_bound: int = Field(ge=2)
    runs_per_level: int = Field(default=3, ge=1, le=10)

TrafficSpec = Annotated[
    Union[
        BaselineTraffic,
        ClosedLoopTraffic,
        OpenLoopTraffic,
        SlaSearchTraffic,
    ],
    Field(discriminator="mode"),
]
```

收益：

- 闭环不会携带 `request_rates`；
- 开环不会携带 `concurrency_levels`；
- 非法组合在进入 Adapter 前被拒绝；
- JSON Schema 对 Agent 清晰；
- 不同模式可独立演进。

---

## 8. Preset、Guided、Expert

### 8.1 Preset

```json
{
  "profile": "standard_closed_loop",
  "workload": {
    "prompt_tokens": 2048,
    "output_tokens": 512
  }
}
```

Compiler 自动生成：

- 并发档位；
- 每档请求数；
- Warmup；
- 休息时间；
- Token Budget。

### 8.2 Guided

AI 可修改公开领域参数：

```json
{
  "mode": "open_loop_sweep",
  "request_rates": [0.5, 1, 2, 4],
  "duration_seconds": 120
}
```

### 8.3 Expert

```json
{
  "overrides": {
    "log_every_n_query": 10
  },
  "reason": "需要更细的执行日志"
}
```

Expert 字段来自 Version Profile 白名单：

```yaml
evalscope:
  version: pinned
  allowed_overrides:
    log_every_n_query:
      type: integer
      min: 1
      max: 1000
    sleep_interval:
      type: integer
      min: 0
      max: 60
```

严禁通用：

```json
{"extra_args": {"任意键": "任意值"}}
```

---

## 9. Capability Discovery

Agent 不需要知道底层版本的所有参数，但可以查询领域能力：

```json
{
  "capability": "benchmark",
  "provider": "evalscope",
  "supported_modes": [
    "baseline",
    "closed_loop_sweep",
    "open_loop_sweep",
    "sla_search"
  ],
  "limits": {
    "max_duration_seconds": 1800,
    "max_total_requests": 10000,
    "max_total_tokens": 50000000
  }
}
```

这个查询来自 Registry，不直接读取 CLI Help。

---

## 10. Tool Gateway 流程

```text
1. 接收 Tool Call
2. 查找 Capability Manifest
3. 验证 Tool 是否对当前 State 可见
4. 验证调用方角色
5. JSON Schema/Pydantic 校验
6. 领域规则校验
7. 预算评估
8. OPA 决策
9. 必要时创建审批
10. 幂等检查
11. 获取资源锁
12. 调用 Capability Service
13. 记录 Audit/Event
14. 返回结构化结果
```

Tool Gateway 必须是执行的唯一入口。

---

## 11. Compiler 设计

Compiler 是纯函数优先：

```python
compiled = compiler.compile(
    domain_plan=plan,
    provider_profile=evalscope_profile,
)
```

输出：

```json
{
  "provider": "evalscope",
  "provider_version": "pinned",
  "api": "openai",
  "parallel": [1, 2, 4, 8],
  "number": [5, 10, 20, 40],
  "rate": -1,
  "open_loop": false,
  "stream": true,
  "warmup_num": 0.1
}
```

Compiler 不执行命令，不读取 Secret。

---

## 12. Adapter 设计

统一接口：

```python
class BenchmarkAdapter(Protocol):
    def capabilities(self) -> BenchmarkCapabilities: ...
    def validate(self, compiled: CompiledBenchmark) -> ValidationReport: ...
    def start(self, compiled: CompiledBenchmark, context: JobContext) -> ProviderJob: ...
    def status(self, provider_job_id: str) -> ProviderStatus: ...
    def cancel(self, provider_job_id: str) -> None: ...
    def collect(self, provider_job_id: str) -> RawArtifactSet: ...
    def normalize(self, artifacts: RawArtifactSet) -> BenchmarkResult: ...
```

未来更换 GuideLLM/AIPerf 时，Agent Tool 和领域 Plan 不变，只新增 Adapter 和支持矩阵。

---

## 13. Result Normalization

外部工具指标名称不同，内部统一为：

```json
{
  "latency": {
    "e2e_ms": {"p50": 0, "p95": 0, "p99": 0},
    "ttft_ms": {"p50": 0, "p95": 0, "p99": 0},
    "tpot_ms": {"p50": 0, "p95": 0, "p99": 0},
    "itl_ms": {"p50": 0, "p95": 0, "p99": 0}
  },
  "throughput": {
    "request_per_second": 0,
    "successful_request_per_minute": 0,
    "input_tokens_per_second": 0,
    "output_tokens_per_second": 0,
    "total_tokens_per_minute": 0
  },
  "reliability": {
    "submitted": 0,
    "completed": 0,
    "failed": 0,
    "timed_out": 0,
    "window_completion_ratio": 0
  },
  "source": "measured"
}
```

必须区分：

- 发起请求数；
- 完成请求数；
- 测试窗口内完成率；
- 允许在 Soft Duration 后完成的请求；
- 成功吞吐和调度速率。

---

## 14. Error Envelope

```json
{
  "error": {
    "code": "CAPABILITY_VALIDATION_FAILED",
    "category": "validation_error",
    "message": "open_loop_sweep requires positive request_rates",
    "field_errors": [
      {
        "path": "traffic.request_rates",
        "reason": "must_not_be_empty"
      }
    ],
    "retryable": false,
    "provider": "evalscope",
    "provider_detail_ref": null
  }
}
```

AI 只能根据错误建议修订领域计划，不能拿 Provider Detail 自由拼命令。

---

## 15. 扩展原则

新增外部工具时：

1. 先判断是新领域能力还是同能力的新 Provider；
2. 同能力优先新增 Adapter；
3. 不为了某 Provider 污染领域 Schema；
4. Provider 特有能力放到 Capability Extension；
5. Extension 需要版本和可见性；
6. UI、Agent 和工作流依赖领域接口，不依赖 CLI。

---

## 16. 参考资料

- EvalScope Parameters: https://evalscope.readthedocs.io/en/latest/user_guides/stress_test/parameters.html
- EvalScope Quick Start: https://evalscope.readthedocs.io/en/latest/user_guides/stress_test/quick_start.html
- LangGraph Overview: https://docs.langchain.com/oss/python/langgraph/overview
