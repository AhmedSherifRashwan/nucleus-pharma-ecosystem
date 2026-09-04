"""Run and task persistence (PRD s14, FR-002, FR-015)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.planning import Plan
from app.schemas.specialist import SpecialistResult
from app.storage.models import Run, RunTask, SpecialistResultRecord

# Terminal states: a run in one of these is finished and must not be resumed.
TERMINAL_STATUSES = frozenset({"complete", "failed", "cancelled", "review_required"})


class RunRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # -- runs ---------------------------------------------------------------

    async def next_run_ref(self) -> str:
        """Human-facing reference, e.g. RUN-2026-0042 (s17.1)."""
        year = datetime.now(UTC).year
        count = await self.session.scalar(
            select(func.count()).select_from(Run).where(Run.run_ref.like(f"RUN-{year}-%"))
        )
        return f"RUN-{year}-{(count or 0) + 1:04d}"

    async def create(
        self,
        *,
        user_request: str,
        project_id: uuid.UUID | None = None,
        slack_channel_id: str | None = None,
        slack_thread_ts: str | None = None,
        slack_user_id: str | None = None,
        idempotency_key: str | None = None,
        requested_output_format: str = "brief",
        budgets: dict[str, Any] | None = None,
        run_ref: str | None = None,
    ) -> Run:
        run = Run(
            run_ref=run_ref or await self.next_run_ref(),
            project_id=project_id,
            user_request=user_request,
            slack_channel_id=slack_channel_id,
            slack_thread_ts=slack_thread_ts,
            slack_user_id=slack_user_id,
            idempotency_key=idempotency_key,
            requested_output_format=requested_output_format,
            budgets=budgets or {},
            status="queued",
            current_stage="intake",
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def get(self, run_id: uuid.UUID) -> Run | None:
        return await self.session.get(Run, run_id)

    async def get_by_ref(self, run_ref: str) -> Run | None:
        stmt = select(Run).where(Run.run_ref == run_ref)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(self, key: str) -> Run | None:
        """FR-002 / s31.2: the same request must never create two runs."""
        stmt = select(Run).where(Run.idempotency_key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def set_stage(self, run_id: uuid.UUID, stage: str, status: str | None = None) -> None:
        values: dict[str, Any] = {"current_stage": stage}
        if status:
            values["status"] = status
        if stage == "intake":
            values["started_at"] = datetime.now(UTC)
        await self.session.execute(update(Run).where(Run.id == run_id).values(**values))

    async def save_plan(self, run_id: uuid.UUID, plan: Plan) -> None:
        await self.session.execute(
            update(Run).where(Run.id == run_id).values(plan=plan.model_dump(mode="json"))
        )

    async def record_routes(
        self,
        run_id: uuid.UUID,
        *,
        executive_route: str | None = None,
        specialist_route: str | None = None,
        auth_mode: str | None = None,
    ) -> None:
        """s20: a run records which routes and auth mode produced it."""
        values = {
            k: v
            for k, v in (
                ("executive_route", executive_route),
                ("specialist_route", specialist_route),
                ("auth_mode", auth_mode),
            )
            if v is not None
        }
        if values:
            await self.session.execute(update(Run).where(Run.id == run_id).values(**values))

    async def increment_challenge_round(self, run_id: uuid.UUID) -> int:
        run = await self.session.get(Run, run_id)
        if run is None:
            return 0
        run.challenge_round += 1
        await self.session.flush()
        return run.challenge_round

    async def finish(
        self,
        run_id: uuid.UUID,
        *,
        status: str,
        final_summary: str | None = None,
        executive_brief: dict[str, Any] | None = None,
        error_state: str | None = None,
    ) -> None:
        await self.session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(
                status=status,
                current_stage="finished",
                final_summary=final_summary,
                executive_brief=executive_brief,
                error_state=error_state,
                completed_at=datetime.now(UTC),
            )
        )

    # -- tasks --------------------------------------------------------------

    async def create_tasks(
        self, run_id: uuid.UUID, plan: Plan, execution_groups: list[list[str]]
    ) -> list[RunTask]:
        group_of = {task_id: idx for idx, group in enumerate(execution_groups) for task_id in group}
        rows: list[RunTask] = []
        for task in plan.tasks:
            row = RunTask(
                run_id=run_id,
                planner_task_id=task.task_id,
                agent_id=task.agent_id,
                objective=task.objective,
                required_output=task.required_output,
                depends_on=list(task.depends_on),
                context_refs=list(task.context_refs),
                execution_group=group_of.get(task.task_id, 0),
                max_steps=task.max_steps,
                status="pending",
            )
            self.session.add(row)
            rows.append(row)
        await self.session.flush()
        return rows

    async def add_followup_tasks(
        self, run_id: uuid.UUID, tasks: list, execution_group: int
    ) -> list[RunTask]:
        rows: list[RunTask] = []
        for task in tasks:
            row = RunTask(
                run_id=run_id,
                planner_task_id=task.task_id,
                agent_id=task.agent_id,
                objective=task.objective,
                required_output=task.required_output,
                depends_on=[],
                context_refs=list(task.context_refs),
                execution_group=execution_group,
                max_steps=task.max_steps,
                status="pending",
                is_followup=True,
            )
            self.session.add(row)
            rows.append(row)
        await self.session.flush()
        return rows

    async def list_tasks(self, run_id: uuid.UUID) -> list[RunTask]:
        stmt = (
            select(RunTask)
            .where(RunTask.run_id == run_id)
            .order_by(RunTask.execution_group, RunTask.planner_task_id)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def set_task_status(
        self,
        task_row_id: uuid.UUID,
        status: str,
        *,
        error: str | None = None,
        agent_version: str | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if error is not None:
            values["error"] = error[:4000]
        if agent_version is not None:
            values["agent_version"] = agent_version
        await self.session.execute(
            update(RunTask).where(RunTask.id == task_row_id).values(**values)
        )

    # -- specialist results -------------------------------------------------

    async def save_result(
        self,
        *,
        run_id: uuid.UUID,
        task_row_id: uuid.UUID | None,
        result: SpecialistResult,
        agent_version: str | None = None,
        gateway_route: str | None = None,
        model_id: str | None = None,
        duration_ms: int | None = None,
    ) -> SpecialistResultRecord:
        row = SpecialistResultRecord(
            run_id=run_id,
            task_id=task_row_id,
            planner_task_id=result.task_id,
            agent_id=result.agent_id,
            agent_version=agent_version,
            gateway_route=gateway_route,
            model_id=model_id,
            status=result.status,
            confidence=result.confidence,
            executive_summary=result.executive_summary,
            structured_result=result.model_dump(mode="json"),
            escalation_required=result.escalation_required,
            duration_ms=duration_ms,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_results(self, run_id: uuid.UUID) -> list[SpecialistResultRecord]:
        stmt = (
            select(SpecialistResultRecord)
            .where(SpecialistResultRecord.run_id == run_id)
            .order_by(SpecialistResultRecord.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())
