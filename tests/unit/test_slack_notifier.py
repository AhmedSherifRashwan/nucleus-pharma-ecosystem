"""Slack delivery behaviour (PRD s17, Phase 11)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.slack.notifier import SlackNotifier, resolve_bot_token


class FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, payload: dict, status: int = 200):
        self.payload = payload
        self.status = status
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


@pytest.fixture
def patched(monkeypatch):
    def install(payload, status=200):
        transport = FakeTransport(payload, status)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return transport

    return install


async def test_post_returns_the_message_ts(patched):
    transport = patched({"ok": True, "ts": "1788.42"})
    ts = await SlackNotifier(token="xoxb-test").post(
        channel="C1", blocks=[{"type": "section"}], text="hello"
    )
    assert ts == "1788.42"
    body = json.loads(transport.requests[0].content)
    assert body["channel"] == "C1"
    assert body["text"] == "hello"


async def test_thread_ts_is_passed_so_replies_stay_in_thread(patched):
    transport = patched({"ok": True, "ts": "1"})
    await SlackNotifier(token="xoxb-test").post(
        channel="C1", blocks=[], text="x", thread_ts="900.1"
    )
    assert json.loads(transport.requests[0].content)["thread_ts"] == "900.1"


async def test_slack_error_returns_none_rather_than_raising(patched):
    """A Slack outage must not fail an otherwise-complete pharmaceutical run."""
    patched({"ok": False, "error": "channel_not_found"})
    assert await SlackNotifier(token="xoxb-test").post(channel="C1", blocks=[], text="x") is None


async def test_transport_failure_returns_none(monkeypatch):
    class Boom(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            raise httpx.ConnectError("refused")

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda *a, **k: original(*a, **{**k, "transport": Boom()})
    )
    assert await SlackNotifier(token="xoxb-test").post(channel="C1", blocks=[], text="x") is None


async def test_no_token_disables_delivery_without_error():
    notifier = SlackNotifier(token=None)
    notifier._token = None
    assert not notifier.enabled
    assert await notifier.post(channel="C1", blocks=[], text="x") is None


def test_env_token_takes_precedence(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    assert resolve_bot_token() == "xoxb-from-env"


async def test_fallback_text_is_truncated(patched):
    transport = patched({"ok": True, "ts": "1"})
    await SlackNotifier(token="xoxb-test").post(channel="C1", blocks=[], text="y" * 5000)
    assert len(json.loads(transport.requests[0].content)["text"]) <= 3000
