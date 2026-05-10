"""End-to-end FastAPI tests with respx-mocked Model Connector.

Verifies:
  - GET /health returns 200 + version
  - POST /v1/chat/completions auth gate (401 missing/invalid token)
  - json_object route → openrouter, response shape OpenAI-compat
  - json_schema route → codex, fallback to openrouter on retryable error
  - x_arcanada extension carries route_taken + fallback_reason
"""

import os

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from app.auth import set_shared_secret
from app.main import app, mc_client

SECRET = "test-shared-secret"


@pytest.fixture(autouse=True)
def _setup():
    os.environ["LLM_PROXY_SHARED_SECRET"] = SECRET
    set_shared_secret(SECRET)
    yield


@pytest.fixture
def client():
    mc_client.base_url = "http://mc.test:3900"
    mc_client.api_key = "test-mc-key"
    with TestClient(app) as c:
        yield c


def _openrouter_success() -> Response:
    return Response(
        200,
        json={
            "id": "c50ffdc1",
            "connector": "openrouter",
            "model": "google/gemini-2.5-flash",
            "result": '{"answer":"pong"}',
            "structured": {"answer": "pong"},
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15, "costUsd": 0},
            "latencyMs": 700,
            "queueWaitMs": 0,
            "status": "success",
            "attempt": 1,
            "maxAttempts": 1,
        },
    )


def _codex_retryable_error() -> Response:
    return Response(
        200,
        json={
            "id": "4c74b98c",
            "connector": "codex",
            "model": "o4-mini",
            "result": "",
            "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "costUsd": 0},
            "latencyMs": 29,
            "queueWaitMs": 0,
            "status": "error",
            "error": {"type": "execution_error", "message": "boom", "retryable": True},
            "attempt": 2,
            "maxAttempts": 2,
        },
    )


def test_health_no_auth_required(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_chat_missing_token_401(client):
    r = client.post("/v1/chat/completions", json={"model": "openrouter/auto", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 401


def test_chat_wrong_token_401(client):
    r = client.post(
        "/v1/chat/completions",
        json={"model": "openrouter/auto", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


@respx.mock
def test_chat_json_object_routes_to_openrouter(client):
    respx.post("http://mc.test:3900/connectors/openrouter/execute").mock(return_value=_openrouter_success())
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "openrouter/auto",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 50,
            "response_format": {"type": "json_object"},
        },
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == '{"answer":"pong"}'
    assert body["usage"]["prompt_tokens"] == 10
    assert body["x_arcanada"]["route_taken"] == "openrouter"
    assert body["x_arcanada"]["fallback_reason"] == "none"


@respx.mock
def test_chat_json_schema_falls_back_to_openrouter_on_codex_retryable(client):
    respx.post("http://mc.test:3900/connectors/codex/execute").mock(return_value=_codex_retryable_error())
    respx.post("http://mc.test:3900/connectors/openrouter/execute").mock(return_value=_openrouter_success())
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "openrouter/auto",
            "messages": [{"role": "user", "content": "ping"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "t",
                    "strict": True,
                    "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False},
                },
            },
        },
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["x_arcanada"]["route_taken"] == "openrouter"
    assert body["x_arcanada"]["fallback_reason"] == "auth_error"


@respx.mock
def test_chat_json_schema_429_fallback_marks_429(client):
    respx.post("http://mc.test:3900/connectors/codex/execute").mock(return_value=Response(429))
    respx.post("http://mc.test:3900/connectors/openrouter/execute").mock(return_value=_openrouter_success())
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "openrouter/auto",
            "messages": [{"role": "user", "content": "ping"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "t",
                    "strict": True,
                    "schema": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False},
                },
            },
        },
        headers={"Authorization": f"Bearer {SECRET}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["x_arcanada"]["route_taken"] == "openrouter"
    assert body["x_arcanada"]["fallback_reason"] == "429"
