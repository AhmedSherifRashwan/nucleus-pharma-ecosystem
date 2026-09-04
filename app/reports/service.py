"""Report generation as a run step (PRD s18, s32, Phase 12).

Assembles the report from persisted records, verifies that every evidence ID it
cites resolves to a real evidence item (s32.3), writes a new version rather than
overwriting an earlier one (s18.3), and registers the file as an artifact.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from app.audit.logger import get_logger
from app.config import get_settings
from app.reports.builder import (
    ReportBuilder,
    ReportInputs,
    ReportResult,
    safe_component,
)
from app.reports.pdf import convert_to_pdf
from app.schemas.executive import ExecutiveDecisionBrief
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


class ReportService:
    def __init__(self, output_dir: Path | str | None = None):
        settings = get_settings()
        self.output_dir = Path(output_dir or Path(settings.artifact_local_path) / "reports")
        self.template_path = settings.report_template
        self.pdf_enabled = settings.report_pdf_enabled

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
        report_type: str = "Executive_Decision_Brief",
    ) -> tuple[ReportResult, list[str]]:
        """Build the report. Returns the result and any QA warnings."""
        async with session_scope() as s:
            evidence_repo = EvidenceRepository(s)
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
            governance = GovernanceRepository(s)
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
            artifacts = ArtifactRepository(s)
            version = await artifacts.next_report_version(run_id)
            if project_name is None and project_id is not None:
                project = await ProjectRepository(s).get(project_id)
                project_name = project.name if project else None
            known_refs = {str(e["evidence_ref"]) for e in evidence}

        warnings = _qa_warnings(brief, known_refs)

        inputs = ReportInputs(
            run_ref=run_ref,
            project_name=project_name or "Neucleus",
            brief=brief,
            results=results,
            agent_names=agent_names or {},
            evidence=evidence,
            conflicts=conflicts,
            risks=risks,
            report_type=report_type,
            version=version,
        )
        # One folder per run: every version of a run's report sits together, and
        # two runs can never contend for a filename even if a ref were recycled.
        run_dir = self.output_dir / f"{safe_component(run_ref)}_{run_id.hex[:8]}"
        builder = ReportBuilder(run_dir, template_path=self.template_path)
        result = builder.build(inputs)

        paths = [str(result.path)]
        if self.pdf_enabled:
            pdf_path = await convert_to_pdf(result.path)
            if pdf_path is not None:
                paths.append(str(pdf_path))

        async with session_scope() as s:
            artifacts = ArtifactRepository(s)
            record = await artifacts.create(
                title=result.filename,
                kind="report",
                content_type=(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
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
            await AuditRepository(s).record(
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


def _qa_warnings(brief: ExecutiveDecisionBrief, known_refs: set[str]) -> list[str]:
    """s32.3: every evidence ID printed in a report must resolve to a record.

    A dangling ID is worse than no ID: it reads as provenance the reader cannot
    actually check, so it is surfaced rather than silently printed.
    """
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
