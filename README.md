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
