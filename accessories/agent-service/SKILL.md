---
name: agent-service
description: 通过本地 Agent Service HTTP server 管理模型、工具、MCP、Skill、提示模板、环境变量、会话、Agent，并发起/中止推理；使用 bash + curl 调用 server API，server 地址和端口可由环境变量配置。
---

# Agent Service 操作技能

本 Skill 用于通过 HTTP API 操作当前 Agent Service server。需要执行实际操作时，优先使用 `bash` 工具调用 `curl`。所有请求都应面向运行中的 server；如果 server 未启动，先提示用户启动（通常为 `python app.py` 或 `python app.py 127.0.0.1:7988`）。

## 1. Server 地址配置

每次调用前先解析 base URL。支持以下环境变量：

- `RUNTIME_SERVER_URL`：完整地址，优先级最高，例如 `http://127.0.0.1:7988`
- `AGENT_SERVER_URL`：完整地址，次优先级
- `RUNTIME_SERVER_HOST`：host，默认 `127.0.0.1`
- `RUNTIME_SERVER_PORT`：port，默认 `7988`

推荐在 bash 中使用：

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
echo "SERVER_URL=$SERVER_URL"
curl -fsS "$SERVER_URL/v1/models" | python -m json.tool
```

如需复用，可定义 helper：

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
api_get() { curl -fsS "$SERVER_URL$1"; }
api_json() { method="$1"; path="$2"; data="$3"; curl -fsS -X "$method" "$SERVER_URL$path" -H 'Content-Type: application/json' -d "$data"; }
```

> 注意：`curl -f` 在 HTTP 4xx/5xx 时会返回非零退出码。排查错误时可去掉 `-f` 或加 `-i` 查看响应状态和错误 JSON。

## 2. 通用请求规则

- JSON 请求头：`-H 'Content-Type: application/json'`
- JSON 输出美化：追加 `| python -m json.tool`（如果响应不是 JSON 不要美化）
- 路径参数中的 `session_id`、环境变量 key 等可能包含特殊字符时，先 URL encode；简单 ID 通常可直接拼接。
- 对会修改注册表或会话的数据接口，执行前确认用户意图；删除操作尤其要谨慎。
- API 数据默认持久化在 server 侧 `~/.agents_runtime/` 相关 JSON 或 chat_data 中。

## 3. API 总览

| 方法 | 路径 | 功能 |
| --- | --- | --- |
| `GET` | `/v1/models` | 列出模型配置 |
| `POST` | `/v1/models` | 注册模型配置 |
| `PUT` | `/v1/models/{model_id}` | 更新模型配置 |
| `DELETE` | `/v1/models/{model_id}` | 删除模型配置 |
| `GET` | `/v1/tools` | 列出工具配置 |
| `POST` | `/v1/tools` | 注册工具（function/MCP tool 配置） |
| `PUT` | `/v1/tools/{tool_id}` | 更新工具 |
| `DELETE` | `/v1/tools/{tool_id}` | 删除工具（内置工具不可删） |
| `DELETE` | `/v1/tools/batch` | 批量删除工具 |
| `POST` | `/v1/tools/call` | 直接调用工具 |
| `POST` | `/v1/tools/mcp` | 注册 MCP server 并发现工具 |
| `GET` | `/v1/mcp-servers` | 列出已持久化 MCP server 配置 |
| `DELETE` | `/v1/mcp-servers/{server_name}` | 删除 MCP server 及其工具 |
| `POST` | `/v1/tools/skill` | 注册包含 `SKILL.md` 的 Skill 目录 |
| `GET` | `/v1/prompt-templates` | 列出提示模板 |
| `POST` | `/v1/prompt-templates` | 创建提示模板 |
| `PUT` | `/v1/prompt-templates/{template_id}` | 更新提示模板 |
| `DELETE` | `/v1/prompt-templates/{template_id}` | 删除提示模板 |
| `GET` | `/v1/env` | 获取 server 环境变量配置 |
| `POST` | `/v1/env` | 设置环境变量 |
| `POST` | `/v1/env/detect` | 检测代码中使用到的环境变量 key |
| `DELETE` | `/v1/env/{key}` | 删除环境变量 |
| `POST` | `/v1/infer` | 非流式推理 |
| `POST` | `/v1/infer/stream` | SSE 流式推理 |
| `POST` | `/v1/infer/abort` | 中止指定 session 的流式推理 |
| `GET` | `/v1/sessions` | 列出历史会话 |
| `GET` | `/v1/sessions/search?q=...` | 搜索会话（按标题/内容关键词） |
| `GET` | `/v1/sessions/{session_id}` | 获取会话完整记录 |
| `DELETE` | `/v1/sessions/{session_id}` | 删除会话 |
| `POST` | `/v1/sessions/{session_id}/generate-title` | 强制生成会话标题 |
| `POST` | `/v1/sessions/{session_id}/read` | 将指定会话标记为已读 |
| `POST` | `/v1/sessions/{session_id}/revoke` | 按用户消息 timestamp 撤回该消息及之后消息 |
| `GET` | `/v1/sessions/events` | SSE 端点，实时推送会话状态变更 |
| `GET` | `/v1/agents` | 列出 Agent |
| `GET` | `/v1/agents/{agent_id}` | 获取单个 Agent |
| `POST` | `/v1/agents` | 创建 Agent |
| `PUT` | `/v1/agents/{agent_id}` | 更新 Agent |
| `DELETE` | `/v1/agents/{agent_id}` | 删除 Agent |

## 4. 模型管理

### 列出模型

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
curl -fsS "$SERVER_URL/v1/models" | python -m json.tool
```

### 注册 OpenAI-compatible 模型

必需字段：`model_id`、`api_base`、`model_name`。可选字段：`api_key`、`model_type`（`llm`/`vlm`）、`api_protocol`（通常 `openai` 或 `ollama`）、`generate_params`。

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
curl -fsS -X POST "$SERVER_URL/v1/models" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "model_id": "openai-compatible",
  "api_base": "http://127.0.0.1:8000/v1",
  "model_name": "gpt-4o-mini",
  "api_key": "",
  "model_type": "llm",
  "api_protocol": "openai",
  "generate_params": {"temperature": 0.7}
}
JSON
```

### 注册 Ollama 模型

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
curl -fsS -X POST "$SERVER_URL/v1/models" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "model_id": "qwen3-14b",
  "api_base": "http://localhost:11434",
  "model_name": "qwen3:14b",
  "api_key": "",
  "model_type": "llm",
  "api_protocol": "ollama",
  "generate_params": {"temperature": 0.7}
}
JSON
```

### 更新或删除模型

```bash
# 更新：PUT body 是完整 ModelConfig
curl -fsS -X PUT "$SERVER_URL/v1/models/qwen3-14b" \
  -H 'Content-Type: application/json' \
  -d '{"model_id":"qwen3-14b","api_base":"http://localhost:11434","model_name":"qwen3:14b","api_protocol":"ollama","model_type":"llm","generate_params":{"temperature":0.2}}' \
  | python -m json.tool

# 删除
curl -fsS -X DELETE "$SERVER_URL/v1/models/qwen3-14b" | python -m json.tool
```

## 5. 工具管理

### 列出工具

```bash
curl -fsS "$SERVER_URL/v1/tools" | python -m json.tool
```

### 注册 Python function 工具

1. 先创建 Python 文件，包含可调用函数。
2. 再注册 `tool_type: "function"`。`function_file_path` 必须是 server 进程能访问的路径；`function_name` 为函数名。
3. `parameters` 使用 OpenAI function calling JSON Schema。

```bash
cat >/tmp/runtime_echo_tool.py <<'PY'
def echo(text: str) -> str:
    return text
PY

curl -fsS -X POST "$SERVER_URL/v1/tools" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "tool_id": "function-echo",
  "tool_type": "function",
  "name": "echo",
  "description": "Echo input text.",
  "parameters": {
    "type": "object",
    "properties": {
      "text": {"type": "string", "description": "Text to echo"}
    },
    "required": ["text"]
  },
  "function_file_path": "/tmp/runtime_echo_tool.py",
  "function_name": "echo"
}
JSON
```

### 直接调用工具

`format` 可选：默认返回文本；设为 `json` 时 server 会尝试从工具结果中提取 JSON。

```bash
curl -fsS -X POST "$SERVER_URL/v1/tools/call" \
  -H 'Content-Type: application/json' \
  -d '{"tool_id":"function-echo","arguments":{"text":"hello","format":"json"}}' \
  | python -m json.tool
```

### 更新或删除工具

```bash
# 更新：PUT body 是完整 ToolConfig。内置工具不可更新。
curl -fsS -X PUT "$SERVER_URL/v1/tools/function-echo" \
  -H 'Content-Type: application/json' \
  -d '{"tool_id":"function-echo","tool_type":"function","name":"echo","description":"Echo text","parameters":{"type":"object","properties":{"text":{"type":"string"}},"required":["text"]},"function_file_path":"/tmp/runtime_echo_tool.py","function_name":"echo"}' \
  | python -m json.tool

# 删除单个工具。内置工具不可删除。
curl -fsS -X DELETE "$SERVER_URL/v1/tools/function-echo" | python -m json.tool

# 批量删除
curl -fsS -X DELETE "$SERVER_URL/v1/tools/batch" \
  -H 'Content-Type: application/json' \
  -d '{"tool_ids":["tool-a","tool-b"]}' \
  | python -m json.tool
```

## 6. MCP server 管理

### 注册 MCP servers

Body 形如 `{ "mcpServers": { ... } }`。server 会连接 MCP server、发现工具、注册工具，并持久化 server 配置。

stdio 示例：

```bash
curl -fsS -X POST "$SERVER_URL/v1/tools/mcp" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "mcpServers": {
    "time": {
      "command": "uvx",
      "args": ["mcp-server-time"]
    }
  }
}
JSON
```

HTTP MCP 示例：

```bash
curl -fsS -X POST "$SERVER_URL/v1/tools/mcp" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "mcpServers": {
    "remote-mcp": {
      "url": "http://127.0.0.1:8081/mcp",
      "headers": {}
    }
  }
}
JSON
```

### 列出或删除 MCP server

```bash
curl -fsS "$SERVER_URL/v1/mcp-servers" | python -m json.tool
curl -fsS -X DELETE "$SERVER_URL/v1/mcp-servers/time" | python -m json.tool
```

## 7. Skill 注册

Skill 是包含 `SKILL.md` 的目录。调用注册接口后，它会成为 `tool_type: "skill"` 的工具；推理时模型选择该 Skill 后，完整 `SKILL.md` 会注入上下文，模型再使用内置工具执行。

```bash
SKILL_DIR="$(pwd)/examples/skill"
curl -fsS -X POST "$SERVER_URL/v1/tools/skill" \
  -H 'Content-Type: application/json' \
  -d "$(python - <<PY
import json, os
print(json.dumps({"skill_dir": os.environ.get("SKILL_DIR", "$SKILL_DIR")}))
PY
)" | python -m json.tool
```

更稳妥的写法：

```bash
SKILL_DIR="$(pwd)/examples/skill"
python - <<PY | curl -fsS -X POST "$SERVER_URL/v1/tools/skill" -H 'Content-Type: application/json' -d @- | python -m json.tool
import json, os
print(json.dumps({"skill_dir": os.path.abspath(os.environ.get("SKILL_DIR", "$SKILL_DIR"))}))
PY
```

## 8. 提示模板管理

### 列出模板

```bash
curl -fsS "$SERVER_URL/v1/prompt-templates" | python -m json.tool
```

### 创建模板

```bash
curl -fsS -X POST "$SERVER_URL/v1/prompt-templates" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "template_id": "default-assistant",
  "content": "你是一个有帮助的助手。用户信息：{{USER_INFO}}"
}
JSON
```

### 更新或删除模板

```bash
curl -fsS -X PUT "$SERVER_URL/v1/prompt-templates/default-assistant" \
  -H 'Content-Type: application/json' \
  -d '{"template_id":"default-assistant","content":"你是一个有帮助的助手。"}' \
  | python -m json.tool

curl -fsS -X DELETE "$SERVER_URL/v1/prompt-templates/default-assistant" | python -m json.tool
```

## 9. 环境变量管理

这些变量由 server 的 `EnvManager` 管理，并同步到 server 进程环境中。

```bash
# 获取全部
curl -fsS "$SERVER_URL/v1/env" | python -m json.tool

# 设置或更新
curl -fsS -X POST "$SERVER_URL/v1/env" \
  -H 'Content-Type: application/json' \
  -d '{"key":"SUMMARY_MODEL_ID","value":"qwen3-14b"}' \
  | python -m json.tool

# 检测 server 代码中使用的环境变量 key
curl -fsS -X POST "$SERVER_URL/v1/env/detect" | python -m json.tool

# 删除
curl -fsS -X DELETE "$SERVER_URL/v1/env/SUMMARY_MODEL_ID" | python -m json.tool
```

## 10. 推理调用

### 非流式推理

必需字段：`model_id`（或使用 `agent_id`）。常用字段：`tool_ids`、`messages`、`text`、`max_tool_rounds`、`session_id`。

`session_id` 规则：

- 省略：无状态推理
- `"new"`：创建新会话并持久化
- 现有 session ID：恢复并追加会话；不存在则按无状态推理

```bash
curl -fsS -X POST "$SERVER_URL/v1/infer" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "model_id": "qwen3-14b",
  "tool_ids": ["write_file", "execute_command"],
  "messages": [
    {"role": "system", "content": "你是一个有帮助的助手。"},
    {"role": "user", "content": "请用一句话介绍你自己。"}
  ],
  "max_tool_rounds": 10,
  "session_id": "new"
}
JSON
```

### 使用 Agent 推理

当传入 `agent_id` 时，server 会使用该 Agent 的 `model_id`、`tool_ids` 和系统提示覆盖请求中的相关字段。

```bash
curl -fsS -X POST "$SERVER_URL/v1/infer" \
  -H 'Content-Type: application/json' \
  -d '{"agent_id":"250101_120000","messages":[{"role":"user","content":"你好"}],"session_id":"new"}' \
  | python -m json.tool
```

### 流式推理（SSE）

使用 `curl -N` 保持流式输出。首个 SSE event 通常是 `init`，之后是 `data: {...}`，结束为 `data: [DONE]`。响应头 `X-Session-Id` 或 init payload 中包含 session ID。

```bash
curl -N -X POST "$SERVER_URL/v1/infer/stream" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON'
{
  "model_id": "qwen3-14b",
  "tool_ids": ["write_file", "execute_command"],
  "messages": [
    {"role": "user", "content": "现在几点？如果需要可以使用工具。"}
  ],
  "max_tool_rounds": 10,
  "session_id": "new"
}
JSON
```

### 中止流式推理

```bash
curl -fsS -X POST "$SERVER_URL/v1/infer/abort" \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"<session_id>"}' \
  | python -m json.tool
```

## 11. 会话管理

### 列出、读取、删除会话

```bash
curl -fsS "$SERVER_URL/v1/sessions" | python -m json.tool
curl -fsS "$SERVER_URL/v1/sessions/<session_id>" | python -m json.tool
curl -fsS -X DELETE "$SERVER_URL/v1/sessions/<session_id>" | python -m json.tool
```

### 生成会话标题

```bash
curl -fsS -X POST "$SERVER_URL/v1/sessions/<session_id>/generate-title" | python -m json.tool
```

### 撤回会话消息

传入要撤回的用户消息 `timestamp`；server 会删除该消息及其后的所有消息。

```bash
curl -fsS -X POST "$SERVER_URL/v1/sessions/<session_id>/revoke" \
  -H 'Content-Type: application/json' \
  -d '{"timestamp":"2026-01-01T12:00:00"}' \
  | python -m json.tool
```

### 搜索会话

按关键词搜索会话标题和内容，返回匹配的会话列表。

```bash
curl -fsS "$SERVER_URL/v1/sessions/search?q=关键词" | python -m json.tool
```

参数说明：
- `q` (query string): 搜索关键词，匹配会话标题和内容

### 标记会话为已读

将指定会话标记为已读状态，清除 unread 标记。

```bash
curl -fsS -X POST "$SERVER_URL/v1/sessions/<session_id>/read" | python -m json.tool
```

### 订阅会话状态变更（SSE）

SSE 端点，用于实时推送会话状态变更。使用 `curl -N` 保持流式输出。

```bash
curl -N "$SERVER_URL/v1/sessions/events"
```

支持的事件类型：
- `streaming`：会话正在流式推理中
- `done_success_unread`：推理完成，有未读消息
- `deleted`：会话已删除

## 12. Agent 管理

### 列出或读取 Agent

```bash
curl -fsS "$SERVER_URL/v1/agents" | python -m json.tool
curl -fsS "$SERVER_URL/v1/agents/<agent_id>" | python -m json.tool
```

### 创建 Agent

必需字段：`model_id`、`nickname`。常用可选字段：`tool_ids`、`template_id`、`template_arguments`、`system_prompt`、`myself_view`、`description`、`avatar`。`agent_id` 由 server 生成，格式类似 `YYMMDD_HHMMSS`。

```bash
curl -fsS -X POST "$SERVER_URL/v1/agents" \
  -H 'Content-Type: application/json' \
  -d @- <<'JSON' | python -m json.tool
{
  "model_id": "qwen3-14b",
  "nickname": "助手",
  "tool_ids": ["write_file", "execute_command"],
  "system_prompt": "你是一个严谨、简洁的助手。",
  "description": "默认助手 Agent",
  "avatar": ""
}
JSON
```

### 更新或删除 Agent

更新接口接受部分字段，直接传要修改的键值。

```bash
curl -fsS -X PUT "$SERVER_URL/v1/agents/<agent_id>" \
  -H 'Content-Type: application/json' \
  -d '{"nickname":"新昵称","description":"更新后的描述"}' \
  | python -m json.tool

curl -fsS -X DELETE "$SERVER_URL/v1/agents/<agent_id>" | python -m json.tool
```

## 13. 常见任务模板

### 任务：检查 server 是否可用

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
if curl -fsS "$SERVER_URL/v1/models" >/tmp/runtime_models.json; then
  echo "OK: $SERVER_URL"
  python -m json.tool </tmp/runtime_models.json
else
  echo "无法连接 Agent Service: $SERVER_URL" >&2
  exit 1
fi
```

### 任务：注册当前 Skill

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
SKILL_DIR="$(pwd)/examples/skill"
python - <<PY | curl -fsS -X POST "$SERVER_URL/v1/tools/skill" -H 'Content-Type: application/json' -d @- | python -m json.tool
import json, os
print(json.dumps({"skill_dir": os.path.abspath("$SKILL_DIR")}))
PY
```

### 任务：一次性查看所有可操作资源

```bash
SERVER_URL="${RUNTIME_SERVER_URL:-${AGENT_SERVER_URL:-http://${RUNTIME_SERVER_HOST:-127.0.0.1}:${RUNTIME_SERVER_PORT:-7988}}}"
for path in /v1/models /v1/tools /v1/mcp-servers /v1/prompt-templates /v1/env /v1/sessions /v1/agents; do
  echo "===== $path ====="
  curl -fsS "$SERVER_URL$path" | python -m json.tool || true
  echo
 done
```

## 14. 故障排查

- `Connection refused`：server 未启动或 host/port 不正确。检查 `RUNTIME_SERVER_URL`、`RUNTIME_SERVER_HOST`、`RUNTIME_SERVER_PORT`。
- `404 Not found`：路径错误，注意不要遗漏 `/v1` 前缀。
- `400 Missing required field`：检查请求 JSON 必需字段。
- 注册 function tool 失败：确认 `function_file_path` 是 server 进程可访问的绝对路径，且文件内存在 `function_name`。
- 注册 Skill 失败：确认目录内存在 `SKILL.md`，文件以 YAML front matter 开头，且包含 `name` 和 `description`。
- 推理返回 `Model not found`：先 `GET /v1/models` 确认 `model_id` 已注册。
- 工具调用返回 `Tool not found`：先 `GET /v1/tools` 确认 `tool_id`，注意工具名 `name` 和工具 ID `tool_id` 不是一回事。
- SSE 输出看起来不是 JSON：这是正常的 Server-Sent Events 格式；逐行读取 `data:` 后面的内容即可。
