"""The per-specialist slash commands (PRD s9.1, s19)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.agents.registry import get_registry
from scripts.generate_command_skills import TARGET, skill_name

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_every_enabled_specialist_has_a_command():
    """A specialist nobody can address is a specialist nobody uses."""
    generated = {p.name for p in TARGET.iterdir() if p.is_dir()}
    expected = {skill_name(p.slug) for p in get_registry().enabled_profiles()}
    assert generated == expected


def test_a_provisional_specialist_gets_no_command():
    """A15/A16 have no client SDS behind them, so they are not addressable."""
    generated = {p.name for p in TARGET.iterdir() if p.is_dir()}
    for profile in get_registry().all_profiles():
        if profile not in get_registry().enabled_profiles():
            assert skill_name(profile.slug) not in generated


def test_a_command_skill_names_its_own_agent_and_nothing_else():
    text = (TARGET / "qtpp" / "SKILL.md").read_text()
    assert "--agent A03" in text
    assert "QTPP Expert / Director" in text
    # It must not offer to route elsewhere.
    assert "Do not route to a different specialist" in text


def test_command_names_are_gateway_safe():
    """OpenClaw sanitises command names to a-z0-9_, max 32 characters."""
    for profile in get_registry().enabled_profiles():
        name = skill_name(profile.slug)
        assert name == name.lower()
        assert len(name) <= 32
        assert all(c.isalnum() or c == "_" for c in name)


def test_the_committed_command_skills_match_the_registry():
    """Drift here means a command silently points at the wrong specialist."""
    result = subprocess.run(
        [sys.executable, "scripts/generate_command_skills.py", "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
