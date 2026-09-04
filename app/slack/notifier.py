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
        if not self._token:
            log.warning("slack_delivery_skipped", reason="no bot token configured")
            return None

        payload: dict[str, Any] = {
            "channel": channel,
            "blocks": blocks,
            "text": text[:3000],
            "unfurl_links": False,
            "unfurl_media": False,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    SLACK_POST_MESSAGE,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "Content-Type": "application/json; charset=utf-8",
                    },
                    json=payload,
                )
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("slack_delivery_failed", error=str(exc)[:200], channel=channel)
            return None

        if not body.get("ok"):
            # Surfaced, never silent: a missing scope or bad channel is actionable.
            log.warning(
                "slack_delivery_rejected",
                error=body.get("error"),
                channel=channel,
                thread_ts=thread_ts,
            )
            return None

        return body.get("ts")


_notifier: SlackNotifier | None = None


def get_notifier() -> SlackNotifier:
    global _notifier
    if _notifier is None:
        _notifier = SlackNotifier()
    return _notifier


def reset_notifier() -> None:
    global _notifier
    _notifier = None
