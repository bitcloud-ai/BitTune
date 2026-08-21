# Bittune

> 面向 GPU 推理部署、压测和调优的工程智能体。

Bittune 将环境检查、模型发现、服务部署、可用性探测、性能测试和证据记录组织为可审计的工程工具。Agent 根据目标、当前观测和已有运行记录选择下一步，而不是执行固定流水线。

[快速开始](guide/getting-started.md) · [运行指南](guide/operations.md) · [用户文档](guide/README.md)

## 功能

- 读取 GPU、Linux、Docker 和 NVIDIA Runtime 状态，发现本机模型缓存与已有服务。
- 用受限配置创建并管理 vLLM 服务，独立执行启动、就绪检查、端点探测、日志读取和停止。
- 调用 EvalScope `perf` 测量受管服务，并将原始输出保存为 Run Record 和 Artifact。
- 从同一部署、环境、负载和配置的实测数据推导 `MeasuredOperatingPoint`，不将单次成功误报为最大容量。
- 记录调优和容量探索实验，支持重复基准、候选比较和可追溯结论。
- 默认不接管外部 Runtime、模型、服务或端点；写操作始终通过受限领域工具执行。
- 通过 Capability 按需向 Agent 开放经过审查的工具集，避免把无关高权限操作暴露给每一轮会话。
- 可选接入管理员配置的只读 MCP 服务，用于获取外部参考；实际环境事实和执行证据仍以本机工具为准。

## 快速开始

从 [GitHub Releases](https://github.com/bitcloud-ai/BitTune/releases) 下载 `bittune-installer-<version>.tar.gz`，在 Ubuntu x86_64 主机安装：

```bash
tar -xzf bittune-installer-<version>.tar.gz
cd bittune-installer-<version>
sudo ./install-ubuntu.sh ./bittune-runtime-<version>.tgz <linux-user>
```

配置 OpenAI-compatible Agent LLM，然后启动：

```bash
export BITTUNE_AGENT_LLM_API_KEY='your-api-key'
bittune configure --base-url https://endpoint.example.com/v1 --model-id your-tool-capable-model
bittune doctor
bittune
```

完整的前置条件、离线安装和配置说明见[快速开始](guide/getting-started.md)。

## 运行要求

- Linux 发行包当前支持 apt-based x86_64 主机，并携带固定 Node.js 运行时。
- 任意 OpenAI-compatible Agent LLM endpoint 是启动 Bittune 的必需条件。
- GPU、Docker、NVIDIA Container Toolkit、vLLM、模型缓存和 EvalScope 都是按需能力；只有目标涉及对应操作时才需要准备。
- Bittune 不会自动安装或修改 GPU 驱动、Docker/NVIDIA Toolkit、容器镜像或模型。

## 从源码运行

开发环境需要 Node.js >= 22.19.0：

```bash
npm install
npm run check
npm test
npm run bittune
```

构建发行物：

```bash
npm run package:agent
```

## 文档

- [快速开始](guide/getting-started.md)：安装、首次配置与会话恢复。
- [运行指南](guide/operations.md)：运行目录、Provider 前置条件、证据存储和 MCP 运维。
- [用户文档首页](guide/README.md)：文档导航与支持范围。

## 许可证

Bittune 自有代码采用 [MIT License](LICENSE)。第三方组件的版权与许可见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 和 [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md)。
