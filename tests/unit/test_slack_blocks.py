"""Slack presentation must not lose the caveats (PRD s17, s21)."""

from __future__ import annotations

import json

from app.schemas.executive import DecisionRequired, ExecutiveDecisionBrief
from app.schemas.planning import Plan, PlannedTask
from app.slack.blocks import (
    MAX_SECTION_CHARS,
    acknowledgement,
    decision_request,
    executive_brief,
    failure,
    plan_preview,
)

NAMES = {"A07": "Softgel Shell Formulation Expert", "A10": "Stability Studies Expert"}


def brief(**kw) -> ExecutiveDecisionBrief:
    base = {
        "objective": "Assess the change.",
        "executive_conclusion": "Feasible with mitigations.",
        "recommendation": "Proceed with mitigations.",
        "confidence": "medium",
        "consulted_agents": ["A07", "A10"],
    }
    return ExecutiveDecisionBrief(**{**base, **kw})


def dumps(blocks) -> str:
    return json.dumps(blocks)


def test_brief_always_carries_the_advisory_disclaimer():
    text = dumps(executive_brief("RUN-1", brief(), NAMES))
    assert "not a regulatory approval" in text
    assert "GMP release" in text


def test_review_required_is_prominent():
    text = dumps(
        executive_brief(
            "RUN-1",
            brief(review_required=True, review_reason="A05 failed to return."),
            NAMES,
        )
    )
    assert "REVIEW REQUIRED" in text
    assert "A05 failed to return" in text


def test_disagreement_is_surfaced_not_hidden():
    text = dumps(
        executive_brief(
            "RUN-1",
            brief(agent_disagreements=["Shell and fill disagree on migration risk."]),
            NAMES,
        )
    )
    assert "Unresolved specialist disagreement" in text
    assert "migration risk" in text


def test_assumptions_and_gaps_are_shown():
    text = dumps(
        executive_brief(
            "RUN-1",
            brief(
                critical_assumptions=["RLD data is representative."],
                information_required=["Peroxide value of the PEG 400 lot."],
            ),
            NAMES,
        )
    )
    assert "Critical assumptions" in text and "RLD data" in text
    assert "Information still required" in text and "Peroxide value" in text


def test_decisions_are_classified():
    text = dumps(
        executive_brief(
            "RUN-1",
            brief(
                decisions_required_from_user=[
                    DecisionRequired(question="Approve the variation?", decision_class="red")
                ]
            ),
            NAMES,
        )
    )
    assert "[RED]" in text


def test_specialists_are_named_not_coded():
    text = dumps(executive_brief("RUN-1", brief(), NAMES))
    assert "Softgel Shell Formulation Expert" in text


def test_long_text_stays_within_block_kit_limits():
    blocks = executive_brief("RUN-1", brief(executive_conclusion="x" * 9000), NAMES)
    for block in blocks:
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= MAX_SECTION_CHARS


def test_plan_preview_button_values_carry_only_an_id():
    """s17: a button value must be a compact id, never the whole plan."""
    plan = Plan(
        objective="Assess the dissolution slow-down.",
        tasks=[
            PlannedTask(
                task_id="T1",
                agent_id="A10",
                objective="Characterise the stability trend.",
                required_output="Assessment",
            )
        ],
        approval_rationale="Regulated stability signal.",
    )
    blocks = plan_preview("RUN-1", plan, NAMES, "approval-123")
    actions = [b for b in blocks if b["type"] == "actions"][0]
    for element in actions["elements"]:
        assert element["value"] == "approval-123"
        assert len(element["value"]) < 100
    assert "Regulated stability signal" in dumps(blocks)


def test_plan_preview_without_approval_has_no_buttons():
    plan = Plan(
        objective="Assess something.",
        tasks=[
            PlannedTask(
                task_id="T1",
                agent_id="A10",
                objective="Characterise the trend here.",
                required_output="Assessment",
            )
        ],
    )
    assert not [b for b in plan_preview("RUN-1", plan, NAMES, None) if b["type"] == "actions"]


def test_decision_request_states_the_system_will_not_act():
    text = dumps(
        decision_request(
            "RUN-1",
            brief(
                decisions_required_from_user=[
                    DecisionRequired(question="Submit the variation?", decision_class="red")
                ]
            ),
            "ap-1",
        )
    )
    assert "will not act on it" in text
    assert "A named human must decide" in text
    assert "RED decision required" in text


def test_failure_offers_no_partial_conclusion():
    text = dumps(failure("RUN-1", "The plan failed validation."))
    assert "No partial conclusion" in text


def test_acknowledgement_names_the_run():
    assert "RUN-2026-0042" in dumps(acknowledgement("RUN-2026-0042"))
