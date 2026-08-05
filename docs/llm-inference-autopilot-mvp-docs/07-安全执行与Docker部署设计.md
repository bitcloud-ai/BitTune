# 07. 安全执行与 Docker 部署设计

## 1. 威胁模型

主要风险：

1. Prompt Injection 诱导执行危险操作；
2. LLM 生成任意 Shell；
3. Docker Socket 导致宿主机权限泄露；
4. 模型仓库包含恶意自定义代码；
5. Secret 泄露到 Prompt、日志或 MLflow；
6. 压测导致资源耗尽；
7. 路径穿越或任意文件读取；
8. 未审批计划被替换；
9. 失控 Job 长时间占用 GPU；
10. 供应链版本漂移导致结果不可复现。

---

## 2. 安全边界

```text
Untrusted
- 用户自然语言
- LLM 输出
- 模型仓库元数据
- 外部工具日志

Validated
- Pydantic Domain Plan
- Capability Compiler Output

Trusted Control Plane
- Tool Gateway
- OPA Policy
- Approval Service
- Job Manager

Privileged Execution
- Host Runner
- Docker
- GPU
```

LLM 永远位于不可信区域。

---

## 3. 禁止能力

MVP 永久不提供以下 Agent Tool：

- `execute_shell`
- `run_python`
- `docker_run`
- `docker_exec`
- `delete_path`
- `install_driver`
- `apt_install`
- `pip_install`
- `kill_process`
- `modify_kernel`
- `mount_volume`

需要的操作由固定 Adapter 实现。

---

## 4. 认证与 Tool Gateway 强制路径

### 4.1 认证主体

- 外部请求唯一认证来源是至少 32 Byte CSPRNG 随机数据生成的 opaque Bearer Token；
- 部署配置只保存 `sha256:<64 lowercase hex>` Token Hash 到稳定 HumanSubject 的映射；
- 明文 Token 只经过 Secret/认证边界，不进入 Prompt、OPA、Graph、日志、Artifact 或数据库；
- HumanSubject 固定为 `UserId + viewer/operator/admin`，ServiceSubject 使用独立 `service_name` 且没有用户角色；
- 仅 human admin 可以决定 L2 Approval，Requester 与 Approver 必须是不同 UserId，Service 永远不能审批；
- MVP 不建立 users 表，也不实现 JWT、OAuth/OIDC 或 Session。

### 4.2 强制顺序

任何执行：

```text
Bearer Authentication / Service Identity
→ 持久化 Tool Set ID + Version 绑定
→ Tool 存在与可见性
→ Schema
→ 持久化 State
→ Budget
→ OPA
→ Approval 候选
→ PostgreSQL 权威时间复验 Approval
→ PostgreSQL 事务级 Idempotency Claim
→ Resource Reservation
→ Job + Idempotency + Job Authorization + Event 同事务入队
→ Worker 执行前重新校验 Plan / Budget / OPA / Approval
→ Capability Service / Adapter
→ 需要高权限时调用 Host Runner
```

相同 Idempotency Key 的并发请求先通过 `pg_advisory_xact_lock` 串行化；已存在 Job 时直接重放，不能再次预留资源。
不能让 Capability 绕过 Gateway 直接调用 Runner，也不能创建缺少 `job_authorizations` 记录的 Job。

---

## 5. OPA 策略

示例 Rego 逻辑（示意）：

```rego
package autopilot.authz

default allow := false

allow if {
  input.action == "start_environment_inspection"
  input.subject.role in {"viewer", "operator", "admin"}
}

allow if {
  input.action == "start_benchmark"
  input.subject.role in {"operator", "admin"}
  input.approval.decision == "approve"
  input.approval.plan_hash == input.plan.hash
  input.plan.estimated_tokens <= data.limits.max_tokens
  input.plan.estimated_duration_seconds <= data.limits.max_duration
}

deny_reason := "arbitrary shell is forbidden" if {
  input.action == "execute_shell"
}
```

实际实现应返回：

```json
{
  "allow": false,
  "reason_code": "APPROVAL_REQUIRED",
  "decision_id": "...",
  "requirements": {
    "human_approval": true
  }
}
```

---

## 6. 审批矩阵

所有“是”的 L2 审批都使用 Approval v2，并同时绑定 Experiment、Plan ID、Plan Hash 和 Action；
`plans.status = approved` 只表示 Plan 生命周期状态，不等于当前执行仍被授权。

| 动作 | 风险 | 审批 |
|---|---:|---|
| 查看环境报告 | L0 | 无 |
| 执行只读检测 | L1 | 无 |
| 读取模型配置 | L1 | 无 |
| 下载模型 | L2 | 是 |
| 启动/停止 vLLM | L2 | 是 |
| 单次小规模基线测试 | L1/L2 | 可按额度自动 |
| 开环压测 | L2 | 是 |
| 完整调优 | L2 | 是 |
| 删除实验临时文件 | L2 | 是 |
| 删除模型缓存 | L3 | MVP 拒绝 |
| 修改驱动/内核 | L3 | MVP 拒绝 |
| 任意 Shell | L3 | MVP 拒绝 |

---

## 7. Plan Hash

审批针对不可变计划：

```text
plan_hash = sha256(canonical_json(plan))
```

执行时：

- 重新计算；
- 与审批记录匹配；
- Tool/Adapter Version 发生变化则计划失效；
- 模型 Revision 或镜像 Digest 变化则重新审批。

---

## 8. Docker 安全

### 8.1 控制面容器

- `read_only: true`；
- 非 Root；
- Drop Capabilities；
- 无 Docker Socket；
- 只挂载必要 Artifact；
- Secret 不写镜像；
- Healthcheck；
- 限制日志大小。

### 8.2 vLLM 容器

需要 GPU 和模型缓存，但仍：

- 禁止 `--privileged`；
- 固定镜像 Digest；
- 固定 EntryPoint；
- 白名单参数；
- 只挂载模型缓存和实验日志；
- 端口只绑定需要的接口；
- 不挂载宿主机敏感目录；
- 适当 `--ipc=host` 或 `shm_size` 按 vLLM 官方建议，由 Adapter 固定，而非 AI 设置。

### 8.3 EvalScope 容器

- 不需要 GPU；
- 只访问被测 Endpoint；
- 只读数据集；
- 输出目录独立；
- 限制 CPU、内存和 PIDs；
- 限制总请求/Token/时间。

---

## 9. Host Runner

### 9.1 通信

优先 Unix Domain Socket：

```text
/opt/autopilot/runtime/runner.sock
```

权限只给专用用户组。

### 9.2 Runner 用户

- 专用系统用户；
- 最小 Docker 权限；
- 不作为通用 SSH 用户；
- 目录 ACL；
- systemd Hardening。

### 9.3 白名单

- 镜像 Digest；
- 模型 Cache 根目录；
- 输出根目录；
- 端口区间；
- 参数；
- 环境变量；
- GPU ID。

---

## 10. Secret 管理

Secret 包括：

- Agent Model API Key；
- Hugging Face Token；
- 私有模型仓库凭据；
- 数据库密码。

规则：

- 不进入 Graph State；
- 不进入 LLM Prompt；
- 不进入 MLflow 参数；
- 日志脱敏；
- Runner 根据 Secret Ref 注入；
- Artifact Manifest 不记录 Secret 值；
- OPA Decision Log 做字段 Mask。

---

## 11. 模型供应链

要求：

- 固定模型 Revision；
- 记录 Commit；
- 默认 `trust_remote_code=false`；
- 模型文件 Hash；
- 只允许白名单仓库；
- 私有仓库使用凭据 Ref；
- 下载目录隔离；
- 不自动执行仓库脚本；
- 许可证信息记录在 Model Profile。

如果模型必须 `trust_remote_code=true`，MVP 默认拒绝或升级到人工安全审核。

---

## 12. 镜像供应链

- 固定 Digest；
- 保存 SBOM（后续）；
- 私有 Registry；
- 扫描高危漏洞；
- 不在正式实验使用 `latest`；
- Adapter Version 与镜像 Profile 绑定；
- 升级先跑回归实验。

---

## 13. 预算控制

每个 Plan 计算：

```json
{
  "max_duration_seconds": 600,
  "max_requests": 5000,
  "max_input_tokens": 10000000,
  "max_output_tokens": 1000000,
  "max_disk_growth_bytes": 20000000000
}
```

Runner 和 EvalScope Adapter 均实施限制，不能只依赖 AI。

---

## 14. 超时和看门狗

- Job Lease 的获取、Heartbeat 和过期判定使用 PostgreSQL `clock_timestamp()`，不信任 Worker 提交的时间；
- 每次领取 Job 都递增 Fencing Token，旧 Token 的 Heartbeat、进度和终态写入一律拒绝；
- `waiting_approval` 保留 Lease；Worker 崩溃或 Lease 过期后，新 Worker 可以重新领取，但执行前仍须重新校验审批、Plan Hash、策略和预算；
- Runner Heartbeat；
- 容器 Startup Timeout；
- Benchmark Deadline；
- Trial Deadline；
- Experiment Global Deadline；
- GPU Lock Lease；
- 取消请求先持久化并追加 Event；Worker 观察到请求后停止发送新请求、等待安全窗口、执行清理，最后写入 `cancelled` 终态。

---

## 15. 日志和审计

审计必须记录：

- 谁发起；
- LLM 建议；
- Tool Call；
- Schema Version；
- Plan Hash；
- OPA Decision ID；
- 审批；
- Runner Action；
- Docker ID；
- 结果；
- Artifact Hash。

OPA Decision Log 中的 Secret、Token、Header 必须 Mask。

---

## 16. Docker Compose 与临时容器边界

Compose 只负责长期控制面：

```text
API / DB / MLflow / OPA / UI
```

Runner 负责临时数据面：

```text
vLLM / EvalScope / Planner Job
```

这样 Trial 生命周期不会污染 Compose，未来替换为 Kubernetes Job 时也更自然。

---

## 17. 环境前置验证

安装后先执行：

1. `nvidia-smi` 人工确认；
2. NVIDIA Container Toolkit 官方 Sample Workload；
3. NVML Probe；
4. Docker GPU 临时容器；
5. 小模型 vLLM Smoke；
6. EvalScope 1 请求 Smoke；
7. Artifact 写入；
8. OPA Deny Test；
9. Graph Interrupt/Resume；
10. Runner 重启 Reconciliation。

---

## 18. 参考资料

- NVIDIA Container Toolkit Installation: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
- NVIDIA Container Toolkit Sample: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html
- Docker Compose Services: https://docs.docker.com/reference/compose-file/services/
- OPA Decision Logs: https://www.openpolicyagent.org/docs/management-decision-logs
