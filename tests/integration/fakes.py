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
        fail_schemas: set[str] | None = None,
    ):
        self.plan = plan
        self.challenge = challenge if challenge is not None else ChallengeResult()
        self.executive_brief = executive_brief or brief()
        self.results_by_marker = results_by_marker or {}
        self.fail_on = fail_on or set()
        self.default_result = default_result
        self.specialist_calls: list[str] = []
        self.schemas: list[str] = []
        self.fail_schemas = fail_schemas or set()
        self.section_calls = 0
        self.references: list[dict[str, Any]] = []

    AGENT_IDS = {
        "executive": "pharma-executive",
        "specialist": "pharma-specialist",
        "local": "pharma-local",
    }

    def agent_id_for(self, route):
        return self.AGENT_IDS[route.value]

    def session_key_for_route(self, route, suffix: str) -> str:
        return f"agent:{self.agent_id_for(route)}:{suffix}"

    async def complete_structured(
        self,
        *,
        route,
        system,
        user,
        schema,
        idempotency_key=None,
        session_key=None,
        timeout_seconds=None,
    ):
        self.schemas.append(schema.__name__)
        if schema.__name__ in self.fail_schemas:
            raise RuntimeError(f"simulated failure for {schema.__name__}")
        usage = Usage(duration_ms=30, steps=1)
        info = route_for(route)
        if schema is Plan:
            return self.plan, usage, info
        if schema is ChallengeResult:
            return self.challenge, usage, info
        if schema is ExecutiveDecisionBrief:
            return self.executive_brief, usage, info
        composed = self._compose(schema)
        if composed is not None:
            return composed, usage, info
        raise AssertionError(f"unexpected schema: {schema.__name__}")

    def _compose(self, schema):
        """Canned answers for the report composer's staged calls."""
        name = schema.__name__
        if name == "ReportOutline":
            return schema.model_validate(
                {
                    "document_title": "Dissolution Investigation — Candidate X Softgel",
                    "product": "Candidate X",
                    "strength": "25 mg",
                    "dosage_form": "Soft gelatin capsule",
                    "sections": [
                        {
                            "heading": "Regulatory framework",
                            "purpose": "The submission criteria the work is measured against.",
                            "subsections": [],
                            "source_artifact_refs": [],
                            "expected_tables": [],
                        },
                        {
                            "heading": "Quality target product profile",
                            "purpose": "The QTPP derived from those criteria.",
                            "subsections": [{"heading": "Dissolution", "purpose": "Release."}],
                            "source_artifact_refs": [],
                            "expected_tables": ["QTPP register"],
                        },
                        {
                            "heading": "Critical quality attributes",
                            "purpose": "Attributes that determine performance.",
                            "subsections": [],
                            "source_artifact_refs": [],
                            "expected_tables": [],
                        },
                    ],
                }
            )
        if name == "SectionDraft":
            self.section_calls += 1
            return schema.model_validate(
                {
                    "heading": f"Composed section {self.section_calls}",
                    "body": ("The submission criteria are stated in the retrieved guidance. " * 20),
                    "tables": [
                        {
                            "title": "Register",
                            "columns": ["Attribute", "Target", "Status"],
                            "rows": [["Dissolution", "Q=75% at 45 min", "provisional"]],
                        }
                    ],
                    "subsections": [],
                    "evidence_refs": [],
                    "status": "provisional",
                }
            )
        if name == "ClosingMatter":
            return schema.model_validate(
                {
                    "review_summary": "An independent review examined the draft.",
                    "critical_review": [
                        {
                            "issue": "Classification conflict",
                            "severity": "high",
                            "why_it_mattered": "It drives the bioequivalence approach.",
                            "original_position": "An earlier draft stated the wrong class.",
                            "evidence_reviewed": [],
                            "resolution": "Primary regulatory evidence was retrieved.",
                            "final_position": "The evidence-supported class is used.",
                            "state": "resolved",
                        }
                    ],
                    "revision_crosswalk": [
                        {
                            "issue": "Classification conflict",
                            "original": "An earlier draft stated the wrong class.",
                            "critique": "The evidence was inconsistent.",
                            "action_taken": "Primary source retrieved.",
                            "revision": "The report states the supported position only.",
                            "state": "resolved",
                        }
                    ],
                    "information_gaps": [
                        {
                            "category": "regulatory",
                            "gap": "EU orphan status not independently confirmed.",
                            "why_it_matters": "It affects filing timing.",
                            "how_to_close": "Request confirmation from the authority.",
                            "blocks_completion": False,
                        }
                    ],
                    "conclusions": "The change is workable under the stated controls.",
                    "recommended_actions": [
                        {"priority": "high", "action": "Run the confirmatory study."}
                    ],
                    "unresolved_conflicts": [],
                }
            )
        if name == "BriefSection":
            return schema.model_validate(
                {
                    "brief": {
                        "executive_summary": self.executive_brief.executive_conclusion,
                        "objective_and_scope": self.executive_brief.objective,
                        "overall_recommendation": self.executive_brief.recommendation,
                        "confidence_and_evidence_status": "Medium confidence.",
                        "major_risks": self.executive_brief.key_risks,
                        "key_findings": self.executive_brief.key_findings,
                        "critical_assumptions": self.executive_brief.critical_assumptions,
                        "outstanding_information": self.executive_brief.information_required,
                        "decisions_required": [
                            d.question for d in self.executive_brief.decisions_required_from_user
                        ],
                    }
                }
            )
        if name == "ReferenceList":
            return schema.model_validate({"references": list(self.references)})
        if name == "SummaryDraft":
            return schema.model_validate(
                {
                    "summary": {
                        "headline": "Workable under the stated controls.",
                        "situation": "The requested assessment is complete.",
                        "key_technical_findings": ["Dissolution is release-controlling."],
                        "decisions_taken": ["Proceed under provisional controls."],
                        "what_changed_after_review": [],
                        "critical_risks": [],
                        "open_questions": [],
                        "actions": [
                            {
                                "action": "Run the confirmatory study.",
                                "priority": "high",
                                "owner_function": "Analytical Development",
                            }
                        ],
                    }
                }
            )
        return None

    async def run_specialist(
        self,
        *,
        system,
        user,
        result_schema,
        idempotency_key=None,
        session_key=None,
        timeout_seconds=None,
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
