"""Live progress rendering for a run (PRD s17.1).

A run takes minutes. Without visible progress the user cannot tell the
difference between "thinking" and "broken", so this shows which specialist is
working, which stage the ecosystem is in, and an indicative time remaining.

The estimate is explicitly indicative. It is derived from observed durations
rather than measured per run, and is labelled as approximate wherever it is
shown - an over-precise number would be worse than an honest range.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Observed on this deployment: a specialist turn runs 60-105s, and the evidence
# correction round roughly doubles it when it fires.
SECONDS_PER_SPECIALIST_GROUP = 110
SECONDS_PLANNING = 40
SECONDS_CHALLENGE = 70
SECONDS_SYNTHESIS = 45
SECONDS_REPORT = 15

STAGE_LABELS: dict[str, str] = {
    "intake": "Interpreting the request",
    "load_context": "Loading project context",
    "plan": "Planning specialist workstreams",
    "plan_validated": "Plan validated",
    "awaiting_approval": "Waiting for your approval",
    "dispatch": "Specialists working",
    "collect_results": "Collecting specialist results",
    "conflict_detection": "Checking for contradictions",
    "challenge": "Challenging the findings",
    "follow_up_tasks": "Running targeted follow-ups",
    "synthesis": "Writing the executive brief",
    "quality_gate": "Quality gate",
    "awaiting_decision": "Waiting for your decision",
    "reporting": "Building the report",
    "finished": "Complete",
}

STATUS_ICON = {
    "complete": ":white_check_mark:",
    "running": ":hourglass_flowing_sand:",
    "pending": ":black_small_square:",
    "ready": ":black_small_square:",
    "failed": ":x:",
    "blocked": ":no_entry:",
    "needs_information": ":large_yellow_circle:",
}

STATUS_NOTE = {
    "needs_information": "needs data",
    "blocked": "blocked",
    "failed": "failed",
}


@dataclass
class TaskProgress:
    agent_id: str
    agent_name: str
    objective: str
    status: str
    execution_group: int


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{max(seconds, 0)}s"
    minutes, remainder = divmod(int(seconds), 60)
    return f"{minutes}m {remainder:02d}s" if remainder else f"{minutes}m"


def estimate_remaining_seconds(tasks: list[TaskProgress], stage: str) -> int:
    """Indicative time to completion. Deliberately coarse."""
    if stage in ("finished", "awaiting_approval", "awaiting_decision"):
        return 0

    outstanding_groups = {
        t.execution_group for t in tasks if t.status in ("pending", "ready", "running")
    }
    remaining = len(outstanding_groups) * SECONDS_PER_SPECIALIST_GROUP

    if stage in ("intake", "load_context", "plan"):
        remaining += SECONDS_PLANNING
    if stage not in ("challenge", "follow_up_tasks", "synthesis", "quality_gate", "reporting"):
        remaining += SECONDS_CHALLENGE
    if stage not in ("synthesis", "quality_gate", "reporting"):
        remaining += SECONDS_SYNTHESIS
    if stage != "reporting":
        remaining += SECONDS_REPORT
    return remaining


def estimate_label(seconds: int) -> str:
    """A range, not a false precision."""
    if seconds <= 0:
        return "finishing up"
    if seconds < 90:
        return "about a minute"
    minutes = round(seconds / 60)
    return f"about {minutes}-{minutes + 1} minutes"


def render(
    run_ref: str,
    tasks: list[TaskProgress],
    stage: str,
    elapsed_seconds: int,
) -> list[dict[str, Any]]:
    """Block Kit for a progress update."""
    stage_label = STAGE_LABELS.get(stage, stage.replace("_", " ").title())

    if tasks:
        lines = []
        for t in sorted(tasks, key=lambda t: (t.execution_group, t.agent_id)):
            icon = STATUS_ICON.get(t.status, ":grey_question:")
            note = STATUS_NOTE.get(t.status)
            suffix = f" _({note})_" if note else ""
            lines.append(f"{icon} *{t.agent_name}*{suffix}\n     {t.objective[:110]}")
        body = "\n".join(lines)
    else:
        body = "_Selecting the specialist team…_"

    done = sum(1 for t in tasks if t.status not in ("pending", "ready", "running"))
    counter = f"{done}/{len(tasks)} specialists" if tasks else "planning"

    remaining = estimate_remaining_seconds(tasks, stage)
    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*{run_ref} — {stage_label}*\n\n{body}"[:2900],
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"{counter} · elapsed {format_duration(elapsed_seconds)} · "
                        f"est. {estimate_label(remaining)} remaining _(approximate)_"
                    ),
                }
            ],
        },
    ]
    return blocks
