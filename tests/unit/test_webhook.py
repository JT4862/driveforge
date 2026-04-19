from __future__ import annotations

import asyncio

import httpx
import pytest

from driveforge.core import webhook


def test_empty_url_returns_false_immediately() -> None:
    result = asyncio.run(webhook.dispatch(None, {"event": "test"}))
    assert result is False


def test_successful_post_returns_true(monkeypatch: pytest.MonkeyPatch) -> None:
    class _OKTransport(httpx.MockTransport):
        pass

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    async def fake_send(self, request, **kw):  # noqa: ANN001
        return handler(request)

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    result = asyncio.run(webhook.dispatch("https://example.invalid/hook", {"event": "test"}))
    assert result is True


def test_failing_post_eventually_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def fake_send(self, request, **kw):  # noqa: ANN001
        return handler(request)

    async def fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(httpx.AsyncClient, "send", fake_send)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    result = asyncio.run(
        webhook.dispatch("https://example.invalid/hook", {"event": "test"}, attempts=2)
    )
    assert result is False
