# ADR-015：MVP 实现基线

- 状态：Accepted
- 日期：2026-08-05
- 范围：MVP 控制面、Runner 和 Provider 集成

## 背景

`09-文档审查与准确性报告.md` 保留了前端、Job Queue、Artifact Store 和 llm-d Planner 运行方式等实现选项。根 `AGENTS.md` 要求开发前将当前 MVP 可从已有架构和单卡边界推导的选项收敛为唯一方案。

## 决策

1. MVP 对外交互面为 FastAPI REST + SSE + OpenAPI，不实现 Web UI。
2. 长任务使用 PostgreSQL Lease Queue 和单 GPU Worker，不引入 Celery、Dramatiq、Ray 或 Temporal。
3. Artifact 使用受限根目录内的本地文件系统，数据库仅保存元数据和引用。
4. API 与 Host Runner 通过 Unix Domain Socket 通信，使用文件权限限制专用用户组。
5. llm-d Capacity Planner 使用固定 Digest 容器，由 Host Runner 执行带类型白名单请求；未配置 Phase 0 验证过的 Digest 时 Provider 不可用。
6. Agent 仅使用部署配置提供的远程 OpenAI-compatible `ModelProvider`，不实现本地 GPU 模型或供应商自动回退。
7. MVP Registry 仅注册 NVML、llm-d Planner、vLLM、EvalScope、Optuna、MLflow 和 OPA 七个已确定 Provider，不建设通用 Plugin System。
8. Agent Tool 名称必须遵守根规则中的 `create_*_plan`、`preview_*`、`start_*`、`get_*_status`、`get_*_result`、`cancel_*` 动作形式。

## 结果

- 当前 MVP 不需要前端工具链、分布式任务系统或对象存储。
- 未固定、未通过 Contract/GPU 回归的 Provider 不能进入正式实验。
- 将来扩展上述边界必须新增 ADR，不在当前代码中保留备用实现。
