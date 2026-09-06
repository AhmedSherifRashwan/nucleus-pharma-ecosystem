"""Install the Neucleus skills into the OpenClaw agent workspaces (PRD s7, s11).

Skills live in this repository so they are versioned and reviewed with the code
that backs them. OpenClaw loads them from an agent's `<workspace>/skills`, so they
have to be installed there.

Commands inside a skill run wherever the agent happens to be, so `{{NEUCLEUS_ROOT}}`
is rendered to this checkout's absolute path at install time. The repository copy
stays portable; the installed copy is concrete.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOURCE = PROJECT_ROOT / "openclaw" / "workspace" / "skills"
PLACEHOLDER = "{{NEUCLEUS_ROOT}}"

OPENCLAW_HOME = Path("~/.openclaw").expanduser()

# Which agent gets which skills. The executive agent needs neither: it plans
# through the gateway contract, not through skills.
# The specialist gets the 16 profiles and the tool wrappers; approvals belong to
# the Slack-facing agent alone, so a specialist can never decide its own gate.
TARGETS: dict[str, tuple[Path, tuple[str, ...], tuple[str, ...]]] = {
    # The dedicated front door: the ecosystem is all it does, so a person never
    # has to ask for it by name.
    "neucleus": (
        OPENCLAW_HOME / "workspaces" / "neucleus" / "skills",
        ("pharma-ecosystem", "neucleus-approvals", "commands"),
        (),
    ),
    "main": (
        OPENCLAW_HOME / "workspace" / "skills",
        # `commands` holds one generated skill per specialist, so /qtpp and the
        # rest reach the Slack-facing agent as slash commands.
        ("pharma-ecosystem", "neucleus-approvals", "commands"),
        (),
    ),
    "pharma-specialist": (
        OPENCLAW_HOME / "workspaces" / "pharma-specialist" / "skills",
        ("neucleus-*",),
        ("neucleus-approvals",),
    ),
}


def sources_for(patterns: tuple[str, ...], excluded: tuple[str, ...] = ()) -> list[Path]:
    """Skill directories matching these patterns.

    A matched directory holding no SKILL.md of its own is a grouping folder, and
    its children are the skills - that is how the per-specialist commands are laid
    out, so they stay together in the repository without becoming one skill.
    """
    found: dict[str, Path] = {}
    for pattern in patterns:
        for path in sorted(SOURCE.glob(pattern)):
            if path.name in excluded or not path.is_dir():
                continue
            if (path / "SKILL.md").is_file():
                found[path.name] = path
                continue
            for child in sorted(path.iterdir()):
                if child.is_dir() and (child / "SKILL.md").is_file() and child.name not in excluded:
                    found[child.name] = child
    return list(found.values())


def render(text: str) -> str:
    return text.replace(PLACEHOLDER, str(PROJECT_ROOT))


# The front-door agent's standing instructions live in the repository, so what it
# will and will not do is reviewed with the code rather than edited in place.
AGENT_INSTRUCTIONS = PROJECT_ROOT / "openclaw" / "workspace" / "agent-neucleus"
AGENT_WORKSPACE = OPENCLAW_HOME / "workspaces" / "neucleus"


def install_agent_instructions(*, check: bool) -> None:
    if not AGENT_INSTRUCTIONS.is_dir():
        return
    for source in sorted(AGENT_INSTRUCTIONS.glob("*.md")):
        target = AGENT_WORKSPACE / source.name
        content = render(source.read_text())
        if target.is_file() and target.read_text() == content:
            continue
        if check:
            print(f"  - neucleus/{source.name} (out of date)", file=sys.stderr)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        print(f"neucleus: {source.name} -> {target}")


def install_one(source: Path, destination: Path, *, check: bool) -> bool:
    """Returns True when the destination already matches the source."""
    expected = {
        path.relative_to(source): render(path.read_text())
        for path in source.rglob("*")
        if path.is_file()
    }
    current = {
        path.relative_to(destination): path.read_text()
        for path in destination.rglob("*")
        if path.is_file()
    }
    if current == expected:
        return True
    if check:
        return False

    if destination.exists():
        shutil.rmtree(destination)
    for relative, content in expected.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Install Neucleus skills into OpenClaw.")
    parser.add_argument(
        "--check", action="store_true", help="report drift without writing anything"
    )
    args = parser.parse_args()

    if not SOURCE.is_dir():
        print(f"no skills directory at {SOURCE}", file=sys.stderr)
        return 1

    install_agent_instructions(check=args.check)

    drifted: list[str] = []
    for agent, (workspace_skills, patterns, excluded) in TARGETS.items():
        sources = sources_for(patterns, excluded)
        if not sources:
            print(f"{agent}: no matching skills in {SOURCE}", file=sys.stderr)
            return 1
        if not args.check:
            workspace_skills.mkdir(parents=True, exist_ok=True)
        elif not workspace_skills.is_dir():
            drifted.extend(f"{agent}/{s.name}" for s in sources)
            continue

        for source in sources:
            up_to_date = install_one(source, workspace_skills / source.name, check=args.check)
            if not up_to_date:
                drifted.append(f"{agent}/{source.name}")

        # Remove installed Neucleus skills that no longer exist in the repository.
        names = {s.name for s in sources}
        if workspace_skills.is_dir():
            for existing in workspace_skills.iterdir():
                if not existing.is_dir() or existing.name in names:
                    continue
                if not (existing.name.startswith(("neucleus-", "pharma-"))):
                    continue
                drifted.append(f"{agent}/{existing.name} (stale)")
                if not args.check:
                    shutil.rmtree(existing)

        print(f"{agent}: {len(sources)} skill(s) -> {workspace_skills}")

    if args.check and drifted:
        print("skills are not installed or out of date:", file=sys.stderr)
        for name in drifted:
            print(f"  - {name}", file=sys.stderr)
        print("run: uv run python scripts/install_skills.py", file=sys.stderr)
        return 1
    if not args.check:
        print("installed. Restart the gateway for the agents to pick them up:")
        print("  openclaw gateway restart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
