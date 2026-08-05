# Generated JSON Schema

该目录的 JSON 文件由 `src/autopilot` 中的 Pydantic v2 契约生成，不手工编辑。

这里只发布 Agent/API 可见或需要持久化的领域契约。Version Profile、Provider 编译 DTO
和原始 Provider 报告属于内部边界，不生成公共 Schema。

更新契约后执行：

```bash
uv run python scripts/export_schemas.py --write
uv run python scripts/export_schemas.py
```
