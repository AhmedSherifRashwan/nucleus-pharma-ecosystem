"""Compose the specialist system prompt from the shared template plus a profile."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from app.config import get_settings
from app.schemas.agent import AgentProfile

VERSION_MARKER = re.compile(r"^<!--\s*version:\s*[0-9.]+\s*-->\s*\n?")


@lru_cache
def _common_template() -> str:
    path = Path(get_settings().worker_prompts_path) / "common_v1.md"
    if not path.is_file():
        raise FileNotFoundError(f"common worker prompt not found: {path}")
    # The version marker is metadata for the seeder's immutability check, not an
    # instruction. A specialist reading a stray HTML comment learns nothing.
    return VERSION_MARKER.sub("", path.read_text(encoding="utf-8"), count=1).lstrip()


def _bullets(items: list[str], empty: str = "- (none specified)") -> str:
    return "\n".join(f"- {i}" for i in items) if items else empty


def compose_worker_prompt(profile: AgentProfile, domain_prompt: str) -> str:
    """Build the system prompt. Policy and profile data only - never task data."""
    template = _common_template()
    replacements = {
        "{{profile_name}}": profile.name,
        "{{operating_persona}}": profile.operating_persona,
        "{{purpose}}": profile.purpose,
        "{{mission}}": profile.mission,
        "{{core_responsibilities}}": _bullets(profile.core_responsibilities),
        "{{domain_prompt}}": domain_prompt.strip(),
        "{{knowledge_base}}": _bullets(profile.knowledge_base),
        "{{quality_controls}}": _bullets(profile.quality_controls),
        "{{forbidden_actions}}": _bullets(
            profile.forbidden_actions, "- (no profile-specific prohibitions)"
        ),
        "{{escalation_triggers}}": _bullets(
            profile.escalation_policy.triggers, "- (no specific triggers defined)"
        ),
        "{{escalation_action}}": profile.escalation_policy.action,
        "{{evidence_policy}}": profile.evidence_policy,
    }
    for key, value in replacements.items():
        template = template.replace(key, value)
    return template
