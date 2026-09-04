"""CHALLENGE stage (PRD s9.2).

Adversarial review of aggregated specialist output. Produces targeted follow-ups
rather than restarting the run, and escalates material conflict rather than
resolving it by majority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agents.registry import AgentRegistry, get_registry
from app.audit.logger import get_logger
from app.orchestration import prompts
from app.providers.openclaw_client import (
    AgentRoute,
    OpenClawClient,
    RouteInfo,
    Usage,
    get_gateway_client,
)
from app.schemas.executive import ChallengeResult
from app.schemas.planning import Plan, PlannedTask, RunBudget
from app.schemas.specialist import SpecialistResult

log = get_logger(__name__)

CHALLENGE_SYSTEM = (
    "You are the Executive Orchestrator for Neucleus performing adversarial review. "
    "Your job is to find contradictions, unsupported claims and missing perspectives "
    "before a recommendation reaches a human. Agreement between specialists is not "
    "evidence."
)


@dataclass
class ChallengeOutcome:
    result: ChallengeResult
    usage: Usage
    route: RouteInfo
    dropped_followups: list[str] = field(default_factory=list)


def summarise_results(
    results: list[SpecialistResult], registry: AgentRegistry
) -> list[dict[str, Any]]:
    """Compact specialist output for the executive context window (s22)."""
    out = []
    for r in results:
        name = registry.get(r.agent_id).name if registry.exists(r.agent_id) else r.agent_id
        out.append(
            {
                "task_id": r.task_id,
                "agent_id": r.agent_id,
                "agent_name": name,
                "status": r.status,
                "confidence": r.confidence,
                "executive_summary": r.executive_summary,
                "findings": [
                    {
                        "statement": f.statement,
                        "basis": f.basis,
                        "confidence": f.confidence,
                        "evidence_refs": [e.evidence_id for e in f.evidence_refs],
                    }
                    for f in r.findings
                ],
                "recommendations": r.recommendations,
                "assumptions": r.assumptions,
                "risks": [
                    {"description": k.description, "likelihood": k.likelihood, "impact": k.impact}
                    for k in r.risks
                ],
                "conflicts_or_questions": [
                    {"description": c.description, "with": c.conflicting_with_agent}
                    for c in r.conflicts_or_questions
                ],
                "evidence_refs": [e.evidence_id for e in r.evidence_refs],
                "escalation_required": r.escalation_required,
                "escalation_reason": r.escalation_reason,
                "information_required": r.information_required,
            }
        )
    return out


async def challenge_results(
    user_request: str,
    plan: Plan,
    results: list[SpecialistResult],
    conflict_signals: list[dict[str, Any]],
    *,
    budget: RunBudget,
    registry: AgentRegistry | None = None,
    client: OpenClawClient | None = None,
    idempotency_key: str | None = None,
) -> ChallengeOutcome:
    registry = registry or get_registry()
    client = client or get_gateway_client()
    enabled = registry.enabled_ids()
    max_followups = max(1, budget.max_specialists // 2)

    user_prompt = prompts.render(
        prompts.CHALLENGE_PROMPT,
        user_request=user_request,
        plan_summary={
            "objective": plan.objective,
            "assumptions": plan.assumptions,
            "information_gaps": plan.information_gaps,
            "tasks": [
                {"task_id": t.task_id, "agent_id": t.agent_id, "objective": t.objective}
                for t in plan.tasks
            ],
        },
        specialist_results=summarise_results(results, registry),
        conflict_signals=conflict_signals,
        max_followups=max_followups,
        available_agents=sorted(enabled),
    )

    result, usage, route = await client.complete_structured(
        route=AgentRoute.EXECUTIVE,
        system=CHALLENGE_SYSTEM,
        user=user_prompt,
        schema=ChallengeResult,
        idempotency_key=idempotency_key,
    )

    # Follow-ups are proposals and pass the same policy gate as a plan.
    kept: list[PlannedTask] = []
    dropped: list[str] = []
    existing = {t.task_id for t in plan.tasks}
    for task in result.follow_up_tasks:
        if task.agent_id not in enabled:
            dropped.append(f"{task.task_id}: agent {task.agent_id} is not enabled")
        elif task.task_id in existing:
            dropped.append(f"{task.task_id}: duplicate of an existing task id")
        elif len(kept) >= max_followups:
            dropped.append(f"{task.task_id}: exceeds the limit of {max_followups}")
        else:
            # Follow-ups run in a fresh round; upstream deps are already satisfied.
            kept.append(task.model_copy(update={"depends_on": []}))
            existing.add(task.task_id)

    result = result.model_copy(update={"follow_up_tasks": kept})

    log.info(
        "challenge_complete",
        contradictions=len(result.contradictions),
        material=sum(1 for c in result.contradictions if c.material),
        unsupported=len(result.unsupported_claims),
        followups=len(kept),
        dropped=len(dropped),
        escalation=result.requires_human_escalation,
    )
    return ChallengeOutcome(result=result, usage=usage, route=route, dropped_followups=dropped)
