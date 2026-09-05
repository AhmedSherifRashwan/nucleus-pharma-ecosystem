"""Run-time estimation from observed durations (PRD s17.1).

The first estimator used fixed constants and was wrong by a factor of six: it
told the user "8-10 minutes" on a run that took an hour. Constants cannot know
that this deployment's specialists take four minutes on report-length work while
another's take one.

So the estimate is measured. Every specialist turn already records `duration_ms`;
this reads them back — the run's own turns first, this deployment's recent history
next, and only then a conservative default. The estimate widens rather than
narrows when it is uncertain, because a range that keeps expiring reads as a stall
and destroys the user's trust in the number.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit.logger import get_logger
from app.storage.models import SpecialistResultRecord

log = get_logger(__name__)

# Used only when nothing has ever been measured on this deployment. Deliberately
# generous: a first run that finishes early is a pleasant surprise, one that
# overruns its own estimate is not.
DEFAULT_SPECIALIST_SECONDS = 300.0
DEFAULT_STAGE_SECONDS = {
    "planning": 70.0,
    "challenge": 150.0,
    "synthesis": 120.0,
    "report": 420.0,
}

# How many recent turns inform the deployment-level figure.
HISTORY_LIMIT = 60
MIN_SAMPLES_FOR_RUN = 2


@dataclass
class DurationModel:
    """What a specialist turn costs here, in seconds."""

    specialist_seconds: float = DEFAULT_SPECIALIST_SECONDS
    samples: int = 0
    source: str = "default"

    @property
    def is_measured(self) -> bool:
        return self.samples > 0

    def group_seconds(self, parallel_width: int = 1) -> float:
        """A group runs in parallel, so it costs about one turn, plus a little.

        Fanning out four specialists does not cost four turns of wall clock, but
        it is not free either: the group finishes when its slowest member does,
        and a wider group has a slower slowest member.
        """
        width = max(1, parallel_width)
        return self.specialist_seconds * (1.0 + 0.12 * (width - 1))


async def measure_durations(
    session: AsyncSession, run_id: uuid.UUID | None = None
) -> DurationModel:
    """Median specialist duration: this run's own turns, else recent history."""
    if run_id is not None:
        stmt = select(SpecialistResultRecord.duration_ms).where(
            SpecialistResultRecord.run_id == run_id,
            SpecialistResultRecord.duration_ms.is_not(None),
        )
        own = [d for (d,) in (await session.execute(stmt)).all() if d]
        if len(own) >= MIN_SAMPLES_FOR_RUN:
            return DurationModel(
                specialist_seconds=statistics.median(own) / 1000.0,
                samples=len(own),
                source="this run",
            )

    stmt = (
        select(SpecialistResultRecord.duration_ms)
        .where(SpecialistResultRecord.duration_ms.is_not(None))
        .order_by(SpecialistResultRecord.created_at.desc())
        .limit(HISTORY_LIMIT)
    )
    recent = [d for (d,) in (await session.execute(stmt)).all() if d]
    if recent:
        return DurationModel(
            specialist_seconds=statistics.median(recent) / 1000.0,
            samples=len(recent),
            source="recent runs",
        )
    return DurationModel()


def remaining_seconds(
    model: DurationModel,
    *,
    stage: str,
    outstanding_groups: int,
    widest_outstanding_group: int = 1,
    challenge_done: bool = False,
    synthesis_done: bool = False,
    report_done: bool = False,
) -> int:
    """Seconds of work still ahead, from measured cost per turn."""
    if stage in ("finished", "awaiting_approval", "awaiting_decision"):
        return 0

    total = outstanding_groups * model.group_seconds(widest_outstanding_group)

    if stage in ("intake", "load_context", "plan"):
        total += DEFAULT_STAGE_SECONDS["planning"]
    if not challenge_done and stage not in ("synthesis", "quality_gate", "reporting"):
        total += DEFAULT_STAGE_SECONDS["challenge"]
        # A challenge round can spawn follow-ups; they cost about one more group.
        total += model.group_seconds(2) * 0.5
    if not synthesis_done and stage not in ("quality_gate", "reporting"):
        total += DEFAULT_STAGE_SECONDS["synthesis"]
    if not report_done and stage != "reporting":
        total += DEFAULT_STAGE_SECONDS["report"]
    return int(total)


def format_range(seconds: int, *, measured: bool) -> str:
    """A range whose width reflects how much the estimate is trusted."""
    if seconds <= 0:
        return "finishing up"
    minutes = seconds / 60.0
    if minutes < 2:
        return "a minute or two"
    # An unmeasured estimate is a guess and says so by being wider.
    spread = 0.25 if measured else 0.5
    low = max(1, round(minutes * (1 - spread)))
    high = max(low + 1, round(minutes * (1 + spread)))
    if high >= 60:
        return f"about {low // 60}-{high // 60 + 1} hours"
    return f"about {low}-{high} minutes"
