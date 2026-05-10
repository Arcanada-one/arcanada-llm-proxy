# How-to — Deploy `arcanada-llm-proxy` to arcana-prod

Triggered automatically by `git push origin main` via the self-hosted CI runner.
Manual deploy is below.

## Prerequisites

- Vault path `arcanada/prod/env/llm-proxy.shared_secret` populated (`shared_secret` field).
- GitHub Secrets on the repo: `LLM_PROXY_SHARED_SECRET`, `MC_API_KEY`, `OPS_BOT_TOKEN`,
  optionally `LANGFUSE_OTLP_ENDPOINT` + `LANGFUSE_OTLP_HEADERS`.
- arcana-prod self-hosted runner labels: `self-hosted, linux, arcana-prod, docker`.

## Manual deploy

```bash
ssh root@arcana-prod
mkdir -p /opt/arcanada-llm-proxy && cd /opt/arcanada-llm-proxy
git pull origin main || git clone https://github.com/Arcanada-one/arcanada-llm-proxy.git .
export LLM_PROXY_SHARED_SECRET=$(vault kv get -field=shared_secret arcanada/prod/env/llm-proxy)
export MC_API_KEY=$(vault kv get -field=api_key arcanada/prod/env/llm-proxy)
docker compose up -d --build
curl -fsS http://127.0.0.1:4000/health
```

## Health check

`GET /health` returns `{"status":"ok","version":"0.1.0"}`. The CI runner waits up to
30 seconds; on failure it posts a `category: fatal` event to Ops Bot at
`https://ops.arcanada.one/events`.

## Rollback

```bash
docker compose down
git reset --hard <previous-sha>
docker compose up -d --build
```

Or flip an env-only kill switch (zero-downtime):

```bash
LLM_PROXY_FORCE_ROUTE=openrouter docker compose up -d
```
