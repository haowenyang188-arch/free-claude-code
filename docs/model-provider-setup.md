# Model Provider Setup

## Xiaomi MiMo V2.5 Pro — 已接入完成

### 改动文件

| 文件 | 改动内容 |
|------|----------|
| `providers/openai_compat.py` | 新增 `_AuthHeaderOpenAI` 子类，支持自定义 auth header 名称。`OpenAICompatibleProvider` 新增 `auth_header` 参数，默认 `"Authorization"`（向后兼容），小米传 `"api-key"` |
| `providers/xiaomi/client.py` | `__init__` 传入 `auth_header="api-key"`，默认 base URL 更新为 `https://api.xiaomimimo.com/v1` |
| `.env.example` | `XIAOMI_BASE_URL` 默认值修正为 `https://api.xiaomimimo.com/v1` |

### 已验证结果

| 验证项 | 结果 |
|--------|------|
| Server 链路 HTTP 状态 | 200 OK |
| 鉴权方式 | 使用 `api-key` 环境变量，无 Authorization Bearer |
| 流式响应（SSE） | 正常收到 chunk |

### 启动命令

```bash
cd /home/gnen/free-claude-code
uv run python server.py
```

### 已验证模型

在 `.env` 中修改 `MODEL=`：

```
MODEL="xiaomi/mimo-v2.5-pro"           # 小米 MiMo V2.5 Pro
MODEL="nvidia_nim/z-ai/glm4.7"   # NVIDIA NIM GLM 4.7
```

### MiniMax M2.7 当前状态

当前 `.env` 中 `MINIMAX_API_KEY` 与 `XIAOMI_API_KEY` 为同一 key，MiniMax API 需要独立 key 才能验证。MiniMax M2.7 需补充独立 key 后再验证，MODEL 写法以 `GET /v1/models` 输出为准。

### 安全注意

本文档不包含任何真实 API Key 或 Key 片段。所有密钥通过环境变量读取，不硬编码。
