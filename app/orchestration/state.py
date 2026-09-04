"""LangGraph run state (PRD s8.1)."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from app.schemas.executive import (
    ChallengeResult,
    ExecutiveDecisionBrief,
    QualityGateResult,
)
from app.schemas.planning import Plan, RunBudget
from app.schemas.specialist import SpecialistResult


def merge_results(
    left: list[SpecialistResult], right: list[SpecialistResult]
) -> list[SpecialistResult]:
    """Reducer for concurrent specialist writes.

    Parallel branches append independently; a later result for the same task id
    (a follow-up or retry) replaces the earlier one.
    """
    merged = {r.task_id: r for r in left}
    for r in right:
        merged[r.task_id] = r
    return list(merged.values())


def merge_unique(left: list[Any], right: list[Any]) -> list[Any]:
    out = list(left)
    for item in right:
        if item not in out:
            out.append(item)
    return out


class RunState(TypedDict, total=False):
    # Identity
    run_id: str
    run_ref: str
    project_id: str | None
    slack_channel: str | None
    slack_thread_ts: str | None
    slack_user_id: str | None

    # Request
    user_request: str
    requested_output_format: str
    project_context: str | None
    prior_decisions: list[str]

    # Planning
    plan: Plan | None
    plan_errors: list[str]
    plan_warnings: list[str]
    execution_groups: list[list[str]]
    selected_agents: list[str]

    # Approval (s16)
    approval_required: bool
    approval_id: str | None
    approval_status: str | None
    decision_class: str

    # Execution
    specialist_results: Annotated[list[SpecialistResult], merge_results]
    failed_task_ids: Annotated[list[str], merge_unique]
    evidence_refs: Annotated[list[str], merge_unique]
    artifacts: Annotated[list[str], merge_unique]
    evidence_violations: Annotated[list[str], merge_unique]

    # Challenge (s9.2)
    conflict_signals: list[dict[str, Any]]
    challenge: ChallengeResult | None
    challenge_round: int

    # Synthesis and gate
    executive_brief: ExecutiveDecisionBrief | None
    quality_gate: QualityGateResult | None

    # Reporting (s18)
    report_paths: list[str]
    report_warnings: list[str]
    report_error: str | None

    # Slack progress (s17.1). Progress is narrated as a stream of messages, so
    # there is no single message id to carry.
    started_epoch: float

    # Control
    budgets: RunBudget
    current_stage: str
    status: str
    error_state: str | None
