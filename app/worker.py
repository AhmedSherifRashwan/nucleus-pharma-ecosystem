"""The Neucleus job worker (PRD s31, Phase 10).

A single supervised process claims queued jobs and executes the LangGraph run.
Long multi-specialist work never runs inside a Slack handler or the OpenClaw
skill; it happens here, so the user gets an acknowledgement in milliseconds and
the run survives a restart.

    uv run python -m app.worker
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import uuid
from typing import Any

from app.agents.registry import get_registry
from app.audit.logger import (
    bind_run_context,
    clear_run_context,
    configure_logging,
    get_logger,
)
from app.config import get_settings
from app.orchestration.engine import execute_run, orchestration_app, resume_run, setup_checkpointer
from app.providers.openclaw_client import get_gateway_client
from app.storage.db import dispose_engine, session_scope
from app.storage.repositories import AuditRepository, EventType, JobRepository, RunRepository

log = get_logger(__name__)

STALE_JOB_SECONDS = 1800
TERMINAL_RUN_STATUSES = frozenset({"complete", "failed", "cancelled", "review_required"})


class Worker:
    def __init__(self, worker_id: str | None = None):
        settings = get_settings()
        self.worker_id = worker_id or settings.worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.poll_interval = settings.worker_poll_interval_seconds
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        log.info("worker_stop_requested", worker_id=self.worker_id)
        self._stopping.set()

    async def preflight(self) -> None:
        """Fail fast on a broken registry or an unreachable gateway.

        Accepting jobs we cannot execute would burn retries and leave runs stuck.
        """
        registry = get_registry()
        await setup_checkpointer()
        health = await get_gateway_client().health_check()
        if not health.reachable:
            log.warning("gateway_unreachable_at_startup", detail=health.detail[:200])
        log.info(
            "worker_started",
            worker_id=self.worker_id,
            profiles=len(registry),
            enabled=len(registry.enabled_profiles()),
            gateway_reachable=health.reachable,
        )

    async def run_forever(self) -> None:
        await self.preflight()
        async with orchestration_app() as app:
            await self._reclaim_stale()
            while not self._stopping.is_set():
                try:
                    claimed = await self._process_one(app)
                except Exception as exc:
                    log.exception("worker_loop_error", error=str(exc)[:500])
                    claimed = False
                if not claimed:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self._stopping.wait(), timeout=self.poll_interval)
        log.info("worker_stopped", worker_id=self.worker_id)

    async def _reclaim_stale(self) -> None:
        async with session_scope() as session:
            count = await JobRepository(session).reclaim_stale(STALE_JOB_SECONDS)
        if count:
            log.warning("reclaimed_stale_jobs", count=count)

    async def _process_one(self, app: Any) -> bool:
        async with session_scope() as session:
            job = await JobRepository(session).claim_next(self.worker_id)
            if job is None:
                return False
            job_id, run_id, attempts = job.id, job.run_id, job.attempts
            job_type, payload = job.job_type, dict(job.payload or {})

        bind_run_context(run_id=str(run_id), worker=self.worker_id)
        log.info("job_claimed", job_id=str(job_id), type=job_type, attempt=attempts)

        try:
            async with session_scope() as session:
                await AuditRepository(session).record(
                    event_type=EventType.JOB_CLAIMED,
                    actor_type="system",
                    actor_id=self.worker_id,
                    run_id=run_id,
                    metadata={"job_id": str(job_id), "type": job_type, "attempt": attempts},
                )

            await self._execute(app, run_id, job_type, payload)

            async with session_scope() as session:
                await JobRepository(session).complete(job_id)
            log.info("job_complete", job_id=str(job_id))

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("job_failed", job_id=str(job_id), error=error[:400])
            async with session_scope() as session:
                outcome = await JobRepository(session).fail(job_id, error)
                await AuditRepository(session).record(
                    event_type=EventType.JOB_FAILED,
                    actor_type="system",
                    actor_id=self.worker_id,
                    run_id=run_id,
                    metadata={"job_id": str(job_id), "error": error[:1000], "outcome": outcome},
                )
                # Attempts exhausted: the run must not sit in "running" forever.
                if outcome == "failed":
                    await RunRepository(session).finish(
                        run_id, status="failed", error_state=error[:4000]
                    )
        finally:
            clear_run_context()
        return True

    async def _execute(
        self, app: Any, run_id: uuid.UUID, job_type: str, payload: dict[str, Any]
    ) -> None:
        if job_type == "resume":
            decision = payload.get("resume") or {}
            log.info("resuming_run", run_id=str(run_id), decision=decision.get("status"))
            await resume_run(app, run_id, decision)
            return

        async with session_scope() as session:
            run = await RunRepository(session).get(run_id)
            if run is None:
                raise RuntimeError(f"run {run_id} not found")
            if run.status in TERMINAL_RUN_STATUSES:
                log.info("run_already_terminal", status=run.status)
                return
            initial_state = {
                "run_id": str(run.id),
                "run_ref": run.run_ref,
                "project_id": str(run.project_id) if run.project_id else None,
                "slack_channel": run.slack_channel_id,
                "slack_thread_ts": run.slack_thread_ts,
                "slack_user_id": run.slack_user_id,
                "user_request": run.user_request,
                "requested_output_format": run.requested_output_format,
                "budgets": run.budgets or {},
                "challenge_round": run.challenge_round or 0,
            }

        await execute_run(app, initial_state, run_id)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    worker = Worker()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)

    try:
        await worker.run_forever()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())
