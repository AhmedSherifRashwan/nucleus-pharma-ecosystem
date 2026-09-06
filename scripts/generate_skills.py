"""Generate one OpenClaw skill per specialist profile (PRD s11, s25, s30).

The 16 skills are GENERATED from the agent registry, never hand-edited. The YAML
profile plus its domain prompt remain the single source of truth, so a skill can
never drift from the client SDS it derives from (CLAUDE.md rule 12).

    uv run python scripts/generate_skills.py [--check]

`--check` verifies the committed skills match the registry and exits non-zero if
they do not. Used by the test suite and suitable for CI.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

from app.agents.prompt_builder import compose_worker_prompt
from app.agents.registry import AgentRegistry, load_registry
from app.schemas.agent import AgentProfile

SKILLS_ROOT = Path("openclaw/workspace/skills")
SKILL_PREFIX = "neucleus"

GENERATED_BANNER = (
    "<!-- GENERATED FILE - DO NOT EDIT.\n"
    "     Source: app/agents/definitions/{yaml} + app/prompts/workers/\n"
    "     Regenerate: uv run python scripts/generate_skills.py\n"
    "     Verify:     uv run python scripts/generate_skills.py --check -->"
)


def _yaml_name(profile: AgentProfile) -> str:
    return f"{profile.id}_{profile.slug}.yaml"


def skill_dir(profile: AgentProfile) -> Path:
    return SKILLS_ROOT / f"{SKILL_PREFIX}-{profile.slug}"


def _one_line(text: str, limit: int = 340) -> str:
    """Frontmatter descriptions must be a single line."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rsplit(" ", 1)[0] + "…"


def render_skill(profile: AgentProfile, registry: AgentRegistry) -> str:
    """A skill is frontmatter plus the composed specialist prompt.

    The body is exactly what the Python runtime would send, so the two paths
    cannot diverge.
    """
    # Discovery text: OpenClaw reads name/description before loading the skill,
    # so it must say what the specialist covers and when it applies.
    description = _one_line(
        f"{profile.name} for generic oral softgel development. {profile.purpose} "
        f"Use for: {', '.join(profile.key_outputs[:4])}."
    )

    body = compose_worker_prompt(profile, registry.domain_prompt(profile.id))

    if not profile.source_sds:
        provenance = (
            "PROVISIONAL: no client source document. Scope is inferred; this "
            "specialist is disabled unless explicitly enabled."
        )
    elif profile.source_basis == "framework_definition":
        provenance = (
            f"Derived from `{profile.source_sds}`, which defines this role at "
            "framework level. No specification was written for this role alone, so "
            "where a question needs detail beyond that definition, say so rather "
            "than inferring the scope."
        )
    else:
        provenance = f"Derived from `{profile.source_sds}`."

    return "\n".join(
        [
            "---",
            f"name: {SKILL_PREFIX}-{profile.slug}",
            f"description: {description}",
            "metadata:",
            f"  short-description: {_one_line(profile.operating_persona, 90)}",
            f"  agent-id: {profile.id}",
            f"  profile-version: {profile.version}",
            f"  status: {profile.status}",
            "---",
            "",
            GENERATED_BANNER.format(yaml=_yaml_name(profile)),
            "",
            f"# {profile.name} ({profile.id})",
            "",
            provenance,
            "",
            body.rstrip(),
            "",
        ]
    )


def generate(check_only: bool = False) -> int:
    registry = load_registry(enable_provisional=True)
    SKILLS_ROOT.mkdir(parents=True, exist_ok=True)

    expected_dirs: set[Path] = set()
    drift: list[str] = []
    written = 0

    for profile in registry.all_profiles():
        directory = skill_dir(profile)
        expected_dirs.add(directory)
        path = directory / "SKILL.md"
        content = render_skill(profile, registry)

        if check_only:
            if not path.is_file():
                drift.append(f"{path} is missing")
            elif path.read_text(encoding="utf-8") != content:
                drift.append(f"{path} differs from the registry")
            continue

        directory.mkdir(parents=True, exist_ok=True)
        if not path.is_file() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            written += 1

    # A profile removed from the registry must not leave a stale skill behind.
    # Only directories this script generated are considered: hand-written skills
    # under the same prefix (neucleus-tools, for instance) are left alone.
    for existing in SKILLS_ROOT.glob(f"{SKILL_PREFIX}-*"):
        if not existing.is_dir() or existing in expected_dirs:
            continue
        skill_file = existing / "SKILL.md"
        was_generated = (
            skill_file.is_file()
            and "GENERATED FILE - DO NOT EDIT" in skill_file.read_text(encoding="utf-8")
        )
        if not was_generated:
            continue
        if check_only:
            drift.append(f"{existing} is stale (no matching profile)")
        else:
            shutil.rmtree(existing)

    if check_only:
        if drift:
            print("Skills are out of date with the agent registry:", file=sys.stderr)
            for item in drift:
                print(f"  - {item}", file=sys.stderr)
            print("\nRegenerate: uv run python scripts/generate_skills.py", file=sys.stderr)
            return 1
        print(f"All {len(registry)} skills match the registry.")
        return 0

    digest = hashlib.sha256(
        "".join(
            (skill_dir(p) / "SKILL.md").read_text(encoding="utf-8") for p in registry.all_profiles()
        ).encode()
    ).hexdigest()[:12]
    print(
        f"Generated {len(registry)} skills into {SKILLS_ROOT} "
        f"({written} changed, set digest {digest})."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="verify without writing; non-zero on drift"
    )
    args = parser.parse_args()
    return generate(check_only=args.check)


if __name__ == "__main__":
    sys.exit(main())
