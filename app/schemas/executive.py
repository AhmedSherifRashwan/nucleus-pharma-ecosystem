"""Executive orchestrator contracts (PRD s9.2, s9.3, Appendix A) and the
quality gate (s8, s12 of the orchestrator specification)."""

from __future__ import annotations

from pydantic import Field

from app.schemas.common import (
    ConfidenceLevel,
    DecisionClass,
    Severity,
    StrictModel,
)
from app.schemas.planning import PlannedTask


class Contradiction(StrictModel):
    description: str = Field(min_length=1)
    agent_ids: list[str] = Field(default_factory=list)
    severity: Severity = "medium"
    material: bool = True


class UnsupportedClaim(StrictModel):
    claim: str = Field(min_length=1)
    agent_id: str | None = None
    why_unsupported: str = Field(min_length=1)


class ChallengeResult(StrictModel):
    """The complete adversarial review result: submit this whole object (s9.2)."""

    contradictions: list[Contradiction] = Field(default_factory=list)
    unsupported_claims: list[UnsupportedClaim] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    unresolved_assumptions: list[str] = Field(default_factory=list)
    missing_workstreams: list[str] = Field(default_factory=list)
    follow_up_tasks: list[PlannedTask] = Field(default_factory=list)
    requires_human_escalation: bool = False
    escalation_reason: str | None = None


class DecisionRequired(StrictModel):
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list)
    recommendation: str | None = None
    decision_class: DecisionClass = "amber"
    impact: str | None = None


class ExecutiveDecisionBrief(StrictModel):
    """The integrated executive output: the TOP-LEVEL object to submit (s9.3).

    Section order mirrors the report structure in s18.1 so the deterministic
    report builder can render it without an LLM reformatting the deliverable.
    """

    objective: str = Field(min_length=1)
    executive_conclusion: str = Field(min_length=1)
    recommendation: str = Field(min_length=1)
    confidence: ConfidenceLevel
    why: list[str] = Field(default_factory=list)
    key_evidence: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    scientific_technical_implication: str | None = None
    manufacturing_implication: str | None = None
    regulatory_implication: str | None = None
    quality_implication: str | None = None
    commercial_implication: str | None = None
    key_risks: list[str] = Field(default_factory=list)
    # s9.3: synthesis must expose disagreement, never silently rewrite it.
    agent_disagreements: list[str] = Field(default_factory=list)
    critical_assumptions: list[str] = Field(default_factory=list)
    information_required: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    decisions_required_from_user: list[DecisionRequired] = Field(default_factory=list)
    consulted_agents: list[str] = Field(default_factory=list)
    # s2 fail-closed: a run that lost specialist output is never "complete".
    review_required: bool = False
    review_reason: str | None = None

    @property
    def highest_decision_class(self) -> DecisionClass:
        """The strictest gate any required decision demands (s16)."""
        classes = {d.decision_class for d in self.decisions_required_from_user}
        if "red" in classes:
            return "red"
        if "amber" in classes:
            return "amber"
        return "green"


class QualityGateCheck(StrictModel):
    name: str
    passed: bool
    detail: str | None = None


class QualityGateResult(StrictModel):
    """Deterministic completeness review before delivery (s8 QUALITY_GATE)."""

    checks: list[QualityGateCheck] = Field(default_factory=list)
    passed: bool = True
    review_required: bool = False
    reasons: list[str] = Field(default_factory=list)

    @classmethod
    def from_checks(cls, checks: list[QualityGateCheck]) -> QualityGateResult:
        failed = [c for c in checks if not c.passed]
        return cls(
            checks=checks,
            passed=not failed,
            review_required=bool(failed),
            reasons=[f"{c.name}: {c.detail or 'failed'}" for c in failed],
        )
