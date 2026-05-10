# Tutorial — Get arcanada-llm-proxy running locally

End state: a local instance of `arcanada-llm-proxy` answering OpenAI-compat
`POST /v1/chat/completions`, routing to a real Model Connector instance.

## Prerequisites

- Python 3.12.
- A reachable Model Connector — either local Docker (port 3900) or via Tailscale to `arcana-prod` (`http://100.121.155.54:3900`).
- A valid Model Connector API key (`Authorization: Bearer mc-…`).

## Steps

### 1. Clone + venv

```bash
git clone https://github.com/Arcanada-one/arcanada-llm-proxy.git
cd arcanada-llm-proxy
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

### 2. Set environment

```bash
export LLM_PROXY_SHARED_SECRET=dev-secret
export MC_BASE_URL=http://127.0.0.1:3900   # or Tailscale-routed endpoint
export MC_API_KEY=mc-…                     # MC API key (DB-issued)
```

### 3. Run

```bash
uvicorn app.main:app --reload --port 4000
```

You should see:

```
INFO arcanada-llm-proxy 0.1.0 ready
```

### 4. Test

```bash
curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{"model":"openrouter/auto","messages":[{"role":"user","content":"ping"}],"response_format":{"type":"json_object"}}' \
  | jq .
```

Look for `x_arcanada.route_taken == "openrouter"` in the response.

### 5. Try a `json_schema` request

```bash
curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openrouter/auto",
    "messages": [{"role":"user","content":"Whiskers, 3 years"}],
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "pet",
        "strict": true,
        "schema": {
          "type": "object",
          "properties": {"name":{"type":"string"},"age":{"type":"integer"}},
          "required": ["name","age"],
          "additionalProperties": false
        }
      }
    }
  }' | jq .
```

`route_taken` should be `"codex"` (or `"openrouter"` with `fallback_reason="auth_error"` if Codex is unhealthy — see [`how-to/troubleshoot-codex-fallback.md`](../how-to/troubleshoot-codex-fallback.md)).

## Next

- [Reference: full request/response shape](../reference/api.md)
- [Explanation: why proxy and not direct MC](../explanation/architecture.md)
