"""Typed HTTP client for Model Connector :3900.

Source-of-truth for wire shape: datarim/tasks/CONN-0064-fixtures.md (live capture
2026-05-10). MC contract: HTTP 200 with body.status="error" for retryable failures
(NOT 4xx); 4xx only for validation. Caller MUST inspect body.status, not just HTTP code.
"""

import logging
import os

import httpx

from .schemas import MCExecuteRequest, MCResponse

log = logging.getLogger("proxy.mc")


class MCClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 300.0):
        self.base_url = (base_url or os.environ.get("MC_BASE_URL") or "http://127.0.0.1:3900").rstrip("/")
        self.api_key = api_key or os.environ.get("MC_API_KEY", "")
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def execute(self, connector: str, body: MCExecuteRequest) -> MCResponse:
        client = await self._ensure_client()
        url = f"{self.base_url}/connectors/{connector}/execute"
        payload = body.model_dump(exclude_none=True)
        try:
            r = await client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.warning("mc network error connector=%s: %s", connector, exc)
            return _synthetic_error("network_error", str(exc), retryable=True)

        # MC returns structured ConnectorResponse on retryable failures with HTTP
        # 5xx (circuit_open, auth_error, queue_timeout) or 429 (rate_limited).
        # Try to parse the body before falling back to synthetic shapes — the
        # body's error.type carries finer-grained recovery hints.
        if r.status_code >= 400 and r.status_code < 600:
            try:
                parsed = MCResponse.model_validate(r.json())
                if parsed.status == "error":
                    return parsed
            except Exception:
                pass
            if r.status_code in (401, 403):
                return _synthetic_error("auth_error", f"MC returned {r.status_code}", retryable=False)
            if r.status_code == 429:
                return _synthetic_error("rate_limited", "MC returned 429", retryable=True)
            if r.status_code >= 500:
                return _synthetic_error("upstream_5xx", r.text[:500], retryable=True)
            return _synthetic_error("validation_error", r.text[:500], retryable=False)

        try:
            return MCResponse.model_validate(r.json())
        except Exception as exc:
            log.warning("mc response parse error connector=%s: %s", connector, exc)
            return _synthetic_error("parse_error", str(exc), retryable=False)


def _synthetic_error(error_type: str, message: str, *, retryable: bool) -> MCResponse:
    from .schemas import MCError, MCUsage  # local import to avoid cycle

    return MCResponse(
        id="synthetic",
        result="",
        usage=MCUsage(),
        latencyMs=0,
        queueWaitMs=0,
        status="error",
        error=MCError(type=error_type, message=message, retryable=retryable),
    )
