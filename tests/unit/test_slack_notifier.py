"""Slack delivery behaviour (PRD s17, Phase 11)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.slack import notifier as notifier_module
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


# -- file upload (s18: the report must actually reach the client) ------------


class SequencedTransport(httpx.AsyncBaseTransport):
    """Replies with one payload per request, in order."""

    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = self.payloads[min(len(self.requests) - 1, len(self.payloads) - 1)]
        return httpx.Response(200, json=payload)


@pytest.fixture
def sequenced(monkeypatch):
    def install(payloads):
        transport = SequencedTransport(payloads)
        original = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", factory)
        return transport

    return install


async def test_upload_walks_the_external_upload_flow(sequenced, tmp_path):
    report = tmp_path / "Brief_v1.docx"
    report.write_bytes(b"docx bytes")
    transport = sequenced(
        [
            {"ok": True, "upload_url": "https://files.slack.com/upload/x", "file_id": "F1"},
            {"ok": True},
            {"ok": True, "files": [{"id": "F1"}]},
        ]
    )

    uploaded = await SlackNotifier(token="xoxb-test").upload_file(
        channel="C1", path=report, title="Brief v1", thread_ts="1788.1"
    )

    assert uploaded is True
    urls = [str(r.url) for r in transport.requests]
    assert "files.getUploadURLExternal" in urls[0]
    assert urls[1] == "https://files.slack.com/upload/x"
    assert "files.completeUploadExternal" in urls[2]


async def test_upload_of_a_missing_file_fails_quietly(tmp_path):
    ok = await SlackNotifier(token="xoxb-test").upload_file(
        channel="C1", path=tmp_path / "absent.docx"
    )
    assert ok is False


async def test_upload_without_a_token_does_not_raise(tmp_path, monkeypatch):
    # Resolution is stubbed so an installed bot token cannot turn this into a
    # real call to Slack.
    monkeypatch.setattr(notifier_module, "resolve_bot_token", lambda *a, **k: None)
    report = tmp_path / "Brief_v1.docx"
    report.write_bytes(b"docx bytes")
    assert await SlackNotifier().upload_file(channel="C1", path=report) is False


async def test_a_rejected_upload_ticket_is_reported(sequenced, tmp_path):
    report = tmp_path / "Brief_v1.docx"
    report.write_bytes(b"docx bytes")
    sequenced([{"ok": False, "error": "invalid_auth"}])
    assert await SlackNotifier(token="xoxb-test").upload_file(channel="C1", path=report) is False
