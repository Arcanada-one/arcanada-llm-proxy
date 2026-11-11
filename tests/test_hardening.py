"""#6519 hardening: /v1 network allowlist, disabled docs, log redaction."""

import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.auth import set_shared_secret
from app.main import app
from app.network import (
    DEFAULT_ALLOWED_NETWORKS,
    client_allowed,
    parse_allowed_networks,
)
from app.observability import SecretRedactor, install_log_redaction

SECRET = "test-shared-secret"


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setenv("LLM_PROXY_SHARED_SECRET", SECRET)
    set_shared_secret(SECRET)


def _client(host: str) -> TestClient:
    return TestClient(app, client=(host, 50000))


# --- network allowlist -------------------------------------------------------


def test_default_networks_parse() -> None:
    nets = parse_allowed_networks(None)
    assert [str(n) for n in nets] == list(DEFAULT_ALLOWED_NETWORKS)
    assert parse_allowed_networks("  ") == nets


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "172.30.0.1", "10.1.2.3", "192.168.5.5", "100.90.7.20", "::1", "::ffff:172.30.0.1"],
)
def test_private_and_loopback_clients_allowed(host: str) -> None:
    assert client_allowed(host, parse_allowed_networks(None))


@pytest.mark.parametrize("host", ["203.0.113.7", "8.8.8.8", "2001:db8::1", "testclient", "", None])
def test_public_or_non_ip_clients_refused(host) -> None:
    assert not client_allowed(host, parse_allowed_networks(None))


def test_custom_networks_replace_default() -> None:
    nets = parse_allowed_networks("127.0.0.1/32, 172.30.0.0/16")
    assert client_allowed("172.30.0.1", nets)
    assert not client_allowed("10.0.0.1", nets)


@pytest.mark.parametrize("raw", ["not-a-cidr", "10.0.0.1/8", ",", "127.0.0.0/8,300.1.1.1/32"])
def test_invalid_allowlist_fails_closed(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_allowed_networks(raw)


def test_public_client_gets_403_before_auth() -> None:
    with _client("203.0.113.7") as c:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
            headers={"Authorization": f"Bearer {SECRET}"},
        )
    assert r.status_code == 403
    assert r.json() == {"detail": "forbidden"}


def test_public_client_refused_on_unknown_v1_path() -> None:
    with _client("203.0.113.7") as c:
        assert c.get("/v1/models").status_code == 403


def test_health_stays_open_for_probes() -> None:
    with _client("203.0.113.7") as c:
        assert c.get("/health").status_code == 200


def test_private_client_reaches_auth_gate() -> None:
    with _client("172.30.0.1") as c:
        r = c.post("/v1/chat/completions", json={"model": "auto", "messages": []})
    assert r.status_code == 401


# --- docs disabled ------------------------------------------------------------


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
def test_docs_and_schema_not_served(path: str) -> None:
    with _client("127.0.0.1") as c:
        assert c.get(path).status_code == 404


# --- redaction ----------------------------------------------------------------


def _record(msg: str, exc_info=None) -> logging.LogRecord:
    return logging.LogRecord("t", logging.ERROR, "", 0, msg, (), exc_info)


@pytest.mark.parametrize(
    "token",
    [
        "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "gho_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
        "github_pat_" + "11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz",
        "sk-" + "proj-abcdefghijklmnop0123456789",
        "AKIA" + "IOSFODNN7EXAMPLE",
    ],
)
def test_token_shapes_redacted(token: str) -> None:
    rec = _record(f"upstream said: {token} end")
    SecretRedactor().filter(rec)
    out = rec.getMessage()
    assert token not in out
    assert "REDACTED" in out
    assert out.endswith(" end")


def test_mc_api_key_value_redacted() -> None:
    key = "fixture-" + "k" * 24  # synthetic, built at runtime
    rec = _record(f"calling MC with {key}")
    SecretRedactor(extra_secrets=[key]).filter(rec)
    assert key not in rec.getMessage()


def test_traceback_and_stack_redacted() -> None:
    key = "fixture-" + "k" * 24  # synthetic, built at runtime
    try:
        raise RuntimeError(f"boom Authorization: Bearer {key} ghp_{'Z' * 36}")
    except RuntimeError:
        rec = _record("request failed", exc_info=sys.exc_info())
    rec.stack_info = f"Stack (most recent call last):\n  token {key}"
    SecretRedactor(extra_secrets=[key]).filter(rec)
    rendered = logging.Formatter().format(rec)
    assert "RuntimeError" in rendered
    assert key not in rendered
    assert "Z" * 36 not in rendered


def test_install_covers_uvicorn_handlers() -> None:
    logger = logging.getLogger("uvicorn.error")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        f = install_log_redaction(extra_secrets=["topsecret-value-123"])
        assert f in handler.filters
        assert f in logger.filters
    finally:
        logger.removeHandler(handler)
        logger.removeFilter(f)
        logging.getLogger().removeFilter(f)
        for name in ("uvicorn", "uvicorn.access"):
            logging.getLogger(name).removeFilter(f)
