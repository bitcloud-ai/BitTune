# 快速开始

## 1. 安装

当前 Linux 发行包支持 apt-based x86_64 主机。从 GitHub Release 下载
`bittune-installer-<version>.tar.gz` 后执行：

```bash
tar -xzf bittune-installer-<version>.tar.gz
cd bittune-installer-<version>
sudo ./install-ubuntu.sh ./bittune-runtime-<version>.tgz <linux-user>
```

安装器将 Bittune 安装到 `/opt/bittune`，并创建 `/usr/local/bin/bittune`。
它只安装 Node.js 和 Bittune 运行时，不会安装或修改 Docker、GPU Driver、NVIDIA
Container Toolkit、推理镜像或模型。

若默认路径已被其他产品使用，可指定隔离目录：

```bash
sudo BITTUNE_INSTALL_ROOT=/opt/bittune-agent-runtime \
  ./install-ubuntu.sh ./bittune-runtime-<version>.tgz <linux-user>
```

## 2. 配置 Agent LLM

Bittune 使用 OpenAI-compatible endpoint 进行 Agent 推理。API Key 只通过环境变量提供：

```bash
export BITTUNE_AGENT_LLM_API_KEY='your-api-key'
bittune configure \
  --base-url https://endpoint.example.com/v1 \
  --model-id your-tool-capable-model
```

也可以用 `--api-key-env` 指定其他环境变量名：

```bash
export COMPANY_LLM_KEY='your-api-key'
bittune configure \
  --base-url https://endpoint.example.com/v1 \
  --model-id your-tool-capable-model \
  --api-key-env COMPANY_LLM_KEY
```

配置完成后检查本机状态：

```bash
bittune doctor
```

## 3. 启动会话

```bash
bittune
```

在会话中直接说明工程目标，例如：

```text
检查这台机器是否具备部署 vLLM 的条件，并说明缺少的前置项。
```

当目标涉及部署、压测、容量分析或实验时，Bittune 会按需启用相应能力。缺少 Docker、GPU、模型或 EvalScope 时，相关操作会返回明确限制，不会伪造测量结果。

## 4. 恢复会话

Bittune 会在会话输出中显示恢复命令。也可以使用：

```bash
bittune --session <session-id>
```

## 5. 从源码运行

开发环境要求 Node.js >= 22.19.0：

```bash
npm install
npm run check
npm test
npm run bittune
```

构建可发布安装包：

```bash
npm run package:agent
```
