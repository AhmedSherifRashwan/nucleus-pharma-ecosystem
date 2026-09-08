"""Report a run's state (PRD s17.1, s31).

Slack learns what a run is doing because the graph posts to it. A run started
anywhere else — the Control UI, a terminal — is silent, and silence is
indistinguishable from failure. This is how any surface asks.

Read-only. It decides nothing and changes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from sqlalchemy import select

from app.agents.registry import get_registry
from app.audit.logger import configure_logging
from app.config import get_settings
from app.storage.db import dispose_engine, session_scope
from app.storage.models import Approval, Artifact, EvidenceItem, Run, RunTask
from app.storage.repositories import GovernanceRepository

TERMINAL = {"complete", "failed", "cancelled", "review_required"}


async def latest_run_ref() -> str | None:
    async with session_scope() as session:
        run = (
            await session.execute(select(Run).order_by(Run.created_at.desc()).limit(1))
        ).scalar_one_or_none()
        return run.run_ref if run else None


async def status(run_ref: str) -> dict:
    names = {p.id: p.name for p in get_registry().all_profiles()}

    async with session_scope() as session:
        run = (
            await session.execute(select(Run).where(Run.run_ref == run_ref))
        ).scalar_one_or_none()
        if run is None:
            raise ValueError(f"no run {run_ref}")

        tasks = (
            (
                await session.execute(
                    select(RunTask)
                    .where(RunTask.run_id == run.id)
                    .order_by(RunTask.execution_group, RunTask.planner_task_id)
                )
            )
            .scalars()
            .all()
        )

        pending = await GovernanceRepository(session).pending_approvals(run.id)
        decided = (
            (
                await session.execute(
                    select(Approval).where(Approval.run_id == run.id, Approval.status != "pending")
                )
            )
            .scalars()
            .all()
        )

        reports = (
            (
                await session.execute(
                    select(Artifact).where(Artifact.run_id == run.id, Artifact.kind == "report")
                )
            )
            .scalars()
            .all()
        )
        evidence = (
            (await session.execute(select(EvidenceItem).where(EvidenceItem.run_id == run.id)))
            .scalars()
            .all()
        )

        started = run.created_at
        finished = run.completed_at if hasattr(run, "completed_at") else None
        brief = run.executive_brief

    elapsed = int(((finished or datetime.now(UTC)) - started).total_seconds())
    done = [t for t in tasks if t.status not in ("pending", "ready", "running")]

    result: dict = {
        "run_ref": run.run_ref,
        "status": run.status,
        "stage": run.current_stage,
        "mode": run.mode,
        "elapsed_seconds": elapsed,
        "finished": run.status in TERMINAL,
        "specialists": {
            "total": len(tasks),
            "returned": len(done),
            "tasks": [
                {
                    "task": t.planner_task_id,
                    "agent": t.agent_id,
                    "name": names.get(t.agent_id, t.agent_id),
                    "status": t.status,
                    "group": t.execution_group,
                    **({"error": t.error[:200]} if t.error else {}),
                }
                for t in tasks
            ],
        },
        "evidence_items": len(evidence),
        "reports": [
            {"ref": a.artifact_ref, "version": a.version, "path": a.storage_path} for a in reports
        ],
    }

    if pending:
        gate = pending[0]
        result["awaiting_decision"] = {
            "approval_id": str(gate.id),
            "gate": gate.gate,
            "decision_class": gate.decision_class,
            "payload": gate.requested_payload,
            "how_to_answer": (
                "uv run python scripts/record_decision.py "
                f"--approval-id {gate.id} "
                f"--action {'approve_plan' if gate.gate == 'plan' else 'accept_decision'} "
                "--user operator"
            ),
        }
    if decided:
        result["decisions"] = [
            {"gate": a.gate, "status": a.status, "by": a.decided_by} for a in decided
        ]

    if run.status in TERMINAL and brief:
        result["outcome"] = {
            "confidence": brief.get("confidence"),
            "review_required": brief.get("review_required"),
            "review_reason": brief.get("review_reason"),
            "conclusion": brief.get("executive_conclusion"),
            "recommendation": brief.get("recommendation"),
            "information_required": brief.get("information_required", [])[:5],
        }
    if run.error_state:
        result["error"] = run.error_state[:500]
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="Report the state of a Nucleus run.")
    parser.add_argument("--run-ref", help="e.g. RUN-2026-0009")
    parser.add_argument("--latest", action="store_true", help="the most recent run")
    parser.add_argument(
        "--wait",
        type=int,
        default=0,
        metavar="SECONDS",
        help="poll until the run finishes or needs a decision, up to this long",
    )
    args = parser.parse_args()

    configure_logging(get_settings().log_level, "console")
    try:
        run_ref = args.run_ref
        if not run_ref or args.latest:
            run_ref = await latest_run_ref()
            if not run_ref:
                raise ValueError("no runs yet")

        deadline = asyncio.get_running_loop().time() + args.wait
        while True:
            snapshot = await status(run_ref)
            # Stop for anything the caller has to act on, not just completion.
            if (
                snapshot["finished"]
                or "awaiting_decision" in snapshot
                or asyncio.get_running_loop().time() >= deadline
            ):
                break
            await asyncio.sleep(15)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    print(json.dumps(snapshot, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
