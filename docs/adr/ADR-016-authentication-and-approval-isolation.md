# ADR-016：认证来源与审批身份隔离

- 状态：Accepted
- 日期：2026-08-06
- 范围：MVP API 认证、Tool Gateway 主体与 L2 人工审批

## 背景

M4 需要把用户角色纳入动态 Tool 可见性，并为 L2 操作保存可审计的审批人。既有设计尚未固定认证来源，也未明确人类用户与内部服务身份的隔离方式。如果同一身份既能申请又能批准，或者服务身份能伪装成人类管理员，Plan Hash 和 OPA 检查仍无法形成有效的职责分离。

## 决策

1. MVP 的外部请求只接受高强度 opaque Bearer Token。Token 仅通过 `Authorization: Bearer <token>` 传递，不接受 Query、Cookie、Basic Auth 或其他回退认证来源。
2. Token 由 `secrets.token_urlsafe(32)` 生成，即至少 32 Byte 的 CSPRNG（密码学安全随机数生成器）随机数据。认证边界只接受 43～128 位、由 `A-Z`、`a-z`、`0-9`、`_` 和 `-` 组成的 base64url 字符串。Token 是不可解析的不透明凭据，不承载用户、角色或过期时间声明。
3. Token 明文仅以 `SecretStr` 在部署 Secret 注入和单次请求认证边界短暂存在。它不得持久化，也不得进入 Graph State、LLM Prompt、日志、Artifact、MLflow、OPA 输入或 Decision Log。
4. 注册表只保留 `sha256:<64 lowercase hex>` 形式的 `BearerTokenHash`，并通过恒定时间 `compare_digest` 校验候选哈希。认证失败统一返回 `AUTHENTICATION_FAILED`，不区分格式错误、未知 Token 或空注册表。
5. `BearerTokenBinding` 只能将 Token Hash 映射到稳定 `UserId`、`viewer/operator/admin` 角色组成的 `HumanSubject`。角色来自服务端注册表，客户端不能提交或覆盖角色。
6. 内部服务使用独立 `ServiceSubject(service_name)`。服务身份没有用户角色，不能进入 Bearer Token 注册表，也永远不能批准人工审批。
7. L2 审批只接受 `admin` 的 `HumanSubject`，并强制 `requester_id != decided_by`。该检查同时在审批写入边界与执行前 Gateway 复核中实施；LLM、OPA 和服务身份不能给自己授权。
8. MVP 不建立 `users` 表，不实现 JWT、OAuth/OIDC、Session 或 Token 刷新协议。Token Hash 与 HumanSubject 的绑定由部署配置和 Secret 边界提供；扩大身份生命周期管理必须新增 ADR。

## 结果

- API、Tool Gateway、OPA 输入和 Audit 使用同一判别联合主体契约，人类用户与内部服务无法互相伪装。
- Token 泄露时通过替换部署 Secret 和对应 Hash 绑定完成轮换；MVP 不提供在线发放或撤销端点。
- 本决策补齐此前未实现的认证契约，不改变 M0～M3 公共 Schema、既有 Plan Hash 或 Evidence。此前没有可继续信任的已认证审批记录。
- Approval v2 必须保存请求人与决策人的人类 `UserId`，并同时绑定 Experiment、Plan ID、Plan Hash
  和 Action。Approval v1 不向后兼容；M4 之前没有可继续信任的持久化审批记录，因此不执行旧记录迁移。
- 已审批 Plan、既有 Evidence 和 M0～M3 其他公共契约不因认证升级自动失效；只有重新进入 L2 审批流程的
  执行请求使用 Approval v2。
