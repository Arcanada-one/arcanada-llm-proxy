# arcanada-llm-proxy

> Жизнь одного человека имеет значение / One human life matters

OpenAI-compat shim over [Model Connector](https://github.com/Arcanada-one/model-connector).
Routes `response_format.type=json_schema` to **Codex** (Class B subscription, structured
outputs) and `json_object`/`text`/absent to **OpenRouter** (per-token, vendor-neutral
fallback). Codex 429 / `auth_error` falls back to OpenRouter automatically and surfaces
the reason in `response.x_arcanada.fallback_reason`.

Phase 2 of [CONN-0045 Codex ecosystem migration](https://github.com/Arcanada-one/datarim).
Promotes the [LTM-0004 v3 benchmark proxy](https://github.com/Arcanada-one/long-term-memory)
to a standalone production service.

## At a glance

- **Endpoint:** `POST /v1/chat/completions` (OpenAI-compat envelope).
- **Bind:** `127.0.0.1:4000` (Tier 1 per Network Exposure Baseline).
- **Auth:** Bearer token, Vault-managed (`arcanada/prod/env/llm-proxy.shared_secret`).
- **Observability:** OpenTelemetry → Langfuse OTLP HTTP, span `llm-proxy-request`.
- **Stack:** FastAPI 0.136.1, Starlette 1.0.0, httpx 0.28.1, Python 3.12.

## Documentation (Diátaxis)

- [`docs/tutorials/`](docs/tutorials/) — get started.
- [`docs/how-to/`](docs/how-to/) — solve a specific problem.
- [`docs/reference/`](docs/reference/) — API + config lookup.
- [`docs/explanation/`](docs/explanation/) — design background.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
LLM_PROXY_SHARED_SECRET=dev-secret uvicorn app.main:app --reload --port 4000
```

```bash
curl -s -X POST http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer dev-secret" \
  -H "Content-Type: application/json" \
  -d '{"model":"openrouter/auto","messages":[{"role":"user","content":"ping"}],"response_format":{"type":"json_object"}}'
```

## Testing

```bash
pytest --cov=app
pip-audit --strict -r requirements.txt
ruff check app/ tests/
```

## License

MIT (see [LICENSE](LICENSE)).
