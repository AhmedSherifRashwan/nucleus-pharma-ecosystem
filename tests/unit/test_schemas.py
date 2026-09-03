"""Typed contract behaviour (PRD s10.2, s15, s16, Appendix A)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas import (
    DECISION_CLASS_RULES,
    RELIABILITY_DESCRIPTIONS,
    ChallengeResult,
    DecisionRequired,
    EvidenceRef,
    ExecutiveDecisionBrief,
    Finding,
    Plan,
    PlannedTask,
    QualityGateCheck,
    QualityGateResult,
    SpecialistResult,
)


def _result(**kw) -> SpecialistResult:
    base = {
        "task_id": "T1",
        "agent_id": "A07",
        "status": "complete",
        "executive_summary": "Shell assessment complete.",
    }
    return SpecialistResult(**{**base, **kw})


# -- evidence enforcement (s15, FR-007) -------------------------------------


def test_fact_without_evidence_is_flagged_unsupported():
    r = _result(findings=[Finding(statement="Bloom strength is 180.", basis="fact")])
    assert len(r.unsupported_findings()) == 1


def test_fact_with_evidence_is_supported():
    r = _result(
        findings=[
            Finding(
                statement="Bloom strength is 180.",
                basis="fact",
                evidence_refs=[EvidenceRef(evidence_id="E-1")],
            )
        ]
    )
    assert r.unsupported_findings() == []


def test_labelled_assumption_needs_no_evidence():
    """s15: a claim cites evidence OR is explicitly labelled an assumption."""
    r = _result(findings=[Finding(statement="Assume gelatin shell.", basis="assumption")])
    assert r.unsupported_findings() == []


def test_estimate_needs_no_evidence_but_inference_does():
    r = _result(
        findings=[
            Finding(statement="Roughly 20% loss.", basis="estimate"),
            Finding(statement="Cross-linking is occurring.", basis="inference"),
        ]
    )
    unsupported = r.unsupported_findings()
    assert len(unsupported) == 1
    assert unsupported[0].basis == "inference"


# -- escalation (s10.2) ------------------------------------------------------


def test_escalation_requires_a_reason():
    with pytest.raises(ValidationError, match="escalation_reason"):
        _result(escalation_required=True)


def test_escalation_with_reason_is_valid():
    r = _result(escalation_required=True, escalation_reason="Compatibility data absent.")
    assert r.escalation_required


def test_unknown_field_is_rejected():
    """Gateway or model drift must fail loudly, not pass silently."""
    with pytest.raises(ValidationError):
        _result(unexpected_field="x")


def test_agent_id_pattern_is_enforced():
    with pytest.raises(ValidationError):
        _result(agent_id="A7")


def test_status_vocabulary_matches_prd():
    for status in ("complete", "needs_information", "blocked", "failed"):
        assert _result(status=status).status == status
    with pytest.raises(ValidationError):
        _result(status="needs_input")


# -- planning (s9.1, s12.2) --------------------------------------------------


def _plan(**kw) -> Plan:
    base = {
        "objective": "Assess the dissolution slow-down.",
        "tasks": [
            PlannedTask(
                task_id="T1",
                agent_id="A10",
                objective="Characterise the stability trend.",
                required_output="Trend assessment.",
            )
        ],
    }
    return Plan(**{**base, **kw})


def test_plan_reports_selected_agents_in_order():
    p = _plan(
        tasks=[
            PlannedTask(
                task_id="T1", agent_id="A10", objective="x" * 12, required_output="assessment"
            ),
            PlannedTask(
                task_id="T2", agent_id="A07", objective="x" * 12, required_output="assessment"
            ),
            PlannedTask(
                task_id="T3", agent_id="A10", objective="x" * 12, required_output="assessment"
            ),
        ]
    )
    assert p.selected_agents == ["A10", "A07"]


def test_duplicate_task_ids_are_rejected():
    with pytest.raises(ValidationError, match="duplicate task ids"):
        _plan(
            tasks=[
                PlannedTask(
                    task_id="T1", agent_id="A10", objective="x" * 12, required_output="assessment"
                ),
                PlannedTask(
                    task_id="T1", agent_id="A07", objective="x" * 12, required_output="assessment"
                ),
            ]
        )


def test_self_dependency_is_rejected():
    with pytest.raises(ValidationError, match="depends on itself"):
        PlannedTask(
            task_id="T1",
            agent_id="A10",
            objective="x" * 12,
            required_output="assessment",
            depends_on=["T1"],
        )


def test_plan_requires_at_least_one_task():
    with pytest.raises(ValidationError):
        _plan(tasks=[])


# -- decision classes (s16) --------------------------------------------------


def _brief(**kw) -> ExecutiveDecisionBrief:
    base = {
        "objective": "o",
        "executive_conclusion": "c",
        "recommendation": "r",
        "confidence": "medium",
    }
    return ExecutiveDecisionBrief(**{**base, **kw})


def test_red_dominates_amber_in_decision_class():
    b = _brief(
        decisions_required_from_user=[
            DecisionRequired(question="q1", decision_class="amber"),
            DecisionRequired(question="q2", decision_class="red"),
        ]
    )
    assert b.highest_decision_class == "red"


def test_no_decisions_means_green():
    assert _brief().highest_decision_class == "green"


def test_amber_alone_is_amber():
    b = _brief(
        decisions_required_from_user=[DecisionRequired(question="q", decision_class="amber")]
    )
    assert b.highest_decision_class == "amber"


def test_all_three_decision_classes_are_documented():
    assert set(DECISION_CLASS_RULES) == {"green", "amber", "red"}


def test_four_reliability_levels_are_documented():
    """s15.1 levels 1-4."""
    assert set(RELIABILITY_DESCRIPTIONS) == {1, 2, 3, 4}


# -- quality gate (s8) -------------------------------------------------------


def test_quality_gate_passes_when_all_checks_pass():
    q = QualityGateResult.from_checks(
        [
            QualityGateCheck(name="evidence", passed=True),
            QualityGateCheck(name="completeness", passed=True),
        ]
    )
    assert q.passed and not q.review_required and q.reasons == []


def test_quality_gate_requires_review_on_any_failure():
    q = QualityGateResult.from_checks(
        [
            QualityGateCheck(name="evidence", passed=True),
            QualityGateCheck(name="conflicts", passed=False, detail="1 unresolved"),
        ]
    )
    assert not q.passed
    assert q.review_required
    assert "conflicts: 1 unresolved" in q.reasons


def test_challenge_result_defaults_to_no_findings():
    c = ChallengeResult()
    assert c.contradictions == []
    assert not c.requires_human_escalation
