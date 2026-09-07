"""Capture this machine's OpenClaw configuration as a portable template.

The gateway configuration is real infrastructure — agents, models, routing,
channel policy — and until now it existed only as a file on one Mac, edited by
hand. If that Mac is lost, so is every decision in it.

This writes the same structure to the repository with the secrets removed and
machine-specific values turned into placeholders, so `bootstrap.py` can rebuild
it somewhere else. Secrets are never written: they come from the environment at
install time, from the new machine's own subscriptions and Slack app.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

OPENCLAW_CONFIG = Path("~/.openclaw/openclaw.json").expanduser()
TEMPLATE = Path(__file__).resolve().parent.parent / "openclaw" / "setup" / "openclaw.template.json"

HOME = str(Path.home())

# Anything matching these is either a credential or an identifier belonging to one
# workspace. Neither survives a move, so both become placeholders.
SECRET_KEYS = {"token", "botToken", "appToken", "apiKey", "password", "secret"}
SLACK_USER = re.compile(r"^U[A-Z0-9]{6,}$")
SLACK_CHANNEL = re.compile(r"^C[A-Z0-9]{6,}$")

PLACEHOLDERS = {
    "gateway.auth.token": "${OPENCLAW_GATEWAY_TOKEN}",
    "channels.slack.botToken": "${SLACK_BOT_TOKEN}",
    "channels.slack.appToken": "${SLACK_APP_TOKEN}",
}

# Written by the wizard on the machine that ran it; meaningless anywhere else.
DROP_KEYS = {"meta", "wizard"}


def scrub(node: Any, path: str = "") -> Any:
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if here in PLACEHOLDERS:
                out[key] = PLACEHOLDERS[here]
                continue
            if key in SECRET_KEYS and isinstance(value, str):
                out[key] = "${" + key.upper() + "}"
                continue
            out[key] = scrub(value, here)
        return out
    if isinstance(node, list):
        return [scrub(item, path) for item in node]
    if isinstance(node, str):
        if SLACK_USER.match(node):
            return "${SLACK_APPROVER_ID}"
        if SLACK_CHANNEL.match(node):
            return "${SLACK_CHANNEL_ID}"
        if node.startswith(HOME):
            return node.replace(HOME, "${HOME}", 1)
    return node


def rekey_channels(config: dict[str, Any]) -> dict[str, Any]:
    """Channel ids are dictionary keys as well as values."""
    slack = (config.get("channels") or {}).get("slack") or {}
    channels = slack.get("channels")
    if isinstance(channels, dict):
        slack["channels"] = {
            ("${SLACK_CHANNEL_ID}" if SLACK_CHANNEL.match(k) else k): v for k, v in channels.items()
        }
    return config


def main() -> int:
    parser = argparse.ArgumentParser(description="Export OpenClaw config as a template.")
    parser.add_argument("--check", action="store_true", help="report drift, write nothing")
    args = parser.parse_args()

    if not OPENCLAW_CONFIG.is_file():
        print(f"no OpenClaw config at {OPENCLAW_CONFIG}", file=sys.stderr)
        return 1

    live = json.loads(OPENCLAW_CONFIG.read_text())
    for key in DROP_KEYS:
        live.pop(key, None)
    template = rekey_channels(scrub(live))
    rendered = json.dumps(template, indent=2, sort_keys=True) + "\n"

    # A secret that slipped through would be committed, so check rather than trust.
    for needle in ("xoxb-", "xapp-", "sk-", "xoxe-"):
        if needle in rendered:
            print(f"refusing to write: {needle!r} appears in the template", file=sys.stderr)
            return 1

    if args.check:
        current = TEMPLATE.read_text() if TEMPLATE.is_file() else ""
        if current != rendered:
            print(
                f"{TEMPLATE.name} is out of date; run scripts/export_openclaw_config.py",
                file=sys.stderr,
            )
            return 1
        print(f"{TEMPLATE.name} matches this machine")
        return 0

    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE.write_text(rendered)
    print(f"wrote {TEMPLATE}")
    print(f"  agents:   {', '.join(sorted((template.get('agents') or {}).get('entries', {})))}")
    print(f"  channels: {', '.join(sorted(template.get('channels', {})))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
