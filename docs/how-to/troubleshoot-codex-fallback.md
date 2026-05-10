# How-to — Diagnose Codex fallback to OpenRouter

Symptom: `response.x_arcanada.fallback_reason` is `"auth_error"` or `"429"`.

## Decision tree

| `fallback_reason` | Meaning | Next step |
|-------------------|---------|-----------|
| `none` | No fallback — request was served by the original route. | None. |
| `429` | Codex returned HTTP 429 (rate-limited / credits depleted). | Check ChatGPT subscription tier (Pro = ~20× Plus). Wait for reset window. |
| `auth_error` | Codex returned 401/403 OR sidecar permission failure. | Check Codex OAuth blob freshness in Vault (`arcanada/prod/env/codex-cli.oauth_credentials`). Re-run `codex login` on Mac and re-upload. |
| `validation_error` | Proxy could not build a valid MC payload. | Inspect proxy logs for `app.router` ValueError. |

## Known issue (2026-05-10)

`CONN-0079` (sidecar `is_tmpfs_mount` regression) makes the `codex` route return
`error.type == "execution_error"` with retryable=true. The proxy treats this as
`fallback_reason = "auth_error"` and falls back to OpenRouter. Once `CONN-0079`
ships, the codex path will return `success` again with no fallback.

## Verifying upstream

```bash
# Codex via MC directly (bypassing proxy)
curl -s -H "Authorization: Bearer $MC_API_KEY" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:3900/connectors/codex/execute \
  -d '{"prompt":"ping","maxTokens":10}' | jq .status
```

If this returns `"error"`, the issue is upstream of the proxy.
