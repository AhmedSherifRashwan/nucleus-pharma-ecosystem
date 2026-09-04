"""Record a human decision on a run gate and resume it (PRD s16, FR-011).

Called by the OpenClaw approval skill when the named user clicks Approve or
Cancel in Slack, or types the equivalent in the thread.

This script decides nothing itself. It authorises the person, then enqueues a
`resume` job carrying their decision; the graph node that raised the gate is the
one that records it, so the decision and the resumption can never disagree.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from app.audit.logger import configure_logging, get_logger
from app.config import get_settings
from app.storage.db import dispose_engine, session_scope
from app.storage.models import Approval
from app.storage.repositories import (
    AuditRepository,
    EventType,
    JobRepository,
    RunRepository,
)

log = get_logger(__name__)

# A button carries its gate: a stale plan button must never decide a final gate.
ACTIONS = {
    "approve_plan": ("plan", "approved"),
    "cancel_plan": ("plan", "rejected"),
    "accept_decision": ("final_decision", "approved"),
    "reject_decision": ("final_decision", "rejected"),
}

TERMINAL_RUN_STATUSES = {"complete", "failed", "cancelled", "review_required"}


class DecisionRefused(RuntimeError):
    """The decision was not accepted. Never resume on refusal."""


def approver_ids() -> set[str]:
    raw = get_settings().slack_approver_ids or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def authorise(*, user: str | None, run_owner: str | None) -> str:
    """s16: a gate is decided by a named human, not by whoever clicks first."""
    if not user:
        raise DecisionRefused("no Slack user id was supplied with the decision")
    allowed = approver_ids()
    if user in allowed:
        return user
    if run_owner and user == run_owner:
        return user
    raise DecisionRefused(
        f"{user} is not authorised to decide this run. "
        "Add them to SLACK_APPROVER_IDS, or ask the person who started the run."
    )


async def record(
    *, approval_id: str, action: str, user: str | None, note: str | None = None
) -> dict:
    if action not in ACTIONS:
        raise DecisionRefused(f"unknown action {action!r}; expected one of {sorted(ACTIONS)}")
    expected_gate, status = ACTIONS[action]

    try:
        approval_uuid = uuid.UUID(approval_id)
    except ValueError as exc:
        raise DecisionRefused(f"{approval_id!r} is not a valid approval id") from exc

    async with session_scope() as session:
        approval = await session.get(Approval, approval_uuid)
        if approval is None:
            raise DecisionRefused("that approval no longer exists")
        if approval.gate != expected_gate:
            raise DecisionRefused(
                f"that control belongs to the {expected_gate} gate but the approval "
                f"is a {approval.gate} gate"
            )
        if approval.status != "pending":
            # A double-click is not an error; it is the same decision arriving twice.
            return {
                "approval_id": approval_id,
                "status": approval.status,
                "run_id": str(approval.run_id),
                "already_decided": True,
                "resumed": False,
            }

        run = await RunRepository(session).get(approval.run_id)
        if run is None:
            raise DecisionRefused("the run behind that approval no longer exists")
        if run.status in TERMINAL_RUN_STATUSES:
            raise DecisionRefused(f"run {run.run_ref} has already finished ({run.status})")

        decided_by = authorise(user=user, run_owner=run.slack_user_id)

        jobs = JobRepository(session)
        idempotency_key = f"approval:{approval_id}"
        existing = await jobs.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return {
                "approval_id": approval_id,
                "status": status,
                "run_id": str(run.id),
                "run_ref": run.run_ref,
                "already_decided": True,
                "resumed": False,
            }

        await jobs.enqueue(
            run.id,
            job_type="resume",
            payload={"resume": {"status": status, "decided_by": decided_by, "note": note}},
            idempotency_key=idempotency_key,
            max_attempts=get_settings().worker_max_job_attempts,
        )
        await AuditRepository(session).record(
            event_type=EventType.APPROVAL_DECIDED,
            actor_type="user",
            actor_id=decided_by,
            run_id=run.id,
            metadata={
                "approval_id": approval_id,
                "gate": approval.gate,
                "status": status,
                "source": "slack_interaction",
            },
        )
        result = {
            "approval_id": approval_id,
            "status": status,
            "run_id": str(run.id),
            "run_ref": run.run_ref,
            "already_decided": False,
            "resumed": True,
        }

    log.info("decision_recorded", run_ref=result["run_ref"], status=status, gate=expected_gate)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description="Record a Neucleus approval decision.")
    parser.add_argument("--approval-id", required=True, help="the button's value")
    parser.add_argument("--action", required=True, help=f"one of {sorted(ACTIONS)}")
    parser.add_argument("--user", required=True, help="Slack user id of the decider")
    parser.add_argument("--note", help="optional rationale from the user")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level, "json")

    try:
        result = await record(
            approval_id=args.approval_id, action=args.action, user=args.user, note=args.note
        )
    except DecisionRefused as exc:
        # Refusal is a normal outcome; the skill reports the reason verbatim.
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
