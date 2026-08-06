# BitTune LLM Inference Autopilot

BitTune 是面向单台 Linux 主机、单张 NVIDIA RTX 5090 32 GB 的大模型推理 Autopilot MVP。它使用可恢复工作流程管理环境检测、容量规划、vLLM 部署、EvalScope 压测、Optuna 搜索、候选复测和证据归档。

架构基线见 [MVP 技术文档](docs/llm-inference-autopilot-mvp-docs/README.md)，分阶段实施与验收条件见 [开发计划](docs/DEVELOPMENT_PLAN.md)。

## 开发环境

- Python 3.12
- uv 0.12.x
- Linux 是真实 Runner 和 GPU 测试的唯一正式环境
- Windows 可运行领域逻辑、Golden、Contract 和不依赖 Linux 的集成测试

## 安装

```bash
uv sync --all-extras
```

API 、Worker 和 Host Runner 在部署时使用分离的依赖集；`--all-extras` 只用于开发和全量非 GPU 检查。

## 标准检查

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy src/autopilot runner
uv run pytest tests/unit tests/contract
uv run python scripts/validate_docs.py
uv run python scripts/export_schemas.py
```

GPU 测试不在默认检查中。只能在获得明确批准、GPU 0 空闲且预算已配置后执行 `tests/gpu`。

## 控制面部署

MVP 的交付入口是单机 Linux 上的 Docker Compose 控制面和独立的 systemd Host Runner，
对外提供 FastAPI REST、SSE 和 OpenAPI，不包含 Web UI。部署步骤、不可变镜像 Digest、
Secret 文件、数据库迁移以及备份恢复见 [deploy/README.md](deploy/README.md)。

当前未通过 G0 固定真实 Provider Profile 时，控制面会安全启动但对环境检测、部署、压测、
调优和证据归档保持 fail-closed；不能把 Fake Adapter 的结果当作 RTX 5090 性能结果。

## CLI 交互

MVP 的主交互是持续会话，而不是只能从头提交一次完整流程。官方 LangChain Agent harness 驱动模型-工具循环，Click 负责命令入口，Textual 负责终端布局、输入、Command Palette、异步 Worker 和流式事件渲染：

```bash
uv run autopilot chat
```

会话中直接输入自然语言即可，支持 `/approve`、`/reject`、`/status`、`/cancel`、`/new` 和 `/quit`；相同动作也注册在 Textual Command Palette。同一会话持久化到 PostgreSQL，重启 CLI 或 API 后可继续。Agent 只会看到经过 Tool Gateway、OPA 和当前阶段校验的 Autopilot 领域工具，不具备任意 Shell、Docker、Python 或文件系统能力。

项目提供基于开源 Click 框架的 `autopilot` 命令。CLI 是 REST/SSE 客户端，不复制服务端
Graph、审批或 Provider 逻辑。先设置 API 地址和 Bearer Token；Token 不支持命令行参数，
未设置环境变量时会通过交互式输入读取：

```bash
export AUTOPILOT_API_URL=http://127.0.0.1:8000
export AUTOPILOT_API_TOKEN='your-token'
uv run autopilot --help
uv run autopilot create '在指定 5090 上为 7B 模型优化吞吐，TTFT P95 不超过 2 秒'
uv run autopilot status <experiment_id>
uv run autopilot events <experiment_id>
uv run autopilot resume <experiment_id> --decision approved --comment '人工确认部署计划'
uv run autopilot cancel <experiment_id>
```

`create/status/events/resume/cancel` 是兼容旧控制面客户端的单次 REST 命令；新的 Agent 主入口是上面的 `autopilot chat`，不会把用户限制在固定流程命令中。

`create` 返回的 `experiment_id` 用于后续查询、恢复和取消。长时间操作通过服务端 Job
异步执行，`events` 只输出结构化 SSE；真实 GPU Provider 未配置时服务端按契约拒绝执行，
不会伪造性能结果。
