"""Deterministic gateway double for graph tests."""

from __future__ import annotations

from typing import Any

from app.providers.openclaw_client import AgentRoute, RouteInfo, Usage
from app.schemas.executive import ChallengeResult, ExecutiveDecisionBrief
from app.schemas.planning import Plan
from app.schemas.specialist import Finding, SpecialistResult


def route_for(agent_route: AgentRoute) -> RouteInfo:
    return route("pharma-executive" if agent_route is AgentRoute.EXECUTIVE else "pharma-specialist")


def route(agent_id="pharma-specialist") -> RouteInfo:
    return RouteInfo(
        route=AgentRoute.SPECIALIST if "specialist" in agent_id else AgentRoute.EXECUTIVE,
        agent_id=agent_id,
        auth_mode="subscription",
        provider="claude-cli" if "specialist" in agent_id else "codex",
        model="claude-sonnet-5" if "specialist" in agent_id else "gpt-5.4",
    )


def specialist_result(task_id: str, agent_id: str, **kw) -> SpecialistResult:
    base: dict[str, Any] = {
        "task_id": task_id,
        "agent_id": agent_id,
        "status": "complete",
        "executive_summary": f"Assessment for {task_id} by {agent_id}.",
        "findings": [Finding(statement="A material consideration.", basis="assumption")],
        "confidence": "medium",
    }
    return SpecialistResult(**{**base, **kw})


def brief(**kw) -> ExecutiveDecisionBrief:
    base: dict[str, Any] = {
        "objective": "Assess the change.",
        "executive_conclusion": "Feasible with mitigations.",
        "recommendation": "Proceed with the identified mitigations.",
        "confidence": "medium",
        "recommended_actions": ["Run the confirmatory study."],
        "critical_assumptions": ["Reference product data is representative."],
    }
    return ExecutiveDecisionBrief(**{**base, **kw})


class FakeGateway:
    """Returns canned structured objects, chosen by requested schema.

    Specialist results are keyed by a substring of the task objective, because the
    runner passes the objective through verbatim and the gateway sees only text.
    """

    def __init__(
        self,
        plan: Plan,
        *,
        challenge: ChallengeResult | None = None,
        executive_brief: ExecutiveDecisionBrief | None = None,
        results_by_marker: dict[str, SpecialistResult] | None = None,
        fail_on: set[str] | None = None,
        default_result: SpecialistResult | None = None,
    ):
        self.plan = plan
        self.challenge = challenge if challenge is not None else ChallengeResult()
        self.executive_brief = executive_brief or brief()
        self.results_by_marker = results_by_marker or {}
        self.fail_on = fail_on or set()
        self.default_result = default_result
        self.specialist_calls: list[str] = []
        self.schemas: list[str] = []

    async def complete_structured(
        self, *, route, system, user, schema, idempotency_key=None, timeout_seconds=None
    ):
        self.schemas.append(schema.__name__)
        usage = Usage(duration_ms=30, steps=1)
        info = route_for(route)
        if schema is Plan:
            return self.plan, usage, info
        if schema is ChallengeResult:
            return self.challenge, usage, info
        if schema is ExecutiveDecisionBrief:
            return self.executive_brief, usage, info
        raise AssertionError(f"unexpected schema: {schema.__name__}")

    async def run_specialist(
        self, *, system, user, result_schema, idempotency_key=None, timeout_seconds=None
    ):
        objective = _objective(user)
        self.specialist_calls.append(objective)
        for marker in self.fail_on:
            if marker in objective:
                raise RuntimeError(f"simulated specialist failure for '{marker}'")
        for marker, result in self.results_by_marker.items():
            if marker in objective:
                return result, Usage(duration_ms=50, steps=1), route()
        if self.default_result is not None:
            return self.default_result, Usage(duration_ms=50, steps=1), route()
        return (
            specialist_result("unknown", "A01"),
            Usage(duration_ms=50, steps=1),
            route(),
        )


def _objective(message: str) -> str:
    for line in message.splitlines():
        if line.startswith("Objective:"):
            return line[len("Objective:") :].strip()
    return ""
