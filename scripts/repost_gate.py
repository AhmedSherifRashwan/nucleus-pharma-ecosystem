"""Re-post a pending approval gate into its Slack thread (PRD s16, s17).

A gate message can be lost — a client deletes it, or another app replaces it
through the interaction response URL. The approval is still pending in the
database, so the run is recoverable: this re-renders the same gate, carrying the
same approval id, rather than restarting the run.

It refuses to invent a gate: only a pending approval on a live run is re-posted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.agents.registry import get_registry
from app.audit.logger import configure_logging, get_logger
from app.config import get_settings
from app.schemas.executive import ExecutiveDecisionBrief
from app.schemas.planning import Plan
from app.slack import blocks as slack_blocks
from app.slack.notifier import get_notifier
from app.storage.db import dispose_engine, session_scope
from app.storage.repositories import GovernanceRepository, RunRepository

log = get_logger(__name__)


async def repost(run_ref: str) -> dict:
    async with session_scope() as session:
        run = await RunRepository(session).get_by_ref(run_ref)
        if run is None:
            raise ValueError(f"no run {run_ref}")
        if not run.slack_channel_id:
            raise ValueError(f"{run_ref} did not come from Slack")
        pending = await GovernanceRepository(session).pending_approvals(run.id)
        if not pending:
            raise ValueError(f"{run_ref} has no pending approval ({run.status})")
        approval = pending[0]
        plan_data = run.plan
        brief_data = run.executive_brief

    names = {p.id: p.name for p in get_registry().all_profiles()}
    approval_id = str(approval.id)

    if approval.gate == "plan":
        if not plan_data:
            raise ValueError(f"{run_ref} has no stored plan to show")
        plan = Plan.model_validate(plan_data)
        blocks = slack_blocks.plan_preview(run.run_ref, plan, names, approval_id)
        fallback = f"Plan ready for approval: {plan.objective}"
    elif approval.gate == "final_decision":
        if not brief_data:
            raise ValueError(f"{run_ref} has no stored brief to show")
        brief = ExecutiveDecisionBrief.model_validate(brief_data)
        blocks = slack_blocks.decision_request(run.run_ref, brief, approval_id)
        fallback = f"Decision required: {brief.recommendation[:150]}"
    else:
        raise ValueError(f"cannot re-post a {approval.gate} gate")

    ts = await get_notifier().post(
        channel=run.slack_channel_id,
        blocks=blocks,
        text=fallback,
        thread_ts=run.slack_thread_ts,
    )
    if ts is None:
        raise RuntimeError("Slack rejected the message; nothing was posted")

    log.info("gate_reposted", run_ref=run.run_ref, gate=approval.gate, ts=ts)
    return {"run_ref": run.run_ref, "gate": approval.gate, "approval_id": approval_id, "ts": ts}


async def main() -> int:
    parser = argparse.ArgumentParser(description="Re-post a pending Neucleus approval gate.")
    parser.add_argument("--run-ref", required=True)
    args = parser.parse_args()

    configure_logging(get_settings().log_level, "console")
    try:
        result = await repost(args.run_ref)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
