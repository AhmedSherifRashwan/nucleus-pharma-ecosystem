"""Slack delivery (PRD s17, Phase 11).

Posts progress and the Executive Decision Brief back into the originating thread.

DESIGN NOTE — why this posts directly rather than through an agent turn.
OpenClaw's only outbound path is an `agent` turn with `deliver: true`, which means
a language model generates the delivered text. For a regulated deliverable that is
the wrong shape: §18 requires deterministic assembly precisely so a model cannot
reformat or paraphrase the output. This module therefore posts the exact rendered
blocks through the Slack Web API using the bot token OpenClaw already holds.
OpenClaw remains the inbound gateway (§7.1); only outbound fidelity is ours.
See decisions.md D-014.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from app.audit.logger import get_logger
from app.config import Settings, get_settings

log = get_logger(__name__)

SLACK_POST_MESSAGE = "https://slack.com/api/chat.postMessage"
SLACK_UPDATE_MESSAGE = "https://slack.com/api/chat.update"
SLACK_GET_UPLOAD_URL = "https://slack.com/api/files.getUploadURLExternal"
SLACK_COMPLETE_UPLOAD = "https://slack.com/api/files.completeUploadExternal"
OPENCLAW_CONFIG = "~/.openclaw/openclaw.json"


class SlackDeliveryError(RuntimeError):
    """Delivery failed. A run must not be reported as delivered when it was not."""


def resolve_bot_token(settings: Settings | None = None) -> str | None:
    """Find the bot token: environment first, then OpenClaw's own config."""
    token = os.environ.get("SLACK_BOT_TOKEN")
    if token:
        return token
    try:
        config = json.loads(Path(OPENCLAW_CONFIG).expanduser().read_text())
    except (OSError, json.JSONDecodeError):
        return None
    slack = (config.get("channels") or {}).get("slack") or {}
    value = slack.get("botToken")
    # A SecretRef points at an environment variable rather than holding the value.
    if isinstance(value, dict):
        return os.environ.get(value.get("id", ""), None)
    return value if isinstance(value, str) and value else None


class SlackNotifier:
    """Posts to a Slack thread. Never raises into orchestration by default."""

    def __init__(self, token: str | None = None, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._token = token or resolve_bot_token(self._settings)

    @property
    def enabled(self) -> bool:
        return bool(self._token)

    async def _call(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self._token:
            log.warning("slack_delivery_skipped", reason="no bot token configured")
            return None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=payload,
                )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("slack_delivery_failed", error=str(exc)[:200])
            return None
        if not body.get("ok"):
            log.warning("slack_delivery_rejected", error=body.get("error"), url=url)
            return None
        return body

    async def update(
        self, *, channel: str, ts: str, blocks: list[dict[str, Any]], text: str
    ) -> bool:
        """Edit an existing message in place.

        Progress is narrated as a stream of posts rather than one edited message
        (an edit notifies nobody and is invisible to anyone who scrolled past it),
        so this is kept for corrections to a message already delivered.
        """
        body = await self._call(
            SLACK_UPDATE_MESSAGE,
            {"channel": channel, "ts": ts, "blocks": blocks, "text": text[:3000]},
        )
        return body is not None

    async def upload_file(
        self,
        *,
        channel: str,
        path: Path | str,
        title: str | None = None,
        comment: str | None = None,
        thread_ts: str | None = None,
    ) -> bool:
        """Upload a file into the thread using Slack's external-upload flow.

        The report is the deliverable (s18); a path in a message is useless to a
        client who is not sitting at this machine. Returns False rather than
        raising: a delivered brief with an undelivered attachment is still worth
        more than a failed run.
        """
        file_path = Path(path)
        if not self._token:
            log.warning("slack_upload_skipped", reason="no bot token configured")
            return False
        if not file_path.is_file():
            log.warning("slack_upload_skipped", reason="file missing", path=str(file_path))
            return False

        size = file_path.stat().st_size
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                headers = {"Authorization": f"Bearer {self._token}"}
                ticket = await client.post(
                    SLACK_GET_UPLOAD_URL,
                    headers=headers,
                    data={"filename": file_path.name, "length": str(size)},
                )
                ticket_body = ticket.json()
                if not ticket_body.get("ok"):
                    log.warning("slack_upload_rejected", error=ticket_body.get("error"))
                    return False

                upload = await client.post(
                    ticket_body["upload_url"],
                    files={"file": (file_path.name, file_path.read_bytes())},
                )
                upload.raise_for_status()

                payload: dict[str, Any] = {
                    "files": json.dumps(
                        [{"id": ticket_body["file_id"], "title": title or file_path.name}]
                    ),
                    "channel_id": channel,
                }
                if comment:
                    payload["initial_comment"] = comment
                if thread_ts:
                    payload["thread_ts"] = thread_ts
                complete = await client.post(SLACK_COMPLETE_UPLOAD, headers=headers, data=payload)
                complete_body = complete.json()
        except (httpx.HTTPError, ValueError, KeyError, OSError) as exc:
            log.warning("slack_upload_failed", error=str(exc)[:200])
            return False

        if not complete_body.get("ok"):
            log.warning("slack_upload_rejected", error=complete_body.get("error"))
            return False
        log.info("slack_file_uploaded", filename=file_path.name, bytes=size)
        return True

    async def post(
        self,
        *,
        channel: str,
        blocks: list[dict[str, Any]],
        text: str,
        thread_ts: str | None = None,
    ) -> str | None:
        """Post to a thread. Returns the message ts, or None if delivery failed.

        `text` is the notification fallback and must summarise the blocks: Slack
        shows it in notifications and to clients that cannot render Block Kit.
        """
        payload: dict[str, Any] = {
            "channel": channel,
            "blocks": blocks,
            "text": text[:3000],
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        body = await self._call(SLACK_POST_MESSAGE, payload)
        return body.get("ts") if body else None


_notifier: SlackNotifier | None = None


def get_notifier() -> SlackNotifier:
    global _notifier
    if _notifier is None:
        _notifier = SlackNotifier()
    return _notifier


def reset_notifier() -> None:
    global _notifier
    _notifier = None
