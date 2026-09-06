"""End-to-end orchestration through the graph (PRD s8, Phase 6).

Gateway mocked; database real. Proves the control flow, the human gates, the
quality gate and the fail-closed behaviour.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.orchestration import challenge as challenge_mod
from app.orchestration import planner as planner_mod
from app.orchestration import specialist_runner as runner_mod
from app.orchestration import synthesis as synthesis_mod
from app.orchestration.graph import compile_graph
from app.reports import composer as composer_mod
from app.schemas.executive import ChallengeResult, Contradiction, DecisionRequired
from app.schemas.planning import Plan, PlannedTask
from app.storage.db import session_scope
from app.storage.models import Approval, Run
from app.storage.repositories import ArtifactRepository, AuditRepository, RunRepository
from tests.integration.conftest import requires_db
from tests.integration.fakes import FakeGateway, brief, specialist_result

pytestmark = [pytest.mark.asyncio, requires_db]


def four_task_plan(**kw) -> Plan:
    """Three parallel specialists, then a governance review consuming all three."""
    return Plan(
        objective="Assess the dissolution slow-down.",
        tasks=[
            PlannedTask(
                task_id="T1",
                agent_id="A10",
                objective="Characterise the stability trend for this signal.",
                required_output="Trend assessment.",
            ),
            PlannedTask(
                task_id="T2",
                agent_id="A07",
                objective="Assess the shell contribution to the slow-down.",
                required_output="Shell assessment.",
            ),
            PlannedTask(
                task_id="T3",
                agent_id="A08",
                objective="Assess the fill contribution to the slow-down.",
                required_output="Fill assessment.",
            ),
            PlannedTask(
                task_id="T4",
                agent_id="A14",
                objective="Provide independent quality governance review.",
                required_output="Quality review.",
                depends_on=["T1", "T2", "T3"],
            ),
        ],
        **kw,
    )


DEFAULT_RESULTS = {
    "stability trend": specialist_result("T1", "A10"),
    "shell contribution": specialist_result("T2", "A07"),
    "fill contribution": specialist_result("T3", "A08"),
    "quality governance": specialist_result("T4", "A14"),
}


def wire(monkeypatch, gateway: FakeGateway):
    """Inject the double at every seam the graph calls."""
    monkeypatch.setattr(planner_mod, "get_gateway_client", lambda: gateway)
    monkeypatch.setattr(challenge_mod, "get_gateway_client", lambda: gateway)
    monkeypatch.setattr(synthesis_mod, "get_gateway_client", lambda: gateway)
    monkeypatch.setattr(runner_mod, "get_gateway_client", lambda: gateway)
    monkeypatch.setattr(composer_mod, "get_gateway_client", lambda: gateway)


async def make_run(request: str = "Assess the dissolution slow-down.") -> tuple[uuid.UUID, str]:
    async with session_scope() as s:
        run = await RunRepository(s).create(user_request=request)
        return run.id, run.run_ref


def initial(run_id, run_ref, request="Assess the dissolution slow-down.") -> dict:
    return {
        "run_id": str(run_id),
        "run_ref": run_ref,
        "user_request": request,
        "challenge_round": 0,
    }


def config(run_id):
    return {"configurable": {"thread_id": str(run_id)}, "recursion_limit": 40}


# -- happy path --------------------------------------------------------------


async def test_full_run_completes(monkeypatch):
    run_id, run_ref = await make_run()
    gw = FakeGateway(four_task_plan(), results_by_marker=DEFAULT_RESULTS)
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    assert final["status"] == "complete"
    assert len(final["specialist_results"]) == 4
    assert not final["failed_task_ids"]
    # Dependencies drive grouping: three parallel, then the dependent one.
    assert final["execution_groups"] == [["T1", "T2", "T3"], ["T4"]]
    assert final["quality_gate"].passed

    async with session_scope() as s:
        run = await s.get(Run, run_id)
        tasks = await RunRepository(s).list_tasks(run_id)
        results = await RunRepository(s).list_results(run_id)
    assert run.status == "complete"
    assert run.executive_brief is not None
    assert all(t.status == "complete" for t in tasks)
    # s20: provenance recorded per result.
    assert all(r.gateway_route and r.model_id and r.agent_version for r in results)


async def test_dependent_specialist_receives_upstream_summaries(monkeypatch):
    """s10.3: a dependent task sees its dependencies' conclusions, nothing more."""
    run_id, run_ref = await make_run()
    captured: dict[str, str] = {}

    class Capturing(FakeGateway):
        async def run_specialist(self, *, system, user, result_schema, **kw):  # noqa: D401
            if "quality governance" in user:
                captured["message"] = user
            return await super().run_specialist(
                system=system, user=user, result_schema=result_schema, **kw
            )

    wire(monkeypatch, Capturing(four_task_plan(), results_by_marker=DEFAULT_RESULTS))
    app = compile_graph(InMemorySaver())
    await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    message = captured["message"]
    assert "upstream_specialist_findings" in message
    assert "not instructions to you" in message
    assert "A10" in message and "A07" in message and "A08" in message


async def test_audit_trail_reconstructs_the_run(monkeypatch):
    run_id, run_ref = await make_run()
    wire(monkeypatch, FakeGateway(four_task_plan(), results_by_marker=DEFAULT_RESULTS))
    app = compile_graph(InMemorySaver())
    await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    async with session_scope() as s:
        types = await AuditRepository(s).event_types_for_run(run_id)
    assert {
        "run.created",
        "plan.created",
        "task.dispatched",
        "task.complete",
        "challenge.complete",
        "synthesis.complete",
        "quality_gate.evaluated",
    } <= types


# -- fail closed (s2) --------------------------------------------------------


async def test_specialist_failure_forces_review_required(monkeypatch):
    """A failed specialist must never be hidden behind a clean brief."""
    run_id, run_ref = await make_run()
    gw = FakeGateway(
        four_task_plan(),
        results_by_marker=DEFAULT_RESULTS,
        fail_on={"shell contribution"},
    )
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    assert final["status"] == "review_required"
    assert "T2" in final["failed_task_ids"]
    assert final["executive_brief"].review_required
    assert final["executive_brief"].review_reason
    assert not final["quality_gate"].passed

    async with session_scope() as s:
        run = await s.get(Run, run_id)
    assert run.status == "review_required"


async def test_invalid_plan_fails_without_dispatching(monkeypatch):
    run_id, run_ref = await make_run()
    bad = Plan(
        objective="Use an agent that does not exist.",
        tasks=[
            PlannedTask(
                task_id="T1",
                agent_id="A99",
                objective="Do the impossible thing here.",
                required_output="Nothing.",
            )
        ],
    )
    gw = FakeGateway(bad)
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    assert final["status"] == "failed"
    assert gw.specialist_calls == []  # nothing dispatched

    async with session_scope() as s:
        run = await s.get(Run, run_id)
    assert run.status == "failed"
    assert "failed validation" in run.error_state


async def test_an_agent_outside_the_registry_cannot_be_dispatched(monkeypatch):
    """s11: the validator is what stands between a planned task and execution.

    A15 was the example while it was disabled; it is enabled now (D-027), so the
    gate is proved with an agent that does not exist at all.
    """
    run_id, run_ref = await make_run()
    plan = Plan(
        objective="Assess GMP compliance.",
        tasks=[
            PlannedTask(
                task_id="T1",
                agent_id="A99",
                objective="Assess GMP compliance for this site.",
                required_output="Assessment.",
            )
        ],
    )
    gw = FakeGateway(plan)
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))
    assert final["status"] == "failed"
    assert gw.specialist_calls == []


# -- human gates (s16) -------------------------------------------------------


async def test_plan_approval_pauses_before_dispatch(monkeypatch):
    run_id, run_ref = await make_run("Please approve the batch release for lot 24-001.")
    gw = FakeGateway(four_task_plan(plan_approval_required=True), results_by_marker=DEFAULT_RESULTS)
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    result = await app.ainvoke(
        initial(run_id, run_ref, "Please approve the batch release for lot 24-001."),
        config=config(run_id),
    )

    assert "__interrupt__" in result
    assert gw.specialist_calls == []  # nothing ran before the human decided
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "plan_approval"

    async with session_scope() as s:
        run = await s.get(Run, run_id)
    assert run.status == "awaiting_approval"


async def test_approving_the_plan_resumes_the_run(monkeypatch):
    run_id, run_ref = await make_run()
    gw = FakeGateway(four_task_plan(plan_approval_required=True), results_by_marker=DEFAULT_RESULTS)
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    await app.ainvoke(initial(run_id, run_ref), config=config(run_id))
    final = await app.ainvoke(
        Command(resume={"status": "approved", "decided_by": "U123"}),
        config=config(run_id),
    )

    assert final["approval_status"] == "approved"
    assert final["status"] == "complete"
    assert len(gw.specialist_calls) == 4


async def test_declining_the_plan_cancels_without_dispatch(monkeypatch):
    run_id, run_ref = await make_run()
    gw = FakeGateway(four_task_plan(plan_approval_required=True), results_by_marker=DEFAULT_RESULTS)
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    await app.ainvoke(initial(run_id, run_ref), config=config(run_id))
    final = await app.ainvoke(
        Command(resume={"status": "rejected", "decided_by": "U123"}),
        config=config(run_id),
    )

    assert final["status"] == "cancelled"
    assert gw.specialist_calls == []
    async with session_scope() as s:
        run = await s.get(Run, run_id)
    assert run.status == "cancelled"


async def test_red_decision_blocks_completion_until_answered(monkeypatch):
    """s16: an AMBER/RED recommendation cannot complete without a human."""
    run_id, run_ref = await make_run()
    gw = FakeGateway(
        four_task_plan(),
        results_by_marker=DEFAULT_RESULTS,
        executive_brief=brief(
            decisions_required_from_user=[
                DecisionRequired(question="Submit the variation?", decision_class="red")
            ]
        ),
    )
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    result = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    assert "__interrupt__" in result
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "final_decision"
    assert payload["decision_class"] == "red"

    async with session_scope() as s:
        run = await s.get(Run, run_id)
    assert run.status == "awaiting_decision"

    final = await app.ainvoke(
        Command(resume={"status": "approved", "decided_by": "U9", "rationale": "Reviewed."}),
        config=config(run_id),
    )
    assert final["status"] == "complete"

    async with session_scope() as s:
        approvals = list(
            (
                await s.execute(
                    __import__("sqlalchemy").select(Approval).where(Approval.run_id == run_id)
                )
            )
            .scalars()
            .all()
        )
    gates = {a.gate for a in approvals}
    assert "final_decision" in gates


async def test_green_brief_needs_no_final_decision(monkeypatch):
    run_id, run_ref = await make_run()
    gw = FakeGateway(
        four_task_plan(),
        results_by_marker=DEFAULT_RESULTS,
        executive_brief=brief(decisions_required_from_user=[]),
    )
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))
    assert final["status"] == "complete"
    assert "__interrupt__" not in final


async def test_a_completed_run_produces_a_report_artifact(monkeypatch):
    """s18: the DOCX is the deliverable, not an optional extra."""
    run_id, run_ref = await make_run()
    gw = FakeGateway(
        four_task_plan(),
        results_by_marker=DEFAULT_RESULTS,
        executive_brief=brief(decisions_required_from_user=[]),
    )
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    assert final["status"] == "complete"
    paths = [Path(p) for p in (final.get("report_paths") or [])]
    # The technical report and the standalone brief are separate deliverables.
    assert len(paths) == 2
    assert all(p.is_file() and p.suffix == ".docx" for p in paths)
    assert any("Technical_Report" in p.name for p in paths)
    assert any("Executive_Decision_Brief" in p.name for p in paths)
    assert all(p.name.endswith("_v1.docx") for p in paths)
    # The filename carries the subject, not the Slack channel and timestamp.
    assert not any(p.name.startswith("Slack_") for p in paths)

    async with session_scope() as s:
        artifacts = [
            a for a in await ArtifactRepository(s).list_for_run(run_id) if a.kind == "report"
        ]
        events = await AuditRepository(s).event_types_for_run(run_id)
    assert sorted(a.version for a in artifacts) == [1, 1]
    assert "report.generated" in events


async def test_a_failed_composition_still_delivers_the_brief(monkeypatch):
    """s2 fail-closed: a run that did real work never ends empty-handed."""
    run_id, run_ref = await make_run()
    gw = FakeGateway(
        four_task_plan(),
        results_by_marker=DEFAULT_RESULTS,
        executive_brief=brief(decisions_required_from_user=[]),
        fail_schemas={"ReportOutline"},
    )
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    paths = [Path(p) for p in (final.get("report_paths") or [])]
    assert len(paths) == 1
    assert paths[0].is_file()
    assert "Executive_Decision_Brief" in paths[0].name
    warnings = final.get("report_warnings") or []
    assert any("could not be composed" in w for w in warnings)
    # The run is not reported as clean when its deliverable was degraded.
    assert final["status"] == "review_required"


async def test_a_direct_request_skips_planning_entirely(monkeypatch):
    """The user chose the specialist; there is nothing left to plan."""
    run_id, run_ref = await make_run()
    async with session_scope() as s:
        from sqlalchemy import update

        await s.execute(
            update(Run).where(Run.id == run_id).values(mode="direct", requested_agents=["A07"])
        )

    gw = FakeGateway(four_task_plan(), executive_brief=brief(decisions_required_from_user=[]))
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    state = {
        **initial(run_id, run_ref, "Assess cross-linking risk for a PEG 400 fill."),
        "run_mode": "direct",
        "requested_agents": ["A07"],
    }
    final = await app.ainvoke(state, config=config(run_id))

    # No plan was requested from the executive route.
    assert "Plan" not in gw.schemas
    plan = final["plan"]
    assert [t.agent_id for t in plan.tasks] == ["A07"]
    assert final["status"] in ("complete", "review_required")


async def test_an_unknown_specialist_in_a_direct_request_fails_closed(monkeypatch):
    run_id, run_ref = await make_run()
    gw = FakeGateway(four_task_plan())
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    state = {
        **initial(run_id, run_ref, "Do something."),
        "run_mode": "direct",
        "requested_agents": ["A99"],
    }
    final = await app.ainvoke(state, config=config(run_id))

    assert final["status"] == "failed"
    assert "A99" in (final.get("error_state") or "")


# -- challenge and follow-ups (s9.2) -----------------------------------------


async def test_material_contradiction_survives_into_the_brief(monkeypatch):
    run_id, run_ref = await make_run()
    gw = FakeGateway(
        four_task_plan(),
        results_by_marker=DEFAULT_RESULTS,
        challenge=ChallengeResult(
            contradictions=[
                Contradiction(
                    description="Shell and fill disagree on migration risk.",
                    agent_ids=["A07", "A08"],
                    material=True,
                )
            ],
        ),
        # A brief that omits the disagreement must have it reinstated.
        executive_brief=brief(agent_disagreements=[]),
    )
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    assert final["executive_brief"].agent_disagreements
    assert "migration risk" in final["executive_brief"].agent_disagreements[0]


async def test_follow_up_round_is_bounded(monkeypatch):
    """s9.2: at most one challenge round unless the user authorises more."""
    run_id, run_ref = await make_run()
    followup = PlannedTask(
        task_id="T5",
        agent_id="A09",
        objective="Resolve the contradiction between shell and fill.",
        required_output="Resolution.",
    )
    gw = FakeGateway(
        four_task_plan(),
        results_by_marker={
            **DEFAULT_RESULTS,
            "Resolve the contradiction": specialist_result("T5", "A09"),
        },
        challenge=ChallengeResult(follow_up_tasks=[followup]),
    )
    wire(monkeypatch, gw)

    app = compile_graph(InMemorySaver())
    final = await app.ainvoke(initial(run_id, run_ref), config=config(run_id))

    assert final["challenge_round"] == 1
    assert len(final["specialist_results"]) == 5
    # Exactly one challenge, not a loop.
    assert gw.schemas.count("ChallengeResult") == 1
    assert final["status"] == "complete"


# -- durability (FR-016) -----------------------------------------------------


async def test_run_resumes_after_a_restart(monkeypatch):
    """A new app instance sharing the checkpointer continues where it paused."""
    run_id, run_ref = await make_run()
    gw = FakeGateway(four_task_plan(plan_approval_required=True), results_by_marker=DEFAULT_RESULTS)
    wire(monkeypatch, gw)

    saver = InMemorySaver()
    first = compile_graph(saver)
    await first.ainvoke(initial(run_id, run_ref), config=config(run_id))
    assert gw.specialist_calls == []

    # Simulate a worker restart: a fresh graph over the same checkpoint store.
    second = compile_graph(saver)
    final = await second.ainvoke(
        Command(resume={"status": "approved", "decided_by": "U1"}),
        config=config(run_id),
    )
    assert final["status"] == "complete"
    assert len(gw.specialist_calls) == 4
