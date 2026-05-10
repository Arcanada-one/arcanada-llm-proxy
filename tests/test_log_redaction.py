"""SecretRedactor logging filter — bearer + shared_secret substring redaction.

Defensive guard: current code paths do not log Authorization headers, but a
future contributor logging request.headers must not leak the shared_secret.
"""

import logging

import pytest

from app.observability import SecretRedactor, install_log_redaction


def _make_record(msg: str, args: tuple = ()) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="t", level=logging.INFO, pathname="", lineno=0,
        msg=msg, args=args, exc_info=None,
    )
    return rec


def test_authorization_header_redacted():
    f = SecretRedactor()
    rec = _make_record("incoming Authorization: Bearer abc.def.ghi tail")
    assert f.filter(rec) is True
    out = rec.getMessage()
    assert "abc.def.ghi" not in out
    assert "***REDACTED***" in out
    assert "tail" in out


def test_bare_bearer_token_redacted():
    f = SecretRedactor()
    rec = _make_record("token=Bearer eyJhbGciOiJIUzI1NiJ9 rest")
    f.filter(rec)
    out = rec.getMessage()
    assert "eyJhbGciOiJIUzI1NiJ9" not in out
    assert "***REDACTED***" in out


def test_shared_secret_substring_redacted_when_extra_provided():
    f = SecretRedactor(extra_secrets=["TOPSECRET-ABCDEF-1234567890"])
    rec = _make_record("loaded TOPSECRET-ABCDEF-1234567890 at startup")
    f.filter(rec)
    out = rec.getMessage()
    assert "TOPSECRET-ABCDEF-1234567890" not in out
    assert "***REDACTED***" in out


def test_short_secret_skipped():
    """Defensive — never accept secrets shorter than 8 chars (false-positive risk)."""
    f = SecretRedactor(extra_secrets=["short", ""])
    rec = _make_record("nothing to redact here")
    f.filter(rec)
    assert rec.getMessage() == "nothing to redact here"


def test_install_attaches_filter_to_root():
    root = logging.getLogger()
    before = len(root.filters)
    install_log_redaction(extra_secrets=["UNIQ-FILTER-SENTINEL-12345"])
    try:
        assert len(root.filters) > before
        rec = _make_record("see UNIQ-FILTER-SENTINEL-12345 here")
        # Apply all filters manually since LogRecord.getMessage doesn't run them.
        for filt in root.filters:
            filt.filter(rec)
        assert "UNIQ-FILTER-SENTINEL-12345" not in rec.getMessage()
    finally:
        # Cleanup — never leave global state polluted across test files.
        for filt in list(root.filters):
            if isinstance(filt, SecretRedactor):
                root.removeFilter(filt)


@pytest.mark.parametrize(
    "msg",
    [
        "plain log line",
        "request to https://example.com/path?id=42",
        "Authorization header omitted by design",
    ],
)
def test_no_bearer_no_change(msg):
    f = SecretRedactor()
    rec = _make_record(msg)
    f.filter(rec)
    assert rec.getMessage() == msg
