"""MC client — uses captured fixtures from CONN-0064-fixtures.md as source of truth.

F2: openrouter json_object success → status:success, structured field present
F3: openrouter responseFormat.type=json_schema → 400 validation_error
F4: codex /execute → status:error, error.type=execution_error, retryable=true
"""

import pytest
import respx
from httpx import Response

from app.mc_client import MCClient
from app.schemas import MCExecuteRequest, MCResponseFormat


@pytest.fixture
def client():
    return MCClient(base_url="http://mc.test:3900", api_key="test-key")


@pytest.mark.asyncio
@respx.mock
async def test_openrouter_success_parses_response(client):
    respx.post("http://mc.test:3900/connectors/openrouter/execute").mock(
        return_value=Response(
            200,
            json={
                "id": "c50ffdc1-edf5-40a7-82b5-d51009280647",
                "connector": "openrouter",
                "model": "google/gemini-2.5-flash",
                "result": '{"answer":"pong"}',
                "structured": {"answer": "pong"},
                "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15, "costUsd": 0},
                "latencyMs": 700,
                "queueWaitMs": 0,
                "status": "success",
                "attempt": 1,
                "maxAttempts": 2,
            },
        )
    )
    req = MCExecuteRequest(
        prompt="ping",
        model="google/gemini-2.5-flash",
        responseFormat=MCResponseFormat(type="json_object"),
        maxTokens=50,
    )
    r = await client.execute("openrouter", req)
    assert r.status == "success"
    assert r.usage.inputTokens == 10
    assert r.usage.outputTokens == 5
    assert r.latencyMs == 700
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_validation_400_becomes_synthetic_error(client):
    respx.post("http://mc.test:3900/connectors/openrouter/execute").mock(
        return_value=Response(400, json={"message": "Validation failed", "errors": ["responseFormat.type: bad"]})
    )
    req = MCExecuteRequest(prompt="ping")
    r = await client.execute("openrouter", req)
    assert r.status == "error"
    assert r.error is not None
    assert r.error.type == "validation_error"
    assert r.error.retryable is False
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_codex_retryable_error_passes_through(client):
    respx.post("http://mc.test:3900/connectors/codex/execute").mock(
        return_value=Response(
            200,
            json={
                "id": "4c74b98c-917f-4481-887f-324d1ce5e8c0",
                "connector": "codex",
                "model": "o4-mini",
                "result": "",
                "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "costUsd": 0},
                "latencyMs": 29,
                "queueWaitMs": 0,
                "status": "error",
                "error": {
                    "type": "execution_error",
                    "message": "Permission denied (os error 13)",
                    "retryable": True,
                    "recommendation": "retry",
                },
                "attempt": 2,
                "maxAttempts": 2,
            },
        )
    )
    req = MCExecuteRequest(prompt="ping")
    r = await client.execute("codex", req)
    assert r.status == "error"
    assert r.error is not None
    assert r.error.type == "execution_error"
    assert r.error.retryable is True
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_429_becomes_rate_limited(client):
    respx.post("http://mc.test:3900/connectors/codex/execute").mock(return_value=Response(429))
    req = MCExecuteRequest(prompt="ping")
    r = await client.execute("codex", req)
    assert r.status == "error"
    assert r.error is not None
    assert r.error.type == "rate_limited"
    assert r.error.retryable is True
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_401_becomes_auth_error(client):
    respx.post("http://mc.test:3900/connectors/codex/execute").mock(return_value=Response(401))
    req = MCExecuteRequest(prompt="ping")
    r = await client.execute("codex", req)
    assert r.status == "error"
    assert r.error is not None
    assert r.error.type == "auth_error"
    assert r.error.retryable is False
    await client.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_5xx_unparseable_body_logs_warning(client, caplog):
    """MC contract drift visibility — 5xx with non-JSON body must log a warning
    so the synthetic upstream_5xx fallback does not silently mask schema breaks."""
    respx.post("http://mc.test:3900/connectors/codex/execute").mock(
        return_value=Response(503, text="<html>upstream gateway error</html>")
    )
    req = MCExecuteRequest(prompt="ping")
    with caplog.at_level("WARNING", logger="proxy.mc"):
        r = await client.execute("codex", req)
    assert r.status == "error"
    assert r.error is not None
    assert r.error.type == "upstream_5xx"
    assert any(
        "5xx body parse failed" in rec.getMessage() for rec in caplog.records
    ), f"expected 'mc 5xx body parse failed' warning; got {[r.getMessage() for r in caplog.records]}"
    await client.aclose()
