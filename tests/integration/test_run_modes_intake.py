"""Modes survive intake into the run record (PRD s9.1, s31.2)."""

from __future__ import annotations

import pytest

from app.storage.db import session_scope
from app.storage.repositories import RunRepository
from scripts.enqueue_run import enqueue
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


async def run_for(request: str, **kw):
    result = await enqueue(
        request=request, channel="C1", thread="1.1", user="U1", event_id=None, **kw
    )
    async with session_scope() as s:
        run = await RunRepository(s).get_by_ref(result["run_ref"])
    return result, run


async def test_an_ordinary_request_is_recorded_as_standard():
    result, run = await run_for("Provide a QTPP for Midostaurin 25mg softgels.")
    assert result["mode"] == "standard"
    assert run.mode == "standard"
    assert run.requested_agents == []


async def test_asking_for_all_16_is_recorded_on_the_run():
    result, run = await run_for("Use all 16 agents to assess this shell change.")
    assert result["mode"] == "full"
    assert run.mode == "full"


async def test_a_slash_command_records_the_specialist_and_strips_the_prefix():
    result, run = await run_for("/shell-formulation assess cross-linking for a PEG 400 fill")
    assert result["mode"] == "direct"
    assert result["agent"] == "A07"
    assert run.requested_agents == ["A07"]
    # The specialist receives the task, not the routing instruction.
    assert run.user_request == "assess cross-linking for a PEG 400 fill"


async def test_a_direct_command_with_no_task_is_refused_at_intake():
    with pytest.raises(ValueError, match="needs a task"):
        await run_for("/A07")


async def test_an_explicit_agent_argument_overrides_the_text():
    result, run = await run_for("look at the shell please", agent="A07")
    assert (result["mode"], run.requested_agents) == ("direct", ["A07"])


async def test_the_project_is_named_from_the_subject_not_the_thread():
    from app.storage.repositories import ProjectRepository

    _, run = await run_for("Assess the dissolution slow-down at 6 months.")
    async with session_scope() as s:
        project = await ProjectRepository(s).get(run.project_id)
    assert not project.name.startswith("Slack ")
    assert "dissolution" in project.name.lower()


# -- binding a run to an ingested project ------------------------------------


async def test_a_named_project_binds_the_run_to_its_documents():
    """A fresh thread would otherwise start a project with nothing to retrieve."""
    from app.storage.models import Project

    async with session_scope() as s:
        project = Project(name="Midostaurin Programme Test")
        s.add(project)
        await s.flush()
        project_id = project.id

    result, run = await run_for(
        "Assess the dissolution strategy.", project="Midostaurin Programme Test"
    )
    assert run.project_id == project_id
    assert result["project"] == "Midostaurin Programme Test"


async def test_an_unknown_project_is_refused_rather_than_created():
    """Silently creating it would give the run an empty document set."""
    with pytest.raises(ValueError, match="no project"):
        await run_for("Assess the dissolution strategy.", project="Does Not Exist")


async def test_without_a_project_the_thread_still_gets_its_own():
    _, run = await run_for("Assess the dissolution slow-down at 6 months.")
    assert run.project_id is not None
