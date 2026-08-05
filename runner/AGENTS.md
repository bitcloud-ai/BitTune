# Host Runner 局部规则

> 作用范围：`runner/`；本文件只能收紧根 `AGENTS.md` 规则。

1. Runner 不得导入、调用或托管 LLM/Agent 代码。
2. 对外请求必须是以 Action Enum 为判别字段的 Pydantic 联合，禁止原始 `command`、Shell、argv、任意环境变量 Map、Volume Map 和宿主机路径。
3. Runner 只监听配置根目录内的 Unix Domain Socket，不增加 TCP 备用通道。
4. Docker 操作只使用带类型 SDK Adapter，镜像必须命中 Digest 白名单，并固定 EntryPoint、GPU 0、Network、Mount Root 和允许的环境变量名。
5. 路径解析必须拒绝绝对路径输入、`..` 穿越、符号链接逃逸和未注册根目录。
6. 修改 GPU 锁、容器或临时目录的动作必须具有幂等键、Lease、Heartbeat、超时、取消、对账和所有退出路径的清理。
7. Runner 只根据 `SecretRef` 在 systemd Credential 边界解析 Secret，日志、错误和 Artifact 不得包含 Secret 值。
8. 修改 Runner 边界必须增加 Contract 和集成测试；真实 Docker/Linux/systemd 测试只在满足对应环境前置时运行。
