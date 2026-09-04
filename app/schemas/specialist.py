"""Specialist input and output contracts (PRD s10.1, s10.2, Appendix A).

Findings keep per-item `basis` tagging (fact / inference / assumption / estimate)
rather than the plain strings in Appendix A. That tagging is what makes the s2
evidence-first principle machine-checkable instead of advisory - see decisions.md
D-003.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import (
    ConfidenceLevel,
    Severity,
    StrictModel,
    TaskStatus,
)

# What kind of statement a finding is. Enforced so synthesis and the report
# builder can separate fact from assumption without re-reading prose.
FindingBasis = Literal["fact", "inference", "assumption", "estimate"]


class EvidenceRef(StrictModel):
    """A reference from a claim to a persisted EvidenceItem (Appendix A)."""

    evidence_id: str = Field(min_length=1, max_length=64)
    claim: str | None = None


class Finding(StrictModel):
    statement: str = Field(min_length=1)
    basis: FindingBasis = "inference"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    confidence: ConfidenceLevel = "medium"
    detail: str | None = None

    @property
    def requires_evidence(self) -> bool:
        """s15: a stated fact needs provenance; an assumption needs labelling."""
        return self.basis in ("fact", "inference")


class RiskItem(StrictModel):
    description: str = Field(min_length=1)
    likelihood: Severity = "medium"
    impact: Severity = "medium"
    mitigation: str | None = None
    owner: str | None = None


class ConflictOrQuestion(StrictModel):
    description: str = Field(min_length=1)
    conflicting_with_agent: str | None = None
    severity: Severity = "medium"
    resolution_suggestion: str | None = None


class TaskEnvelope(StrictModel):
    """What a specialist receives. Deliberately narrow (s10.1, s10.3)."""

    run_id: str
    task_id: str
    agent_id: str = Field(pattern=r"^A\d{2}$")
    objective: str = Field(min_length=10)
    required_output: str = Field(min_length=3)
    project_context_summary: str | None = None
    upstream_artifact_refs: list[str] = Field(default_factory=list)
    upstream_findings: list[dict] = Field(default_factory=list)
    source_document_refs: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    knowledge_scopes: list[str] = Field(default_factory=list)
    restrictions: list[str] = Field(default_factory=list)
    escalation_rules: list[str] = Field(default_factory=list)
    # RUN-REF:TASK-ID, quoted back on every tool call and verified server-side.
    tool_context: str | None = None
    output_schema_version: str = "1.0.0"
    max_steps: int = Field(default=12, ge=1, le=50)


class SpecialistResult(StrictModel):
    """The one output contract every specialist profile returns (s10.2).

    This is the TOP-LEVEL object a specialist submits. Every material finding,
    evidence reference, assumption, risk, conflict and escalation belongs here;
    nothing outside it is seen by the orchestrator.
    """

    task_id: str
    agent_id: str = Field(pattern=r"^A\d{2}$")
    status: TaskStatus
    executive_summary: str = Field(min_length=1)
    findings: list[Finding] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    conflicts_or_questions: list[ConflictOrQuestion] = Field(default_factory=list)
    confidence: ConfidenceLevel = "medium"
    artifacts: list[str] = Field(default_factory=list)
    escalation_required: bool = False
    escalation_reason: str | None = None
    information_required: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _reason_required_when_escalating(self) -> SpecialistResult:
        if self.escalation_required and not self.escalation_reason:
            raise ValueError("escalation_reason is required when escalation_required")
        return self

    def unsupported_findings(self) -> list[Finding]:
        """Findings asserting fact or inference with no evidence reference.

        s15 / FR-007: a material finding must cite evidence or be labelled an
        assumption. This surfaces the violations for the quality gate.
        """
        return [f for f in self.findings if f.requires_evidence and not f.evidence_refs]
