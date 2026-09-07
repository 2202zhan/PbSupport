from unittest.mock import AsyncMock

import pytest
from openai import APIConnectionError, AuthenticationError

import openai_utils


def _connection_error() -> APIConnectionError:
    return APIConnectionError(request=AsyncMock())


def _auth_error() -> AuthenticationError:
    return AuthenticationError(message="bad key", response=AsyncMock(status_code=401, headers={}), body=None)


async def test_succeeds_on_first_try_without_retry(monkeypatch):
    monkeypatch.setattr(openai_utils.asyncio, "sleep", AsyncMock())
    call = AsyncMock(return_value="ok")
    result = await openai_utils.call_with_one_retry(call)
    assert result == "ok"
    assert call.call_count == 1


async def test_retries_once_on_transient_error_then_succeeds(monkeypatch):
    monkeypatch.setattr(openai_utils.asyncio, "sleep", AsyncMock())
    call = AsyncMock(side_effect=[_connection_error(), "ok"])
    result = await openai_utils.call_with_one_retry(call)
    assert result == "ok"
    assert call.call_count == 2


async def test_propagates_after_second_transient_failure(monkeypatch):
    monkeypatch.setattr(openai_utils.asyncio, "sleep", AsyncMock())
    call = AsyncMock(side_effect=[_connection_error(), _connection_error()])
    with pytest.raises(APIConnectionError):
        await openai_utils.call_with_one_retry(call)
    assert call.call_count == 2


async def test_non_transient_error_is_not_retried():
    call = AsyncMock(side_effect=_auth_error())
    with pytest.raises(AuthenticationError):
        await openai_utils.call_with_one_retry(call)
    assert call.call_count == 1
