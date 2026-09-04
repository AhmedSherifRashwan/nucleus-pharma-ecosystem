from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.storage.db import session_scope
from app.storage.models import Project, Run, RunJob
from app.storage.repositories import JobRepository, RunRepository
from scripts.enqueue_run import enqueue
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


# -- Phase 9: enqueue --------------------------------------------------------


async def test_enqueue_creates_run_project_and_job():
    result = await enqueue(
        request="Assess shell and fill compatibility for Candidate X.",
        channel="C1",
        thread="100.1",
        user="U1",
        event_id="e1",
    )
    assert result["run_ref"].startswith("RUN-")
    assert not result["deduplicated"]

    async with session_scope() as s:
        run = await RunRepository(s).get_by_ref(result["run_ref"])
        job = await JobRepository(s).latest_for_run(run.id)
        project = await s.get(Project, run.project_id)

    assert run.slack_channel_id == "C1"
    assert run.slack_thread_ts == "100.1"
    assert run.slack_user_id == "U1"
    assert job.status == "queued"
    assert job.job_type == "orchestrate"
    assert project is not None


async def test_slack_retry_does_not_create_a_second_run():
    """s31.2: Slack redelivers events; one message must mean one run."""
    first = await enqueue(
        request="Assess the change please.",
        channel="C1",
        thread="100.1",
        user="U1",
        event_id="dup-1",
    )
    second = await enqueue(
        request="Assess the change please.",
        channel="C1",
        thread="100.1",
        user="U1",
        event_id="dup-1",
    )

    assert second["deduplicated"]
    assert second["run_ref"] == first["run_ref"]

    async with session_scope() as s:
        runs = list((await s.execute(select(Run))).scalars().all())
        jobs = list((await s.execute(select(RunJob))).scalars().all())
    assert len(runs) == 1
    assert len(jobs) == 1


async def test_same_thread_reuses_one_project():
    """s17.2: a Slack thread is one project across follow-up requests."""
    a = await enqueue(
        request="First question about the formulation.",
        channel="C9",
        thread="200.1",
        user="U1",
        event_id="t1",
    )
    b = await enqueue(
        request="Follow-up question in the same thread.",
        channel="C9",
        thread="200.1",
        user="U1",
        event_id="t2",
    )

    async with session_scope() as s:
        repo = RunRepository(s)
        run_a = await repo.get_by_ref(a["run_ref"])
        run_b = await repo.get_by_ref(b["run_ref"])
    assert run_a.id != run_b.id  # separate runs, auditable separately
    assert run_a.project_id == run_b.project_id  # shared project memory


@pytest.mark.parametrize("request_text", ["", "  ", "hi"])
async def test_too_short_a_request_is_refused(request_text):
    with pytest.raises(ValueError, match="too short"):
        await enqueue(request=request_text, channel="C1", thread="1", user="U1", event_id=None)


async def test_oversized_request_is_refused():
    with pytest.raises(ValueError, match="exceeds"):
        await enqueue(request="x" * 20_001, channel="C1", thread="1", user="U1", event_id=None)


async def test_run_without_slack_context_still_works():
    """The API path and tests create runs with no channel."""
    result = await enqueue(
        request="A direct request with no Slack context.",
        channel=None,
        thread=None,
        user=None,
        event_id=None,
    )
    async with session_scope() as s:
        run = await RunRepository(s).get_by_ref(result["run_ref"])
    assert run.project_id is None
    assert run.slack_channel_id is None


# -- Phase 10: worker --------------------------------------------------------


class RecordingApp:
    """Stands in for the compiled graph."""

    def __init__(self, fail: bool = False):
        self.invocations: list[tuple[str, Any]] = []
        self.fail = fail

    async def ainvoke(self, state, config=None):
        self.invocations.append(("invoke", state))
        if self.fail:
            raise RuntimeError("simulated graph failure")
        return {"status": "complete"}

    async def aget_state(self, config):
        class S:
            interrupts = ()
            tasks = ()
            next = ()

        return S()


async def test_worker_claims_and_executes_a_job():
    from app.worker import Worker

    result = await enqueue(
        request="Assess the formulation change.",
        channel="C1",
        thread="1.1",
        user="U1",
        event_id="w1",
    )
    app = RecordingApp()
    worker = Worker(worker_id="test-worker")

    assert await worker._process_one(app) is True
    assert len(app.invocations) == 1
    state = app.invocations[0][1]
    assert state["run_ref"] == result["run_ref"]
    assert state["slack_channel"] == "C1"

    async with session_scope() as s:
        run = await RunRepository(s).get_by_ref(result["run_ref"])
        job = await JobRepository(s).latest_for_run(run.id)
    assert job.status == "complete"


async def test_worker_returns_false_on_an_empty_queue():
    from app.worker import Worker

    assert await Worker(worker_id="idle")._process_one(RecordingApp()) is False


async def test_graph_failure_retries_then_fails_the_run():
    from app.worker import Worker

    result = await enqueue(
        request="Assess something that will fail.",
        channel="C1",
        thread="1.1",
        user="U1",
        event_id="w2",
    )
    async with session_scope() as s:
        run = await RunRepository(s).get_by_ref(result["run_ref"])
        job = await JobRepository(s).latest_for_run(run.id)
        job.max_attempts = 1
        run_id = run.id

    worker = Worker(worker_id="failing")
    await worker._process_one(RecordingApp(fail=True))

    async with session_scope() as s:
        run = await s.get(Run, run_id)
        job = await JobRepository(s).latest_for_run(run_id)
    # Attempts exhausted: the run must not sit in "running" forever.
    assert job.status == "failed"
    assert run.status == "failed"
    assert "simulated graph failure" in run.error_state


async def test_a_terminal_run_is_not_re_executed():
    from app.worker import Worker

    result = await enqueue(
        request="Assess an already finished run.",
        channel="C1",
        thread="1.1",
        user="U1",
        event_id="w3",
    )
    async with session_scope() as s:
        run = await RunRepository(s).get_by_ref(result["run_ref"])
        await RunRepository(s).finish(run.id, status="complete", final_summary="done")

    app = RecordingApp()
    await Worker(worker_id="w")._process_one(app)
    assert app.invocations == []  # no re-run, no duplicate spend


async def test_resume_job_uses_the_resume_path():
    from app.worker import Worker

    result = await enqueue(
        request="Assess something needing approval.",
        channel="C1",
        thread="1.1",
        user="U1",
        event_id="w4",
    )
    async with session_scope() as s:
        run = await RunRepository(s).get_by_ref(result["run_ref"])
        await JobRepository(s).complete((await JobRepository(s).latest_for_run(run.id)).id)
        await JobRepository(s).enqueue(
            run.id,
            job_type="resume",
            payload={"resume": {"status": "approved", "decided_by": "U1"}},
        )

    app = RecordingApp()
    assert await Worker(worker_id="w")._process_one(app) is True
    # Resume passes a Command, not an initial state dict.
    assert len(app.invocations) == 1
    assert not isinstance(app.invocations[0][1], dict)
