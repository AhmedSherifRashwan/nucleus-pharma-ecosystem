"""Composition of the final technical report (PRD s18, s32).

The model writes structured content; Python renders the document. This module is
the writing half.

It composes in stages rather than one call: an outline, then each section against
the specialist material that supports it, then the closing matter, then the
reference list. Three reasons. A single call asked to emit a 30-page report
truncates. A section given only its own sources stays anchored to real work. And a
staged composition can be checked between stages, which a single opaque call
cannot.

Nothing here is chronological. The composer is never told which agent ran when,
only what the current material says - that is what stops the deliverable from
reading like an execution log.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import BaseModel

from app.audit.logger import get_logger
from app.config import get_settings
from app.providers.openclaw_client import (
    AgentRoute,
    GatewayError,
    GatewayTransientError,
    OpenClawClient,
    Usage,
    get_gateway_client,
)
from app.reports.context import FinalReportContext, LoadedArtifact
from app.reports.outline import ReportOutline, known_refs_only
from app.schemas.common import StrictModel
from app.schemas.report import (
    Action,
    ExecutiveBriefSection,
    FinalTechnicalReport,
    InformationGap,
    Reference,
    ReportMetadata,
    ReportSection,
    ReviewIssue,
    RevisionEntry,
    WorkProductReference,
)

log = get_logger(__name__)

# Enough to cut the wall clock without stampeding the gateway or the provider's
# rate limit. Observed: a section takes 1-4 minutes against report-length material.
MAX_PARALLEL_SECTIONS = 4
RETRY_DELAY_SECONDS = 8

COMPOSER_SYSTEM = (
    "You are the senior technical writer for a pharmaceutical development team, "
    "assembling a formal report from the team's completed work. You write for a "
    "qualified reader who will act on the document: precise, sourced, and explicit "
    "about what is established versus provisional.\n\n"
    "Absolute rules:\n"
    "- Use ONLY the material supplied. Never introduce a specification, limit, "
    "figure, identifier or citation that is not in it.\n"
    "- Never present an assumption, estimate or screening-level result as an "
    "established fact. Say which it is.\n"
    "- Never describe the process that produced the material. No agent names, no "
    "task ids, no chronology, no 'the specialist found'. The reader receives a "
    "report, not a transcript.\n"
    "- Where a value is unknown or unretrieved, say so plainly. An honest gap is "
    "worth more than a confident invention.\n"
    "- State uncertainty once, where it belongs, and then write. Hedging every "
    "sentence does not make a document more honest — it makes the honest parts "
    "unreadable, and a reader who skims past the fortieth caveat has been warned "
    "less effectively than one who read a single clear status line."
)


class SectionDraft(StrictModel):
    """One composed section. Mirrors ReportSection without its recursion."""

    heading: str
    body: str = ""
    tables: list[dict] = []
    subsections: list[dict] = []
    evidence_refs: list[str] = []
    status: str | None = None
    status_note: str | None = None
    evidence_confidence: str | None = None
    open_gap_refs: list[str] = []


class ClosingMatter(StrictModel):
    """Everything after the body: review, revision, gaps, conclusions, actions."""

    review_summary: str | None = None
    critical_review: list[ReviewIssue] = []
    revision_crosswalk: list[RevisionEntry] = []
    information_gaps: list[InformationGap] = []
    conclusions: str = ""
    recommended_actions: list[Action] = []
    unresolved_conflicts: list[str] = []


class ReferenceList(StrictModel):
    references: list[Reference] = []


class BriefSection(StrictModel):
    brief: ExecutiveBriefSection


@dataclass
class CompositionOutcome:
    report: FinalTechnicalReport
    usage: Usage
    warnings: list[str] = field(default_factory=list)
    outline_headings: list[str] = field(default_factory=list)


def _artifacts_for(
    context: FinalReportContext, refs: list[str], *, fallback_all: bool
) -> list[LoadedArtifact]:
    chosen = [a for a in context.current_work_products if a.artifact_ref in refs]
    if chosen or not fallback_all:
        return chosen
    return context.current_work_products


def _evidence_index(context: FinalReportContext) -> str:
    """The citable evidence, as an index rather than full text."""
    lines = []
    for e in context.evidence:
        parts = [f"{e['evidence_ref']}: {e['source_title']}"]
        if e.get("source_url_or_document_id"):
            parts.append(str(e["source_url_or_document_id"]))
        if e.get("excerpt_or_locator"):
            parts.append(f"locator: {e['excerpt_or_locator']}")
        parts.append(f"reliability {e['reliability_level']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines) or "(no evidence recorded)"


def composer_route() -> AgentRoute:
    """Which agent writes the report (s7.3).

    Composition is long-form writing rather than an orchestration decision, so
    either route can do it. It is also the most token-hungry stage in a run: on a
    deployment where one subscription is near its limit, moving the writing to the
    other keeps runs completing.
    """
    configured = get_settings().report_composer_route
    return AgentRoute.SPECIALIST if configured == "specialist" else AgentRoute.EXECUTIVE


async def _call[T: BaseModel](
    client: OpenClawClient,
    schema: type[T],
    user: str,
    *,
    key: str,
    session_key: str,
) -> tuple[T, Usage]:
    """One composition call, with a single retry on a transient gateway state.

    A turn left in flight by an earlier client, or a session still settling, is a
    condition that clears on its own. Losing a composed section to it would be
    losing real work.
    """
    total = Usage()
    for attempt in range(2):
        try:
            parsed, usage, _route = await client.complete_structured(
                route=composer_route(),
                system=COMPOSER_SYSTEM,
                user=user,
                schema=schema,
                idempotency_key=f"{key}-t{attempt}",
                session_key=f"{session_key}-t{attempt}",
            )
            return parsed, total + usage
        except GatewayTransientError as exc:
            if attempt == 1:
                raise
            log.info("composer_retry", schema=schema.__name__, error=str(exc)[:120])
            await asyncio.sleep(RETRY_DELAY_SECONDS)
    raise AssertionError("unreachable")


async def compose_outline(
    context: FinalReportContext, client: OpenClawClient, session_key: str, key_prefix: str
) -> tuple[ReportOutline, Usage]:
    inventory = (
        "\n".join(
            f"- {a.artifact_ref}: {a.title} ({a.size_bytes or len(a.content)} bytes)"
            for a in context.current_work_products
        )
        or "(none)"
    )

    user = (
        "Plan the table of contents for a formal technical report answering this "
        "request.\n\n"
        f"## The request\n{context.user_request}\n\n"
        f"## Material available (current work products)\n{inventory}\n\n"
        f"## Evidence recorded\n{_evidence_index(context)}\n\n"
        "Rules for the outline:\n"
        "- Order sections the way the subject demands. Where the request asks for a "
        "regulatory basis, that basis comes before what is derived from it.\n"
        "- Propose a section ONLY where the material above can support it. An empty "
        "section is worse than an absent one.\n"
        "- Give each section the artifact references whose content belongs in it.\n"
        "- Name the tables the section should contain, where a table is the right "
        "form for the material (registers, attribute frameworks, traceability).\n"
        "- Do NOT include sections for the executive summary, critical review, "
        "revision history, information gaps, conclusions, next actions, references, "
        "evidence or appendices. Those are added automatically.\n"
        "- Subsections are one level deep."
    )
    outline, usage = await _call(
        client,
        ReportOutline,
        user,
        key=f"{context.run_ref}:report-outline",
        session_key=session_key,
    )
    assert isinstance(outline, ReportOutline)
    available = {a.artifact_ref for a in context.current_work_products}
    return known_refs_only(outline, available), usage


STATUS_CODES = ("confirmed", "provisional", "proposed", "screening", "unverified")

# Order matters: the weaker claim wins. A section that is "established in part,
# provisional in part" is provisional, because that is what a reader must act on.
STATUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("unverified", "unverified"),
    ("not retrieved", "unverified"),
    ("unretrieved", "unverified"),
    ("screening", "screening"),
    ("provisional", "provisional"),
    ("proposed", "proposed"),
    ("mixed", "provisional"),
    ("partially", "provisional"),
    ("established", "confirmed"),
    ("confirmed", "confirmed"),
)


def normalise_status(value: str | None) -> tuple[str | None, str | None]:
    """Map a written status onto a code, keeping the original wording.

    The composer writes sentences here - "established mechanism, provisional
    limits" - because that is the truthful answer. Rejecting the section over it
    threw away the content; coding it and keeping the sentence keeps both.
    """
    if not value:
        return None, None
    text = value.strip()
    lowered = text.lower()
    if lowered in STATUS_CODES:
        return lowered, None
    for needle, code in STATUS_KEYWORDS:
        if needle in lowered:
            return code, text[:400]
    return None, text[:400]


CONFIDENCE_CODES = ("very_high", "high", "medium", "low", "speculative")


def normalise_confidence(value: str | None) -> str | None:
    if not value:
        return None
    lowered = value.strip().lower().replace(" ", "_").replace("-", "_")
    if lowered in CONFIDENCE_CODES:
        return lowered
    for code in CONFIDENCE_CODES:
        if code in lowered:
            return code
    return None


def _normalise_node(node: dict) -> dict:
    code, note = normalise_status(node.get("status"))
    node = {**node, "status": code}
    node["evidence_confidence"] = normalise_confidence(node.get("evidence_confidence"))
    if note and not node.get("status_note"):
        node["status_note"] = note
    node["subsections"] = [_normalise_node(child) for child in (node.get("subsections") or [])]
    return node


def _to_section(draft: SectionDraft) -> ReportSection:
    return ReportSection.model_validate(
        _normalise_node(
            {
                "heading": draft.heading,
                "body": draft.body,
                "tables": draft.tables,
                "subsections": draft.subsections,
                "evidence_refs": draft.evidence_refs,
                "status": draft.status,
                "status_note": draft.status_note,
                "evidence_confidence": draft.evidence_confidence,
                "open_gap_refs": draft.open_gap_refs,
            }
        )
    )


async def compose_section(
    context: FinalReportContext,
    outline_section,
    client: OpenClawClient,
    session_key: str,
    index: int,
    key_prefix: str,
) -> tuple[ReportSection | None, Usage, str | None]:
    sources = _artifacts_for(context, outline_section.source_artifact_refs, fallback_all=index == 0)
    material = (
        "\n\n---\n\n".join(a.as_prompt_block() for a in sources)
        if sources
        else "(no work product was assigned to this section)"
    )
    planned_subs = (
        "\n".join(f"- {s.heading}: {s.purpose}" for s in outline_section.subsections)
        or "(decide the subsections yourself from the material)"
    )
    tables = "\n".join(f"- {t}" for t in outline_section.expected_tables) or "(none required)"

    user = (
        f'Write section "{outline_section.heading}" of the technical report.\n\n'
        f"## What this section is for\n{outline_section.purpose}\n\n"
        f"## Planned subsections\n{planned_subs}\n\n"
        f"## Tables expected in this section\n{tables}\n\n"
        f"## The original request (for scope only)\n{context.user_request}\n\n"
        f"## Source material — use this and nothing else\n{material}\n\n"
        f"## Citable evidence ids\n{_evidence_index(context)}\n\n"
        "Requirements:\n"
        "- Preserve the technical depth of the source material. This is a working "
        "document for specialists: do not compress it into a summary. If the source "
        "supports several pages, write several pages.\n"
        "- Carry across specifics — mechanisms, attributes, parameters, limits, "
        "methods, rationale — wherever the source states them.\n"
        "- Put register-like material in tables. Every row must be complete.\n"
        "- State the section's standing ONCE, in the fields, not in the prose: set "
        "`status`, `evidence_confidence`, and `open_gap_refs` for what this section "
        "depends on and does not have. The document prints them as a status line "
        "under the heading.\n"
        "- Having stated it there, write the technical content plainly. Do not "
        "qualify every sentence with 'provisional', 'not independently verified', "
        "'unconfirmed' or 'assumption'. A caveat repeated forty times stops being "
        "read, and the reader loses the science between the hedges.\n"
        "- Qualify inline ONLY where a specific number, limit or claim is weaker "
        "than the section's stated standing — a provisional acceptance criterion "
        "inside an otherwise established framework. Then say so once, at that "
        "number, and move on.\n"
        "- Where a table row's standing differs from its neighbours, that is what a "
        "status column is for.\n"
        "- Cite evidence ids in `evidence_refs`, and only ids that appear above.\n"
        "- Write prose and tables. No agent names, no task ids, no history.\n"
        "- `tables` entries are objects: {title, columns, rows, note}. Each row is a "
        "list of strings, one per column.\n"
        "- `subsections` entries have the same shape as this section, one level only."
    )
    try:
        draft, usage = await _call(
            client,
            SectionDraft,
            user,
            key=f"{key_prefix}-section-{index}",
            session_key=session_key,
        )
    except GatewayError as exc:
        log.warning(
            "section_composition_failed", heading=outline_section.heading, error=str(exc)[:200]
        )
        return None, Usage(), f"Section '{outline_section.heading}' could not be composed: {exc}"

    assert isinstance(draft, SectionDraft)
    try:
        section = _to_section(draft)
    except ValueError as exc:
        log.warning("section_invalid", heading=outline_section.heading, error=str(exc)[:200])
        return None, usage, f"Section '{outline_section.heading}' was malformed: {exc}"
    return section, usage, None


async def compose_closing(
    context: FinalReportContext, client: OpenClawClient, session_key: str, key_prefix: str
) -> tuple[ClosingMatter, Usage]:
    challenge = context.challenge
    review_material = {
        "contradictions": [
            {
                "description": c.description,
                "agents": c.agent_ids,
                "material": c.material,
                "severity": c.severity,
            }
            for c in (challenge.contradictions if challenge else [])
        ],
        "unsupported_claims": [
            {"claim": u.claim, "why": u.why_unsupported}
            for u in (challenge.unsupported_claims if challenge else [])
        ],
        "missing_evidence": challenge.missing_evidence if challenge else [],
        "unresolved_assumptions": challenge.unresolved_assumptions if challenge else [],
        "recorded_conflicts": context.conflicts,
        "superseded_work_products": [
            {"ref": a.artifact_ref, "title": a.title, "replaced_by": a.superseded_by_ref}
            for a in context.superseded_work_products
        ],
        "recorded_assumptions": context.assumptions,
        "recorded_risks": context.risks,
        "outstanding_from_specialists": sorted(
            {item for r in context.results for item in r.information_required}
        ),
        "specialist_statuses": [
            {"status": r.status, "confidence": r.confidence} for r in context.results
        ],
    }
    brief = context.brief
    user = (
        "Write the closing matter of the technical report.\n\n"
        f"## The request\n{context.user_request}\n\n"
        f"## The report's own body sections\n"
        + "\n".join(f"- {h}" for h in context.__dict__.get("_composed_headings", []))
        + "\n\n"
        f"## Review and revision record\n{json.dumps(review_material, indent=1)[:60000]}\n\n"
        f"## Integrated conclusion already reached\n"
        f"{brief.executive_conclusion if brief else '(none)'}\n\n"
        f"## Recommendation already reached\n{brief.recommendation if brief else '(none)'}\n\n"
        "Produce:\n"
        "- `critical_review`: the material issues an independent review raised, each "
        "with why it mattered, what was reviewed, how it was resolved and the final "
        "position. Where an earlier draft was wrong, say that an earlier draft was "
        "wrong and was corrected — never restate the wrong position as current.\n"
        "- `revision_crosswalk`: what changed after the critique, one row per issue.\n"
        "- `information_gaps`: categorised, with why each matters and how to close it. "
        "Set `blocks_completion` where the gap prevents a defensible conclusion.\n"
        "- `conclusions`: a coherent scientific conclusion. Not a summary of who did "
        "what.\n"
        "- `recommended_actions`: prioritised and risk-based.\n"
        "- `unresolved_conflicts`: disagreements that remain open. Do not resolve one "
        "by choosing a side the evidence does not support."
    )
    closing, usage = await _call(
        client,
        ClosingMatter,
        user,
        key=f"{context.run_ref}:report-closing",
        session_key=session_key,
    )
    assert isinstance(closing, ClosingMatter)
    return closing, usage


async def compose_references(
    context: FinalReportContext, client: OpenClawClient, session_key: str, key_prefix: str
) -> tuple[list[Reference], Usage]:
    """Turn the evidence register into precise citations (s32.3).

    Every reference is anchored to a stored evidence item: the model structures
    what was recorded, it does not supply new sources. A citation whose source was
    never retrieved is marked, not dropped.
    """
    if not context.evidence:
        return [], Usage()

    records = json.dumps(context.evidence, indent=1)[:80000]
    user = (
        "Turn this evidence register into a precise reference list.\n\n"
        f"## Evidence records\n{records}\n\n"
        "For each record produce one reference:\n"
        "- `citation_id`: R1, R2, ... in order.\n"
        "- `evidence_ref`: the E-n id it came from. Every reference must have one.\n"
        "- Split the recorded title into `organisation`, `title`, "
        "`document_identifier` (guidance number, NDA/ANDA number, ICH code, "
        "monograph number), `version_or_revision`, `published_or_revised`, "
        "`authority`, and `relevant_locator` (section, table, page) — but ONLY where "
        "the record actually contains that detail. Leave a field null rather than "
        "guessing at it.\n"
        "- `url_or_locator` and `retrieved_at` from the record.\n"
        "- `verification`: `primary_source_verified` only where the record is an "
        "authoritative source (reliability 1) with a locator or URL showing it was "
        "actually retrieved. Use `secondary_verified` for reliability 2 with a "
        "locator, `partially_verified` where identifying detail is missing, "
        "`not_retrieved` where nothing shows the document was obtained, and "
        "`unverified` otherwise.\n"
        "Do not invent a document number, date or URL. A reference that cannot be "
        "made precise must say so through its verification status."
    )
    result, usage = await _call(
        client,
        ReferenceList,
        user,
        key=f"{context.run_ref}:report-references",
        session_key=session_key,
    )
    assert isinstance(result, ReferenceList)
    known = context.evidence_refs
    kept = [r for r in result.references if r.evidence_ref in known]
    return kept, usage


async def compose_brief(
    context: FinalReportContext, client: OpenClawClient, session_key: str, key_prefix: str
) -> tuple[ExecutiveBriefSection, Usage]:
    brief = context.brief
    if brief is None:
        raise ValueError("an executive brief is required before the report is composed")
    user = (
        "Write the Executive Decision Brief that opens the report: one to three "
        "pages for a decision-maker who will not read the technical body.\n\n"
        f"## The request\n{context.user_request}\n\n"
        f"## The integrated position\n{brief.model_dump_json(indent=1)[:30000]}\n\n"
        f"## Body sections of the report that follows\n"
        + "\n".join(f"- {h}" for h in context.__dict__.get("_composed_headings", []))
        + "\n\nKeep the substance of the integrated position — the conclusion, the "
        "recommendation, the confidence and its basis, the risks, the assumptions, "
        "the outstanding information and the decisions required. Write it as prose a "
        "director can act on. Do not describe how the analysis was performed."
    )
    result, usage = await _call(
        client, BriefSection, user, key=f"{key_prefix}-brief", session_key=session_key
    )
    assert isinstance(result, BriefSection)
    return result.brief, usage


async def compose_report(
    context: FinalReportContext,
    *,
    report_version: int = 1,
    client: OpenClawClient | None = None,
    generator_version: str = "2.0.0",
    template_version: str | None = None,
) -> CompositionOutcome:
    """Compose the whole report. Raises only if the outline itself fails."""
    client = client or get_gateway_client()
    # A composition is not resumable: it either completes or is redone from the
    # outline. So each invocation gets its own keys. Reusing them means the gateway
    # matches an earlier turn - including one abandoned by a client that died - and
    # refuses the new call with `in_flight` until that turn settles.
    nonce = uuid.uuid4().hex[:8]
    key_prefix = f"{context.run_ref}:v{report_version}:report:{nonce}"
    session_key = client.session_key_for_route(
        composer_route(), f"{context.run_ref}-report-v{report_version}-{nonce}"
    )
    warnings: list[str] = []
    total = Usage()

    outline, usage = await compose_outline(context, client, session_key, key_prefix)
    total = total + usage
    log.info("report_outline", sections=len(outline.sections), title=outline.document_title)

    # Sections are independent: each is composed against its own sources. Running
    # them in parallel turns a serial 30 minutes into a few, and each gets its own
    # gateway session so one section's material cannot leak into another's (s10.3).
    limit = asyncio.Semaphore(MAX_PARALLEL_SECTIONS)

    async def compose_one(index: int, planned):
        async with limit:
            return await compose_section(
                context, planned, client, f"{session_key}-s{index}", index, key_prefix
            )

    composed = await asyncio.gather(
        *(compose_one(i, planned) for i, planned in enumerate(outline.sections))
    )
    sections: list[ReportSection] = []
    for section, usage, warning in composed:
        total = total + usage
        if warning:
            warnings.append(warning)
        if section is not None:
            sections.append(section)

    # The closing matter and the brief are written knowing the body's shape.
    context.__dict__["_composed_headings"] = [s.heading for s in sections]

    try:
        closing, usage = await compose_closing(
            context, client, f"{session_key}-closing", key_prefix
        )
        total = total + usage
    except (GatewayError, ValueError) as exc:
        warnings.append(f"The closing matter could not be composed: {exc}")
        closing = ClosingMatter()

    try:
        brief_section, usage = await compose_brief(context, client, session_key, key_prefix)
        total = total + usage
    except (GatewayError, ValueError) as exc:
        warnings.append(f"The executive brief could not be composed: {exc}")
        brief_section = _fallback_brief(context)

    try:
        references, usage = await compose_references(context, client, session_key, key_prefix)
        total = total + usage
    except (GatewayError, ValueError) as exc:
        warnings.append(f"The reference list could not be composed: {exc}")
        references = []

    brief = context.brief
    metadata = ReportMetadata(
        document_title=outline.document_title,
        subject=context.plan.objective if context.plan else None,
        product=outline.product,
        strength=outline.strength,
        dosage_form=outline.dosage_form,
        run_ref=context.run_ref,
        report_version=report_version,
        generated_at=datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        status="REVIEW REQUIRED" if (brief and brief.review_required) else "Complete",
        confidence=brief.confidence if brief else "medium",
        human_review_status=(
            "Human review required" if (brief and brief.review_required) else "Reviewed by run gate"
        ),
        supersedes_version=report_version - 1 if report_version > 1 else None,
        evidence_cutoff=max((e.get("retrieved_at") or "" for e in context.evidence), default=None)
        or None,
        agent_versions={
            r.agent_id: context.agent_names.get(r.agent_id, r.agent_id) for r in context.results
        },
        generator_version=generator_version,
        template_version=template_version,
    )

    gaps = [
        gap.model_copy(update={"gap_id": gap.gap_id or f"G-{index:02d}"})
        for index, gap in enumerate(closing.information_gaps, start=1)
    ]
    known_gap_ids = {g.gap_id for g in gaps}
    sections = [_keep_known_gaps(section, known_gap_ids) for section in sections]

    report = FinalTechnicalReport(
        metadata=metadata,
        executive_brief=brief_section,
        body_sections=sections,
        critical_review=closing.critical_review,
        review_summary=closing.review_summary,
        revision_crosswalk=closing.revision_crosswalk,
        information_gaps=gaps,
        conclusions=closing.conclusions,
        recommended_actions=closing.recommended_actions,
        references=references,
        work_products=[
            WorkProductReference(
                artifact_ref=a.artifact_ref,
                title=a.title,
                originating_agent=a.agent_id,
                status="current",
                size_bytes=a.size_bytes,
            )
            for a in context.current_work_products
        ]
        + [
            WorkProductReference(
                artifact_ref=a.artifact_ref,
                title=a.title,
                originating_agent=a.agent_id,
                status="superseded",
                superseded_by_ref=a.superseded_by_ref,
                size_bytes=a.size_bytes,
            )
            for a in context.superseded_work_products
        ],
        unresolved_conflicts=closing.unresolved_conflicts,
    )

    log.info(
        "report_composed",
        run_ref=context.run_ref,
        sections=len(sections),
        body_chars=report.body_length,
        references=len(references),
        warnings=len(warnings),
    )
    return CompositionOutcome(
        report=report,
        usage=total,
        warnings=warnings,
        outline_headings=[s.heading for s in outline.sections],
    )


def _keep_known_gaps(section: ReportSection, known: set[str | None]) -> ReportSection:
    """Drop gap references that point at nothing.

    A status line citing G-07 when no G-07 exists is worse than one citing
    nothing: the reader goes looking for a gap that was never recorded.
    """
    return section.model_copy(
        update={
            "open_gap_refs": [ref for ref in section.open_gap_refs if ref in known],
            "subsections": [_keep_known_gaps(child, known) for child in section.subsections],
        }
    )


def _fallback_brief(context: FinalReportContext) -> ExecutiveBriefSection:
    """The brief from the integrated position, without a further model call.

    Used when composition of the brief fails: the report still opens with the
    decision material rather than with nothing.
    """
    brief = context.brief
    if brief is None:
        return ExecutiveBriefSection(
            executive_summary="No integrated conclusion was produced for this run.",
            objective_and_scope=context.user_request,
            overall_recommendation="Review required.",
            confidence_and_evidence_status="Not established.",
        )
    return ExecutiveBriefSection(
        executive_summary=brief.executive_conclusion,
        objective_and_scope=brief.objective,
        overall_recommendation=brief.recommendation,
        confidence_and_evidence_status=(
            f"Confidence: {brief.confidence.replace('_', ' ')}. "
            f"{len(context.evidence)} evidence item(s) recorded."
            + (f" {brief.review_reason}" if brief.review_reason else "")
        ),
        major_risks=brief.key_risks,
        key_findings=brief.key_findings,
        critical_assumptions=brief.critical_assumptions,
        outstanding_information=brief.information_required,
        decisions_required=[d.question for d in brief.decisions_required_from_user],
    )
