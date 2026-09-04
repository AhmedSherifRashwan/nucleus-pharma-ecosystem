"""Deterministic conflict detection (PRD s8 CONFLICT_DETECTION, s14).

Runs before the LLM challenge stage so a clean run can skip it, and so the
signals are computed from the record rather than from a model's opinion.
"""

from __future__ import annotations

from typing import Any

from app.schemas.specialist import SpecialistResult


def detect_conflict_signals(results: list[SpecialistResult]) -> list[dict[str, Any]]:
    """Structural signals that something needs adversarial review.

    These are facts about the results, not judgements: a specialist said it
    conflicts, escalated, did not complete, or asserted something unevidenced.
    """
    signals: list[dict[str, Any]] = []

    for r in results:
        for c in r.conflicts_or_questions:
            signals.append(
                {
                    "kind": "declared_conflict",
                    "source_agent": r.agent_id,
                    "task_id": r.task_id,
                    "description": c.description,
                    "with_agent": c.conflicting_with_agent,
                    "severity": c.severity,
                }
            )

        if r.escalation_required:
            signals.append(
                {
                    "kind": "escalation",
                    "source_agent": r.agent_id,
                    "task_id": r.task_id,
                    "description": r.escalation_reason or "escalation raised",
                    "severity": "high",
                }
            )

        if r.status != "complete":
            signals.append(
                {
                    "kind": "incomplete",
                    "source_agent": r.agent_id,
                    "task_id": r.task_id,
                    "description": f"task status is '{r.status}'",
                    "severity": "medium",
                }
            )

        unsupported = r.unsupported_findings()
        if unsupported:
            signals.append(
                {
                    "kind": "unsupported_claim",
                    "source_agent": r.agent_id,
                    "task_id": r.task_id,
                    "description": (
                        f"{len(unsupported)} finding(s) assert fact or inference without evidence"
                    ),
                    "severity": "high",
                }
            )

        if r.confidence in ("low", "speculative"):
            signals.append(
                {
                    "kind": "low_confidence",
                    "source_agent": r.agent_id,
                    "task_id": r.task_id,
                    "description": f"specialist confidence is '{r.confidence}'",
                    "severity": "medium",
                }
            )

    return signals


def material_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [s for s in signals if s.get("severity") in ("high", "critical")]


def needs_challenge(results: list[SpecialistResult], signals: list[dict[str, Any]]) -> bool:
    """Whether to run the adversarial review.

    Even a clean result set gets one pass: unsupported claims and confidence
    inflation do not announce themselves as conflicts (s9.2).
    """
    return bool(results)
