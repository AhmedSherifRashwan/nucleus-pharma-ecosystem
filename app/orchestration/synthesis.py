"""SYNTHESIZE stage (PRD s9.3) and the deterministic quality gate (s8).

Synthesis integrates without flattening disagreement. The quality gate then
checks the brief against the record, in code, because a fluent model is exactly
what will smooth over a partial run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.registry import AgentRegistry, get_registry
from app.audit.logger import get_logger
from app.orchestration import prompts
from app.orchestration.challenge import summarise_results
from app.providers.openclaw_client import (
    AgentRoute,
    OpenClawClient,
    RouteInfo,
    Usage,
    get_gateway_client,
    session_key_for,
)
from app.schemas.executive import (
    ChallengeResult,
    ExecutiveDecisionBrief,
    QualityGateCheck,
    QualityGateResult,
)
from app.schemas.planning import Plan
from app.schemas.specialist import SpecialistResult

log = get_logger(__name__)

SYNTHESIS_SYSTEM = (
    "You are the Executive Orchestrator for Neucleus producing an Executive "
    "Decision Brief for a pharmaceutical decision-maker. You integrate specialist "
    "work without flattening disagreement or overstating confidence. Your output "
    "supports a human decision and is never itself a regulatory, quality or legal "
    "approval."
)

# Confidence may only be lowered by the platform, never raised.
CONFIDENCE_ORDER = ["speculative", "low", "medium", "high", "very_high"]


@dataclass
class SynthesisOutcome:
    brief: ExecutiveDecisionBrief
    usage: Usage
    route: RouteInfo


def build_execution_status(
    plan: Plan, results: list[SpecialistResult], failed_task_ids: list[str]
) -> dict[str, Any]:
    planned = {t.task_id for t in plan.tasks}
    returned = {r.task_id for r in results}
    by_status: dict[str, list[str]] = {}
    for r in results:
        by_status.setdefault(r.status, []).append(f"{r.task_id}({r.agent_id})")
    return {
        "tasks_planned": len(planned),
        "tasks_returned": len(returned),
        "results_by_status": by_status,
        "failed_task_ids": failed_task_ids,
        "missing_task_ids": sorted(planned - returned - set(failed_task_ids)),
    }


async def synthesize(
    user_request: str,
    plan: Plan,
    results: list[SpecialistResult],
    *,
    challenge: ChallengeResult | None = None,
    failed_task_ids: list[str] | None = None,
    registry: AgentRegistry | None = None,
    client: OpenClawClient | None = None,
    idempotency_key: str | None = None,
) -> SynthesisOutcome:
    registry = registry or get_registry()
    client = client or get_gateway_client()
    failed_task_ids = failed_task_ids or []

    execution_status = build_execution_status(plan, results, failed_task_ids)

    challenge_summary: dict[str, Any] = {"performed": challenge is not None}
    if challenge is not None:
        challenge_summary.update(
            {
                "contradictions": [
                    {
                        "description": c.description,
                        "agents": c.agent_ids,
                        "material": c.material,
                        "severity": c.severity,
                    }
                    for c in challenge.contradictions
                ],
                "unsupported_claims": [
                    {"claim": u.claim, "agent_id": u.agent_id, "why": u.why_unsupported}
                    for u in challenge.unsupported_claims
                ],
                "missing_evidence": challenge.missing_evidence,
                "unresolved_assumptions": challenge.unresolved_assumptions,
                "missing_workstreams": challenge.missing_workstreams,
                "requires_human_escalation": challenge.requires_human_escalation,
                "escalation_reason": challenge.escalation_reason,
            }
        )

    user_prompt = prompts.render(
        prompts.SYNTHESIZE_PROMPT,
        user_request=user_request,
        plan_summary={
            "objective": plan.objective,
            "output_type": plan.output_type,
            "assumptions": plan.assumptions,
            "information_gaps": plan.information_gaps,
        },
        specialist_results=summarise_results(results, registry),
        challenge_summary=challenge_summary,
        execution_status=execution_status,
    )

    brief, usage, route = await client.complete_structured(
        route=AgentRoute.EXECUTIVE,
        system=SYNTHESIS_SYSTEM,
        user=user_prompt,
        schema=ExecutiveDecisionBrief,
        idempotency_key=idempotency_key,
        session_key=session_key_for(idempotency_key),
    )

    brief = enforce_integrity(brief, results, challenge, execution_status)

    log.info(
        "synthesis_complete",
        confidence=brief.confidence,
        review_required=brief.review_required,
        disagreements=len(brief.agent_disagreements),
        decisions=len(brief.decisions_required_from_user),
        decision_class=brief.highest_decision_class,
    )
    return SynthesisOutcome(brief=brief, usage=usage, route=route)


def enforce_integrity(
    brief: ExecutiveDecisionBrief,
    results: list[SpecialistResult],
    challenge: ChallengeResult | None,
    execution_status: dict[str, Any],
) -> ExecutiveDecisionBrief:
    """Correct the brief where its claims conflict with the record (s2 fail closed).

    NFR reliability: a run must never be presented as complete when required work
    failed. Enforced in code because it is exactly what a fluent model smooths over.
    """
    updates: dict[str, Any] = {}

    incomplete = [r for r in results if r.status != "complete"]
    missing = execution_status["missing_task_ids"] + execution_status["failed_task_ids"]
    partial = bool(incomplete or missing or not results)

    if partial and not brief.review_required:
        reasons = []
        if not results:
            reasons.append("no specialist returned a result")
        if incomplete:
            reasons.append(
                "specialist tasks did not complete: "
                + ", ".join(f"{r.task_id}({r.agent_id}, {r.status})" for r in incomplete)
            )
        if missing:
            reasons.append("no result returned for: " + ", ".join(missing))
        updates["review_required"] = True
        updates["review_reason"] = brief.review_reason or "; ".join(reasons)

    # An incomplete run cannot support a high-confidence recommendation.
    if partial and CONFIDENCE_ORDER.index(brief.confidence) > CONFIDENCE_ORDER.index("medium"):
        updates["confidence"] = "medium"

    # An unresolved material conflict must remain visible.
    if challenge is not None:
        material = [c for c in challenge.contradictions if c.material]
        if material and not brief.agent_disagreements:
            updates["agent_disagreements"] = [
                f"{c.description} (agents: {', '.join(c.agent_ids) or 'unspecified'})"
                for c in material
            ]
        if challenge.requires_human_escalation and not brief.review_required:
            updates["review_required"] = True
            updates["review_reason"] = (
                brief.review_reason
                or challenge.escalation_reason
                or "the challenge stage required human escalation"
            )

    consulted = sorted({r.agent_id for r in results})
    if set(brief.consulted_agents) != set(consulted):
        updates["consulted_agents"] = consulted

    return brief.model_copy(update=updates) if updates else brief


def run_quality_gate(
    brief: ExecutiveDecisionBrief,
    plan: Plan,
    results: list[SpecialistResult],
    challenge: ChallengeResult | None,
    *,
    known_evidence_refs: set[str] | None = None,
) -> QualityGateResult:
    """The s8 QUALITY_GATE. Deterministic checks against the record."""
    checks: list[QualityGateCheck] = []

    planned = {t.task_id for t in plan.tasks}
    returned = {r.task_id for r in results}
    checks.append(
        QualityGateCheck(
            name="completeness",
            passed=planned <= returned,
            detail=(
                None
                if planned <= returned
                else f"no result for {', '.join(sorted(planned - returned))}"
            ),
        )
    )

    unsupported = [f for r in results for f in r.unsupported_findings()]
    checks.append(
        QualityGateCheck(
            name="evidence",
            passed=not unsupported,
            detail=(
                None
                if not unsupported
                else f"{len(unsupported)} finding(s) assert fact or inference with no evidence"
            ),
        )
    )

    # Every evidence id cited in the brief must resolve to a real record (s32.3).
    if known_evidence_refs is not None:
        dangling = [
            ref
            for ref in brief.key_evidence
            if ref.startswith("E-") and ref not in known_evidence_refs
        ]
        checks.append(
            QualityGateCheck(
                name="evidence_refs_resolve",
                passed=not dangling,
                detail=None if not dangling else f"unknown evidence ids: {', '.join(dangling)}",
            )
        )

    unresolved = [c for c in challenge.contradictions if c.material] if challenge else []
    disclosed = bool(brief.agent_disagreements)
    checks.append(
        QualityGateCheck(
            name="consistency",
            passed=not unresolved or disclosed,
            detail=(
                None
                if not unresolved or disclosed
                else "material contradictions were not disclosed in the brief"
            ),
        )
    )

    checks.append(
        QualityGateCheck(
            name="assumptions_explicit",
            passed=bool(brief.critical_assumptions)
            or not any(f.basis == "assumption" for r in results for f in r.findings),
            detail="specialists made assumptions but the brief lists none",
        )
    )

    checks.append(
        QualityGateCheck(
            name="uncertainty_disclosed",
            passed=not brief.review_required or bool(brief.review_reason),
            detail="review_required is set but no reason was given",
        )
    )

    checks.append(
        QualityGateCheck(
            name="actionability",
            passed=bool(brief.recommended_actions or brief.decisions_required_from_user),
            detail="the brief offers neither a recommended action nor a decision",
        )
    )

    return QualityGateResult.from_checks(checks)
