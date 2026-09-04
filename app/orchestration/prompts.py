"""Load and render versioned executive prompts (PRD s14 prompt_versions)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import get_settings

PLAN_PROMPT = "plan_v1.md"
CHALLENGE_PROMPT = "challenge_v1.md"
SYNTHESIZE_PROMPT = "synthesize_v1.md"


@lru_cache
def load_prompt(name: str) -> str:
    path = Path(get_settings().executive_prompts_path) / name
    if not path.is_file():
        raise FileNotFoundError(f"executive prompt not found: {path}")
    return path.read_text(encoding="utf-8")


def render(name: str, **values: Any) -> str:
    text = load_prompt(name)
    for key, value in values.items():
        rendered = (
            json.dumps(value, indent=2, default=str)
            if isinstance(value, (dict, list))
            else str(value)
        )
        text = text.replace(f"{{{{{key}}}}}", rendered)
    return text
