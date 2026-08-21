# 运行指南

## 运行目录

默认运行目录为 `~/.bittune`，可以通过环境变量调整：

| 环境变量 | 用途 |
|---|---|
| `BITTUNE_HOME` | Bittune 根目录。 |
| `BITTUNE_AGENT_DIR` | Agent 模型配置和会话目录。 |
| `BITTUNE_SESSION_DIR` | 会话文件目录。 |
| `BITTUNE_STATE_DIR` | SQLite 状态库与 Artifact 索引目录。 |
| `BITTUNE_LOG_DIR` | 运行日志目录。 |
| `BITTUNE_MODEL_CACHE_ROOTS` | 额外模型缓存根目录，使用系统路径分隔符分隔。 |

API Key 环境变量由 `bittune configure --api-key-env` 指定；默认名称为
`BITTUNE_AGENT_LLM_API_KEY`。不要把密钥写入项目文件、MCP 配置或 Shell 历史。

## 推理 Provider 前置条件

只有目标需要本机推理、模型下载或性能测试时，才准备以下工具：

```bash
docker info
nvidia-smi
python3 -m pip install --upgrade 'huggingface_hub[cli]' 'evalscope[perf]'
hf --help
evalscope perf --help
```

Bittune 对外部 Runtime、模型、服务和端点默认只读。它不会因为发现到容器或端点就自动接管、停止或修改它们。

## 证据与状态

受管操作会写入本地 SQLite State Store。每条运行记录包含受限输入、观测摘要、时间、哈希和 Artifact 引用。查询类工具只返回当前会话所需信息，不创建运行记录。

性能或容量结论只适用于生成它的部署、环境、负载和配置。单次成功表示一个已测运行点，不等同于最大吞吐或稳定容量。

## MCP 运维

可选 MCP 配置文件位于 `$BITTUNE_HOME/mcp.json`。第一版仅支持管理员配置的只读 Streamable HTTP 服务；外部响应只作为会话参考，不会替代本机检查或写入 Bittune Evidence。

配置中的密钥必须引用环境变量：

```json
{
  "mcpServers": {
    "knowledge": {
      "enabled": true,
      "transport": "streamable-http",
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer ${BITTUNE_MCP_API_KEY}"
      },
      "allowTools": ["search_knowledge"],
      "effect": "read-only",
      "timeoutMs": 10000
    }
  }
}
```

检查 MCP 配置和连接状态：

```bash
bittune mcp list
bittune mcp get knowledge
bittune mcp test knowledge
```

MCP 服务不可用时，Bittune 保留本机工具能力，并在运行时给出诊断信息。
