# M9 控制面部署

该目录只描述单机控制面的安全启动方式。真实 vLLM、EvalScope、Planner、NVML 和 Optuna
执行仍需 G0 在 Linux RTX 5090 上固定并验证 Provider Profile；未配置时 API 会按契约
fail-closed，不会回退到内存状态或假执行。

## 启动

1. 在受信任构建环境使用仓库固定的 Python 和 uv 镜像 Digest 构建控制面镜像，推送到受控
   Registry 后记录最终镜像 Digest：

   ```bash
   docker build --platform linux/amd64 -f deploy/Dockerfile \
     -t registry.example.invalid/bittune-autopilot:0.1.0 .
   docker push registry.example.invalid/bittune-autopilot:0.1.0
   docker buildx imagetools inspect registry.example.invalid/bittune-autopilot:0.1.0
   ```

   `.env` 中的 `AUTOPILOT_API_IMAGE` 必须填写推送后的
   `registry.example.invalid/bittune-autopilot@sha256:<digest>`，不能填写构建标签。API、迁移和
   MLflow Server 使用同一镜像；MLflow 及其 PostgreSQL 驱动均由 `uv.lock` 固定，不使用缺少
   `psycopg` 的上游 MLflow 基础镜像。
2. 在目标 Linux 主机安装 Docker Compose Plugin，并准备一个 root-only 的
   `model-provider-api-key` 文件。未配置 ModelProvider 时该文件必须为空；启用时必须与
   Base URL 和 Model 同时配置。API 进程只通过 `/run/secrets/model_provider_api_key`
   读取该 Secret；Secret 不进入 Graph State、日志、Artifact 或 MLflow。
3. 复制 `.env.example` 为 `.env`，填入实际镜像 Digest、PostgreSQL 密码、两个不同的
   Human `UserId` 和对应 SHA-256 Token Hash。不要使用 `latest` 或示例占位值。
4. 先执行迁移，再启动控制面：

   ```bash
   docker compose --env-file .env -f deploy/compose.yaml run --rm migrate
   docker compose --env-file .env -f deploy/compose.yaml up -d postgres opa mlflow autopilot-api
   curl -fsS http://127.0.0.1:8000/healthz
   ```

5. API 对外只绑定 `127.0.0.1:8000`，生产入口应由受控反向代理或内网访问策略提供；
   OPA、MLflow 和 PostgreSQL 不发布主机端口。

PostgreSQL 首次初始化数据卷时会创建应用数据库和独立的 `autopilot_mlflow` 数据库。两者
不能合并，否则应用 Alembic 与 MLflow 的 `alembic_version` 表会发生冲突。MVP 只启用
MLflow Tracking/Artifact，关闭不在范围内的 MLflow Job Execution，并将 HTTP worker 和
数值库线程固定为 1。Artifact 通过 MLflow HTTP proxy 写入服务端卷，客户端不共享
`/mlartifacts` 路径；Host 校验只允许 Compose 服务名与 loopback。

API 容器使用固定非 Root UID、只读根文件系统、临时 `tmpfs`、`drop ALL`、
`no-new-privileges`，且没有 Docker Socket、GPU 设备或 Host Runner Socket。Host Runner
继续独立由 `runner/systemd/autopilot-runner.service` 管理；API 容器不能直接调用它。

Compose 将模型密钥映射为 Pydantic Settings 所需的
`/run/secrets/AUTOPILOT_API_MODEL_PROVIDER_API_KEY`，并固定连接 Compose 内的 OPA 服务。
OPA 启用官方 console Decision Log，使每次结构化 Policy 判定返回并记录真实 Decision ID；
`PolicyInput` 不包含 Secret、Authorization Header 或原始 Provider 日志。
`AGENT_VERIFIED_PROVIDERS`、`AGENT_HARDWARE_CAPABILITIES` 和
`AGENT_ENABLED_PROVIDERS` 必须保持 JSON 空数组，直到 G0 产生并验证对应 Profile。

当前 Compose 不创建虚假的 Worker：仓库尚未把真实 Provider Profile 组装成可执行 Worker，
因此没有用空循环进程冒充 Job 执行。G0 固定 Provider 后，Worker 通过 PostgreSQL Lease
Queue 和受控 Runner 接入，仍需保持同一镜像 Digest 和本契约。

## CLI 客户端

控制面启动后可从受控客户端使用仓库提供的 Click CLI。CLI 只调用 REST/SSE API，审批、
策略和执行仍由服务端处理：

```bash
export AUTOPILOT_API_URL=http://127.0.0.1:8000
export AUTOPILOT_API_TOKEN='your-token'
uv run autopilot chat
uv run autopilot create '为指定模型生成单 GPU 优化计划'
uv run autopilot status <experiment_id>
uv run autopilot events <experiment_id>
uv run autopilot resume <experiment_id> --decision approved
uv run autopilot cancel <experiment_id>
```

不要把 Token 放进命令行参数、仓库文件或日志。CLI 未设置 `AUTOPILOT_API_TOKEN` 时会改为
交互式输入。

## 备份与恢复

停止写入后执行 PostgreSQL 逻辑备份，并同时备份 Artifact 根目录和 `.env` 之外的 Secret
交付记录。不得把明文 Token 或模型 Provider Key 写入备份：

```bash
docker compose --env-file .env -f deploy/compose.yaml stop autopilot-api mlflow
docker compose --env-file .env -f deploy/compose.yaml exec -T postgres \
  pg_dump -U "$AUTOPILOT_POSTGRES_USER" -d "$AUTOPILOT_POSTGRES_DB" --format=custom \
  > backup/autopilot-$(date -u +%Y%m%dT%H%M%SZ).dump
docker compose --env-file .env -f deploy/compose.yaml exec -T postgres \
  pg_dump -U "$AUTOPILOT_POSTGRES_USER" -d autopilot_mlflow --format=custom \
  > backup/autopilot-mlflow-$(date -u +%Y%m%dT%H%M%SZ).dump
tar --xattrs --acls -C /var/lib/docker/volumes -czf backup/artifacts-$(date -u +%Y%m%dT%H%M%SZ).tgz \
  bittune-autopilot_artifacts/_data
```

恢复到空主机时先恢复两个 PostgreSQL 数据库和 Artifact 根目录，再运行 `migrate`，最后启动 API。
恢复后必须核对 Alembic revision、镜像 Digest、Policy Bundle 内容、Artifact SHA-256 和
Hardware Passport/模型 Revision；Graph 恢复前还必须对账 Job、Deployment、GPU Lock、
MLflow Run 和 Artifact 是否存在，不能直接假设外部状态未变化。
