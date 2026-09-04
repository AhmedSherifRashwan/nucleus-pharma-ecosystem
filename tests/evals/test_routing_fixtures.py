"""Routing evaluation harness (PRD s34.3, s34.4).

Validates the fixture set and the deterministic policy that applies to any plan.
Scoring a live planner needs gateway access and is gated behind RUN_LIVE_EVALS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.agents.registry import load_registry
from app.orchestration.plan_validator import validate_plan
from app.schemas.planning import Plan, PlannedTask, RunBudget

CASES = json.loads((Path(__file__).parent.parent / "fixtures" / "routing_cases.json").read_text())[
    "cases"
]
IDS = [c["id"] for c in CASES]


@pytest.fixture(scope="module")
def registry():
    return load_registry(enable_provisional=False)


def _plan_for(case: dict) -> Plan:
    return Plan(
        objective=case["request"][:200],
        tasks=[
            PlannedTask(
                task_id=f"T{i}",
                agent_id=agent,
                objective=f"Address the request from the {agent} perspective.",
                required_output="A specialist assessment.",
            )
            for i, agent in enumerate(case["must_include"], 1)
        ],
    )


def test_fixture_set_is_well_formed(registry):
    seen = set()
    for case in CASES:
        assert case["id"] not in seen, f"duplicate fixture {case['id']}"
        seen.add(case["id"])
        assert case["request"].strip()
        assert case["must_include"], f"{case['id']} requires no agents"
        for key in ("must_include", "may_include", "should_not_include"):
            for agent in case[key]:
                assert registry.exists(agent), f"{case['id']}: unknown agent {agent}"
        overlap = set(case["must_include"]) & set(case["should_not_include"])
        assert not overlap, f"{case['id']} contradicts itself on {overlap}"


def test_fixtures_only_require_enabled_agents(registry):
    enabled = registry.enabled_ids()
    for case in CASES:
        missing = set(case["must_include"]) - enabled
        assert not missing, f"{case['id']} requires disabled agents {missing}"


@pytest.mark.parametrize("case", CASES, ids=IDS)
def test_a_correct_plan_fits_the_default_budget(case, registry):
    outcome = validate_plan(_plan_for(case), registry, RunBudget(), user_request=case["request"])
    assert outcome.is_valid, f"{case['id']}: {outcome.errors}"


@pytest.mark.parametrize(
    "case", [c for c in CASES if c["requires_plan_approval"]], ids=lambda c: c["id"]
)
def test_regulated_fixtures_force_approval(case, registry):
    """Policy forces the gate; it is never left to the planner's judgement."""
    p = _plan_for(case).model_copy(update={"plan_approval_required": False})
    outcome = validate_plan(p, registry, RunBudget(), user_request=case["request"])
    assert outcome.plan.plan_approval_required, f"{case['id']} should have been gated"


@pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_EVALS"),
    reason="live planner evaluation needs the gateway (RUN_LIVE_EVALS=1)",
)
@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_live_planner_routing(case, registry):
    from app.orchestration.planner import create_plan

    outcome = await create_plan(
        case["request"],
        budget=RunBudget(),
        registry=registry,
        idempotency_key=f"eval-{case['id']}",
    )
    selected = set(outcome.plan.selected_agents)

    missing = set(case["must_include"]) - selected
    assert not missing, f"{case['id']}: planner omitted {missing}"

    forbidden = set(case["should_not_include"]) & selected
    assert not forbidden, f"{case['id']}: planner over-selected {forbidden}"

    assert outcome.is_valid, outcome.validation.errors
    if case["requires_plan_approval"]:
        assert outcome.plan.plan_approval_required
