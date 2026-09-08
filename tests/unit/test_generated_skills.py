"""Generated OpenClaw skills stay in step with the registry (PRD s11, s25)."""

from __future__ import annotations

import pytest

from app.agents.registry import load_registry
from scripts.generate_skills import SKILLS_ROOT, generate, render_skill, skill_dir


@pytest.fixture(scope="module")
def registry():
    return load_registry(enable_provisional=True)


def test_committed_skills_match_the_registry():
    """The single most important property: skills cannot drift from the profiles.

    If this fails, run: uv run python scripts/generate_skills.py
    """
    assert generate(check_only=True) == 0


def test_one_skill_per_profile(registry):
    for profile in registry.all_profiles():
        assert (skill_dir(profile) / "SKILL.md").is_file(), f"{profile.id} has no skill"


def test_skill_frontmatter_is_well_formed(registry):
    for profile in registry.all_profiles():
        text = (skill_dir(profile) / "SKILL.md").read_text()
        lines = text.splitlines()
        assert lines[0] == "---"
        closing = lines.index("---", 1)
        front = "\n".join(lines[1:closing])
        assert f"name: nucleus-{profile.slug}" in front
        assert f"agent-id: {profile.id}" in front
        assert f"profile-version: {profile.version}" in front
        assert f"status: {profile.status}" in front
        # Discovery text must be a single line or the frontmatter breaks.
        description = next(line for line in front.splitlines() if line.startswith("description:"))
        assert len(description.split("\n")) == 1


def test_skills_are_marked_generated(registry):
    for profile in registry.all_profiles():
        text = (skill_dir(profile) / "SKILL.md").read_text()
        assert "GENERATED FILE - DO NOT EDIT" in text
        assert "scripts/generate_skills.py" in text


def test_active_skills_cite_their_source_sds(registry):
    """CLAUDE.md rule 12: role scope traces to the client document."""
    for profile in registry.all_profiles():
        text = (skill_dir(profile) / "SKILL.md").read_text()
        if profile.status == "active":
            assert profile.source_sds in text
        else:
            assert "PROVISIONAL" in text


def test_skill_body_is_the_composed_specialist_prompt(registry):
    """One source of truth: the skill body is what the runtime would send."""
    for profile in registry.all_profiles():
        text = (skill_dir(profile) / "SKILL.md").read_text()
        assert "<operating_persona>" in text
        assert "UNTRUSTED DATA" in text
        assert "MUST carry at least one evidence reference" in text
        assert "Nucleus" in text


def test_framework_scoped_skills_say_where_their_scope_ends(registry):
    """A15/A16 come from the client's role table, not a specification for the role."""
    for pid in ("A15", "A16"):
        text = (skill_dir(registry.get(pid)) / "SKILL.md").read_text()
        assert "framework level" in text
        assert "rather than inferring the scope" in text


def test_no_stale_skill_directories(registry):
    """Only generated directories are managed; hand-written ones are left alone."""
    expected = {skill_dir(p).name for p in registry.all_profiles()}
    generated = set()
    for d in SKILLS_ROOT.glob("nucleus-*"):
        f = d / "SKILL.md"
        if d.is_dir() and f.is_file() and "GENERATED FILE" in f.read_text():
            generated.add(d.name)
    assert generated == expected


def test_regenerating_is_idempotent(registry):
    before = {p.id: (skill_dir(p) / "SKILL.md").read_text() for p in registry.all_profiles()}
    for profile in registry.all_profiles():
        assert render_skill(profile, registry) == before[profile.id]
