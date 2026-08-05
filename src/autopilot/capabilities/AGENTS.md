# Capability Package 局部规则

> 作用范围：`src/autopilot/capabilities/`；本文件只能收紧根 `AGENTS.md` 规则。

1. 每个能力包使用 `manifest.yaml`、`domain/`、`tools/`、`application/`、`ports/`、`adapters/<provider>/` 和 `tests/` 的固定结构。
2. Domain/Application 仅依赖 Port，不导入 Provider SDK；Provider 术语只出现在 Compiler 输出、Version Profile 和 Adapter 中。
3. Validator、Compiler、Normalizer 和预算计算保持纯函数，不读取 Secret、数据库、网络、文件系统或全局状态。
4. Agent Tool 只使用 `create_*_plan`、`preview_*`、`start_*`、`get_*_status`、`get_*_result`、`cancel_*` 命名；`start_*` 只接收已保存 Plan ID 和预期 Hash。
5. 公共 Schema 使用 Pydantic v2、`extra="forbid"`、稳定 Enum、明确单位、数字/集合上限和版本字段；禁止任意字典、CLI 字符串或路径输入。
6. 每个 Adapter 必须有固定 Version Profile 和 Contract Test；未注册或未验证版本必须失败关闭。
7. 能力包局部 `tests/` 保存与包同版本的 Golden/Fixture；可执行测试放在顶层 `tests/unit` 或 `tests/contract` 并引用这些资源。
