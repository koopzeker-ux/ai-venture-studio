import asyncio

import httpx

from app.core.config import settings
from app.services import telegram as telegram_module


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://api.telegram.org/fake")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("error", request=request, response=response)


class _FakeAsyncClient:
    def __init__(self, response=None, exc=None, **kwargs):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def post(self, url, json=None):
        if self._exc:
            raise self._exc
        return self._response


def _run(coro):
    return asyncio.run(coro)


def test_send_without_credentials_short_circuits(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", None)
    monkeypatch.setattr(settings, "telegram_chat_id", None)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("HTTP client should not be constructed without credentials")

    monkeypatch.setattr(telegram_module.httpx, "AsyncClient", fail_if_called)

    assert _run(telegram_module.send_telegram_message("hello")) is False


def test_send_success(monkeypatch):
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_chat_id", "999")
    monkeypatch.setattr(
        telegram_module.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=_FakeResponse(200)),
    )

    assert _run(telegram_module.send_telegram_message("hello")) is True


def test_send_http_error_returns_false_without_raising(monkeypatch):
    """A Telegram API error (e.g. bad token, rate limit) must not raise."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_chat_id", "999")
    monkeypatch.setattr(
        telegram_module.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(response=_FakeResponse(500)),
    )

    assert _run(telegram_module.send_telegram_message("hello")) is False


def test_send_network_error_returns_false_without_raising(monkeypatch):
    """Telegram being unreachable (timeout/connect error) must not raise."""
    monkeypatch.setattr(settings, "telegram_bot_token", "123:abc")
    monkeypatch.setattr(settings, "telegram_chat_id", "999")
    monkeypatch.setattr(
        telegram_module.httpx,
        "AsyncClient",
        lambda **kwargs: _FakeAsyncClient(exc=httpx.ConnectError("boom")),
    )

    assert _run(telegram_module.send_telegram_message("hello")) is False
