"""Deterministic plan validation (PRD s8 VALIDATE_PLAN, s12.2, FR-004).

The planner is an LLM and is untrusted with respect to policy. It proposes;
this module decides. Agent existence, acyclicity, budgets, forbidden actions and
approval requirements are enforced in code, never by prompt instruction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.agents.registry import AgentRegistry
from app.schemas.planning import Plan, PlannedTask, RunBudget

# A request touching one of these always carries a human approval gate, whatever
# the planner decided (s16 RED/AMBER).
#
# Patterns, not literal substrings: a gate must not be defeated by an article or
# an adjective ("assign A shelf life", "close the MAJOR capa"). `_GAP` allows a
# few filler words between the verb and its object.
_GAP = r"(?:\s+\w+){0,3}\s+"

REGULATED_PATTERNS: tuple[tuple[str, str], ...] = (
    ("batch release", rf"\brelease{_GAP}batch\b|\bbatch release\b"),
    ("disposition", r"\bdisposition\b|\bdispose of\b"),
    (
        "regulatory submission",
        rf"\bsubmit{_GAP}(?:fda|ema|mhra|agency|authority)\b|"
        r"\bsubmission to\b|\bfile the\b|\bfile an? (?:anda|nda|maa)\b",
    ),
    ("sign-off", r"\bsign[- ]off\b|\bsign off on\b"),
    (
        "approval",
        rf"\bapprove{_GAP}(?:batch|protocol|change|deviation|capa|release)\b|"
        r"\bapproval of\b|\bapprove the\b",
    ),
    ("recall", r"\brecall\b|\bfield action\b"),
    ("capa closure", rf"\bclose{_GAP}capa\b|\bcapa closure\b"),
    (
        "shelf-life assignment",
        rf"\bassign{_GAP}shelf[- ]life\b|\bshelf[- ]life assignment\b|"
        rf"\bset{_GAP}(?:expiry|shelf[- ]life)\b",
    ),
    ("validation approval", rf"\bapprove{_GAP}validation\b|\bvalidation approval\b"),
)


@dataclass
class ValidationOutcome:
    plan: Plan
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    execution_groups: list[list[str]] = field(default_factory=list)
    forced_approval: bool = False

    @property
    def is_valid(self) -> bool:
        return not self.errors


def compute_execution_groups(
    tasks: list[PlannedTask],
) -> tuple[list[list[str]], list[str]]:
    """Derive parallel execution groups from dependencies.

    Groups are computed here rather than trusted from the planner, so a dependent
    task can never be scheduled alongside the task it depends on.
    """
    by_id = {t.task_id: t for t in tasks}
    errors: list[str] = []

    for task in tasks:
        for dep in task.depends_on:
            if dep not in by_id:
                errors.append(f"task {task.task_id} depends on unknown task {dep}")
    if errors:
        return [], errors

    remaining = {t.task_id: set(t.depends_on) for t in tasks}
    groups: list[list[str]] = []
    while remaining:
        ready = sorted(tid for tid, deps in remaining.items() if not deps)
        if not ready:
            errors.append("circular dependency among tasks: " + ", ".join(sorted(remaining)))
            return [], errors
        groups.append(ready)
        for tid in ready:
            del remaining[tid]
        for deps in remaining.values():
            deps.difference_update(ready)
    return groups, errors


def validate_plan(
    plan: Plan,
    registry: AgentRegistry,
    budget: RunBudget,
    *,
    user_request: str = "",
) -> ValidationOutcome:
    errors: list[str] = []
    warnings: list[str] = []
    enabled = registry.enabled_ids()

    # -- agent existence and eligibility (s11) ------------------------------
    for task in plan.tasks:
        if not registry.exists(task.agent_id):
            errors.append(f"task {task.task_id}: unknown agent {task.agent_id}")
            continue
        profile = registry.get(task.agent_id)
        if task.agent_id not in enabled:
            reason = (
                "provisional and not enabled in this deployment"
                if profile.is_provisional
                else "disabled"
            )
            errors.append(f"task {task.task_id}: agent {task.agent_id} is {reason}")
        if task.max_steps > profile.max_steps:
            warnings.append(
                f"task {task.task_id}: max_steps {task.max_steps} exceeds the "
                f"{task.agent_id} profile limit {profile.max_steps}; clamping"
            )

    # -- budgets (s12.2, s22) -----------------------------------------------
    agents = plan.selected_agents
    if len(agents) > budget.max_specialists:
        errors.append(
            f"plan selects {len(agents)} specialists; the budget allows "
            f"{budget.max_specialists}. Broader programmes need explicit scope approval."
        )
    if len(plan.tasks) > budget.max_specialists * 2:
        errors.append(
            f"plan contains {len(plan.tasks)} tasks, above the ceiling of "
            f"{budget.max_specialists * 2}"
        )

    # -- dependency graph ---------------------------------------------------
    groups, graph_errors = compute_execution_groups(plan.tasks)
    errors.extend(graph_errors)
    if groups:
        widest = max(len(g) for g in groups)
        if widest > budget.max_parallel_specialists:
            warnings.append(
                f"widest group has {widest} tasks; the dispatcher will limit "
                f"concurrency to {budget.max_parallel_specialists}"
            )

    # -- forbidden actions (s11 restrictions) -------------------------------
    for task in plan.tasks:
        if not registry.exists(task.agent_id):
            continue
        objective = task.objective.lower()
        for forbidden in registry.get(task.agent_id).forbidden_actions:
            phrase = forbidden.replace("_", " ")
            if phrase in objective:
                errors.append(
                    f"task {task.task_id}: objective requests '{phrase}', a forbidden "
                    f"action for {task.agent_id}"
                )

    # -- approval policy (s16) ----------------------------------------------
    forced = False
    lowered = user_request.lower()
    matched = [label for label, pattern in REGULATED_PATTERNS if re.search(pattern, lowered)]
    if matched and not plan.plan_approval_required:
        forced = True
        warnings.append(
            "request touches a regulated decision "
            f"({', '.join(matched[:3])}); approval gate forced on"
        )

    validated = plan
    if forced:
        validated = validated.model_copy(
            update={
                "plan_approval_required": True,
                "approval_rationale": (
                    plan.approval_rationale
                    or "Approval required by platform policy: the request touches a "
                    "regulated decision."
                ),
            }
        )

    # Clamp steps to profile ceilings and install the computed groups.
    clamped: list[PlannedTask] = []
    for task in validated.tasks:
        if registry.exists(task.agent_id):
            limit = registry.get(task.agent_id).max_steps
            if task.max_steps > limit:
                task = task.model_copy(update={"max_steps": limit})
        clamped.append(task)
    validated = validated.model_copy(update={"tasks": clamped, "execution_groups": groups})

    return ValidationOutcome(
        plan=validated,
        errors=errors,
        warnings=warnings,
        execution_groups=groups,
        forced_approval=forced,
    )
