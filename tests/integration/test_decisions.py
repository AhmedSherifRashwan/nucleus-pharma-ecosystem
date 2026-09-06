"""Approval decisions from Slack (PRD s16, FR-011)."""

from __future__ import annotations

import uuid

import pytest

from app.storage.db import session_scope
from app.storage.models import RunJob
from app.storage.repositories import GovernanceRepository, RunRepository
from scripts.record_decision import DecisionRefused, record
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

OWNER = "U-OWNER"
STRANGER = "U-STRANGER"


@pytest.fixture
def approvers(monkeypatch):
    """Set SLACK_APPROVER_IDS for one test, cache included."""
    from app.config import get_settings

    def configure(value: str) -> None:
        monkeypatch.setenv("SLACK_APPROVER_IDS", value)
        get_settings.cache_clear()

    configure("")
    yield configure
    get_settings.cache_clear()


async def _gate(gate: str = "plan", *, owner: str | None = OWNER) -> tuple[uuid.UUID, str, str]:
    async with session_scope() as s:
        run = await RunRepository(s).create(
            user_request="Assess the shell change.", slack_user_id=owner
        )
        await RunRepository(s).set_stage(run.id, "awaiting_approval", status="awaiting_approval")
        approval, _ = await GovernanceRepository(s).request_approval(
            run_id=run.id, gate=gate, decision_class="amber"
        )
        return run.id, run.run_ref, str(approval.id)


async def _jobs(run_id: uuid.UUID) -> list[RunJob]:
    async with session_scope() as s:
        return [j for j in await _all_jobs(s) if j.run_id == run_id]


async def _all_jobs(session):
    from sqlalchemy import select

    return list((await session.execute(select(RunJob))).scalars().all())


async def test_the_run_owner_may_approve_their_own_plan(approvers):
    run_id, run_ref, approval_id = await _gate()

    result = await record(approval_id=approval_id, action="approve_plan", user=OWNER)

    assert result["resumed"] is True
    assert result["run_ref"] == run_ref
    jobs = await _jobs(run_id)
    assert [j.job_type for j in jobs] == ["resume"]
    assert jobs[0].payload["resume"] == {"status": "approved", "decided_by": OWNER, "note": None}


async def test_a_stranger_is_refused(approvers):
    run_id, _, approval_id = await _gate()

    with pytest.raises(DecisionRefused, match="not authorised"):
        await record(approval_id=approval_id, action="approve_plan", user=STRANGER)

    assert await _jobs(run_id) == []


async def test_a_configured_approver_may_decide_any_run(approvers):
    approvers(f"{STRANGER},U-OTHER")
    _, _, approval_id = await _gate()
    result = await record(approval_id=approval_id, action="cancel_plan", user=STRANGER)
    assert result["status"] == "rejected"


async def test_a_plan_button_cannot_decide_the_final_gate():
    """A stale message must not answer a different question."""
    _, _, approval_id = await _gate(gate="final_decision")

    with pytest.raises(DecisionRefused, match="plan gate"):
        await record(approval_id=approval_id, action="approve_plan", user=OWNER)


async def test_a_double_click_does_not_enqueue_twice(approvers):
    run_id, _, approval_id = await _gate()

    first = await record(approval_id=approval_id, action="approve_plan", user=OWNER)
    second = await record(approval_id=approval_id, action="approve_plan", user=OWNER)

    assert first["resumed"] is True
    assert second["resumed"] is False and second["already_decided"] is True
    assert len(await _jobs(run_id)) == 1


async def test_a_finished_run_cannot_be_decided():
    run_id, _, approval_id = await _gate()
    async with session_scope() as s:
        await RunRepository(s).finish(run_id, status="complete", final_summary="done")

    with pytest.raises(DecisionRefused, match="already finished"):
        await record(approval_id=approval_id, action="approve_plan", user=OWNER)


async def test_an_unknown_action_is_refused():
    _, _, approval_id = await _gate()
    with pytest.raises(DecisionRefused, match="unknown action"):
        await record(approval_id=approval_id, action="delete_everything", user=OWNER)


async def test_a_missing_approval_is_refused():
    with pytest.raises(DecisionRefused, match="no longer exists"):
        await record(approval_id=str(uuid.uuid4()), action="approve_plan", user=OWNER)


async def test_a_decision_needs_a_named_person(approvers):
    _, _, approval_id = await _gate()
    with pytest.raises(DecisionRefused, match="no user id"):
        await record(approval_id=approval_id, action="approve_plan", user=None)


async def test_a_gate_raised_twice_reuses_the_open_approval():
    """A resumed node re-runs its body; the user's button must stay valid."""
    run_id, _, approval_id = await _gate()

    async with session_scope() as s:
        again, is_new = await GovernanceRepository(s).request_approval(
            run_id=run_id, gate="plan", decision_class="amber"
        )

    assert str(again.id) == approval_id
    assert is_new is False


async def test_a_decided_gate_can_be_raised_again(approvers):
    """A later gate of the same kind is a genuinely new question."""
    run_id, _, approval_id = await _gate()
    await record(approval_id=approval_id, action="approve_plan", user=OWNER)
    async with session_scope() as s:
        await GovernanceRepository(s).decide_approval(
            uuid.UUID(approval_id), status="approved", decided_by=OWNER
        )
        fresh, is_new = await GovernanceRepository(s).request_approval(
            run_id=run_id, gate="plan", decision_class="amber"
        )

    assert str(fresh.id) != approval_id
    assert is_new is True


# -- runs started outside Slack ----------------------------------------------


async def _ownerless_gate() -> tuple[uuid.UUID, str]:
    """A run from the Control UI or a terminal has no Slack owner."""
    async with session_scope() as s:
        run = await RunRepository(s).create(user_request="Assess the shell change.")
        await RunRepository(s).set_stage(run.id, "awaiting_approval", status="awaiting_approval")
        approval, _ = await GovernanceRepository(s).request_approval(
            run_id=run.id, gate="plan", decision_class="amber"
        )
        return run.id, str(approval.id)


async def test_the_operator_may_decide_a_run_that_has_no_owner(approvers):
    run_id, approval_id = await _ownerless_gate()

    result = await record(approval_id=approval_id, action="approve_plan", user="operator")

    assert result["resumed"] is True
    jobs = await _jobs(run_id)
    assert jobs[0].payload["resume"]["decided_by"] == "operator"


async def test_the_operator_identity_does_not_reach_someone_elses_run(approvers):
    """A Slack run belongs to the person who started it, not to the terminal."""
    _, _, approval_id = await _gate()

    with pytest.raises(DecisionRefused, match="not authorised"):
        await record(approval_id=approval_id, action="approve_plan", user="operator")


async def test_a_stranger_is_still_refused_on_an_ownerless_run(approvers):
    _, approval_id = await _ownerless_gate()

    with pytest.raises(DecisionRefused, match="started outside Slack"):
        await record(approval_id=approval_id, action="approve_plan", user=STRANGER)
