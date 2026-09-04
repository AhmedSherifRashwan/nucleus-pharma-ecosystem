"""Durable single-worker job queue (PRD s31).

Claiming uses SELECT ... FOR UPDATE SKIP LOCKED so a second worker can be added
later without double-processing, and so a crashed worker's job can be reclaimed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import RunJob

RETRY_BASE_SECONDS = 30
STALE_AFTER_SECONDS = 1800


class JobRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def enqueue(
        self,
        run_id: uuid.UUID,
        *,
        job_type: str = "orchestrate",
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        max_attempts: int = 3,
    ) -> RunJob:
        job = RunJob(
            run_id=run_id,
            job_type=job_type,
            payload=payload or {},
            idempotency_key=idempotency_key,
            status="queued",
            max_attempts=max_attempts,
            available_at=datetime.now(UTC),
        )
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_by_idempotency_key(self, key: str) -> RunJob | None:
        stmt = select(RunJob).where(RunJob.idempotency_key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def claim_next(self, worker_id: str) -> RunJob | None:
        """Atomically claim one due job. None when the queue is empty."""
        stmt = (
            select(RunJob)
            .where(RunJob.status == "queued", RunJob.available_at <= datetime.now(UTC))
            .order_by(RunJob.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        job = (await self.session.execute(stmt)).scalar_one_or_none()
        if job is None:
            return None
        job.status = "running"
        job.attempts += 1
        job.locked_at = datetime.now(UTC)
        job.locked_by = worker_id
        await self.session.flush()
        return job

    async def set_status(self, job_id: uuid.UUID, status: str) -> None:
        await self.session.execute(update(RunJob).where(RunJob.id == job_id).values(status=status))

    async def complete(self, job_id: uuid.UUID) -> None:
        await self.session.execute(
            update(RunJob)
            .where(RunJob.id == job_id)
            .values(status="complete", locked_at=None, locked_by=None, last_error=None)
        )

    async def fail(self, job_id: uuid.UUID, error: str) -> str:
        """Reschedule with backoff, or fail once attempts are exhausted.

        Returns the resulting status so the caller reports accurately rather than
        assuming a retry happened.
        """
        job = await self.session.get(RunJob, job_id)
        if job is None:
            return "missing"
        if job.attempts >= job.max_attempts:
            job.status = "failed"
            job.last_error = error[:4000]
            job.locked_at = None
            job.locked_by = None
            await self.session.flush()
            return "failed"

        delay = RETRY_BASE_SECONDS * (2 ** (job.attempts - 1))
        job.status = "queued"
        job.available_at = datetime.now(UTC) + timedelta(seconds=delay)
        job.last_error = error[:4000]
        job.locked_at = None
        job.locked_by = None
        await self.session.flush()
        return "queued"

    async def cancel(self, run_id: uuid.UUID) -> int:
        result = cast(
            CursorResult,
            await self.session.execute(
                update(RunJob)
                .where(RunJob.run_id == run_id, RunJob.status.in_(["queued", "running"]))
                .values(status="cancelled", locked_at=None, locked_by=None)
            ),
        )
        return int(result.rowcount or 0)

    async def reclaim_stale(self, older_than_seconds: int = STALE_AFTER_SECONDS) -> int:
        """Return jobs orphaned by a crashed worker to the queue (s38)."""
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_seconds)
        result = cast(
            CursorResult,
            await self.session.execute(
                update(RunJob)
                .where(RunJob.status == "running", RunJob.locked_at < cutoff)
                .values(status="queued", locked_at=None, locked_by=None)
            ),
        )
        return int(result.rowcount or 0)

    async def latest_for_run(self, run_id: uuid.UUID) -> RunJob | None:
        stmt = (
            select(RunJob)
            .where(RunJob.run_id == run_id)
            .order_by(RunJob.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def queue_depth(self) -> int:
        count = await self.session.scalar(
            select(func.count(RunJob.id)).where(RunJob.status.in_(["queued", "running"]))
        )
        return int(count or 0)
