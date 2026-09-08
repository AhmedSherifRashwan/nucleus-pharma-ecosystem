"""PLAN stage (PRD s9.1).

The executive route proposes a typed task graph. `plan_validator` then decides
whether it may execute - the LLM never has the final say on policy.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.agents.registry import AgentRegistry, get_registry
from app.audit.logger import get_logger
from app.orchestration import prompts
from app.orchestration.plan_validator import ValidationOutcome, validate_plan
from app.providers.openclaw_client import (
    AgentRoute,
    OpenClawClient,
    RouteInfo,
    Usage,
    get_gateway_client,
    session_key_for,
)
from app.schemas.planning import Plan, RunBudget

log = get_logger(__name__)

PLANNER_SYSTEM = (
    "You are the Executive Orchestrator for Nucleus, a control plane for a "
    "specialist AI ecosystem in generic oral softgel pharmaceutical development. "
    "You plan and coordinate; you never perform the specialist analysis yourself."
)


def full_ecosystem_instruction(registry: AgentRegistry) -> str:
    """The user asked for every specialist, so say so plainly (s9.1).

    The planner still decides objectives and dependencies - that is the work. What
    it may not do here is narrow the team: the user has already made that call and
    is paying for it in time.
    """
    ids = sorted(p.id for p in registry.enabled_profiles())
    return (
        "## FULL ECOSYSTEM MODE — the user asked for every specialist\n\n"
        "Give each of these agents a task, and give each one an objective that is "
        "genuinely worth its time on this request:\n"
        + "\n".join(f"- {aid}" for aid in ids)
        + "\n\nDo not narrow the team. Where an agent has no obvious angle, set it "
        "a task that tests the request from its own domain - a specialist that finds "
        "nothing material is a useful answer, and it says so in its own result. "
        "Order the work by dependency as usual, and keep the parallel groups within "
        "the budget."
    )


@dataclass
class PlanOutcome:
    validation: ValidationOutcome
    usage: Usage
    route: RouteInfo

    @property
    def plan(self) -> Plan:
        return self.validation.plan

    @property
    def is_valid(self) -> bool:
        return self.validation.is_valid


async def create_plan(
    user_request: str,
    *,
    budget: RunBudget,
    project_context: str | None = None,
    prior_decisions: list[str] | None = None,
    registry: AgentRegistry | None = None,
    client: OpenClawClient | None = None,
    idempotency_key: str | None = None,
    full_ecosystem: bool = False,
) -> PlanOutcome:
    registry = registry or get_registry()
    client = client or get_gateway_client()

    user_prompt = prompts.render(
        prompts.PLAN_PROMPT,
        agent_catalog=registry.catalog(),
        routing_matrix=registry.routing_matrix(),
        project_context=project_context or "(no project context supplied)",
        prior_decisions=prior_decisions or ["(none recorded for this project)"],
        user_request=user_request,
        budget=budget.model_dump(),
    )
    if full_ecosystem:
        user_prompt += "\n\n" + full_ecosystem_instruction(registry)

    plan, usage, route = await client.complete_structured(
        route=AgentRoute.EXECUTIVE,
        system=PLANNER_SYSTEM,
        user=user_prompt,
        schema=Plan,
        idempotency_key=idempotency_key,
        session_key=session_key_for(idempotency_key),
    )

    validation = validate_plan(plan, registry, budget, user_request=user_request)

    log.info(
        "plan_created",
        valid=validation.is_valid,
        tasks=len(plan.tasks),
        agents=plan.selected_agents,
        groups=len(validation.execution_groups),
        approval_required=validation.plan.plan_approval_required,
        forced_approval=validation.forced_approval,
        errors=validation.errors or None,
        warnings=validation.warnings or None,
        model=route.model,
    )
    return PlanOutcome(validation=validation, usage=usage, route=route)
