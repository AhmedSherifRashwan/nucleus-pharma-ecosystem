"""Phase 0 persistence: the system of record round-trips (PRD s14)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.storage.db import session_scope
from app.storage.models import (
    Approval,
    Artifact,
    Assumption,
    AuditEvent,
    Base,
    Conflict,
    Decision,
    EvidenceItem,
    Project,
    Risk,
    Run,
    RunJob,
)
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def _make_run(**kw) -> uuid.UUID:
    async with session_scope() as session:
        run = Run(
            run_ref=kw.pop("run_ref", f"RUN-{uuid.uuid4().hex[:8]}"),
            user_request=kw.pop("user_request", "Assess the formulation change."),
            **kw,
        )
        session.add(run)
        await session.flush()
        return run.id


async def test_all_prd_tables_exist():
    """s14 names 16 tables; document_chunks supports the retrieval option."""
    expected = {
        "projects",
        "runs",
        "run_jobs",
        "run_tasks",
        "agent_profiles",
        "prompt_versions",
        "specialist_results",
        "evidence_items",
        "artifacts",
        "assumptions",
        "conflicts",
        "risks",
        "approvals",
        "decisions",
        "audit_events",
        "source_documents",
    }
    assert expected <= set(Base.metadata.tables)


async def test_project_and_run_round_trip():
    async with session_scope() as session:
        project = Project(name="Candidate X", objective="Generic softgel")
        session.add(project)
        await session.flush()
        project_id = project.id

    run_id = await _make_run(project_id=project_id, run_ref="RUN-2026-0042")

    async with session_scope() as session:
        run = await session.get(Run, run_id)
        assert run.run_ref == "RUN-2026-0042"
        assert run.status == "queued"
        assert run.project_id == project_id


async def test_run_ref_is_unique():
    await _make_run(run_ref="RUN-DUPLICATE")
    with pytest.raises(IntegrityError):
        await _make_run(run_ref="RUN-DUPLICATE")


async def test_job_idempotency_key_prevents_duplicates():
    """s31.2: a run cannot be enqueued twice for the same key."""
    run_id = await _make_run()
    async with session_scope() as session:
        session.add(RunJob(run_id=run_id, idempotency_key="slack-evt-1"))

    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(RunJob(run_id=run_id, idempotency_key="slack-evt-1"))


async def test_a_run_may_have_several_jobs():
    """s31: orchestrate plus one resume job per human decision."""
    run_id = await _make_run()
    async with session_scope() as session:
        session.add(RunJob(run_id=run_id, job_type="orchestrate", idempotency_key="k1"))
        session.add(RunJob(run_id=run_id, job_type="resume", idempotency_key="k2"))

    async with session_scope() as session:
        jobs = list(
            (await session.execute(select(RunJob).where(RunJob.run_id == run_id))).scalars().all()
        )
    assert {j.job_type for j in jobs} == {"orchestrate", "resume"}


async def test_evidence_reliability_level_persists():
    """s15.1 stores an integer level 1-4, not a free-text tag."""
    run_id = await _make_run()
    async with session_scope() as session:
        session.add(
            EvidenceItem(
                evidence_ref="E-1",
                run_id=run_id,
                source_type="regulatory",
                source_title="FDA PSG",
                reliability_level=1,
                retrieved_at=datetime.now(UTC),
                originating_agent="A02",
            )
        )

    async with session_scope() as session:
        item = (
            await session.execute(select(EvidenceItem).where(EvidenceItem.evidence_ref == "E-1"))
        ).scalar_one()
        assert item.reliability_level == 1


async def test_evidence_ref_is_unique():
    run_id = await _make_run()
    async with session_scope() as session:
        session.add(
            EvidenceItem(
                evidence_ref="E-DUP",
                run_id=run_id,
                source_type="web",
                source_title="t",
                reliability_level=3,
            )
        )
    with pytest.raises(IntegrityError):
        async with session_scope() as session:
            session.add(
                EvidenceItem(
                    evidence_ref="E-DUP",
                    run_id=run_id,
                    source_type="web",
                    source_title="t2",
                    reliability_level=3,
                )
            )


async def test_report_artifacts_are_versioned_not_overwritten():
    """s18.3 / NFR data integrity: never overwrite a prior final report."""
    run_id = await _make_run()
    async with session_scope() as session:
        session.add(
            Artifact(
                artifact_ref="REP-1-v1",
                run_id=run_id,
                kind="report",
                title="Viability report",
                version=1,
                template_version="1.0.0",
            )
        )
        session.add(
            Artifact(
                artifact_ref="REP-1-v2",
                run_id=run_id,
                kind="report",
                title="Viability report",
                version=2,
                template_version="1.0.0",
            )
        )

    async with session_scope() as session:
        reports = list(
            (
                await session.execute(
                    select(Artifact).where(Artifact.run_id == run_id, Artifact.kind == "report")
                )
            )
            .scalars()
            .all()
        )
    assert sorted(r.version for r in reports) == [1, 2]


async def test_governance_records_persist():
    """Assumptions, conflicts, risks, approvals and decisions are first-class."""
    run_id = await _make_run()
    async with session_scope() as session:
        session.add(
            Assumption(run_id=run_id, statement="Gelatin shell assumed.", originating_agent="A07")
        )
        session.add(
            Conflict(
                run_id=run_id,
                description="Shell and fill disagree.",
                agent_ids=["A07", "A08"],
                material=True,
            )
        )
        session.add(
            Risk(run_id=run_id, description="Cross-linking risk.", likelihood="high", impact="high")
        )
        session.add(Approval(run_id=run_id, gate="plan", decision_class="amber"))
        session.add(
            Decision(
                run_id=run_id,
                title="Proceed",
                decision_text="Proceed with mitigations",
                decision_class="amber",
            )
        )

    async with session_scope() as session:
        conflict = (
            await session.execute(select(Conflict).where(Conflict.run_id == run_id))
        ).scalar_one()
        assert conflict.material is True
        assert conflict.state == "open"
        approval = (
            await session.execute(select(Approval).where(Approval.run_id == run_id))
        ).scalar_one()
        assert approval.status == "pending"


async def test_audit_events_record_gateway_route():
    """s20: a run records which route and model produced it."""
    run_id = await _make_run()
    async with session_scope() as session:
        session.add(
            AuditEvent(
                run_id=run_id,
                actor_type="orchestrator",
                event_type="plan.created",
                gateway_route="pharma-executive",
                model_id="unverified-at-phase-0",
                event_metadata={"tasks": 4},
            )
        )

    async with session_scope() as session:
        event = (
            await session.execute(select(AuditEvent).where(AuditEvent.run_id == run_id))
        ).scalar_one()
        assert event.gateway_route == "pharma-executive"
        assert event.event_metadata["tasks"] == 4
