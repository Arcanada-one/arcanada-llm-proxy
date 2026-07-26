# Explanation — Why a proxy, not direct MC calls?

## Context

[CONN-0045](https://github.com/Arcanada-one/datarim) (Codex ecosystem migration) needed a way to route
structured-output requests to **Codex** (Class B subscription, supports OpenAI Structured
Outputs) and everything else to **OpenRouter** (per-token, vendor-neutral). The
Model Connector exposes its own protocol — not OpenAI-compat — and that is intentional
(MC is a connector router, not an OpenAI gateway).

Three approaches were on the table:

| Approach | Tradeoff |
|----------|----------|
| A. Standalone OpenAI-compat proxy (this) | Single integration point for any OpenAI-aware client (Graphiti, Cognee, future tools). MC stays single-purpose. |
| B. Per-engine MC subclasses | Each KG engine forks `OpenAIGenericClient`. DRY violation — N integrations to maintain. |
| C. Add `/v1/chat/completions` to MC | MC scope creep — was vendor-neutral router, now also OpenAI gateway. Single-responsibility violation. |

We picked A.

## The translation problem

OpenAI API:

```json
{"response_format": {"type": "json_schema", "json_schema": {"schema": {...}}}}
```

Model Connector API (verified live 2026-05-10, fixture F3):

```json
{"responseFormat": {"type": "json_object"}, "jsonSchema": {...}}
```

The schema body lives in a **separate top-level field** (`jsonSchema`), not inside
`responseFormat`. MC's Zod validator rejects `responseFormat.type=json_schema` outright.
Without this fixture capture, an early proxy implementation would have looked correct on
paper and failed at runtime.

## Why `127.0.0.1:4000`

[CLAUDE.md § Network Exposure Baseline](https://github.com/Arcanada-one/datarim) Tier 1
(loopback) is the default for ecosystem-internal services. Tailscale CGNAT (Tier 2)
reachability comes via the host network — Docker's `100.64.0.0/10` bind is unnecessary
because the host already has Tailscale routes. The proxy is **Internal-only** by design
(PRD-CONN-0045 R1: ToS grey zone for Codex CLI programmatic use).

## Why FastAPI 0.136.1 + Starlette 1.3.1

The previous LTM-0004 v3 benchmark proxy pinned `fastapi==0.115.12`, which transitively
brought in `starlette<0.49.0`. Starlette 0.48.0 carries CVE-2025-62727 (directory
traversal). Bumping FastAPI to 0.136.1 unlocks the 1.x line; Starlette 1.3.1
includes the original fix plus the subsequently published 1.0.x security fixes.
`pip-audit --strict` is clean against this lock (verified at `/dr-plan` time, see
`datarim/plans/CONN-0064-plan.md` § Live Audit Checkpoint).

## Why fail-closed on missing `shared_secret`

[CLAUDE.md § Backend Stack Standards / Appendix A](https://github.com/Arcanada-one/datarim)
mandates fail-closed for auth surfaces. If the Vault lookup fails AND the env var is
unset, the lifespan handler raises and the container exits. This is louder than serving
unauthenticated requests for a window during startup.

## Why a `mc:execute` scope declaration without Auth Arcana wiring

The proxy is the call site that drives MC `/connectors/{name}/execute`. Declaring
`scopes_consumed: [mc:execute]` in `auth.dependencies.yaml` makes the dependency graph
correct from day one. Once Auth Arcana service-account flow ships (AUTH-0070+), the
proxy itself will mint a service-account JWT carrying that scope and replace the
shared_secret bearer.
