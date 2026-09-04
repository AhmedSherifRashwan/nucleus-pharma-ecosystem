"""Graph execution with durable Postgres checkpointing (PRD s8.2, s14.1)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command

from app.audit.logger import get_logger
from app.config import get_settings
from app.orchestration.graph import compile_graph

log = get_logger(__name__)

# The graph loops through dispatch at most once per follow-up round; this bounds
# any unexpected cycle well below an infinite loop.
DEFAULT_RECURSION_LIMIT = 50


@asynccontextmanager
async def orchestration_app() -> AsyncIterator[Any]:
    """Compile the graph with a Postgres checkpointer for durable state."""
    url = get_settings().langgraph_database_url
    async with AsyncPostgresSaver.from_conn_string(url) as checkpointer:
        yield compile_graph(checkpointer)


async def setup_checkpointer() -> None:
    """Create LangGraph's checkpoint tables. Idempotent; run once at deploy."""
    url = get_settings().langgraph_database_url
    async with AsyncPostgresSaver.from_conn_string(url) as checkpointer:
        await checkpointer.setup()
    log.info("langgraph_checkpointer_ready")


def thread_config(
    run_id: uuid.UUID | str, *, recursion_limit: int = DEFAULT_RECURSION_LIMIT
) -> dict[str, Any]:
    """One LangGraph thread per run, so a run resumes after a restart (FR-016)."""
    return {
        "configurable": {"thread_id": str(run_id)},
        "recursion_limit": recursion_limit,
    }


async def execute_run(
    app: Any, initial_state: dict[str, Any], run_id: uuid.UUID | str
) -> dict[str, Any]:
    return await app.ainvoke(initial_state, config=thread_config(run_id))


def awaiting_interrupt(state: Any) -> bool:
    """True when the graph is genuinely paused on a human gate."""
    if getattr(state, "interrupts", None):
        return True
    return any(getattr(task, "interrupts", None) for task in getattr(state, "tasks", ()) or ())


async def resume_run(app: Any, run_id: uuid.UUID | str, decision: dict[str, Any]) -> dict[str, Any]:
    """Resume from a checkpoint.

    Normally the graph is paused on an approval interrupt and the decision is
    injected. If it is not paused - a crash after the gate, say - continue from
    the last checkpoint instead, so a transient failure does not strand the run
    or re-run completed specialists.
    """
    config = thread_config(run_id)
    state = await app.aget_state(config)

    if awaiting_interrupt(state):
        log.info("resume_from_interrupt", run_id=str(run_id))
        return await app.ainvoke(Command(resume=decision), config=config)

    log.info(
        "resume_from_checkpoint",
        run_id=str(run_id),
        next_nodes=list(getattr(state, "next", ()) or ()),
    )
    return await app.ainvoke(None, config=config)


async def get_run_state(app: Any, run_id: uuid.UUID | str) -> Any:
    return await app.aget_state(thread_config(run_id))
