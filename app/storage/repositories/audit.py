"""Append-only audit trail (PRD s14, s21, FR-019).

Rows are written and never updated or deleted. This is the product record that
makes a run reconstructible, distinct from operational logs.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import AuditEvent


class EventType:
    RUN_CREATED = "run.created"
    RUN_COMPLETE = "run.complete"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_REVIEW_REQUIRED = "run.review_required"

    JOB_CLAIMED = "job.claimed"
    JOB_FAILED = "job.failed"

    PLAN_CREATED = "plan.created"
    PLAN_REJECTED = "plan.rejected"

    TASK_DISPATCHED = "task.dispatched"
    TASK_COMPLETE = "task.complete"
    TASK_FAILED = "task.failed"

    CONFLICT_DETECTED = "conflict.detected"
    CHALLENGE_COMPLETE = "challenge.complete"
    ESCALATION_RAISED = "escalation.raised"
    SYNTHESIS_COMPLETE = "synthesis.complete"
    QUALITY_GATE = "quality_gate.evaluated"

    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    DECISION_RECORDED = "decision.recorded"

    EVIDENCE_CREATED = "evidence.created"
    ARTIFACT_CREATED = "artifact.created"
    REPORT_GENERATED = "report.generated"


class AuditRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def record(
        self,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str | None = None,
        run_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        task_id: uuid.UUID | None = None,
        agent_version: str | None = None,
        gateway_route: str | None = None,
        model_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            event_type=event_type,
            actor_type=actor_type,
            actor_id=actor_id,
            run_id=run_id,
            project_id=project_id,
            task_id=task_id,
            agent_version=agent_version,
            gateway_route=gateway_route,
            model_id=model_id,
            event_metadata=metadata or {},
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def for_run(self, run_id: uuid.UUID) -> list[AuditEvent]:
        stmt = select(AuditEvent).where(AuditEvent.run_id == run_id).order_by(AuditEvent.created_at)
        return list((await self.session.execute(stmt)).scalars().all())

    async def event_types_for_run(self, run_id: uuid.UUID) -> set[str]:
        stmt = select(AuditEvent.event_type).where(AuditEvent.run_id == run_id)
        return {t for (t,) in (await self.session.execute(stmt)).all()}
