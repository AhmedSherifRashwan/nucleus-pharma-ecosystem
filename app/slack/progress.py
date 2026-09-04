"""Live progress narration for a run (PRD s17.1).

A run takes tens of minutes. Without visible progress the user cannot tell the
difference between "thinking" and "broken".

Progress is narrated as a series of messages, not one message edited in place. An
edited message is invisible to anyone who has already scrolled past it, gives no
notification, and loses the history of what happened when. Each update says what
just changed and carries a bar for the whole run, so a reader who joins the thread
late still knows where things stand.

The estimate is explicitly indicative. It is derived from observed durations rather
than measured per run, and is labelled approximate wherever it is shown - an
over-precise number would be worse than an honest range.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Observed on this deployment: a specialist turn runs 150-300s for report-length
# work, and the evidence correction round adds to it when it fires. These are
# deliberately generous - an estimate that keeps expiring reads as a stall.
SECONDS_PER_SPECIALIST_GROUP = 260
SECONDS_PLANNING = 60
SECONDS_CHALLENGE = 120
SECONDS_SYNTHESIS = 90
SECONDS_REPORT = 240

BAR_WIDTH = 18
BAR_FULL = "█"
BAR_EMPTY = "░"

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

# Roughly what fraction of a run is behind you once a stage completes. Used only
# for the bar: it is a progress indicator, not a measurement.
STAGE_FLOOR: dict[str, float] = {
    "intake": 0.02,
    "load_context": 0.04,
    "plan": 0.06,
    "plan_validated": 0.10,
    "awaiting_approval": 0.10,
    "dispatch": 0.15,
    "collect_results": 0.60,
    "conflict_detection": 0.62,
    "challenge": 0.66,
    "follow_up_tasks": 0.68,
    "synthesis": 0.80,
    "quality_gate": 0.86,
    "awaiting_decision": 0.88,
    "reporting": 0.90,
    "finished": 1.0,
}

# Specialist work spans this band of the bar; within it the fraction tracks the
# tasks that have actually returned.
DISPATCH_FLOOR = 0.15
DISPATCH_CEILING = 0.60

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
    "complete": "complete",
    "needs_information": "needs more data",
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
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


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
    return f"about {minutes}-{minutes + 2} minutes"


def overall_fraction(stage: str, tasks: list[TaskProgress]) -> float:
    """How far through the run we are, for the bar."""
    floor = STAGE_FLOOR.get(stage, 0.1)
    if stage != "dispatch" or not tasks:
        return min(max(floor, 0.0), 1.0)

    done = sum(1 for t in tasks if t.status not in ("pending", "ready", "running"))
    share = done / len(tasks)
    return DISPATCH_FLOOR + (DISPATCH_CEILING - DISPATCH_FLOOR) * share


def progress_bar(fraction: float, width: int = BAR_WIDTH) -> str:
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    return f"{BAR_FULL * filled}{BAR_EMPTY * (width - filled)}  {round(fraction * 100)}%"


def status_line(
    stage: str,
    tasks: list[TaskProgress],
    elapsed_seconds: int,
    *,
    remaining_seconds: int | None = None,
) -> str:
    """The bar, the counter, the clock and the estimate, on one line."""
    fraction = overall_fraction(stage, tasks)
    bar = progress_bar(fraction)
    done = sum(1 for t in tasks if t.status not in ("pending", "ready", "running"))
    counter = f"{done}/{len(tasks)} specialists" if tasks else STAGE_LABELS.get(stage, stage)
    remaining = (
        remaining_seconds
        if remaining_seconds is not None
        else estimate_remaining_seconds(tasks, stage)
    )
    return (
        f"`{bar}`\n{counter} · elapsed {format_duration(elapsed_seconds)} · "
        f"est. {estimate_label(remaining)} remaining _(approximate)_"
    )


def event(
    run_ref: str,
    headline: str,
    *,
    stage: str,
    tasks: list[TaskProgress] | None = None,
    elapsed_seconds: int = 0,
    detail: str | None = None,
    bullets: list[str] | None = None,
    remaining_seconds: int | None = None,
) -> list[dict[str, Any]]:
    """One progress message: what just happened, and where the run stands."""
    tasks = tasks or []
    body = f"*{run_ref}* · {headline}"
    if detail:
        body += f"\n>{detail[:600]}"
    if bullets:
        body += "\n" + "\n".join(f"• {b}" for b in bullets)

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": status_line(
                        stage, tasks, elapsed_seconds, remaining_seconds=remaining_seconds
                    ),
                }
            ],
        },
    ]


def specialist_started(agent_name: str, objective: str) -> tuple[str, str]:
    return f":hourglass_flowing_sand: *{agent_name}* is now working", objective[:400]


def specialist_finished(
    agent_name: str, status: str, *, findings: int = 0, evidence: int = 0
) -> tuple[str, str | None]:
    icon = STATUS_ICON.get(status, ":grey_question:")
    note = STATUS_NOTE.get(status, status)
    headline = f"{icon} *{agent_name}* — {note}"
    if status in ("complete", "needs_information") and (findings or evidence):
        return headline, f"{findings} finding(s), {evidence} evidence item(s) recorded."
    return headline, None
