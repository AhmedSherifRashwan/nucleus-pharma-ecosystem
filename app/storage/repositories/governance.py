"""Assumptions, conflicts, risks, approvals and decisions (PRD s14, s16).

These are the records that make a run inspectable after the fact. Nothing here
executes anything: an approval row records that a human decided, it does not
grant the system authority to act.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Approval, Assumption, Conflict, Decision, Risk

DECISION_CLASSES = ("green", "amber", "red")


class GovernanceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    # -- assumptions --------------------------------------------------------

    async def add_assumption(
        self,
        *,
        statement: str,
        run_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        originating_agent: str | None = None,
    ) -> Assumption:
        row = Assumption(
            statement=statement,
            run_id=run_id,
            project_id=project_id,
            originating_agent=originating_agent,
            state="proposed",
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def approve_assumption(
        self, assumption_id: uuid.UUID, *, approved_by: str, rationale: str | None = None
    ) -> None:
        row = await self.session.get(Assumption, assumption_id)
        if row is not None:
            row.state = "approved"
            row.approved_by = approved_by
            row.approved_at = datetime.now(UTC)
            row.rationale = rationale

    async def list_assumptions(self, run_id: uuid.UUID) -> list[Assumption]:
        stmt = select(Assumption).where(Assumption.run_id == run_id)
        return list((await self.session.execute(stmt)).scalars().all())

    # -- conflicts ----------------------------------------------------------

    async def add_conflict(
        self,
        *,
        run_id: uuid.UUID,
        description: str,
        agent_ids: list[str] | None = None,
        severity: str = "medium",
        material: bool = True,
    ) -> Conflict:
        row = Conflict(
            run_id=run_id,
            description=description,
            agent_ids=agent_ids or [],
            severity=severity,
            material=material,
            state="open",
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def resolve_conflict(
        self, conflict_id: uuid.UUID, *, resolution: str, state: str = "resolved"
    ) -> None:
        row = await self.session.get(Conflict, conflict_id)
        if row is not None:
            row.state = state
            row.resolution = resolution

    async def list_conflicts(self, run_id: uuid.UUID) -> list[Conflict]:
        stmt = select(Conflict).where(Conflict.run_id == run_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def open_material_conflicts(self, run_id: uuid.UUID) -> list[Conflict]:
        """s9.3 / quality gate: an unresolved material conflict blocks a clean finish."""
        stmt = select(Conflict).where(
            Conflict.run_id == run_id,
            Conflict.state == "open",
            Conflict.material.is_(True),
        )
        return list((await self.session.execute(stmt)).scalars().all())

    # -- risks --------------------------------------------------------------

    async def add_risk(
        self,
        *,
        description: str,
        run_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        likelihood: str = "medium",
        impact: str = "medium",
        mitigation: str | None = None,
        owner: str | None = None,
        originating_agent: str | None = None,
    ) -> Risk:
        row = Risk(
            description=description,
            run_id=run_id,
            project_id=project_id,
            likelihood=likelihood,
            impact=impact,
            mitigation=mitigation,
            owner=owner,
            originating_agent=originating_agent,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_risks(self, run_id: uuid.UUID) -> list[Risk]:
        stmt = select(Risk).where(Risk.run_id == run_id)
        return list((await self.session.execute(stmt)).scalars().all())

    # -- approvals (s16) ----------------------------------------------------

    async def request_approval(
        self,
        *,
        run_id: uuid.UUID,
        gate: str,
        decision_class: str = "amber",
        requested_payload: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> Approval:
        if decision_class not in DECISION_CLASSES:
            raise ValueError(f"decision_class must be one of {DECISION_CLASSES}")
        row = Approval(
            run_id=run_id,
            gate=gate,
            decision_class=decision_class,
            status="pending",
            requested_payload=requested_payload or {},
            expires_at=expires_at,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def decide_approval(
        self,
        approval_id: uuid.UUID,
        *,
        status: str,
        decided_by: str,
        rationale: str | None = None,
    ) -> Approval | None:
        """Record a human decision. Idempotent: a decided gate is never re-decided."""
        row = await self.session.get(Approval, approval_id)
        if row is None or row.status != "pending":
            return None
        row.status = status
        row.decided_by = decided_by
        row.decided_at = datetime.now(UTC)
        row.rationale = rationale
        await self.session.flush()
        return row

    async def pending_approvals(self, run_id: uuid.UUID) -> list[Approval]:
        stmt = select(Approval).where(Approval.run_id == run_id, Approval.status == "pending")
        return list((await self.session.execute(stmt)).scalars().all())

    # -- decisions ----------------------------------------------------------

    async def record_decision(
        self,
        *,
        title: str,
        decision_text: str,
        decision_class: str = "amber",
        run_id: uuid.UUID | None = None,
        project_id: uuid.UUID | None = None,
        rationale: str | None = None,
        decided_by: str | None = None,
        conditions: list[str] | None = None,
        evidence_refs: list[str] | None = None,
    ) -> Decision:
        row = Decision(
            title=title,
            decision_text=decision_text,
            decision_class=decision_class,
            run_id=run_id,
            project_id=project_id,
            rationale=rationale,
            decided_by=decided_by,
            conditions=conditions or [],
            evidence_refs=evidence_refs or [],
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def project_decisions(self, project_id: uuid.UUID) -> list[Decision]:
        """FR-020: a follow-up run can read prior approved decisions."""
        stmt = (
            select(Decision)
            .where(Decision.project_id == project_id, Decision.superseded_by_id.is_(None))
            .order_by(Decision.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def supersede_decision(self, decision_id: uuid.UUID, superseded_by_id: uuid.UUID) -> None:
        row = await self.session.get(Decision, decision_id)
        if row is not None:
            row.superseded_by_id = superseded_by_id
