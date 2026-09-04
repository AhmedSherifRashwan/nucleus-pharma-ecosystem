"""Tool invocation context and authorisation (PRD s19, decisions.md D-013).

A tool is invoked by a skill script running inside an OpenClaw agent turn, in a
separate process from the orchestrator. It therefore states which run and task it
belongs to, and this module VERIFIES that claim against the database before any
write happens.

The specialist cannot widen its own scope: the task must exist, belong to the
named run, be assigned to the named agent, and the run must still be active.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.agents.registry import get_registry
from app.storage.db import session_scope
from app.storage.models import Run, RunTask

# A run in one of these is finished; nothing may still be written to it.
TERMINAL_RUN_STATUSES = frozenset({"complete", "failed", "cancelled"})


class ToolAuthorisationError(RuntimeError):
    """The claimed context is not valid, so the call is refused."""


@dataclass(frozen=True)
class ToolContext:
    run_id: uuid.UUID
    run_ref: str
    task_row_id: uuid.UUID
    planner_task_id: str
    agent_id: str
    project_id: uuid.UUID | None
    allowed_tools: tuple[str, ...]
    knowledge_scopes: tuple[str, ...]

    def require_tool(self, tool_name: str) -> None:
        """s19: a specialist never gains a tool another specialist has."""
        if tool_name not in self.allowed_tools:
            raise ToolAuthorisationError(
                f"{self.agent_id} is not permitted to use '{tool_name}'. "
                f"Its profile allows: {', '.join(self.allowed_tools) or 'none'}."
            )


def parse_context_arg(value: str) -> tuple[str, str]:
    """Split the `--context RUN-REF:TASK-ID` argument."""
    if not value or ":" not in value:
        raise ToolAuthorisationError("--context must look like RUN-2026-0042:T1")
    run_ref, _, task_id = value.partition(":")
    if not run_ref.strip() or not task_id.strip():
        raise ToolAuthorisationError("--context must supply both a run ref and a task id")
    return run_ref.strip(), task_id.strip()


async def resolve_context(context_arg: str) -> ToolContext:
    """Verify a claimed context against the record, and return it resolved."""
    run_ref, planner_task_id = parse_context_arg(context_arg)

    async with session_scope() as session:
        run = (
            await session.execute(select(Run).where(Run.run_ref == run_ref))
        ).scalar_one_or_none()
        if run is None:
            raise ToolAuthorisationError(f"unknown run '{run_ref}'")
        if run.status in TERMINAL_RUN_STATUSES:
            raise ToolAuthorisationError(
                f"run {run_ref} is {run.status}; it can no longer be written to"
            )

        task = (
            (
                await session.execute(
                    select(RunTask).where(
                        RunTask.run_id == run.id,
                        RunTask.planner_task_id == planner_task_id,
                    )
                )
            )
            .scalars()
            .first()
        )
        if task is None:
            raise ToolAuthorisationError(f"run {run_ref} has no task '{planner_task_id}'")

        run_id, run_ref_value, project_id = run.id, run.run_ref, run.project_id
        task_row_id, agent_id = task.id, task.agent_id

    registry = get_registry()
    if not registry.exists(agent_id):
        raise ToolAuthorisationError(f"task is assigned to unknown agent {agent_id}")
    profile = registry.get(agent_id)

    return ToolContext(
        run_id=run_id,
        run_ref=run_ref_value,
        task_row_id=task_row_id,
        planner_task_id=planner_task_id,
        agent_id=agent_id,
        project_id=project_id,
        allowed_tools=tuple(profile.allowed_tools),
        knowledge_scopes=tuple(profile.knowledge_scopes),
    )
