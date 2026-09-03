"""Structured operational logging (PRD s24 observability).

Distinct from the durable `audit_events` product record. Secrets are redacted
here as a backstop; they should never be passed in the first place (s21).
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_configured = False

_REDACT_KEYS = {
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "openclaw_gateway_token",
    "openai_api_key",
    "anthropic_api_key",
    "slack_bot_token",
    "slack_app_token",
    "slack_signing_secret",
}


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if key.lower() in _REDACT_KEYS:
            event_dict[key] = "***redacted***"
    return event_dict


def configure_logging(level: str = "INFO", fmt: str = "console") -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level.upper())
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if fmt == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


def bind_run_context(**kwargs: Any) -> None:
    """Bind correlation IDs (run_id, task_id, agent_id) for the current context."""
    structlog.contextvars.bind_contextvars(**kwargs)


def clear_run_context() -> None:
    structlog.contextvars.clear_contextvars()
