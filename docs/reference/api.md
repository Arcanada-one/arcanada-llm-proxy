# Reference — API

## `GET /health`

No auth. Returns:

```json
{"status": "ok", "version": "0.1.0"}
```

## `POST /v1/chat/completions`

OpenAI-compatible chat completion. Bearer auth required.

### Request

```json
{
  "model": "openrouter/auto",
  "messages": [
    {"role": "system", "content": "be concise"},
    {"role": "user", "content": "ping"}
  ],
  "max_tokens": 100,
  "temperature": 0.7,
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "result",
      "strict": true,
      "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": false}
    }
  }
}
```

### Routing

| `response_format.type` | Connector | MC payload |
|------------------------|-----------|------------|
| `json_schema`          | `codex`   | `{ jsonSchema: <schema>, responseFormat: {type: "json_object"} }` |
| `json_object`          | `openrouter` | `{ responseFormat: {type: "json_object"} }` |
| `text` / absent        | `openrouter` | `{ }` |

On Codex retryable error (`rate_limited`, `auth_error`, `execution_error`) the
request is retried against OpenRouter and `x_arcanada.fallback_reason` is set.

### Response

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "created": 1747845600,
  "model": "google/gemini-2.5-flash",
  "choices": [
    {"index": 0, "message": {"role": "assistant", "content": "{\"x\":\"pong\"}"}, "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
  "x_arcanada": {
    "route_taken": "openrouter",
    "fallback_reason": "none",
    "upstream_latency_ms": 700,
    "upstream_request_id": "c50ffdc1-…"
  }
}
```

### Errors

| Status | Reason |
|--------|--------|
| `400`  | `response_format.type=json_schema` without `json_schema.schema`. |
| `401`  | Missing or invalid Bearer token. |
| `502`  | Upstream returned a non-retryable error. |
| `503`  | Upstream returned a retryable error AND fallback also failed; or proxy not initialised. |

## Configuration

| Env | Default | Purpose |
|-----|---------|---------|
| `LLM_PROXY_SHARED_SECRET` | — | Bearer token. Required (or set `VAULT_*` for hvac fetch). |
| `MC_BASE_URL` | `http://127.0.0.1:3900` | Model Connector base URL. |
| `MC_API_KEY` | — | Model Connector Bearer token. |
| `LANGFUSE_OTLP_ENDPOINT` | (unset → tracing disabled) | OTLP HTTP traces endpoint. |
| `LANGFUSE_OTLP_HEADERS` | — | Comma-separated `k=v` headers (e.g. `Authorization=Bearer <key>`). |
| `VAULT_ADDR`, `VAULT_TOKEN` / `VAULT_ROLE_ID` + `VAULT_SECRET_ID` | — | Optional fallback for shared_secret. |
| `VAULT_KV_MOUNT` | `secret` | KV2 mount. |
| `VAULT_KV_PATH` | `arcanada/prod/env/llm-proxy.shared_secret` | KV2 path (relative to mount). |
| `VAULT_KV_FIELD` | `shared_secret` | Field inside KV2 secret. |
