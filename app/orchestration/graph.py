"""The orchestration graph (PRD s8).

LangGraph is the deterministic control layer. The executive route proposes; node
code validates, enforces budgets and permissions, executes approved work and
decides completion.

    INTAKE -> LOAD_PROJECT_CONTEXT -> OPENAI_PLAN -> VALIDATE_PLAN
      -> [PLAN_APPROVAL?] -> DISPATCH -> COLLECT -> CONFLICT_DETECTION
      -> [NEEDS_CHALLENGE?] -> CHALLENGE -> FOLLOW_UP_TASKS (max 1 round)
      -> SYNTHESIS -> QUALITY_GATE -> [AMBER/RED?] -> HUMAN_DECISION
      -> BUILD_REPORT -> PUBLISH -> PERSIST_FINAL_STATE
"""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.registry import get_registry
from app.audit.logger import bind_run_context, get_logger
from app.config import get_settings
from app.orchestration.challenge import challenge_results
from app.orchestration.conflict import detect_conflict_signals, needs_challenge
from app.orchestration.direct import build_direct_plan
from app.orchestration.plan_validator import validate_plan
from app.orchestration.planner import create_plan
from app.orchestration.specialist_runner import (
    SpecialistRunner,
    build_envelope,
    summarise_for_downstream,
)
from app.orchestration.state import RunState
from app.orchestration.synthesis import run_quality_gate, synthesize
from app.orchestration.timing import format_range, measure_durations, remaining_seconds
from app.reports.service import get_report_service
from app.schemas.planning import RunBudget
from app.schemas.specialist import SpecialistResult
from app.slack import blocks as slack_blocks
from app.slack import progress as slack_progress
from app.slack.notifier import get_notifier
from app.storage.db import session_scope
from app.storage.repositories import (
    AuditRepository,
    EventType,
    EvidenceRepository,
    GovernanceRepository,
    RunRepository,
)

log = get_logger(__name__)


def _budget(state: RunState) -> RunBudget:
    b = state.get("budgets")
    if isinstance(b, RunBudget):
        return b
    if isinstance(b, dict) and b:
        return RunBudget.model_validate(b)
    s = get_settings()
    return RunBudget(
        max_specialists=s.default_max_specialists,
        max_parallel_specialists=s.default_max_parallel_specialists,
        max_followup_rounds=s.default_max_followup_rounds,
        max_specialist_steps=s.default_max_specialist_steps,
        max_runtime_seconds=s.default_max_runtime_seconds,
    )


def _run_uuid(state: RunState) -> uuid.UUID:
    return uuid.UUID(state["run_id"])


def _agent_names() -> dict[str, str]:
    return {p.id: p.name for p in get_registry().all_profiles()}


async def _task_progress(state: RunState) -> list[slack_progress.TaskProgress]:
    """Live task state for the progress bar. Never raises into orchestration."""
    try:
        async with session_scope() as session:
            rows = await RunRepository(session).list_tasks(_run_uuid(state))
    except Exception:
        return []
    names = _agent_names()
    return [
        slack_progress.TaskProgress(
            agent_id=r.agent_id,
            agent_name=names.get(r.agent_id, r.agent_id),
            objective=r.objective,
            status=r.status,
            execution_group=r.execution_group,
        )
        for r in rows
    ]


async def _estimate_remaining(
    state: RunState, stage: str, tasks: list[slack_progress.TaskProgress]
) -> str | None:
    """Time remaining, measured rather than assumed (s17.1).

    Reads what specialist turns have actually cost — this run's first, then this
    deployment's recent runs. Never raises: an estimate is a courtesy.
    """
    try:
        outstanding = {
            t.execution_group for t in tasks if t.status in ("pending", "ready", "running")
        }
        widest = max(
            (
                sum(
                    1
                    for t in tasks
                    if t.execution_group == g and t.status in ("pending", "ready", "running")
                )
                for g in outstanding
            ),
            default=1,
        )
        async with session_scope() as session:
            model = await measure_durations(session, _run_uuid(state))
        seconds = remaining_seconds(
            model,
            stage=stage,
            outstanding_groups=len(outstanding),
            widest_outstanding_group=widest,
            challenge_done=bool(state.get("challenge")),
            synthesis_done=bool(state.get("executive_brief")),
            report_done=bool(state.get("report_paths")),
        )
        return format_range(seconds, measured=model.is_measured)
    except Exception:
        return None


async def _progress(
    state: RunState,
    stage: str,
    *,
    headline: str | None = None,
    detail: str | None = None,
    bullets: list[str] | None = None,
    tasks: list[slack_progress.TaskProgress] | None = None,
) -> str | None:
    """Post one progress message saying what just changed (s17.1).

    A new message rather than an edit: an edited message gives no notification and
    is invisible to anyone who has scrolled past it, so a long run looks stalled
    even while it works. Never raises - progress is a courtesy, not the deliverable.
    """
    channel = state.get("slack_channel")
    if not channel:
        return None

    if tasks is None:
        tasks = await _task_progress(state)
    elapsed = int(time.time() - (state.get("started_epoch") or time.time()))
    run_ref = state.get("run_ref") or "Run"
    label = headline or slack_progress.STAGE_LABELS.get(stage, stage.replace("_", " "))
    estimate = await _estimate_remaining(state, stage, tasks)

    blocks = slack_progress.event(
        run_ref,
        label,
        stage=stage,
        tasks=tasks,
        elapsed_seconds=elapsed,
        detail=detail,
        bullets=bullets,
        estimate_text=estimate,
    )
    return await get_notifier().post(
        channel=channel,
        blocks=blocks,
        text=f"{run_ref}: {label}",
        thread_ts=state.get("slack_thread_ts"),
    )


async def _specialist_event(
    state: RunState,
    task: Any,
    status: str,
    *,
    findings: int = 0,
    evidence: int = 0,
) -> None:
    """Say what a specialist just finished, as it finishes.

    Posted from inside the fan-out so a parallel group reports each result when it
    lands rather than all of them at the end.
    """
    names = _agent_names()
    headline, detail = slack_progress.specialist_finished(
        names.get(task.agent_id, task.agent_id), status, findings=findings, evidence=evidence
    )
    await _progress(state, "dispatch", headline=headline, detail=detail)


async def _notify(state: RunState, blocks: list[dict[str, Any]], fallback: str) -> str | None:
    """Post to the originating Slack thread, if the run came from Slack.

    Delivery failure is logged and never propagates: a Slack outage must not fail
    an otherwise-complete pharmaceutical run, and the result stays in PostgreSQL
    either way.
    """
    channel = state.get("slack_channel")
    if not channel:
        return None
    return await get_notifier().post(
        channel=channel,
        blocks=blocks,
        text=fallback,
        thread_ts=state.get("slack_thread_ts"),
    )


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def intake(state: RunState) -> dict[str, Any]:
    bind_run_context(run_id=state["run_id"])
    run_id = _run_uuid(state)
    async with session_scope() as s:
        await RunRepository(s).set_stage(run_id, "intake", status="planning")
        await AuditRepository(s).record(
            event_type=EventType.RUN_CREATED,
            actor_type="user",
            actor_id=state.get("slack_user_id"),
            run_id=run_id,
            metadata={"request_chars": len(state["user_request"])},
        )
    log.info("stage_intake", chars=len(state["user_request"]))
    started = time.time()
    await _progress(
        {**state, "started_epoch": started},
        "plan",
        headline=":brain: Planning the specialist workstreams",
        detail="Selecting which specialists the request needs and how their work depends "
        "on each other.",
    )
    return {
        "current_stage": "intake",
        "status": "planning",
        "budgets": _budget(state),
        "started_epoch": started,
    }


async def load_project_context(state: RunState) -> dict[str, Any]:
    """Load project memory and prior approved decisions (s14.2, FR-020)."""
    project_id = state.get("project_id")
    if not project_id:
        return {"current_stage": "load_context", "prior_decisions": []}

    pid = uuid.UUID(project_id)
    async with session_scope() as s:
        from app.storage.repositories.projects import ProjectRepository

        project = await ProjectRepository(s).get(pid)
        decisions = await GovernanceRepository(s).project_decisions(pid)

    context = state.get("project_context")
    if context is None and project is not None:
        context = "\n".join(
            p for p in [project.name, project.objective, project.context_summary] if p
        )
    return {
        "current_stage": "load_context",
        "project_context": context,
        "prior_decisions": [f"{d.title}: {d.decision_text}" for d in decisions],
    }


async def plan_node(state: RunState) -> dict[str, Any]:
    """Build the task graph for the run (s9.1).

    A direct request names its own specialist, so the plan is built in code: there
    is nothing for the planner to decide, and asking it to decide anyway risks it
    answering with a different team than the user asked for.
    """
    run_id = _run_uuid(state)
    mode = state.get("run_mode") or "standard"
    registry = get_registry()

    if mode == "direct":
        requested = state.get("requested_agents") or []
        agent_id = requested[0] if requested else None
        if not agent_id or not registry.exists(agent_id):
            return {
                "current_stage": "plan_failed",
                "status": "failed",
                "error_state": f"Unknown specialist for a direct request: {agent_id!r}",
                "plan_errors": [f"unknown agent {agent_id!r}"],
            }
        plan = build_direct_plan(
            agent_id,
            state["user_request"],
            registry=registry,
            max_steps=_budget(state).max_specialist_steps,
        )
        validation = validate_plan(
            plan, registry, _budget(state), user_request=state["user_request"]
        )
        async with session_scope() as s:
            await AuditRepository(s).record(
                event_type=EventType.PLAN_CREATED,
                actor_type="system",
                run_id=run_id,
                metadata={
                    "mode": "direct",
                    "agents": [agent_id],
                    "valid": validation.is_valid,
                    "errors": validation.errors,
                },
            )
        log.info("direct_plan", agent_id=agent_id, valid=validation.is_valid)
        return {
            "current_stage": "plan",
            "plan": validation.plan,
            "plan_errors": validation.errors,
            "plan_warnings": validation.warnings,
            "execution_groups": validation.execution_groups,
            "selected_agents": [agent_id],
            "approval_required": validation.plan.plan_approval_required,
        }

    outcome = await create_plan(
        state["user_request"],
        budget=_budget(state),
        project_context=state.get("project_context"),
        prior_decisions=state.get("prior_decisions"),
        idempotency_key=f"{state['run_id']}:plan",
        full_ecosystem=mode == "full",
    )

    async with session_scope() as s:
        await RunRepository(s).record_routes(
            run_id,
            executive_route=outcome.route.agent_id,
            auth_mode=outcome.route.auth_mode,
        )
        await AuditRepository(s).record(
            event_type=EventType.PLAN_CREATED,
            actor_type="orchestrator",
            run_id=run_id,
            gateway_route=outcome.route.agent_id,
            model_id=outcome.route.model,
            metadata={
                "mode": mode,
                "tasks": len(outcome.plan.tasks),
                "agents": outcome.plan.selected_agents,
                "valid": outcome.is_valid,
                "errors": outcome.validation.errors,
                "warnings": outcome.validation.warnings,
            },
        )

    return {
        "current_stage": "plan",
        "plan": outcome.plan,
        "plan_errors": outcome.validation.errors,
        "plan_warnings": outcome.validation.warnings,
        "execution_groups": outcome.validation.execution_groups,
        "selected_agents": outcome.plan.selected_agents,
        "approval_required": outcome.plan.plan_approval_required,
    }


async def validate_plan_node(state: RunState) -> dict[str, Any]:
    """Persist an accepted plan as task rows, or terminate the run."""
    run_id = _run_uuid(state)
    errors = state.get("plan_errors") or []
    plan = state.get("plan")

    if errors or plan is None:
        message = "The execution plan failed validation: " + "; ".join(errors)
        async with session_scope() as s:
            await RunRepository(s).finish(run_id, status="failed", error_state=message)
            await AuditRepository(s).record(
                event_type=EventType.PLAN_REJECTED,
                actor_type="system",
                run_id=run_id,
                metadata={"errors": errors},
            )
        await _notify(
            state,
            slack_blocks.failure(state.get("run_ref") or "Run", message),
            "Plan failed validation.",
        )
        log.warning("plan_rejected", errors=errors)
        return {"current_stage": "plan_rejected", "status": "failed", "error_state": message}

    async with session_scope() as s:
        repo = RunRepository(s)
        await repo.save_plan(run_id, plan)
        await repo.create_tasks(run_id, plan, state.get("execution_groups") or [])
        await repo.set_stage(run_id, "plan_validated")

    names = _agent_names()
    await _progress(
        {**state, "current_stage": "plan_validated"},
        "plan_validated",
        headline=f":clipboard: Plan validated — {len(plan.tasks)} workstream(s)",
        bullets=[
            f"*{names.get(t.agent_id, t.agent_id)}* — {t.objective[:120]}" for t in plan.tasks
        ],
    )
    return {"current_stage": "plan_validated"}


def route_after_validation(
    state: RunState,
) -> Literal["plan_approval", "dispatch", "finish_failed"]:
    if state.get("status") == "failed":
        return "finish_failed"
    return "plan_approval" if state.get("approval_required") else "dispatch"


async def plan_approval(state: RunState) -> dict[str, Any]:
    """Pause for a human plan decision (s16, FR-011).

    The graph interrupts; the checkpointer holds state until the run is resumed,
    so a decision survives a worker restart.
    """
    run_id = _run_uuid(state)
    plan = state["plan"]
    assert plan is not None

    async with session_scope() as s:
        approval, is_new = await GovernanceRepository(s).request_approval(
            run_id=run_id,
            gate="plan",
            decision_class="amber",
            requested_payload={
                "objective": plan.objective,
                "agents": plan.selected_agents,
                "task_count": len(plan.tasks),
                "rationale": plan.approval_rationale,
                "output_type": plan.output_type,
            },
        )
        approval_id = str(approval.id)
        await RunRepository(s).set_stage(run_id, "awaiting_approval", status="awaiting_approval")
        if is_new:
            await AuditRepository(s).record(
                event_type=EventType.APPROVAL_REQUESTED,
                actor_type="orchestrator",
                run_id=run_id,
                metadata={"approval_id": approval_id, "gate": "plan"},
            )

    # Resuming re-runs this node from the top. The gate is only posted the first
    # time, so the user is not handed a second copy of a plan they just decided.
    if is_new:
        await _notify(
            state,
            slack_blocks.plan_preview(
                state.get("run_ref") or "Run", plan, _agent_names(), approval_id
            ),
            f"Plan ready for approval: {plan.objective}",
        )

    decision = interrupt(
        {
            "type": "plan_approval",
            "approval_id": approval_id,
            "run_ref": state.get("run_ref"),
            "objective": plan.objective,
            "selected_agents": plan.selected_agents,
            "tasks": [
                {"task_id": t.task_id, "agent_id": t.agent_id, "objective": t.objective}
                for t in plan.tasks
            ],
            "rationale": plan.approval_rationale,
        }
    )

    status = (decision or {}).get("status", "rejected")
    decided_by = (decision or {}).get("decided_by")
    async with session_scope() as s:
        await GovernanceRepository(s).decide_approval(
            uuid.UUID(approval_id), status=status, decided_by=decided_by or "unknown"
        )
        await AuditRepository(s).record(
            event_type=EventType.APPROVAL_DECIDED,
            actor_type="user",
            actor_id=decided_by,
            run_id=run_id,
            metadata={"approval_id": approval_id, "status": status, "gate": "plan"},
        )
    return {
        "current_stage": "plan_decided",
        "approval_id": approval_id,
        "approval_status": status,
    }


def route_after_approval(state: RunState) -> Literal["dispatch", "finish_cancelled"]:
    return "dispatch" if state.get("approval_status") == "approved" else "finish_cancelled"


async def dispatch(state: RunState) -> dict[str, Any]:
    """Execute ready task groups, fanning out within each group (s8, FR-006)."""
    run_id = _run_uuid(state)
    budget = _budget(state)
    registry = get_registry()
    runner = SpecialistRunner(registry=registry)

    async with session_scope() as s:
        await RunRepository(s).set_stage(run_id, "dispatch", status="running")
        pending = [
            t for t in await RunRepository(s).list_tasks(run_id) if t.status in ("pending", "ready")
        ]

    by_task: dict[str, SpecialistResult] = {
        r.task_id: r for r in (state.get("specialist_results") or [])
    }
    summaries: dict[str, dict] = {}
    all_results: list[SpecialistResult] = []
    failed: list[str] = []
    violations: list[str] = []
    semaphore = asyncio.Semaphore(max(1, budget.max_parallel_specialists))

    groups: dict[int, list] = {}
    for task in pending:
        groups.setdefault(task.execution_group, []).append(task)

    for group_index in sorted(groups):
        group = groups[group_index]

        async with session_scope() as s:
            repo, audit = RunRepository(s), AuditRepository(s)
            for task in group:
                await repo.set_task_status(task.id, "running")
                await audit.record(
                    event_type=EventType.TASK_DISPATCHED,
                    actor_type="orchestrator",
                    run_id=run_id,
                    task_id=task.id,
                    metadata={"agent_id": task.agent_id, "task": task.planner_task_id},
                )

        async def run_one(task) -> tuple[Any, Any]:
            async with semaphore:
                profile = registry.get(task.agent_id)
                upstream = [summaries[dep] for dep in (task.depends_on or []) if dep in summaries]
                envelope = build_envelope(
                    run_id=run_id,
                    run_ref=state.get("run_ref"),
                    task_id=task.planner_task_id,
                    profile=profile,
                    objective=task.objective,
                    required_output=task.required_output or "A specialist assessment.",
                    project_context_summary=state.get("project_context"),
                    upstream_findings=upstream,
                    max_steps=min(task.max_steps, budget.max_specialist_steps),
                )
                try:
                    outcome = await runner.run(profile, envelope)
                    await _specialist_event(
                        state,
                        task,
                        outcome.result.status,
                        findings=len(outcome.result.findings),
                        evidence=len(outcome.result.evidence_refs),
                    )
                    return task, outcome
                except Exception as exc:
                    log.warning(
                        "specialist_failed",
                        agent_id=task.agent_id,
                        task_id=task.planner_task_id,
                        error=str(exc)[:300],
                    )
                    await _specialist_event(state, task, "failed")
                    return task, exc

        names = _agent_names()
        started_headline = (
            f":arrow_forward: Starting {len(group)} workstream(s) in parallel"
            if len(group) > 1
            else f":arrow_forward: *{names.get(group[0].agent_id, group[0].agent_id)}* "
            "is now working"
        )
        await _progress(
            state,
            "dispatch",
            headline=started_headline,
            bullets=(
                [f"*{names.get(t.agent_id, t.agent_id)}* — {t.objective[:140]}" for t in group]
                if len(group) > 1
                else None
            ),
            detail=group[0].objective[:400] if len(group) == 1 else None,
        )
        outcomes = await asyncio.gather(*(run_one(t) for t in group))

        async with session_scope() as s:
            repo, audit = RunRepository(s), AuditRepository(s)
            for task, outcome in outcomes:
                if isinstance(outcome, Exception):
                    failed.append(task.planner_task_id)
                    await repo.set_task_status(task.id, "failed", error=str(outcome))
                    await audit.record(
                        event_type=EventType.TASK_FAILED,
                        actor_type="agent",
                        actor_id=task.agent_id,
                        run_id=run_id,
                        task_id=task.id,
                        metadata={"error": str(outcome)[:1000]},
                    )
                    continue

                result = outcome.result
                await repo.save_result(
                    run_id=run_id,
                    task_row_id=task.id,
                    result=result,
                    agent_version=outcome.profile.version,
                    gateway_route=outcome.route.agent_id,
                    model_id=outcome.route.model,
                    duration_ms=outcome.usage.duration_ms,
                )
                await repo.set_task_status(
                    task.id, result.status, agent_version=outcome.profile.version
                )
                await audit.record(
                    event_type=EventType.TASK_COMPLETE,
                    actor_type="agent",
                    actor_id=result.agent_id,
                    run_id=run_id,
                    task_id=task.id,
                    agent_version=outcome.profile.version,
                    gateway_route=outcome.route.agent_id,
                    model_id=outcome.route.model,
                    metadata={
                        "status": result.status,
                        "confidence": result.confidence,
                        "findings": len(result.findings),
                        "escalation": result.escalation_required,
                        "evidence_violations": len(outcome.evidence_violations),
                    },
                )
                by_task[result.task_id] = result
                summaries[result.task_id] = summarise_for_downstream(outcome)
                all_results.append(result)
                violations.extend(
                    f"{result.agent_id}/{result.task_id}: {v}" for v in outcome.evidence_violations
                )

    await _progress(
        state,
        "collect_results",
        headline=":inbox_tray: All specialist workstreams returned",
        detail=(
            f"{len(all_results)} result(s) collected"
            + (f", {len(failed)} failed" if failed else "")
            + ". Checking them against each other for contradictions."
        ),
    )
    evidence = sorted({e.evidence_id for r in all_results for e in r.evidence_refs})
    artifacts = sorted({a for r in all_results for a in r.artifacts})

    log.info(
        "dispatch_complete",
        completed=len(all_results),
        failed=len(failed),
        groups=len(groups),
        violations=len(violations),
    )
    return {
        "current_stage": "collect_results",
        "specialist_results": all_results,
        "failed_task_ids": failed,
        "evidence_refs": evidence,
        "artifacts": artifacts,
        "evidence_violations": violations,
    }


async def conflict_detection(state: RunState) -> dict[str, Any]:
    results = state.get("specialist_results") or []
    signals = detect_conflict_signals(results)

    if signals:
        run_id = _run_uuid(state)
        async with session_scope() as s:
            gov, audit = GovernanceRepository(s), AuditRepository(s)
            for signal in signals:
                if signal["kind"] == "declared_conflict":
                    await gov.add_conflict(
                        run_id=run_id,
                        description=signal["description"],
                        agent_ids=[
                            a for a in (signal["source_agent"], signal.get("with_agent")) if a
                        ],
                        severity=signal.get("severity", "medium"),
                    )
            await audit.record(
                event_type=EventType.CONFLICT_DETECTED,
                actor_type="system",
                run_id=run_id,
                metadata={"signals": len(signals)},
            )
    log.info("conflict_detection", signals=len(signals))
    return {"current_stage": "conflict_detection", "conflict_signals": signals}


def route_after_conflicts(state: RunState) -> Literal["challenge", "synthesis"]:
    budget = _budget(state)
    if state.get("challenge_round", 0) > 0:
        return "synthesis"
    if budget.max_followup_rounds <= 0:
        return "synthesis"
    results = state.get("specialist_results") or []
    signals = state.get("conflict_signals") or []
    return "challenge" if needs_challenge(results, signals) else "synthesis"


async def challenge_node(state: RunState) -> dict[str, Any]:
    run_id = _run_uuid(state)
    plan = state["plan"]
    assert plan is not None

    async with session_scope() as s:
        await RunRepository(s).set_stage(run_id, "challenge", status="challenging")
    await _progress(
        state,
        "challenge",
        headline=":mag: Challenging the findings",
        detail="An independent adversarial review is looking for contradictions, "
        "unsupported claims and missing evidence before anything is integrated.",
    )

    outcome = await challenge_results(
        state["user_request"],
        plan,
        state.get("specialist_results") or [],
        state.get("conflict_signals") or [],
        budget=_budget(state),
        idempotency_key=f"{state['run_id']}:challenge",
    )
    result = outcome.result

    async with session_scope() as s:
        audit = AuditRepository(s)
        await audit.record(
            event_type=EventType.CHALLENGE_COMPLETE,
            actor_type="orchestrator",
            run_id=run_id,
            gateway_route=outcome.route.agent_id,
            model_id=outcome.route.model,
            metadata={
                "contradictions": len(result.contradictions),
                "material": sum(1 for c in result.contradictions if c.material),
                "followups": [t.task_id for t in result.follow_up_tasks],
                "dropped": outcome.dropped_followups,
            },
        )
        if result.requires_human_escalation:
            await audit.record(
                event_type=EventType.ESCALATION_RAISED,
                actor_type="orchestrator",
                run_id=run_id,
                metadata={"reason": result.escalation_reason},
            )
    return {"current_stage": "challenge", "challenge": result}


def route_after_challenge(state: RunState) -> Literal["follow_up_tasks", "synthesis"]:
    challenge = state.get("challenge")
    if challenge is None or not challenge.follow_up_tasks:
        return "synthesis"
    if state.get("challenge_round", 0) >= _budget(state).max_followup_rounds:
        return "synthesis"
    return "follow_up_tasks"


async def follow_up_tasks(state: RunState) -> dict[str, Any]:
    """Persist targeted follow-ups as a new execution group (s9.2, max one round)."""
    run_id = _run_uuid(state)
    challenge = state["challenge"]
    assert challenge is not None

    async with session_scope() as s:
        repo = RunRepository(s)
        existing = await repo.list_tasks(run_id)
        next_group = max((t.execution_group for t in existing), default=0) + 1
        await repo.add_followup_tasks(run_id, challenge.follow_up_tasks, next_group)
        await repo.increment_challenge_round(run_id)

    log.info("followups_created", count=len(challenge.follow_up_tasks))
    return {
        "current_stage": "follow_up_tasks",
        "challenge_round": state.get("challenge_round", 0) + 1,
    }


async def synthesis_node(state: RunState) -> dict[str, Any]:
    run_id = _run_uuid(state)
    plan = state["plan"]
    assert plan is not None

    async with session_scope() as s:
        await RunRepository(s).set_stage(run_id, "synthesis", status="synthesizing")
    await _progress(
        state,
        "synthesis",
        headline=":pencil: Integrating the specialist work",
        detail="Writing the executive conclusion, the recommendation and the "
        "confidence assessment.",
    )

    outcome = await synthesize(
        state["user_request"],
        plan,
        state.get("specialist_results") or [],
        challenge=state.get("challenge"),
        failed_task_ids=state.get("failed_task_ids") or [],
        idempotency_key=f"{state['run_id']}:synthesis",
    )

    async with session_scope() as s:
        await AuditRepository(s).record(
            event_type=EventType.SYNTHESIS_COMPLETE,
            actor_type="orchestrator",
            run_id=run_id,
            gateway_route=outcome.route.agent_id,
            model_id=outcome.route.model,
            metadata={
                "confidence": outcome.brief.confidence,
                "review_required": outcome.brief.review_required,
                "decision_class": outcome.brief.highest_decision_class,
            },
        )
    return {"current_stage": "synthesis", "executive_brief": outcome.brief}


async def quality_gate_node(state: RunState) -> dict[str, Any]:
    """Deterministic completeness review before delivery (s8, s14 QC gate)."""
    run_id = _run_uuid(state)
    brief = state["executive_brief"]
    plan = state["plan"]
    assert brief is not None and plan is not None

    async with session_scope() as s:
        known = {e.evidence_ref for e in await EvidenceRepository(s).list_for_run(run_id)}

    gate = run_quality_gate(
        brief,
        plan,
        state.get("specialist_results") or [],
        state.get("challenge"),
        known_evidence_refs=known,
    )

    async with session_scope() as s:
        await AuditRepository(s).record(
            event_type=EventType.QUALITY_GATE,
            actor_type="system",
            run_id=run_id,
            metadata={
                "passed": gate.passed,
                "review_required": gate.review_required,
                "reasons": gate.reasons,
            },
        )
    log.info("quality_gate", passed=gate.passed, reasons=gate.reasons)
    return {"current_stage": "quality_gate", "quality_gate": gate}


def route_after_quality_gate(
    state: RunState,
) -> Literal["human_decision", "persist_final"]:
    """AMBER or RED requires a human before the run may complete (s16)."""
    brief = state.get("executive_brief")
    if brief is None:
        return "persist_final"
    return "human_decision" if brief.highest_decision_class in ("amber", "red") else "persist_final"


async def human_decision(state: RunState) -> dict[str, Any]:
    """Pause for the final AMBER/RED decision (s16, FR-011)."""
    run_id = _run_uuid(state)
    brief = state["executive_brief"]
    assert brief is not None
    decision_class = brief.highest_decision_class

    async with session_scope() as s:
        approval, is_new = await GovernanceRepository(s).request_approval(
            run_id=run_id,
            gate="final_decision",
            decision_class=decision_class,
            requested_payload={
                "recommendation": brief.recommendation,
                "confidence": brief.confidence,
                "decisions": [
                    {
                        "question": d.question,
                        "class": d.decision_class,
                        "options": d.options,
                        "recommendation": d.recommendation,
                    }
                    for d in brief.decisions_required_from_user
                ],
            },
        )
        approval_id = str(approval.id)
        await RunRepository(s).set_stage(run_id, "awaiting_decision", status="awaiting_decision")
        if is_new:
            await AuditRepository(s).record(
                event_type=EventType.APPROVAL_REQUESTED,
                actor_type="orchestrator",
                run_id=run_id,
                metadata={
                    "approval_id": approval_id,
                    "gate": "final_decision",
                    "decision_class": decision_class,
                },
            )

    if is_new:
        await _notify(
            state,
            slack_blocks.decision_request(state.get("run_ref") or "Run", brief, approval_id),
            f"{decision_class.upper()} decision required: {brief.recommendation[:200]}",
        )

    decision = interrupt(
        {
            "type": "final_decision",
            "approval_id": approval_id,
            "run_ref": state.get("run_ref"),
            "decision_class": decision_class,
            "recommendation": brief.recommendation,
            "decisions_required": [
                {
                    "question": d.question,
                    "options": d.options,
                    "recommendation": d.recommendation,
                    "class": d.decision_class,
                }
                for d in brief.decisions_required_from_user
            ],
        }
    )

    status = (decision or {}).get("status", "pending")
    decided_by = (decision or {}).get("decided_by")
    rationale = (decision or {}).get("rationale")

    async with session_scope() as s:
        gov, audit = GovernanceRepository(s), AuditRepository(s)
        await gov.decide_approval(
            uuid.UUID(approval_id),
            status=status,
            decided_by=decided_by or "unknown",
            rationale=rationale,
        )
        if status == "approved":
            await gov.record_decision(
                title=f"Final decision on {state.get('run_ref') or state['run_id']}",
                decision_text=brief.recommendation,
                decision_class=decision_class,
                run_id=run_id,
                project_id=uuid.UUID(state["project_id"]) if state.get("project_id") else None,
                rationale=rationale,
                decided_by=decided_by,
            )
            await audit.record(
                event_type=EventType.DECISION_RECORDED,
                actor_type="user",
                actor_id=decided_by,
                run_id=run_id,
                metadata={"decision_class": decision_class},
            )
        await audit.record(
            event_type=EventType.APPROVAL_DECIDED,
            actor_type="user",
            actor_id=decided_by,
            run_id=run_id,
            metadata={"approval_id": approval_id, "status": status, "gate": "final_decision"},
        )
    return {
        "current_stage": "decision_recorded",
        "approval_status": status,
        "decision_class": decision_class,
    }


async def build_report(state: RunState) -> dict[str, Any]:
    """Compose and render the deliverable (s18, s32).

    The technical report is composed from the run's current work products, not
    from summaries of them, and never from superseded drafts. If composition
    fails the brief-only document still ships: a run that produced real specialist
    work must not end with nothing in the client's hands.
    """
    run_id = _run_uuid(state)
    brief = state.get("executive_brief")
    if brief is None:
        return {"current_stage": "report_skipped"}

    await _progress(
        state,
        "reporting",
        headline=":page_facing_up: Building the report",
        detail="Composing the technical report from the specialists' work products, "
        "then the Executive Decision Brief. This takes a few minutes.",
    )
    project_id = state.get("project_id")
    project_uuid = uuid.UUID(project_id) if project_id else None
    service = get_report_service()

    try:
        outcome = await service.generate_technical_report(
            run_id=run_id,
            run_ref=state.get("run_ref") or str(run_id),
            user_request=state.get("user_request") or "",
            brief=brief,
            results=state.get("specialist_results") or [],
            plan=state.get("plan"),
            challenge=state.get("challenge"),
            agent_names=_agent_names(),
            project_id=project_uuid,
        )
        paths = [str(p) for p in outcome.paths]
        warnings = outcome.warnings
        review_required = outcome.review_required
        summary = {
            "sections": outcome.section_count,
            "words": outcome.word_count,
            "title": outcome.document_title,
        }
    except Exception as exc:  # noqa: BLE001 - the analysis must not be lost
        log.error("report_composition_failed", error=str(exc)[:300])
        async with session_scope() as s:
            await AuditRepository(s).record(
                event_type=EventType.RUN_REVIEW_REQUIRED,
                actor_type="system",
                run_id=run_id,
                metadata={"reason": "report_composition_failed", "error": str(exc)[:300]},
            )
        try:
            result, warnings = await service.generate(
                run_id=run_id,
                run_ref=state.get("run_ref") or str(run_id),
                brief=brief,
                results=state.get("specialist_results") or [],
                agent_names=_agent_names(),
                project_id=project_uuid,
                user_request=state.get("user_request") or "",
            )
        except Exception as fallback_exc:  # noqa: BLE001
            log.error("report_failed", error=str(fallback_exc)[:300])
            return {
                "current_stage": "report_failed",
                "report_error": f"The report could not be generated: {fallback_exc}",
            }
        paths = [str(result.path), *(str(p) for p in result.extra_paths)]
        review_required = True
        warnings = [
            f"The full technical report could not be composed ({exc}). "
            "The Executive Decision Brief was produced instead."
        ] + warnings
        summary = {"sections": 0, "words": 0, "title": None}

    channel = state.get("slack_channel")
    if channel:
        notifier = get_notifier()
        for path in paths:
            await notifier.upload_file(
                channel=channel,
                path=path,
                title=Path(path).name,
                thread_ts=state.get("slack_thread_ts"),
            )
    log.info("report_delivered", files=len(paths), **summary)
    return {
        "current_stage": "report_built",
        "report_paths": paths,
        "report_warnings": warnings,
        "report_error": (
            "The report gate flagged shortfalls; see the report's document control."
            if review_required
            else None
        ),
    }


async def persist_final(state: RunState) -> dict[str, Any]:
    run_id = _run_uuid(state)
    brief = state.get("executive_brief")
    gate = state.get("quality_gate")
    failed = state.get("failed_task_ids") or []
    results = state.get("specialist_results") or []

    if brief is None:
        status, summary = "failed", None
    elif (
        (gate is not None and gate.review_required)
        or brief.review_required
        or failed
        or state.get("report_error")
    ):
        status = "review_required"
        summary = brief.executive_conclusion
    else:
        status = "complete"
        summary = brief.executive_conclusion

    async with session_scope() as s:
        await RunRepository(s).finish(
            run_id,
            status=status,
            final_summary=summary,
            executive_brief=brief.model_dump(mode="json") if brief else None,
        )
        await AuditRepository(s).record(
            event_type=(
                EventType.RUN_COMPLETE
                if status == "complete"
                else EventType.RUN_REVIEW_REQUIRED
                if status == "review_required"
                else EventType.RUN_FAILED
            ),
            actor_type="system",
            run_id=run_id,
            metadata={
                "status": status,
                "specialists": len(results),
                "failed_tasks": failed,
                "quality_gate_reasons": gate.reasons if gate else [],
            },
        )
    await _progress(
        state,
        "finished",
        headline=":checkered_flag: Run complete",
        detail="Delivering the documents to this thread.",
    )

    if brief is not None:
        warnings = state.get("report_warnings") or []
        blocks = slack_blocks.completion_summary(
            state.get("run_ref") or "Run",
            brief,
            evidence_count=len(state.get("evidence_refs") or []),
            specialist_count=len({r.agent_id for r in results}),
            open_gaps=len(brief.information_required),
            # The files themselves are uploaded to the thread; this names them.
            report_filenames=[Path(p).name for p in (state.get("report_paths") or [])],
            key_issue=warnings[0] if warnings else None,
        )
        report_error = state.get("report_error")
        if report_error:
            blocks.insert(1, slack_blocks.section(f":warning: {report_error}"))
        await _notify(
            state,
            blocks,
            f"{state.get('run_ref') or 'Run'}: {brief.executive_conclusion[:200]}",
        )
    else:
        await _notify(
            state,
            slack_blocks.failure(
                state.get("run_ref") or "Run",
                state.get("error_state") or "The run did not produce a result.",
            ),
            "Run failed.",
        )

    log.info("run_finished", status=status, specialists=len(results), failed=len(failed))
    return {"current_stage": "finished", "status": status}


async def finish_failed(state: RunState) -> dict[str, Any]:
    return {"current_stage": "finished", "status": "failed"}


async def finish_cancelled(state: RunState) -> dict[str, Any]:
    run_id = _run_uuid(state)
    async with session_scope() as s:
        await RunRepository(s).finish(
            run_id, status="cancelled", error_state="The plan was not approved."
        )
        await AuditRepository(s).record(
            event_type=EventType.RUN_CANCELLED,
            actor_type="user",
            run_id=run_id,
            metadata={"reason": "plan_not_approved"},
        )
    return {"current_stage": "finished", "status": "cancelled"}


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------


def build_graph() -> StateGraph:
    g = StateGraph(RunState)

    g.add_node("intake", intake)
    g.add_node("load_context", load_project_context)
    g.add_node("plan", plan_node)
    g.add_node("validate_plan", validate_plan_node)
    g.add_node("plan_approval", plan_approval)
    g.add_node("dispatch", dispatch)
    g.add_node("conflict_detection", conflict_detection)
    g.add_node("challenge", challenge_node)
    g.add_node("follow_up_tasks", follow_up_tasks)
    g.add_node("synthesis", synthesis_node)
    g.add_node("quality_gate", quality_gate_node)
    g.add_node("human_decision", human_decision)
    g.add_node("build_report", build_report)
    g.add_node("persist_final", persist_final)
    g.add_node("finish_failed", finish_failed)
    g.add_node("finish_cancelled", finish_cancelled)

    g.add_edge(START, "intake")
    g.add_edge("intake", "load_context")
    g.add_edge("load_context", "plan")
    g.add_edge("plan", "validate_plan")
    g.add_conditional_edges(
        "validate_plan",
        route_after_validation,
        {
            "plan_approval": "plan_approval",
            "dispatch": "dispatch",
            "finish_failed": "finish_failed",
        },
    )
    g.add_conditional_edges(
        "plan_approval",
        route_after_approval,
        {"dispatch": "dispatch", "finish_cancelled": "finish_cancelled"},
    )
    g.add_edge("dispatch", "conflict_detection")
    g.add_conditional_edges(
        "conflict_detection",
        route_after_conflicts,
        {"challenge": "challenge", "synthesis": "synthesis"},
    )
    g.add_conditional_edges(
        "challenge",
        route_after_challenge,
        {"follow_up_tasks": "follow_up_tasks", "synthesis": "synthesis"},
    )
    # Follow-ups re-enter dispatch; the round counter bounds the loop.
    g.add_edge("follow_up_tasks", "dispatch")
    g.add_edge("synthesis", "quality_gate")
    g.add_conditional_edges(
        "quality_gate",
        route_after_quality_gate,
        {"human_decision": "human_decision", "persist_final": "build_report"},
    )
    g.add_edge("human_decision", "build_report")
    g.add_edge("build_report", "persist_final")
    g.add_edge("persist_final", END)
    g.add_edge("finish_failed", END)
    g.add_edge("finish_cancelled", END)
    return g


def compile_graph(checkpointer: Any = None) -> Any:
    return build_graph().compile(checkpointer=checkpointer)
