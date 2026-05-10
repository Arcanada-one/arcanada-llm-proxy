"""Bearer-token auth guard.

Static comparison against Vault-managed shared_secret loaded at startup.
Constant-time compare via secrets.compare_digest to avoid timing leaks.
"""

import logging
import secrets

from fastapi import Header, HTTPException, status

log = logging.getLogger("proxy.auth")


class _Secret:
    """Single-slot holder set once at startup (see app.vault.load_shared_secret)."""

    value: str | None = None


def set_shared_secret(value: str) -> None:
    if not value:
        raise ValueError("shared_secret must be non-empty")
    _Secret.value = value
    log.info("shared_secret loaded (len=%d)", len(value))


def _extract_bearer(header: str | None) -> str | None:
    if not header:
        return None
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


async def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if _Secret.value is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="proxy not initialised: shared_secret missing",
        )
    token = _extract_bearer(authorization)
    if token is None or not secrets.compare_digest(token, _Secret.value):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing Bearer token",
        )
