"""Report generation as a run step (PRD s18, s32, Phase 12).

The pipeline:

    current artifacts + evidence + review record   (context.py)
        -> outline, sections, closing, references  (composer.py)
        -> deterministic pre-release gate          (gate.py)
        -> DOCX for the technical report and the brief  (technical_builder.py)
        -> optional PDF                            (pdf.py)
        -> versioned artifact rows

Composition can fail - a gateway timeout, a malformed section - and a run that
produced real specialist work must still deliver something. When it does fail the
service falls back to the brief-only document, and says so, rather than losing the
analysis.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.audit.logger import get_logger
from app.config import get_settings
from app.reports.builder import ReportBuilder, ReportInputs, ReportResult
from app.reports.composer import compose_report
from app.reports.context import build_context
from app.reports.gate import GateResult, run_report_gate
from app.reports.naming import document_name
from app.reports.pdf import convert_to_pdf
from app.reports.technical_builder import (
    GENERATOR_VERSION,
    TEMPLATE_VERSION,
    RenderedReport,
    TechnicalReportRenderer,
)
from app.schemas.executive import ChallengeResult, ExecutiveDecisionBrief
from app.schemas.planning import Plan
from app.schemas.report import FinalTechnicalReport
from app.schemas.specialist import SpecialistResult
from app.storage.db import session_scope
from app.storage.repositories import (
    ArtifactRepository,
    AuditRepository,
    EventType,
    EvidenceRepository,
    GovernanceRepository,
    ProjectRepository,
)

log = get_logger(__name__)

EVIDENCE_REF_PATTERN = re.compile(r"\bE-\d+\b")

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass
class ReportOutcome:
    """What the run produced, and what is wrong with it."""

    paths: list[Path] = field(default_factory=list)
    primary: Path | None = None
    brief_path: Path | None = None
    summary_path: Path | None = None
    pdf_path: Path | None = None
    version: int = 1
    warnings: list[str] = field(default_factory=list)
    gate: GateResult | None = None
    review_required: bool = False
    section_count: int = 0
    word_count: int = 0
    document_title: str | None = None
    composed: bool = True

    @property
    def filenames(self) -> list[str]:
        return [p.name for p in self.paths]


class ReportService:
    def __init__(self, output_dir: Path | str | None = None):
        settings = get_settings()
        self.output_dir = Path(output_dir or Path(settings.artifact_local_path) / "reports")
        self.template_path = settings.report_template
        self.pdf_enabled = settings.report_pdf_enabled

    # -- the composed pipeline ---------------------------------------------

    async def generate_technical_report(
        self,
        *,
        run_id: uuid.UUID,
        run_ref: str,
        user_request: str,
        brief: ExecutiveDecisionBrief,
        results: list[SpecialistResult],
        plan: Plan | None = None,
        challenge: ChallengeResult | None = None,
        agent_names: dict[str, str] | None = None,
        project_id: uuid.UUID | None = None,
    ) -> ReportOutcome:
        context = await build_context(
            run_id=run_id,
            run_ref=run_ref,
            user_request=user_request,
            plan=plan,
            brief=brief,
            results=results,
            agent_names=agent_names or {},
            challenge=challenge,
        )

        async with session_scope() as session:
            version = await ArtifactRepository(session).next_report_version(run_id)
            open_conflicts = len(
                await GovernanceRepository(session).open_material_conflicts(run_id)
            )

        composition = await compose_report(
            context,
            report_version=version,
            generator_version=GENERATOR_VERSION,
            template_version=TEMPLATE_VERSION,
        )
        report = composition.report
        warnings = list(composition.warnings)

        gate = run_report_gate(
            report,
            user_request=user_request,
            known_evidence_refs=context.evidence_refs,
            superseded_refs=set(context.superseded_refs()),
            open_conflicts=open_conflicts,
            pdf_expected=self.pdf_enabled,
            pdf_rendered=False,
        )
        if gate.review_required:
            report = _mark_review_required(report, gate)
            warnings.extend(gate.reasons)

        title = document_name(context.project_name, user_request)
        subject = report.metadata.document_title or title
        run_dir = self.output_dir / f"{_safe(run_ref)}_{run_id.hex[:8]}"
        renderer = TechnicalReportRenderer(run_dir)

        rendered = renderer.render(report, project_name=subject)
        brief_doc = renderer.render_brief(report, project_name=subject)
        paths = [rendered.path, brief_doc.path]

        summary_doc = None
        if report.technical_summary is not None:
            summary_doc = renderer.render_summary(report, project_name=subject)
            paths.append(summary_doc.path)
        else:
            warnings.append(
                "No technical summary was produced for this run; the full report and "
                "the brief are unaffected."
            )
        pdf_path = None
        if self.pdf_enabled:
            pdf_path = await convert_to_pdf(rendered.path)
            if pdf_path is not None:
                paths.append(pdf_path)
            else:
                warnings.append(
                    "A PDF was requested but no converter is installed; the DOCX is "
                    "the deliverable."
                )

        await self._register(
            run_id=run_id,
            run_ref=run_ref,
            project_id=project_id,
            version=version,
            rendered=rendered,
            brief_doc=brief_doc,
            summary_doc=summary_doc,
            pdf_path=pdf_path,
            gate=gate,
            report=report,
            warnings=warnings,
        )

        log.info(
            "technical_report_ready",
            run_ref=run_ref,
            version=version,
            sections=rendered.section_count,
            words=rendered.word_count,
            references=len(report.references),
            gate_failures=len(gate.failures),
        )
        return ReportOutcome(
            paths=paths,
            primary=rendered.path,
            brief_path=brief_doc.path,
            summary_path=summary_doc.path if summary_doc else None,
            pdf_path=pdf_path,
            version=version,
            warnings=warnings,
            gate=gate,
            review_required=gate.review_required,
            section_count=rendered.section_count,
            word_count=rendered.word_count,
            document_title=report.metadata.document_title,
            composed=True,
        )

    async def _register(
        self,
        *,
        run_id: uuid.UUID,
        run_ref: str,
        project_id: uuid.UUID | None,
        version: int,
        rendered: RenderedReport,
        brief_doc: RenderedReport,
        summary_doc: RenderedReport | None,
        pdf_path: Path | None,
        gate: GateResult,
        report: FinalTechnicalReport,
        warnings: list[str],
    ) -> None:
        async with session_scope() as session:
            artifacts = ArtifactRepository(session)
            documents = [("", rendered), ("-brief", brief_doc)]
            if summary_doc is not None:
                documents.append(("-summary", summary_doc))
            for suffix, doc in documents:
                await artifacts.create(
                    title=doc.filename,
                    kind="report",
                    content_type=DOCX_CONTENT_TYPE,
                    storage_path=str(doc.path),
                    size_bytes=doc.path.stat().st_size,
                    sha256=doc.sha256,
                    project_id=project_id,
                    run_id=run_id,
                    version=version,
                    template_version=TEMPLATE_VERSION,
                    generator_version=GENERATOR_VERSION,
                    artifact_ref=f"RPT-{run_ref}-v{version}{suffix}",
                )
            if pdf_path is not None:
                await artifacts.create(
                    title=pdf_path.name,
                    kind="report",
                    content_type="application/pdf",
                    storage_path=str(pdf_path),
                    size_bytes=pdf_path.stat().st_size,
                    project_id=project_id,
                    run_id=run_id,
                    version=version,
                    template_version=TEMPLATE_VERSION,
                    generator_version=GENERATOR_VERSION,
                    artifact_ref=f"RPT-{run_ref}-v{version}-pdf",
                )
            await AuditRepository(session).record(
                event_type=EventType.REPORT_GENERATED,
                actor_type="system",
                run_id=run_id,
                metadata={
                    "version": version,
                    "filename": rendered.filename,
                    "sha256": rendered.sha256,
                    "sections": rendered.section_count,
                    "words": rendered.word_count,
                    "documents": [d.filename for _, d in documents],
                    "actions": (
                        len(report.technical_summary.actions) if report.technical_summary else 0
                    ),
                    "references": len(report.references),
                    "gate": gate.as_metadata(),
                    "warnings": warnings[:20],
                },
            )

    # -- the brief-only fallback -------------------------------------------

    async def generate(
        self,
        *,
        run_id: uuid.UUID,
        run_ref: str,
        brief: ExecutiveDecisionBrief,
        results: list[SpecialistResult],
        agent_names: dict[str, str] | None = None,
        project_id: uuid.UUID | None = None,
        project_name: str | None = None,
        user_request: str = "",
        report_type: str = "Executive_Decision_Brief",
    ) -> tuple[ReportResult, list[str]]:
        """The deterministic brief document, with no model call.

        Kept as the floor: when composition fails, this still delivers the
        integrated position rather than nothing.
        """
        async with session_scope() as session:
            evidence_repo = EvidenceRepository(session)
            evidence = [
                {
                    "evidence_ref": e.evidence_ref,
                    "reliability_level": e.reliability_level,
                    "source_title": e.source_title,
                    "excerpt_or_locator": e.excerpt_or_locator,
                    "originating_agent": e.originating_agent,
                }
                for e in await evidence_repo.list_for_run(run_id)
            ]
            governance = GovernanceRepository(session)
            risks = [
                {
                    "description": r.description,
                    "likelihood": r.likelihood,
                    "impact": r.impact,
                    "mitigation": r.mitigation,
                }
                for r in await governance.list_risks(run_id)
            ]
            conflicts = [
                {"description": c.description, "state": c.state, "agent_ids": c.agent_ids}
                for c in await governance.list_conflicts(run_id)
            ]
            artifacts = ArtifactRepository(session)
            version = await artifacts.next_report_version(run_id)
            if project_name is None and project_id is not None:
                project = await ProjectRepository(session).get(project_id)
                project_name = project.name if project else None
            known_refs = {str(e["evidence_ref"]) for e in evidence}

        warnings = _qa_warnings(brief, known_refs)
        title = document_name(project_name, user_request or brief.objective)

        inputs = ReportInputs(
            run_ref=run_ref,
            project_name=title,
            brief=brief,
            results=results,
            agent_names=agent_names or {},
            evidence=evidence,
            conflicts=conflicts,
            risks=risks,
            report_type=report_type,
            version=version,
        )
        run_dir = self.output_dir / f"{_safe(run_ref)}_{run_id.hex[:8]}"
        builder = ReportBuilder(run_dir, template_path=self.template_path)
        result = builder.build(inputs)

        paths = [str(result.path)]
        if self.pdf_enabled:
            pdf_path = await convert_to_pdf(result.path)
            if pdf_path is not None:
                paths.append(str(pdf_path))

        async with session_scope() as session:
            artifacts = ArtifactRepository(session)
            record = await artifacts.create(
                title=result.filename,
                kind="report",
                content_type=DOCX_CONTENT_TYPE,
                storage_path=str(result.path),
                size_bytes=result.path.stat().st_size,
                sha256=result.sha256,
                project_id=project_id,
                run_id=run_id,
                version=result.version,
                template_version=result.template_version,
                generator_version=result.generator_version,
                artifact_ref=f"RPT-{run_ref}-v{result.version}",
            )
            for extra in paths[1:]:
                path = Path(extra)
                await artifacts.create(
                    title=path.name,
                    kind="report",
                    content_type="application/pdf",
                    storage_path=str(path),
                    size_bytes=path.stat().st_size,
                    project_id=project_id,
                    run_id=run_id,
                    version=result.version,
                    template_version=result.template_version,
                    generator_version=result.generator_version,
                    artifact_ref=f"RPT-{run_ref}-v{result.version}-pdf",
                )
            await AuditRepository(session).record(
                event_type=EventType.REPORT_GENERATED,
                actor_type="system",
                run_id=run_id,
                metadata={
                    "artifact_ref": record.artifact_ref,
                    "filename": result.filename,
                    "version": result.version,
                    "sha256": result.sha256,
                    "formats": [Path(p).suffix.lstrip(".") for p in paths],
                    "qa_warnings": warnings,
                    "mode": "brief_only",
                },
            )
        log.info(
            "report_ready",
            run_ref=run_ref,
            version=result.version,
            formats=len(paths),
            qa_warnings=len(warnings),
        )
        return result, warnings


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip()).strip("_") or "run"


def _mark_review_required(report: FinalTechnicalReport, gate: GateResult) -> FinalTechnicalReport:
    """Stamp the document with what the gate found. Never silently complete."""
    reasons = "; ".join(c.detail or c.name for c in gate.mandatory_failures)
    metadata = report.metadata.model_copy(
        update={
            "status": "REVIEW REQUIRED",
            "human_review_status": f"Human review required — {reasons}"[:900],
        }
    )
    return report.model_copy(update={"metadata": metadata})


def _qa_warnings(brief: ExecutiveDecisionBrief, known_refs: set[str]) -> list[str]:
    """s32.3: every evidence ID printed in a report must resolve to a record."""
    cited: set[str] = set()
    for text in [*brief.key_evidence, *brief.key_findings, *brief.why]:
        cited.update(EVIDENCE_REF_PATTERN.findall(text))
    dangling = sorted(cited - known_refs)
    warnings = [f"Cited evidence {ref} does not resolve to a stored record" for ref in dangling]
    if not known_refs:
        warnings.append("No evidence items were recorded for this run")
    return warnings


_service: ReportService | None = None


def get_report_service() -> ReportService:
    global _service
    if _service is None:
        _service = ReportService()
    return _service


def reset_report_service() -> None:
    global _service
    _service = None
