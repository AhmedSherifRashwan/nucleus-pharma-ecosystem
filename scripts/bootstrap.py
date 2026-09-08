"""Bring a machine from a fresh checkout to a running ecosystem (PRD s29, Phase 15).

Everything here is idempotent: run it on a new Mac, or run it again on this one
after a change, and the result is the same. It does only what can be automated —
the two things that cannot are stated at the end, because a subscription login and
a Slack app installation both need a human at a browser.

    uv run python scripts/bootstrap.py            # do it
    uv run python scripts/bootstrap.py --check    # say what it would do
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPENCLAW_HOME = Path("~/.openclaw").expanduser()
OPENCLAW_CONFIG = OPENCLAW_HOME / "openclaw.json"
TEMPLATE = PROJECT_ROOT / "openclaw" / "setup" / "openclaw.template.json"
LAUNCH_AGENTS = Path("~/Library/LaunchAgents").expanduser()
WORKER_LABEL = "ai.nucleus.worker"

# Substituted into the OpenClaw template. Each is a secret or an identifier the
# new deployment supplies for itself.
REQUIRED_ENV = ("OPENCLAW_GATEWAY_TOKEN",)

# Only needed if the ecosystem is reached from Slack. The Control UI needs none of
# them, so their absence is reported and not treated as a failure.
SLACK_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL_ID", "SLACK_APPROVER_ID")

# macOS denies a launchd-spawned process access to these, whatever the file
# permissions say, so a worker service cannot run from inside one.
TCC_PROTECTED = ("Documents", "Desktop", "Downloads")


@dataclass
class Step:
    name: str
    done: bool
    detail: str = ""
    changed: bool = False


@dataclass
class Report:
    steps: list[Step] = field(default_factory=list)
    manual: list[str] = field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        mark = "ok " if step.done else "MISSING"
        change = " (changed)" if step.changed else ""
        print(f"  [{mark}] {step.name}{change}" + (f" — {step.detail}" if step.detail else ""))
        return step

    @property
    def blocked(self) -> list[Step]:
        return [s for s in self.steps if not s.done]


def run(command: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, **kw)


def which(name: str) -> str | None:
    return shutil.which(name) or (
        str(OPENCLAW_HOME / "bin" / name) if (OPENCLAW_HOME / "bin" / name).is_file() else None
    )


# -- prerequisites -----------------------------------------------------------


def check_prerequisites(report: Report) -> None:
    print("\nPrerequisites")
    for tool, hint in (
        ("uv", "https://docs.astral.sh/uv/ — installs Python and dependencies"),
        ("psql", "brew install postgresql@17 && brew services start postgresql@17"),
        ("openclaw", "https://docs.openclaw.ai — the Slack and model gateway"),
    ):
        found = which(tool)
        report.add(Step(tool, bool(found), found or hint))


def check_env(report: Report) -> dict[str, str]:
    print("\nSecrets and identifiers (from .env or the environment)")
    env_file = PROJECT_ROOT / ".env"
    values: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    values = {**values, **{k: v for k, v in os.environ.items() if k in REQUIRED_ENV}}
    values = {**values_from_existing_gateway(), **{k: v for k, v in values.items() if v}}

    # One approver id may be given as a comma-separated list.
    if "SLACK_APPROVER_ID" not in values and values.get("SLACK_APPROVER_IDS"):
        values["SLACK_APPROVER_ID"] = values["SLACK_APPROVER_IDS"].split(",")[0].strip()

    for key in REQUIRED_ENV:
        present = bool(values.get(key))
        report.add(Step(key, present, "set" if present else "add it to .env before rerunning"))
    return values


# -- database ----------------------------------------------------------------


def values_from_existing_gateway() -> dict[str, str]:
    """What an already-configured gateway can tell us about itself.

    On a machine that is already running, the Slack tokens are in the gateway's
    config rather than in `.env`. Reading them back keeps a re-run honest instead
    of demanding they be copied into a second place. A new machine has no config,
    so these come from `.env` there.
    """
    if not OPENCLAW_CONFIG.is_file():
        return {}
    try:
        config = json.loads(OPENCLAW_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}

    slack = (config.get("channels") or {}).get("slack") or {}
    channels = [c for c in (slack.get("channels") or {}) if re.match(r"^C[A-Z0-9]{6,}$", c)]
    approvers = [
        entry.split(":", 1)[-1]
        for entry in ((config.get("commands") or {}).get("ownerAllowFrom") or [])
    ]
    found = {
        "OPENCLAW_GATEWAY_TOKEN": ((config.get("gateway") or {}).get("auth") or {}).get("token"),
        "SLACK_BOT_TOKEN": slack.get("botToken"),
        "SLACK_APP_TOKEN": slack.get("appToken"),
        "SLACK_CHANNEL_ID": channels[0] if channels else None,
        "SLACK_APPROVER_ID": approvers[0] if approvers else None,
    }
    return {k: v for k, v in found.items() if isinstance(v, str) and v}


def database_names() -> tuple[str, str]:
    env = PROJECT_ROOT / ".env"
    url = ""
    if env.is_file():
        for line in env.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
    name = url.rsplit("/", 1)[-1] if url else "pharma_ai"
    return name, f"{name}_test"


def setup_database(report: Report, *, check: bool) -> None:
    print("\nDatabase")
    if not which("psql"):
        report.add(Step("databases", False, "psql not available"))
        return

    existing = run(["psql", "-tAc", "SELECT datname FROM pg_database", "postgres"]).stdout.split()
    for name in database_names():
        if name in existing:
            report.add(Step(f"database {name}", True, "exists"))
            continue
        if check:
            report.add(Step(f"database {name}", False, "would be created"))
            continue
        created = run(["createdb", name])
        report.add(
            Step(
                f"database {name}",
                created.returncode == 0,
                created.stderr.strip()[:120],
                changed=created.returncode == 0,
            )
        )


def migrate(report: Report, *, check: bool) -> None:
    print("\nSchema and registry")
    if check:
        report.add(Step("migrations", True, "would run: alembic upgrade head"))
        report.add(Step("agent registry", True, "would run: scripts/seed_agents.py"))
        return

    for name, command in (
        ("migrations", ["uv", "run", "alembic", "upgrade", "head"]),
        ("agent registry", ["uv", "run", "python", "scripts/seed_agents.py"]),
    ):
        result = run(command, cwd=PROJECT_ROOT)
        report.add(
            Step(
                name,
                result.returncode == 0,
                (result.stdout or result.stderr).strip().splitlines()[-1][:120]
                if (result.stdout or result.stderr)
                else "",
                changed=result.returncode == 0,
            )
        )


# -- OpenClaw ----------------------------------------------------------------


def render_config(values: dict[str, str]) -> str:
    text = TEMPLATE.read_text()
    text = text.replace("${HOME}", str(Path.home()))
    for key in (*REQUIRED_ENV, *SLACK_ENV):
        text = text.replace("${" + key + "}", values.get(key, ""))
    return text


def configure_openclaw(report: Report, values: dict[str, str], *, check: bool) -> None:
    print("\nOpenClaw configuration")
    if not TEMPLATE.is_file():
        report.add(Step("config template", False, f"missing {TEMPLATE}"))
        return

    rendered = render_config(values)
    remaining = re.findall(r"\$\{([A-Z_]+)\}", rendered)
    if remaining:
        report.add(
            Step("config placeholders", False, f"unfilled: {', '.join(sorted(set(remaining)))}")
        )
        return

    if OPENCLAW_CONFIG.is_file():
        # An existing gateway is left alone: overwriting it would discard whatever
        # the operator has since configured by hand.
        report.add(
            Step("gateway config", True, f"already present at {OPENCLAW_CONFIG}; left untouched")
        )
        return
    if check:
        report.add(Step("gateway config", False, f"would write {OPENCLAW_CONFIG}"))
        return

    OPENCLAW_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    OPENCLAW_CONFIG.write_text(json.dumps(json.loads(rendered), indent=2) + "\n")
    OPENCLAW_CONFIG.chmod(0o600)
    report.add(Step("gateway config", True, f"wrote {OPENCLAW_CONFIG}", changed=True))


def install_skills(report: Report, *, check: bool) -> None:
    print("\nAgent skills")
    command = ["uv", "run", "python", "scripts/install_skills.py"] + (["--check"] if check else [])
    result = run(command, cwd=PROJECT_ROOT)
    report.add(
        Step(
            "skills",
            result.returncode == 0 or check,
            (result.stdout or result.stderr).strip().splitlines()[-1][:120]
            if (result.stdout or result.stderr)
            else "",
            changed=not check and result.returncode == 0,
        )
    )


# -- the worker as a service -------------------------------------------------


def worker_plist() -> dict:
    uv = which("uv") or "/opt/homebrew/bin/uv"
    logs = PROJECT_ROOT / "data"
    return {
        "Label": WORKER_LABEL,
        "ProgramArguments": [uv, "run", "python", "-m", "app.worker"],
        "WorkingDirectory": str(PROJECT_ROOT),
        "EnvironmentVariables": {"PATH": f"{Path(uv).parent}:/usr/bin:/bin:/usr/sbin:/sbin"},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": str(logs / "worker.log"),
        "StandardErrorPath": str(logs / "worker.log"),
        "ThrottleInterval": 15,
    }


def tcc_blocked_folder() -> str | None:
    """Is the checkout somewhere launchd is not allowed to read?"""
    relative = (
        PROJECT_ROOT.relative_to(Path.home()) if PROJECT_ROOT.is_relative_to(Path.home()) else None
    )
    if relative and relative.parts and relative.parts[0] in TCC_PROTECTED:
        return relative.parts[0]
    return None


def install_worker_service(report: Report, *, check: bool) -> None:
    print("\nWorker service")

    blocked = tcc_blocked_folder()
    if blocked:
        report.add(
            Step(
                "worker LaunchAgent",
                False,
                f"cannot run from ~/{blocked}: macOS denies a launchd-spawned process "
                f"access to it, whatever the file permissions say. Move the checkout "
                f"outside ~/{blocked} (~/Nucleus, say) and rerun. Until then, start the "
                f"worker by hand: uv run python -m app.worker",
            )
        )
        return

    target = LAUNCH_AGENTS / f"{WORKER_LABEL}.plist"
    desired = plistlib.dumps(worker_plist())

    if target.is_file() and target.read_bytes() == desired:
        loaded = WORKER_LABEL in run(["launchctl", "list"]).stdout
        report.add(
            Step(
                "worker LaunchAgent",
                loaded,
                "installed and loaded"
                if loaded
                else "installed but not loaded; run launchctl load",
            )
        )
        return
    if check:
        report.add(Step("worker LaunchAgent", False, f"would install {target}"))
        return

    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(desired)
    run(["launchctl", "unload", str(target)])
    load_result = run(["launchctl", "load", str(target)])
    report.add(
        Step(
            "worker LaunchAgent",
            load_result.returncode == 0,
            load_result.stderr.strip()[:120] or "installed and loaded",
            changed=True,
        )
    )


# -- what a human still has to do -------------------------------------------


def manual_steps(report: Report) -> None:
    codex_home = OPENCLAW_HOME / "agents" / "pharma-executive" / "agent" / "codex-home"
    claude_state = Path("~/.claude").expanduser()

    if not codex_home.is_dir():
        report.manual.append(
            "Sign in to the OpenAI/Codex subscription: run `openclaw onboard`, or "
            "`openclaw agent --agent pharma-executive --message hello` and follow the prompt."
        )
    if not claude_state.exists():
        report.manual.append(
            "Sign in to the Claude subscription so the specialist route can run: `claude` "
            "once in a terminal, then check with `openclaw agent --agent pharma-specialist "
            "--message hello`."
        )
    report.manual.append(
        "Optional, only for Slack: install the app from "
        "openclaw/setup/slack-app-manifest.json and put its tokens in .env. The Control "
        "UI (`openclaw dashboard`) needs none of this."
    )
    report.manual.append(
        "Ingest the client's source documents: scripts/ingest_documents.py --project <name> <path>"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the Nucleus ecosystem on this machine.")
    parser.add_argument("--check", action="store_true", help="report without changing anything")
    args = parser.parse_args()

    print(f"Nucleus bootstrap — {PROJECT_ROOT}")
    print("(dry run)" if args.check else "")

    report = Report()
    check_prerequisites(report)
    values = check_env(report)

    if report.blocked:
        print("\nStopping: the items marked MISSING above are needed first.")
        for step in report.blocked:
            print(f"  - {step.name}: {step.detail}")
        return 1

    setup_database(report, check=args.check)
    migrate(report, check=args.check)
    configure_openclaw(report, values, check=args.check)
    install_skills(report, check=args.check)
    install_worker_service(report, check=args.check)
    manual_steps(report)

    print("\nStill needs a human:")
    for item in report.manual:
        print(f"  - {item}")

    blocked = report.blocked
    print(
        f"\n{len(report.steps) - len(blocked)}/{len(report.steps)} automated steps in place."
        if not args.check
        else f"\n{len(blocked)} step(s) would change."
    )
    if not args.check:
        print("Verify with: uv run python scripts/health_check.py")
    return 1 if blocked and not args.check else 0


if __name__ == "__main__":
    sys.exit(main())
