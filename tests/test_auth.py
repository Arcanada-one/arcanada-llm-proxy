"""Bearer token guard — reject missing/invalid, accept exact match (constant-time)."""

import pytest
from fastapi import HTTPException

from app.auth import _Secret, require_bearer, set_shared_secret


@pytest.fixture(autouse=True)
def _reset_secret():
    saved = _Secret.value
    yield
    _Secret.value = saved


@pytest.mark.asyncio
async def test_missing_secret_returns_503():
    _Secret.value = None
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization="Bearer abc")
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_missing_header_returns_401():
    set_shared_secret("s3cret")
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization=None)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_wrong_token_returns_401():
    set_shared_secret("s3cret")
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization="Bearer nope")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_non_bearer_scheme_returns_401():
    set_shared_secret("s3cret")
    with pytest.raises(HTTPException) as exc:
        await require_bearer(authorization="Basic czNjcmV0")
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_passes():
    set_shared_secret("s3cret")
    await require_bearer(authorization="Bearer s3cret")


def test_set_empty_secret_raises():
    with pytest.raises(ValueError):
        set_shared_secret("")
