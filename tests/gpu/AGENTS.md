# GPU 测试局部规则

> 作用范围：`tests/gpu/`；本文件只能收紧根 `AGENTS.md` 规则。

1. 每次运行前必须记录用户明确批准、GPU 0 空闲或 Experiment Lock 归属、驱动、镜像 Digest 和模型 Revision。
2. Fixture 只使用已固定 Revision 的目标模型、数据集和 Tokenizer，不允许 `latest` 或浮动分支。
3. 每个用例必须声明 Startup、Job、Request、Input Token、Output Token 和磁盘增长上限。
4. 所有退出路径都必须停止临时容器、保存失败证据、清理临时目录并释放 GPU 锁；不删除模型缓存。
5. GPU 测试不得被默认 pytest 命令或 CI 自动收集。
6. 性能结论必须记录 Warmup、Seed、Sampling、温度/功耗、请求完成口径和原始 Artifact Hash。
