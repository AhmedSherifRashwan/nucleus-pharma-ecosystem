"""Slack Block Kit rendering for Neucleus (PRD s17).

One bot identity. The ecosystem presents as a single assistant that can show
which specialists it consulted, never as sixteen bots.
"""

from __future__ import annotations

from typing import Any

from app.schemas.executive import ExecutiveDecisionBrief
from app.schemas.planning import Plan

MAX_SECTION_CHARS = 2900  # Block Kit section limit is 3000.

STATUS_ICON = {
    "complete": ":white_check_mark:",
    "running": ":hourglass_flowing_sand:",
    "pending": ":black_small_square:",
    "ready": ":black_small_square:",
    "failed": ":x:",
    "blocked": ":no_entry:",
    "needs_information": ":question:",
}

CONFIDENCE_LABEL = {
    "very_high": "Very high",
    "high": "High",
    "medium": "Medium",
    "low": "Low",
    "speculative": "Speculative",
}

DISCLAIMER = (
    "_Neucleus produces AI-generated analysis to support a human decision. "
    "It is not a regulatory approval, GMP release, validated state, compliance "
    "sign-off or legal advice._"
)


def _truncate(text: str, limit: int = MAX_SECTION_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 40].rstrip() + "\n\n_…truncated._"


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text)}}


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _truncate(text, 400)}]}


def acknowledgement(run_ref: str) -> list[dict[str, Any]]:
    return [
        section(
            f"*{run_ref}* — request received.\n"
            "Planning the specialist workstreams. Progress will appear here."
        )
    ]


def plan_preview(
    run_ref: str, plan: Plan, agent_names: dict[str, str], approval_id: str | None
) -> list[dict[str, Any]]:
    consulted = "\n".join(f"• {agent_names.get(a, a)}" for a in plan.selected_agents)
    parallel = sum(1 for t in plan.tasks if not t.depends_on)

    blocks: list[dict[str, Any]] = [
        section(f"*{run_ref} — proposed plan*\n{plan.objective}"),
        section(f"Specialists to consult:\n{consulted}"),
        context(
            f"{len(plan.tasks)} workstreams · {parallel} running in parallel first "
            f"· output: {plan.output_type}"
        ),
    ]
    if plan.information_gaps:
        gaps = "\n".join(f"• {g}" for g in plan.information_gaps[:5])
        blocks.append(section(f"*Information gaps identified*\n{gaps}"))
    if approval_id:
        if plan.approval_rationale:
            blocks.append(section(f":lock: *Approval required*\n{plan.approval_rationale}"))
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve plan"},
                        "style": "primary",
                        "action_id": "approve_plan",
                        "value": approval_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "style": "danger",
                        "action_id": "cancel_plan",
                        "value": approval_id,
                    },
                ],
            }
        )
    return blocks


def progress(
    run_ref: str, tasks: list[dict[str, Any]], stage: str, elapsed: str
) -> list[dict[str, Any]]:
    lines = [f"{STATUS_ICON.get(t['status'], ':grey_question:')} {t['agent_name']}" for t in tasks]
    return [
        section(f"*{run_ref} — in progress*\n" + "\n".join(lines)),
        context(f"Elapsed: {elapsed} · Stage: {stage}"),
    ]


def executive_brief(
    run_ref: str,
    brief: ExecutiveDecisionBrief,
    agent_names: dict[str, str],
    *,
    evidence_count: int = 0,
    report_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    header = f"*{run_ref} — REVIEW REQUIRED*" if brief.review_required else f"*{run_ref} complete*"
    blocks: list[dict[str, Any]] = [section(header)]

    if brief.review_required and brief.review_reason:
        blocks.append(section(f":warning: {brief.review_reason}"))

    blocks.append(section(f"*Conclusion*\n{brief.executive_conclusion}"))
    blocks.append(
        section(
            f"*Recommendation*\n{brief.recommendation}\n\n"
            f"_Confidence: {CONFIDENCE_LABEL.get(brief.confidence, brief.confidence)}_"
        )
    )

    if brief.key_findings:
        items = "\n".join(f"{i}. {f}" for i, f in enumerate(brief.key_findings[:5], 1))
        blocks.append(section(f"*Key findings*\n{items}"))

    if brief.key_risks:
        blocks.append(section("*Key risks*\n" + "\n".join(f"• {r}" for r in brief.key_risks[:5])))

    # s9.3: disagreement must survive into the delivered output.
    if brief.agent_disagreements:
        blocks.append(
            section(
                "*Unresolved specialist disagreement*\n"
                + "\n".join(f"• {d}" for d in brief.agent_disagreements[:5])
            )
        )

    if brief.critical_assumptions:
        blocks.append(
            section(
                "*Critical assumptions*\n"
                + "\n".join(f"• {a}" for a in brief.critical_assumptions[:5])
            )
        )

    if brief.information_required:
        blocks.append(
            section(
                "*Information still required*\n"
                + "\n".join(f"• {i}" for i in brief.information_required[:5])
            )
        )

    if brief.recommended_actions:
        items = "\n".join(f"{i}. {a}" for i, a in enumerate(brief.recommended_actions[:5], 1))
        blocks.append(section(f"*Recommended actions*\n{items}"))

    if brief.decisions_required_from_user:
        items = "\n".join(
            f"• [{d.decision_class.upper()}] {d.question}"
            for d in brief.decisions_required_from_user[:5]
        )
        blocks.append(section(f"*Decisions required from you*\n{items}"))

    if report_paths:
        blocks.append(section("*Report*\n" + "\n".join(f"• `{p}`" for p in report_paths)))

    consulted = ", ".join(agent_names.get(a, a) for a in brief.consulted_agents)
    blocks.append(context(f"Consulted: {consulted or 'none'} · Evidence items: {evidence_count}"))
    blocks.append(context(DISCLAIMER))
    return blocks


def decision_request(
    run_ref: str, brief: ExecutiveDecisionBrief, approval_id: str
) -> list[dict[str, Any]]:
    """The AMBER/RED gate (s16). The system recommends; the human decides."""
    cls = brief.highest_decision_class
    questions = "\n".join(
        f"• {d.question}" + (f"\n   _Recommended: {d.recommendation}_" if d.recommendation else "")
        for d in brief.decisions_required_from_user[:5]
    )
    return [
        section(f":lock: *{run_ref} — {cls.upper()} decision required*\n{brief.recommendation}"),
        section(f"*Decisions needed from you*\n{questions}"),
        context(
            "Neucleus has prepared this recommendation but will not act on it. "
            "A named human must decide."
        ),
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Accept recommendation"},
                    "style": "primary",
                    "action_id": "accept_decision",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject"},
                    "style": "danger",
                    "action_id": "reject_decision",
                    "value": approval_id,
                },
            ],
        },
    ]


def failure(run_ref: str, message: str) -> list[dict[str, Any]]:
    return [
        section(f":x: *{run_ref} could not complete*\n{message}"),
        context("No partial conclusion is offered for a failed run."),
    ]
