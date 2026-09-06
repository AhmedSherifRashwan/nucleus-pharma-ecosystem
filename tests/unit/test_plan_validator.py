"""Deterministic plan validation (PRD s8, s12.2, FR-004).

The planner proposes; this decides. Every rule here is enforced in code rather
than by prompt instruction, so a persuasive plan cannot talk its way past policy.
"""

from __future__ import annotations

import pytest

from app.agents.registry import load_registry
from app.orchestration.plan_validator import compute_execution_groups, validate_plan
from app.schemas.planning import Plan, PlannedTask, RunBudget


@pytest.fixture(scope="module")
def registry():
    return load_registry(enable_provisional=False)


@pytest.fixture
def budget() -> RunBudget:
    return RunBudget(max_specialists=6, max_parallel_specialists=4)


def task(tid: str, agent: str, **kw) -> PlannedTask:
    return PlannedTask(
        task_id=tid,
        agent_id=agent,
        objective=kw.pop("objective", f"Assess the matter from the {agent} perspective."),
        required_output=kw.pop("required_output", "A specialist assessment."),
        **kw,
    )


def plan(tasks: list[PlannedTask], **kw) -> Plan:
    return Plan(objective=kw.pop("objective", "Assess the change."), tasks=tasks, **kw)


# -- execution groups --------------------------------------------------------


def test_independent_tasks_form_one_parallel_group():
    groups, errors = compute_execution_groups(
        [task("T1", "A10"), task("T2", "A07"), task("T3", "A08")]
    )
    assert errors == []
    assert groups == [["T1", "T2", "T3"]]


def test_dependencies_create_ordered_groups():
    groups, _ = compute_execution_groups(
        [task("T1", "A10"), task("T2", "A07"), task("T3", "A14", depends_on=["T1", "T2"])]
    )
    assert groups == [["T1", "T2"], ["T3"]]


def test_circular_dependency_is_detected():
    _, errors = compute_execution_groups(
        [task("T1", "A10", depends_on=["T2"]), task("T2", "A07", depends_on=["T1"])]
    )
    assert any("circular dependency" in e for e in errors)


def test_dependency_on_unknown_task_is_detected():
    _, errors = compute_execution_groups([task("T1", "A10", depends_on=["T99"])])
    assert any("unknown task T99" in e for e in errors)


# -- agent eligibility (s11) -------------------------------------------------


def test_valid_plan_passes(registry, budget):
    outcome = validate_plan(plan([task("T1", "A10")]), registry, budget)
    assert outcome.is_valid
    assert outcome.execution_groups == [["T1"]]


def test_unknown_agent_is_rejected(registry, budget):
    outcome = validate_plan(plan([task("T1", "A99")]), registry, budget)
    assert not outcome.is_valid
    assert any("unknown agent" in e for e in outcome.errors)


def test_the_quality_governance_roles_may_now_be_planned(registry, budget):
    """A15/A16 were enabled from the client's own role definitions (D-027)."""
    for agent_id in ("A15", "A16"):
        outcome = validate_plan(plan([task("T1", agent_id)]), registry, budget)
        assert outcome.is_valid, outcome.errors


def test_a_disabled_agent_is_still_rejected(registry, budget, monkeypatch):
    """The gate did not go away; there is simply nothing disabled to catch."""
    disabled = registry.get("A15").model_copy(update={"enabled": False})
    monkeypatch.setitem(registry._profiles, "A15", disabled)
    outcome = validate_plan(plan([task("T1", "A15")]), registry, budget)
    assert not outcome.is_valid
    assert any("A15" in e for e in outcome.errors)


# -- budgets (s12.2) ---------------------------------------------------------


def test_specialist_budget_is_enforced(registry):
    tight = RunBudget(max_specialists=2, max_parallel_specialists=2)
    outcome = validate_plan(
        plan([task("T1", "A10"), task("T2", "A07"), task("T3", "A08")]), registry, tight
    )
    assert not outcome.is_valid
    assert any("budget allows 2" in e for e in outcome.errors)


def test_default_budget_matches_the_prd(registry):
    """s12.2: 6 specialists, 4 parallel, 1 follow-up round."""
    b = RunBudget()
    assert (b.max_specialists, b.max_parallel_specialists, b.max_followup_rounds) == (6, 4, 1)


def test_excess_parallelism_warns_rather_than_rejects(registry):
    narrow = RunBudget(max_specialists=6, max_parallel_specialists=2)
    outcome = validate_plan(
        plan([task("T1", "A10"), task("T2", "A07"), task("T3", "A08")]), registry, narrow
    )
    assert outcome.is_valid
    assert any("concurrency" in w for w in outcome.warnings)


def test_planner_supplied_groups_are_replaced(registry, budget):
    """A planner cannot schedule a dependent task early by asserting groups."""
    p = plan(
        [task("T1", "A10"), task("T2", "A14", depends_on=["T1"])],
        execution_groups=[["T1", "T2"]],
    )
    outcome = validate_plan(p, registry, budget)
    assert outcome.plan.execution_groups == [["T1"], ["T2"]]


def test_task_steps_are_clamped_to_the_profile_limit(registry, budget):
    outcome = validate_plan(plan([task("T1", "A03", max_steps=50)]), registry, budget)
    assert outcome.plan.tasks[0].max_steps == registry.get("A03").max_steps
    assert any("clamping" in w for w in outcome.warnings)


# -- forbidden actions -------------------------------------------------------


def test_forbidden_action_in_an_objective_is_rejected(registry, budget):
    p = plan([task("T1", "A13", objective="Perform autonomous batch release for lot 9.")])
    outcome = validate_plan(p, registry, budget)
    assert not outcome.is_valid
    assert any("forbidden action" in e for e in outcome.errors)


# -- approval policy (s16) ---------------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        "Please approve the batch release for lot 24-001.",
        # Articles and adjectives must not defeat the gate - literal substring
        # matching missed all four of these.
        "Assign a shelf life based on the stability data.",
        "Can we close the major CAPA for this deviation?",
        "Set the expiry date from the 12-month data.",
        "Release the finished batch to the market.",
        "Prepare the submission to the FDA.",
        "Sign off on the validation protocol.",
    ],
)
def test_regulated_language_forces_the_approval_gate(registry, budget, request_text):
    p = plan([task("T1", "A14")], plan_approval_required=False)
    outcome = validate_plan(p, registry, budget, user_request=request_text)
    assert outcome.forced_approval
    assert outcome.plan.plan_approval_required
    assert outcome.plan.approval_rationale


def test_ordinary_request_is_not_gated(registry, budget):
    p = plan([task("T1", "A06")], plan_approval_required=False)
    outcome = validate_plan(p, registry, budget, user_request="Which excipients suit a BCS II API?")
    assert not outcome.forced_approval
    assert not outcome.plan.plan_approval_required


def test_planner_requested_approval_is_preserved(registry, budget):
    p = plan([task("T1", "A10")], plan_approval_required=True, approval_rationale="Broad scope.")
    outcome = validate_plan(p, registry, budget, user_request="Assess something.")
    assert outcome.plan.plan_approval_required
    assert outcome.plan.approval_rationale == "Broad scope."
