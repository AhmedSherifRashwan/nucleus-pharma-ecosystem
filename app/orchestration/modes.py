"""How much of the ecosystem a request engages (PRD s9.1, s12.2).

Three modes, chosen from the user's own words rather than guessed at:

`standard`  the planner selects the specialists the question needs, within the
            run budget. The default, and the right answer for almost everything.
`full`      every enabled profile is engaged. The user asked for it explicitly;
            it costs proportionally more time and quota.
`direct`    one named specialist answers one task, with no planning stage.

Detection is deliberately literal. Inferring "they probably want everyone" from
a broad question would spend an hour of someone's afternoon on a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RunMode = Literal["standard", "full", "direct"]

# "use all 16 agents", "all sixteen agents", "use the whole ecosystem", "every agent"
FULL_PATTERNS = (
    re.compile(r"\ball\s+(?:16|sixteen)\b", re.I),
    re.compile(r"\b(?:use|engage|run)\s+(?:the\s+)?(?:whole|entire|full)\s+ecosystem\b", re.I),
    re.compile(r"\b(?:every|all)\s+(?:the\s+)?(?:agent|specialist|expert)s?\b", re.I),
    re.compile(r"\bfull\s+ecosystem\b", re.I),
)

# A leading /A07 or /shell-formulation, with the task after it.
DIRECT_ID = re.compile(r"^\s*/\s*(A\d{2})\b[:,\s]*(.*)$", re.I | re.S)
DIRECT_SLUG = re.compile(r"^\s*/\s*([a-z][a-z0-9_-]{2,40})\b[:,\s]*(.*)$", re.I | re.S)


@dataclass
class ModeDecision:
    mode: RunMode
    agent_id: str | None = None
    request: str = ""
    matched_on: str | None = None

    @property
    def is_direct(self) -> bool:
        return self.mode == "direct"


def detect_mode(
    request: str,
    *,
    known_ids: set[str],
    slug_to_id: dict[str, str],
    explicit_mode: RunMode | None = None,
    explicit_agent: str | None = None,
) -> ModeDecision:
    """Read the request for a mode. An explicit argument always wins."""
    text = (request or "").strip()

    if explicit_agent:
        agent = explicit_agent.strip().upper()
        if agent in known_ids:
            return ModeDecision("direct", agent, _strip_direct_prefix(text), "explicit argument")
    if explicit_mode:
        return ModeDecision(explicit_mode, None, text, "explicit argument")

    direct = DIRECT_ID.match(text)
    if direct:
        agent = direct.group(1).upper()
        if agent in known_ids:
            return ModeDecision("direct", agent, direct.group(2).strip(), f"/{agent}")

    slug = DIRECT_SLUG.match(text)
    if slug:
        candidate = slug.group(1).lower().replace("-", "_")
        matched = slug_to_id.get(candidate)
        if matched:
            return ModeDecision("direct", matched, slug.group(2).strip(), f"/{slug.group(1)}")

    for pattern in FULL_PATTERNS:
        found = pattern.search(text)
        if found:
            return ModeDecision("full", None, text, found.group(0))

    return ModeDecision("standard", None, text, None)


def _strip_direct_prefix(text: str) -> str:
    for pattern in (DIRECT_ID, DIRECT_SLUG):
        match = pattern.match(text)
        if match:
            return match.group(2).strip()
    return text
