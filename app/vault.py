"""Vault loader for shared_secret.

Two modes:
  - Direct env injection (sidecar / vault-env): read LLM_PROXY_SHARED_SECRET.
  - Direct hvac fetch: read VAULT_ADDR + VAULT_TOKEN (or AppRole) → KV2 path
    arcanada/prod/env/llm-proxy.shared_secret → field 'shared_secret'.

Fail-closed: startup raises if no secret resolved (Appendix A — fail-closed).
"""

import logging
import os

log = logging.getLogger("proxy.vault")


def load_shared_secret() -> str:
    direct = os.environ.get("LLM_PROXY_SHARED_SECRET", "").strip()
    if direct:
        log.info("shared_secret resolved from env LLM_PROXY_SHARED_SECRET")
        return direct

    vault_addr = os.environ.get("VAULT_ADDR", "").strip()
    if vault_addr:
        secret = _fetch_via_hvac(vault_addr)
        if secret:
            log.info("shared_secret resolved via hvac kv2")
            return secret

    raise RuntimeError(
        "shared_secret not available — set LLM_PROXY_SHARED_SECRET env or "
        "VAULT_ADDR + VAULT_TOKEN with KV2 read on arcanada/prod/env/llm-proxy.shared_secret"
    )


def _fetch_via_hvac(vault_addr: str) -> str | None:
    try:
        import hvac
    except ImportError:
        log.warning("hvac not installed — skipping vault fetch")
        return None

    token = os.environ.get("VAULT_TOKEN", "").strip()
    role_id = os.environ.get("VAULT_ROLE_ID", "").strip()
    secret_id = os.environ.get("VAULT_SECRET_ID", "").strip()
    namespace = os.environ.get("VAULT_NAMESPACE", "").strip() or None
    mount = os.environ.get("VAULT_KV_MOUNT", "secret").strip()
    path = os.environ.get(
        "VAULT_KV_PATH",
        "arcanada/prod/env/llm-proxy.shared_secret",
    ).strip()
    field = os.environ.get("VAULT_KV_FIELD", "shared_secret").strip()

    client = hvac.Client(url=vault_addr, namespace=namespace)
    if token:
        client.token = token
    elif role_id and secret_id:
        client.auth.approle.login(role_id=role_id, secret_id=secret_id)
    else:
        log.warning("VAULT_ADDR set but no auth method — skipping vault fetch")
        return None

    if not client.is_authenticated():
        log.warning("vault auth failed")
        return None

    resp = client.secrets.kv.v2.read_secret_version(path=path, mount_point=mount)
    data = (resp or {}).get("data", {}).get("data", {})
    value = data.get(field, "")
    return value.strip() if value else None
