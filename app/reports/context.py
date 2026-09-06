"""The controlled context the report composer is allowed to see (PRD s18, s32).

Two rules shape this module.

Only current material informs the report. A superseded draft is loaded, but into
`superseded_work_products`, which reaches the audit appendix and the revision
crosswalk - never the body. That is what stops a corrected error from appearing
in the report as though it still stood.

The composer sees the specialists' actual work, not summaries of it. The depth a
reader wants lives in the artifacts; passing only `executive_summary` is what made
earlier reports thin. Content is bounded per artifact and in total so a very long
run cannot silently overflow the model's context - truncation is marked in the
text so nobody mistakes a cut for the end of a document.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.registry import get_registry
from app.audit.logger import get_logger
from app.schemas.executive import ChallengeResult, ExecutiveDecisionBrief
from app.schemas.planning import Plan
from app.schemas.specialist import SpecialistResult
from app.storage.db import session_scope
from app.storage.models import Artifact
from app.storage.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    GovernanceRepository,
    RunRepository,
)

log = get_logger(__name__)

MAX_ARTIFACT_CHARS = 60_000
MAX_TOTAL_ARTIFACT_CHARS = 320_000
TRUNCATION_NOTE = "\n\n[... truncated for context; the full work product is stored ...]"


@dataclass
class LoadedArtifact:
    artifact_ref: str
    title: str
    agent_id: str | None
    status: str
    content: str
    truncated: bool = False
    size_bytes: int | None = None
    superseded_by_ref: str | None = None

    def as_prompt_block(self) -> str:
        head = f"### {self.artifact_ref} — {self.title}"
        if self.agent_id:
            head += f" (produced by {self.agent_id})"
        return f"{head}\n\n{self.content}"


@dataclass
class FinalReportContext:
    """Everything the composer may use, and nothing it may not."""

    run_id: uuid.UUID
    run_ref: str
    user_request: str
    plan: Plan | None = None
    brief: ExecutiveDecisionBrief | None = None
    results: list[SpecialistResult] = field(default_factory=list)
    agent_names: dict[str, str] = field(default_factory=dict)
    current_work_products: list[LoadedArtifact] = field(default_factory=list)
    superseded_work_products: list[LoadedArtifact] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    challenge: ChallengeResult | None = None
    human_decisions: list[dict[str, Any]] = field(default_factory=list)
    project_name: str | None = None
    # Agents whose scope comes from the client's role table rather than a
    # specification written for that one role (s11). A reader deciding how much
    # weight to give a contribution needs to know which.
    framework_scoped_agents: dict[str, str] = field(default_factory=dict)

    @property
    def evidence_refs(self) -> set[str]:
        return {str(e["evidence_ref"]) for e in self.evidence}

    @property
    def available_chars(self) -> int:
        return sum(len(a.content) for a in self.current_work_products)

    def work_product_digest(self) -> str:
        """The current work products, concatenated for the composer."""
        if not self.current_work_products:
            return "(No specialist work products were saved for this run.)"
        return "\n\n---\n\n".join(a.as_prompt_block() for a in self.current_work_products)

    def evidence_digest(self) -> list[dict[str, Any]]:
        return self.evidence

    def superseded_refs(self) -> list[str]:
        return [a.artifact_ref for a in self.superseded_work_products]


def _load_file(artifact: Artifact, budget: int) -> tuple[str, bool]:
    path = artifact.storage_path
    if not path:
        return "(no stored content)", False
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("artifact_unreadable", artifact_ref=artifact.artifact_ref, error=str(exc)[:120])
        return f"(content unavailable: {exc})", False
    limit = min(MAX_ARTIFACT_CHARS, budget)
    if len(text) > limit:
        return text[:limit] + TRUNCATION_NOTE, True
    return text, False


def _to_loaded(artifact: Artifact, budget: int) -> LoadedArtifact:
    content, truncated = _load_file(artifact, budget)
    return LoadedArtifact(
        artifact_ref=artifact.artifact_ref,
        title=artifact.title,
        agent_id=artifact.created_by_agent,
        status=artifact.status,
        content=content,
        truncated=truncated,
        size_bytes=artifact.size_bytes,
        superseded_by_ref=artifact.superseded_by_ref,
    )


async def build_context(
    *,
    run_id: uuid.UUID,
    run_ref: str,
    user_request: str,
    plan: Plan | None,
    brief: ExecutiveDecisionBrief | None,
    results: list[SpecialistResult],
    agent_names: dict[str, str],
    challenge: ChallengeResult | None = None,
) -> FinalReportContext:
    async with session_scope() as session:
        artifacts = ArtifactRepository(session)
        current_rows = await artifacts.current_work_products(run_id)
        superseded_rows = await artifacts.superseded_work_products(run_id)

        evidence = [
            {
                "evidence_ref": e.evidence_ref,
                "reliability_level": e.reliability_level,
                "source_type": e.source_type,
                "source_title": e.source_title,
                "source_url_or_document_id": e.source_url_or_document_id,
                "excerpt_or_locator": e.excerpt_or_locator,
                "retrieved_at": e.retrieved_at.strftime("%Y-%m-%d") if e.retrieved_at else None,
                "originating_agent": e.originating_agent,
                "notes": e.notes,
            }
            for e in await EvidenceRepository(session).list_for_run(run_id)
        ]

        governance = GovernanceRepository(session)
        conflicts = [
            {
                "description": c.description,
                "agents": c.agent_ids,
                "severity": c.severity,
                "material": c.material,
                "state": c.state,
                "resolution": c.resolution,
            }
            for c in await governance.list_conflicts(run_id)
        ]
        risks = [
            {
                "description": r.description,
                "likelihood": r.likelihood,
                "impact": r.impact,
                "mitigation": r.mitigation,
                "originating_agent": r.originating_agent,
            }
            for r in await governance.list_risks(run_id)
        ]
        assumptions = [
            {"statement": a.statement, "state": a.state, "agent": a.originating_agent}
            for a in await governance.list_assumptions(run_id)
        ]
        decisions = [
            {
                "gate": a.gate,
                "decision_class": a.decision_class,
                "status": a.status,
                "decided_by": a.decided_by,
                "payload": a.requested_payload,
            }
            for a in await governance.pending_approvals(run_id)
        ]
        run = await RunRepository(session).get(run_id)
        project_name = None
        if run is not None and run.project_id is not None:
            from app.storage.repositories import ProjectRepository

            project = await ProjectRepository(session).get(run.project_id)
            project_name = project.name if project else None

    budget = MAX_TOTAL_ARTIFACT_CHARS
    current: list[LoadedArtifact] = []
    for row in current_rows:
        loaded = _to_loaded(row, budget)
        budget -= len(loaded.content)
        current.append(loaded)
        if budget <= 0:
            log.warning("artifact_budget_exhausted", loaded=len(current), total=len(current_rows))
            break

    # Superseded drafts are for the crosswalk and the audit appendix: their titles
    # and status matter, their full text does not.
    superseded = [
        LoadedArtifact(
            artifact_ref=row.artifact_ref,
            title=row.title,
            agent_id=row.created_by_agent,
            status=row.status,
            content="",
            size_bytes=row.size_bytes,
            superseded_by_ref=row.superseded_by_ref,
        )
        for row in superseded_rows
    ]

    registry = get_registry()
    contributors = {r.agent_id for r in results}
    framework_scoped = {
        p.id: p.name
        for p in registry.all_profiles()
        if p.id in contributors and p.source_basis != "dedicated_sds"
    }

    log.info(
        "report_context_built",
        run_ref=run_ref,
        current_artifacts=len(current),
        superseded_artifacts=len(superseded),
        evidence=len(evidence),
        chars=sum(len(a.content) for a in current),
    )
    return FinalReportContext(
        run_id=run_id,
        run_ref=run_ref,
        user_request=user_request,
        plan=plan,
        brief=brief,
        results=results,
        agent_names=agent_names,
        current_work_products=current,
        superseded_work_products=superseded,
        evidence=evidence,
        conflicts=conflicts,
        risks=risks,
        assumptions=assumptions,
        challenge=challenge,
        human_decisions=decisions,
        project_name=project_name,
        framework_scoped_agents=framework_scoped,
    )
