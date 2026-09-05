"""Create and enqueue a Neucleus run (PRD s30, s31.2, Phase 9).

Called by the OpenClaw pharma-ecosystem skill. Deliberately narrow: validate the
request envelope, map the Slack thread to a project, create the run and job, and
print the reference. It performs no analysis and holds no orchestration logic.

Exits non-zero on failure so the skill can report the error rather than pretend
a run started.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.agents.registry import get_registry
from app.audit.logger import configure_logging, get_logger
from app.config import get_settings
from app.orchestration.modes import detect_mode
from app.reports.naming import subject_from_request
from app.storage.db import dispose_engine, session_scope
from app.storage.repositories import (
    AuditRepository,
    EventType,
    JobRepository,
    ProjectRepository,
    RunRepository,
)

log = get_logger(__name__)

MIN_REQUEST_CHARS = 5
MAX_REQUEST_CHARS = 20_000


def default_budgets() -> dict:
    s = get_settings()
    return {
        "max_specialists": s.default_max_specialists,
        "max_parallel_specialists": s.default_max_parallel_specialists,
        "max_followup_rounds": s.default_max_followup_rounds,
        "max_specialist_steps": s.default_max_specialist_steps,
        "max_runtime_seconds": s.default_max_runtime_seconds,
    }


def project_name_for(channel: str, thread: str, request: str) -> str:
    """Name the project after its subject.

    The thread identifies the project; it does not describe it, and the name
    reaches the client on the report's filename (s18.2).
    """
    return subject_from_request(request) or f"Slack {channel}/{thread}"


async def enqueue(
    *,
    request: str,
    channel: str | None,
    thread: str | None,
    user: str | None,
    event_id: str | None,
    output_format: str = "brief",
    mode: str | None = None,
    agent: str | None = None,
) -> dict:
    request = (request or "").strip()
    if len(request) > MAX_REQUEST_CHARS:
        raise ValueError(f"request exceeds {MAX_REQUEST_CHARS} characters")

    # s9.1: how much of the ecosystem the user asked for, read from their words.
    registry = get_registry()
    decision = detect_mode(
        request,
        known_ids={p.id for p in registry.enabled_profiles()},
        slug_to_id={p.slug: p.id for p in registry.enabled_profiles()},
        explicit_mode=mode,  # type: ignore[arg-type]
        explicit_agent=agent,
    )
    # The routing prefix is not the task, so length is judged on what remains.
    if decision.is_direct and len(decision.request) < MIN_REQUEST_CHARS:
        raise ValueError(f"a direct request to {decision.agent_id} needs a task after the command")
    request = decision.request or request
    if len(request) < MIN_REQUEST_CHARS:
        raise ValueError("request is too short to plan against")

    # s31.2: Slack retries events. Without a key, one message becomes two runs.
    idempotency_key = f"slack:{event_id}" if event_id else None

    async with session_scope() as session:
        runs = RunRepository(session)

        if idempotency_key:
            existing = await runs.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return {
                    "run_id": str(existing.id),
                    "run_ref": existing.run_ref,
                    "status": existing.status,
                    "deduplicated": True,
                }

        project_id = None
        if channel and thread:
            project, _ = await ProjectRepository(session).get_or_create_for_thread(
                slack_channel_id=channel,
                thread_ts=thread,
                name=project_name_for(channel, thread, request),
            )
            project_id = project.id

        run = await runs.create(
            user_request=request,
            project_id=project_id,
            slack_channel_id=channel,
            slack_thread_ts=thread,
            slack_user_id=user,
            idempotency_key=idempotency_key,
            requested_output_format=output_format,
            budgets=default_budgets(),
            mode=decision.mode,
            requested_agents=[decision.agent_id] if decision.agent_id else [],
        )
        await JobRepository(session).enqueue(
            run.id,
            job_type="orchestrate",
            idempotency_key=idempotency_key,
            max_attempts=get_settings().worker_max_job_attempts,
        )
        await AuditRepository(session).record(
            event_type=EventType.RUN_CREATED,
            actor_type="user",
            actor_id=user,
            run_id=run.id,
            project_id=project_id,
            metadata={
                "channel": channel,
                "thread": thread,
                "chars": len(request),
                "mode": decision.mode,
                "agents": [decision.agent_id] if decision.agent_id else [],
                "mode_matched_on": decision.matched_on,
            },
        )
        result = {
            "run_id": str(run.id),
            "run_ref": run.run_ref,
            "status": run.status,
            "deduplicated": False,
            "mode": decision.mode,
            "agent": decision.agent_id,
        }

    log.info("run_enqueued", run_ref=result["run_ref"], channel=channel, mode=decision.mode)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="Enqueue a Neucleus run.")
    parser.add_argument("--request", required=True, help="the user's request, verbatim")
    parser.add_argument("--channel", help="Slack channel id")
    parser.add_argument("--thread", help="Slack thread timestamp")
    parser.add_argument("--user", help="Slack user id")
    parser.add_argument("--event-id", help="Slack event id, used for deduplication")
    parser.add_argument(
        "--output-format", default="brief", choices=["report", "brief", "analysis", "comparison"]
    )
    parser.add_argument(
        "--mode",
        choices=["standard", "full", "direct"],
        help="override the mode read from the request",
    )
    parser.add_argument("--agent", help="agent id for a direct request, e.g. A07")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, "json")

    try:
        result = await enqueue(
            request=args.request,
            channel=args.channel,
            thread=args.thread,
            user=args.user,
            event_id=args.event_id,
            output_format=args.output_format,
            mode=args.mode,
            agent=args.agent,
        )
    except Exception as exc:
        # The skill must report a failure, never fabricate a run reference.
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
