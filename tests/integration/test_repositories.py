"""Repository behaviour and idempotency (PRD Phase 2, s14, s16, s31.2)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.schemas.planning import Plan, PlannedTask
from app.schemas.specialist import Finding, SpecialistResult
from app.storage.db import session_scope
from app.storage.models import RunJob
from app.storage.repositories import (
    ArtifactRepository,
    AuditRepository,
    EventType,
    EvidenceRepository,
    GovernanceRepository,
    JobRepository,
    ProjectRepository,
    RunRepository,
)
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def _run(**kw) -> uuid.UUID:
    async with session_scope() as s:
        run = await RunRepository(s).create(
            user_request=kw.pop("user_request", "Assess the change."), **kw
        )
        return run.id


# -- projects and thread mapping (s17.2) -------------------------------------


async def test_slack_thread_maps_to_one_project():
    async with session_scope() as s:
        p1, created1 = await ProjectRepository(s).get_or_create_for_thread(
            slack_channel_id="C1", thread_ts="111.1", name="Candidate X"
        )
        assert created1
        first_id = p1.id

    async with session_scope() as s:
        p2, created2 = await ProjectRepository(s).get_or_create_for_thread(
            slack_channel_id="C1", thread_ts="111.1", name="ignored"
        )
    assert not created2
    assert p2.id == first_id


async def test_different_threads_are_different_projects():
    async with session_scope() as s:
        repo = ProjectRepository(s)
        a, _ = await repo.get_or_create_for_thread(slack_channel_id="C1", thread_ts="1.1", name="A")
        b, _ = await repo.get_or_create_for_thread(slack_channel_id="C1", thread_ts="2.2", name="B")
        assert a.id != b.id


# -- run identity and idempotency (FR-002, s31.2) ----------------------------


async def test_run_refs_increment():
    async with session_scope() as s:
        repo = RunRepository(s)
        first = await repo.create(user_request="one")
        second = await repo.create(user_request="two")
    assert first.run_ref.startswith("RUN-")
    assert first.run_ref != second.run_ref


async def test_idempotency_key_returns_the_same_run():
    key = "slack-evt-abc"
    run_id = await _run(idempotency_key=key)
    async with session_scope() as s:
        found = await RunRepository(s).get_by_idempotency_key(key)
    assert found is not None and found.id == run_id


async def test_job_idempotency_key_is_enforced():
    run_id = await _run()
    async with session_scope() as s:
        await JobRepository(s).enqueue(run_id, idempotency_key="evt-1")
    async with session_scope() as s:
        existing = await JobRepository(s).get_by_idempotency_key("evt-1")
    assert existing is not None


# -- job queue (s31) ---------------------------------------------------------


async def test_claim_transitions_and_completes():
    run_id = await _run()
    async with session_scope() as s:
        job = await JobRepository(s).enqueue(run_id)
        job_id = job.id

    async with session_scope() as s:
        claimed = await JobRepository(s).claim_next("w1")
        assert claimed is not None
        assert claimed.status == "running"
        assert claimed.attempts == 1
        assert claimed.locked_by == "w1"

    async with session_scope() as s:
        await JobRepository(s).complete(job_id)
    async with session_scope() as s:
        job = await s.get(RunJob, job_id)
    assert job.status == "complete"
    assert job.locked_by is None


async def test_two_workers_never_claim_the_same_job():
    a, b = await _run(user_request="a"), await _run(user_request="b")
    async with session_scope() as s:
        repo = JobRepository(s)
        await repo.enqueue(a)
        await repo.enqueue(b)

    async def claim(worker):
        async with session_scope() as s:
            job = await JobRepository(s).claim_next(worker)
            return job.id if job else None

    first, second = await asyncio.gather(claim("w1"), claim("w2"))
    assert first and second and first != second


async def test_failure_backs_off_then_fails_when_exhausted():
    run_id = await _run()
    async with session_scope() as s:
        job = await JobRepository(s).enqueue(run_id, max_attempts=2)
        job_id = job.id

    async with session_scope() as s:
        await JobRepository(s).claim_next("w1")
    async with session_scope() as s:
        assert await JobRepository(s).fail(job_id, "transient") == "queued"

    # Backed off, so not yet claimable.
    async with session_scope() as s:
        assert await JobRepository(s).claim_next("w2") is None

    async with session_scope() as s:
        await s.execute(
            update(RunJob).where(RunJob.id == job_id).values(available_at=datetime.now(UTC))
        )
    async with session_scope() as s:
        await JobRepository(s).claim_next("w2")
    async with session_scope() as s:
        assert await JobRepository(s).fail(job_id, "persistent") == "failed"


async def test_stale_job_from_a_crashed_worker_is_reclaimed():
    run_id = await _run()
    async with session_scope() as s:
        job = await JobRepository(s).enqueue(run_id)
        job_id = job.id
    async with session_scope() as s:
        await JobRepository(s).claim_next("crashed")
    async with session_scope() as s:
        await s.execute(
            update(RunJob)
            .where(RunJob.id == job_id)
            .values(locked_at=datetime.now(UTC) - timedelta(hours=2))
        )
    async with session_scope() as s:
        assert await JobRepository(s).reclaim_stale() >= 1
    async with session_scope() as s:
        job = await s.get(RunJob, job_id)
    assert job.status == "queued" and job.locked_by is None


# -- tasks and results -------------------------------------------------------


def _plan() -> Plan:
    return Plan(
        objective="Assess the dissolution slow-down.",
        tasks=[
            PlannedTask(
                task_id="T1",
                agent_id="A10",
                objective="Characterise the trend.",
                required_output="Assessment",
            ),
            PlannedTask(
                task_id="T2",
                agent_id="A14",
                objective="Provide quality governance review.",
                required_output="Review",
                depends_on=["T1"],
            ),
        ],
    )


async def test_tasks_persist_with_execution_groups():
    run_id = await _run()
    async with session_scope() as s:
        await RunRepository(s).create_tasks(run_id, _plan(), [["T1"], ["T2"]])
    async with session_scope() as s:
        tasks = await RunRepository(s).list_tasks(run_id)
    by_id = {t.planner_task_id: t for t in tasks}
    assert by_id["T1"].execution_group == 0
    assert by_id["T2"].execution_group == 1
    assert by_id["T2"].depends_on == ["T1"]


async def test_specialist_result_records_route_provenance():
    """s20: the run record shows which route and model produced each result."""
    run_id = await _run()
    result = SpecialistResult(
        task_id="T1",
        agent_id="A10",
        status="complete",
        executive_summary="Trend characterised.",
        findings=[Finding(statement="Slowing observed.", basis="assumption")],
    )
    async with session_scope() as s:
        await RunRepository(s).save_result(
            run_id=run_id,
            task_row_id=None,
            result=result,
            agent_version="1.0.0",
            gateway_route="pharma-specialist",
            model_id="claude-sonnet-5",
            duration_ms=4200,
        )
    async with session_scope() as s:
        rows = await RunRepository(s).list_results(run_id)
    assert rows[0].gateway_route == "pharma-specialist"
    assert rows[0].model_id == "claude-sonnet-5"
    assert rows[0].agent_version == "1.0.0"


async def test_followup_tasks_land_in_a_later_group():
    run_id = await _run()
    async with session_scope() as s:
        repo = RunRepository(s)
        await repo.create_tasks(run_id, _plan(), [["T1"], ["T2"]])
        await repo.add_followup_tasks(
            run_id,
            [
                PlannedTask(
                    task_id="T3",
                    agent_id="A09",
                    objective="Resolve the contradiction.",
                    required_output="Answer",
                )
            ],
            execution_group=2,
        )
    async with session_scope() as s:
        tasks = await RunRepository(s).list_tasks(run_id)
    followups = [t for t in tasks if t.is_followup]
    assert len(followups) == 1 and followups[0].execution_group == 2


# -- evidence (s15) ----------------------------------------------------------


async def test_evidence_requires_a_valid_reliability_level():
    async with session_scope() as s:
        with pytest.raises(ValueError, match="reliability_level"):
            await EvidenceRepository(s).create(
                source_type="web", source_title="Blog", reliability_level=7
            )


async def test_evidence_refs_can_be_verified():
    """s32.3: report QA must confirm every cited ID resolves."""
    run_id = await _run()
    async with session_scope() as s:
        repo = EvidenceRepository(s)
        a = await repo.create(
            source_type="regulatory",
            source_title="FDA PSG",
            reliability_level=1,
            run_id=run_id,
            excerpt_or_locator="Dissolution.",
        )
        ref = a.evidence_ref
    async with session_scope() as s:
        found = await EvidenceRepository(s).refs_exist([ref, "E-does-not-exist"])
    assert found == {ref}


# -- artifacts and report versioning (s18.3) ---------------------------------


async def test_reports_version_rather_than_overwrite():
    run_id = await _run()
    async with session_scope() as s:
        repo = ArtifactRepository(s)
        v1 = await repo.next_report_version(run_id)
        await repo.create(
            title="Viability",
            kind="report",
            run_id=run_id,
            version=v1,
            artifact_ref=f"REP-{run_id.hex[:6]}-v{v1}",
        )
    async with session_scope() as s:
        repo = ArtifactRepository(s)
        v2 = await repo.next_report_version(run_id)
        await repo.create(
            title="Viability",
            kind="report",
            run_id=run_id,
            version=v2,
            artifact_ref=f"REP-{run_id.hex[:6]}-v{v2}",
        )
    assert (v1, v2) == (1, 2)


# -- governance (s16) --------------------------------------------------------


async def test_approval_is_decided_once_only():
    run_id = await _run()
    async with session_scope() as s:
        approval = await GovernanceRepository(s).request_approval(
            run_id=run_id, gate="plan", decision_class="amber"
        )
        approval_id = approval.id

    async with session_scope() as s:
        first = await GovernanceRepository(s).decide_approval(
            approval_id, status="approved", decided_by="U123"
        )
        assert first is not None

    async with session_scope() as s:
        second = await GovernanceRepository(s).decide_approval(
            approval_id, status="rejected", decided_by="attacker"
        )
    assert second is None  # a decided gate cannot be re-decided


async def test_approval_rejects_an_invalid_decision_class():
    run_id = await _run()
    async with session_scope() as s:
        with pytest.raises(ValueError, match="decision_class"):
            await GovernanceRepository(s).request_approval(
                run_id=run_id, gate="plan", decision_class="purple"
            )


async def test_open_material_conflicts_are_surfaced():
    run_id = await _run()
    async with session_scope() as s:
        repo = GovernanceRepository(s)
        await repo.add_conflict(run_id=run_id, description="Material.", material=True)
        await repo.add_conflict(run_id=run_id, description="Cosmetic.", material=False)
    async with session_scope() as s:
        open_material = await GovernanceRepository(s).open_material_conflicts(run_id)
    assert len(open_material) == 1
    assert open_material[0].description == "Material."


async def test_resolved_conflict_no_longer_blocks():
    run_id = await _run()
    async with session_scope() as s:
        repo = GovernanceRepository(s)
        c = await repo.add_conflict(run_id=run_id, description="Material.")
        cid = c.id
    async with session_scope() as s:
        await GovernanceRepository(s).resolve_conflict(cid, resolution="Agreed after challenge.")
    async with session_scope() as s:
        assert await GovernanceRepository(s).open_material_conflicts(run_id) == []


async def test_project_decisions_exclude_superseded(monkeypatch):
    """FR-020: a follow-up run reads current decisions, not withdrawn ones."""
    async with session_scope() as s:
        project = await ProjectRepository(s).create(name="P")
        pid = project.id
    async with session_scope() as s:
        repo = GovernanceRepository(s)
        old = await repo.record_decision(title="Old", decision_text="Use gelatin.", project_id=pid)
        new = await repo.record_decision(title="New", decision_text="Use HPMC.", project_id=pid)
        await repo.supersede_decision(old.id, new.id)
    async with session_scope() as s:
        current = await GovernanceRepository(s).project_decisions(pid)
    assert [d.title for d in current] == ["New"]


# -- audit (FR-019) ----------------------------------------------------------


async def test_audit_trail_reconstructs_a_run():
    run_id = await _run()
    async with session_scope() as s:
        repo = AuditRepository(s)
        await repo.record(event_type=EventType.RUN_CREATED, actor_type="user", run_id=run_id)
        await repo.record(
            event_type=EventType.PLAN_CREATED,
            actor_type="orchestrator",
            run_id=run_id,
            gateway_route="pharma-executive",
            model_id="gpt-5.4",
            metadata={"tasks": 2},
        )
        await repo.record(event_type=EventType.RUN_COMPLETE, actor_type="system", run_id=run_id)

    async with session_scope() as s:
        events = await AuditRepository(s).for_run(run_id)
        types = await AuditRepository(s).event_types_for_run(run_id)

    assert types == {"run.created", "plan.created", "run.complete"}
    plan_event = next(e for e in events if e.event_type == "plan.created")
    assert plan_event.gateway_route == "pharma-executive"
    assert plan_event.event_metadata["tasks"] == 2
