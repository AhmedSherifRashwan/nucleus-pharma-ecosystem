"""Build an offline migration kit on a USB stick (PRD s29, Phase 15).

Copying `~/Nucleus` to a stick does not move this system. The database lives in
PostgreSQL's own data directory, the gateway's agents and workspaces live under
`~/.openclaw`, and `.venv` holds absolute paths and compiled binaries that mean
nothing on another machine. What a folder copy actually preserves is the source —
which git already preserves, better.

So this writes what does travel: the repository with its full history as a git
bundle, and the data as a verified backup archive. Both restore without a network,
which is the point of a stick.

    uv run python scripts/usb_kit.py create --to /Volumes/USB
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLE = "nucleus-repo.bundle"
SETUP = "SETUP.txt"

INSTRUCTIONS = """\
Nucleus — offline setup kit
Created {created} from commit {commit}

WHAT IS HERE
  {bundle}   the whole repository, every commit, no network needed
  {archive}  the database, artifacts and ingested documents
  {env_note}

ON THE NEW MAC

  1. Install OpenClaw from https://docs.openclaw.ai
     (the only step no script can do)

  2. Clone from the bundle. Not into ~/Documents, ~/Desktop or ~/Downloads:
     macOS denies background services access to those, and the worker will not run.

       git clone "/Volumes/<STICK>/{bundle}" ~/Nucleus
       cd ~/Nucleus
       git remote set-url origin https://github.com/AhmedSherifRashwan/nucleus-pharma-ecosystem.git

  3. System dependencies:

       ./scripts/install-dependencies.sh

  4. Sign in to both subscriptions (a browser each):

       claude
       openclaw onboard

  5. Configuration:
{env_step}

  6. Restore the data BEFORE bootstrap:

       uv run python scripts/backup.py restore --from "/Volumes/<STICK>/{archive}"

  7. Everything else:

       uv run python scripts/bootstrap.py
       uv run python scripts/health_check.py

  8. Open it:

       openclaw dashboard        # select the `nucleus` agent

WHAT IS DELIBERATELY NOT HERE
  .venv                uv embeds absolute paths in console scripts; bootstrap rebuilds it
  ~/.openclaw          agent state and a vendored Node runtime belong to the machine
  PostgreSQL's files   the database travels as a dump inside the archive, not as raw files

See README.md in the repository for the same procedure with explanations, and a
troubleshooting table.
"""

ENV_INCLUDED = """       cp "/Volumes/<STICK>/env/.env" .env

     This kit carries the .env from the old machine. Check DATABASE_URL and
     OPENCLAW_GATEWAY_TOKEN still make sense here; the token is per-machine."""

ENV_ABSENT = """       cp .env.example .env

     Then set OPENCLAW_GATEWAY_TOKEN (from ~/.openclaw/openclaw.json),
     DEFAULT_MAX_SPECIALISTS=8 and REPORT_COMPOSER_ROUTE=specialist.
     The .env was not copied into this kit; see --include-env."""


def run(command: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, cwd=PROJECT_ROOT, **kw)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build an offline migration kit.")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create")
    make.add_argument("--to", required=True, help="the stick, or any directory")
    make.add_argument(
        "--include-env",
        action="store_true",
        help="copy .env as well. It holds the database password and gateway token, "
        "so the stick then carries credentials — keep track of it.",
    )
    args = parser.parse_args()

    destination = Path(args.to).expanduser()
    if not destination.parent.exists():
        print(f"no such location: {destination}", file=sys.stderr)
        return 1
    destination.mkdir(parents=True, exist_ok=True)

    dirty = run(["git", "status", "--porcelain"]).stdout.strip()
    if dirty:
        print(
            "Uncommitted changes will not be in the bundle:\n  "
            + "\n  ".join(dirty.splitlines()[:5]),
            file=sys.stderr,
        )

    print("Bundling the repository...")
    bundle = destination / BUNDLE
    result = run(["git", "bundle", "create", str(bundle), "--all"])
    if result.returncode != 0:
        print(f"git bundle failed: {result.stderr.strip()[:200]}", file=sys.stderr)
        return 1
    verify = run(["git", "bundle", "verify", str(bundle)])
    if verify.returncode != 0:
        print(f"the bundle did not verify: {verify.stderr.strip()[:200]}", file=sys.stderr)
        return 1

    print("Backing up the data...")
    backup = run([sys.executable, "scripts/backup.py", "create", "--to", str(destination)])
    if backup.returncode != 0:
        print(f"backup failed: {backup.stderr.strip()[:300]}", file=sys.stderr)
        return 1
    archives = sorted(destination.glob("nucleus-*.tar.gz"))
    if not archives:
        print("the backup produced no archive", file=sys.stderr)
        return 1
    archive = archives[-1]

    if args.include_env:
        env_source = PROJECT_ROOT / ".env"
        if env_source.is_file():
            env_dir = destination / "env"
            env_dir.mkdir(exist_ok=True)
            shutil.copy2(env_source, env_dir / ".env")
            (env_dir / ".env").chmod(0o600)

    commit = run(["git", "rev-parse", "--short", "HEAD"]).stdout.strip() or "unknown"
    (destination / SETUP).write_text(
        INSTRUCTIONS.format(
            created=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            commit=commit,
            bundle=BUNDLE,
            archive=archive.name,
            env_note=(
                "env/.env                    CREDENTIALS — keep this stick safe"
                if args.include_env
                else "(no .env — rebuild it from .env.example, see below)"
            ),
            env_step=ENV_INCLUDED if args.include_env else ENV_ABSENT,
        )
    )

    total = sum(f.stat().st_size for f in destination.rglob("*") if f.is_file())
    print(f"\nKit written to {destination} ({total / 1_048_576:.1f} MB)")
    for item in sorted(destination.iterdir()):
        print(f"  {item.name}")
    if args.include_env:
        print("\nThis kit contains credentials. Treat the stick accordingly.")
    print(f"\nRead {SETUP} on the new machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
