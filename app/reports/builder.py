"""Deterministic DOCX report generation (PRD s18, s32, Phase 12).

The report is assembled from structured database records and a controlled
template. A language model never formats the final file: s18 requires
deterministic assembly so the deliverable cannot be paraphrased, reordered or
quietly abridged between runs.

Section order follows s18.1. Naming and versioning follow s18.2 and s18.3: a
report is never overwritten, only superseded by a new version.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docx import Document as new_document
from docx.document import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt, RGBColor

from app.audit.logger import get_logger
from app.schemas.executive import ExecutiveDecisionBrief
from app.schemas.specialist import SpecialistResult

log = get_logger(__name__)

GENERATOR_VERSION = "1.0.0"
TEMPLATE_VERSION = "builtin-1.0.0"

DISCLAIMER = (
    "This report contains AI-generated analysis produced by the Neucleus "
    "ecosystem to support a human decision. It is not a regulatory approval, a "
    "GMP release, a validated state, a compliance sign-off, or legal advice. "
    "Every material conclusion must be reviewed by a qualified person before it "
    "is acted upon."
)

RELIABILITY_LABEL = {
    1: "1 — Authoritative",
    2: "2 — High-quality secondary",
    3: "3 — Secondary intelligence",
    4: "4 — Unverified",
}


@dataclass
class ReportInputs:
    """Everything the builder needs. All of it comes from the database."""

    run_ref: str
    project_name: str
    brief: ExecutiveDecisionBrief
    results: list[SpecialistResult] = field(default_factory=list)
    agent_names: dict[str, str] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    report_type: str = "Executive_Decision_Brief"
    version: int = 1
    generated_at: datetime | None = None


@dataclass
class ReportResult:
    path: Path
    filename: str
    version: int
    sha256: str
    template_version: str = TEMPLATE_VERSION
    generator_version: str = GENERATOR_VERSION


def safe_component(value: str) -> str:
    """Filename component: no separators, no surprises (s18.2)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("_") or "Untitled"


def build_filename(project: str, report_type: str, run_ref: str, version: int) -> str:
    """<Project>_<Report_Type>_<RUN-ID>_v<version>.docx (s18.2)."""
    return (
        f"{safe_component(project)}_{safe_component(report_type)}_"
        f"{safe_component(run_ref)}_v{version}.docx"
    )


class ReportBuilder:
    def __init__(self, output_dir: Path | str, template_path: Path | str | None = None):
        self.output_dir = Path(output_dir).expanduser()
        self.template_path = Path(template_path).expanduser() if template_path else None

    def _new_document(self) -> tuple[DocxDocument, str]:
        """Open the house template when one is installed, else the built-in.

        The version travels with the artifact record so a report can always be
        traced back to the layout that produced it (s18.3).
        """
        if self.template_path and self.template_path.is_file():
            return new_document(str(self.template_path)), f"file:{self.template_path.name}"
        return new_document(), TEMPLATE_VERSION

    # -- document helpers ---------------------------------------------------

    def _heading(self, doc: DocxDocument, text: str, level: int = 1) -> None:
        doc.add_heading(text, level=level)

    def _para(self, doc: DocxDocument, text: str, *, italic: bool = False) -> None:
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.italic = italic

    def _bullets(self, doc: DocxDocument, items: list[str], *, numbered: bool = False) -> None:
        style = "List Number" if numbered else "List Bullet"
        for item in items:
            doc.add_paragraph(str(item), style=style)

    def _section(
        self, doc: DocxDocument, title: str, items: list[str], *, numbered: bool = False
    ) -> None:
        """A section is always rendered, even when empty.

        An absent section would read as an oversight; 'None identified' is a
        statement the reader can rely on.
        """
        self._heading(doc, title, 1)
        if items:
            self._bullets(doc, items, numbered=numbered)
        else:
            self._para(doc, "None identified.", italic=True)

    def _table(self, doc: DocxDocument, headers: list[str], rows: list[list[str]]) -> None:
        table = doc.add_table(rows=1, cols=len(headers))
        table.style = "Light Grid Accent 1"
        for cell, header in zip(table.rows[0].cells, headers, strict=True):
            cell.text = header
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True
        for row in rows:
            cells = table.add_row().cells
            for cell, value in zip(cells, row, strict=True):
                cell.text = str(value)

    # -- build --------------------------------------------------------------

    def build(self, inputs: ReportInputs) -> ReportResult:
        brief = inputs.brief
        generated_at = inputs.generated_at or datetime.now(UTC)
        doc, template_version = self._new_document()

        # Title page (s18.1)
        title = doc.add_heading("Executive Decision Brief", level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        subtitle = doc.add_paragraph()
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = subtitle.add_run(inputs.project_name)
        run.bold = True
        run.font.size = Pt(14)

        if brief.review_required:
            warning = doc.add_paragraph()
            warning.alignment = WD_ALIGN_PARAGRAPH.CENTER
            flag = warning.add_run("REVIEW REQUIRED")
            flag.bold = True
            flag.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

        self._table(
            doc,
            ["Field", "Value"],
            [
                ["Run reference", inputs.run_ref],
                ["Report version", str(inputs.version)],
                ["Generated", generated_at.strftime("%Y-%m-%d %H:%M UTC")],
                ["Overall confidence", brief.confidence.replace("_", " ").title()],
                ["Decision class", brief.highest_decision_class.upper()],
                ["Specialists consulted", str(len(brief.consulted_agents))],
                ["Template version", template_version],
                ["Generator version", GENERATOR_VERSION],
            ],
        )
        self._para(doc, DISCLAIMER, italic=True)
        doc.add_page_break()

        # Objective and scope
        self._heading(doc, "Objective and scope", 1)
        self._para(doc, brief.objective)

        # Conclusion and recommendation
        self._heading(doc, "Executive conclusion", 1)
        self._para(doc, brief.executive_conclusion)
        self._heading(doc, "Recommendation", 1)
        self._para(doc, brief.recommendation)

        # Confidence and evidence quality
        self._heading(doc, "Confidence and evidence quality", 1)
        tiers: dict[int, int] = {}
        for item in inputs.evidence:
            level = int(item.get("reliability_level") or 0)
            tiers[level] = tiers.get(level, 0) + 1
        self._para(
            doc,
            f"Overall confidence: {brief.confidence.replace('_', ' ')}. "
            f"{len(inputs.evidence)} evidence item(s) recorded.",
        )
        if tiers:
            self._table(
                doc,
                ["Reliability", "Items"],
                [[RELIABILITY_LABEL.get(k, str(k)), str(v)] for k, v in sorted(tiers.items())],
            )
        if brief.review_required and brief.review_reason:
            self._para(doc, f"Review required: {brief.review_reason}", italic=True)

        self._section(doc, "Why — decision rationale", brief.why)

        # Specialist workstreams
        self._heading(doc, "Specialist workstreams and findings", 1)
        if inputs.results:
            self._table(
                doc,
                ["Agent", "Specialist", "Status", "Confidence", "Findings"],
                [
                    [
                        r.agent_id,
                        inputs.agent_names.get(r.agent_id, r.agent_id),
                        r.status,
                        r.confidence,
                        str(len(r.findings)),
                    ]
                    for r in inputs.results
                ],
            )
            for r in inputs.results:
                self._heading(
                    doc,
                    f"{r.agent_id} — {inputs.agent_names.get(r.agent_id, r.agent_id)}",
                    2,
                )
                self._para(doc, r.executive_summary)
                for finding in r.findings:
                    refs = ", ".join(e.evidence_id for e in finding.evidence_refs)
                    suffix = f" [{refs}]" if refs else ""
                    doc.add_paragraph(
                        f"({finding.basis}) {finding.statement}{suffix}",
                        style="List Bullet",
                    )
        else:
            self._para(doc, "No specialist returned a result.", italic=True)

        # Implications (s18.1)
        self._heading(doc, "Implications", 1)
        implications = [
            ("Scientific / technical", brief.scientific_technical_implication),
            ("Manufacturing", brief.manufacturing_implication),
            ("Regulatory", brief.regulatory_implication),
            ("Quality / GMP", brief.quality_implication),
            ("Commercial / portfolio", brief.commercial_implication),
        ]
        present = [(label, text) for label, text in implications if text]
        if present:
            self._table(doc, ["Area", "Implication"], [[a, b] for a, b in present])
        else:
            self._para(doc, "None stated.", italic=True)

        # Evidence register
        self._heading(doc, "Key evidence and provenance register", 1)
        if inputs.evidence:
            self._table(
                doc,
                ["ID", "Reliability", "Source", "Locator", "By"],
                [
                    [
                        e.get("evidence_ref", ""),
                        RELIABILITY_LABEL.get(int(e.get("reliability_level") or 0), ""),
                        str(e.get("source_title", ""))[:80],
                        str(e.get("excerpt_or_locator") or "")[:60],
                        e.get("originating_agent", ""),
                    ]
                    for e in inputs.evidence
                ],
            )
        else:
            self._para(doc, "No evidence items were recorded for this run.", italic=True)

        # Risks
        self._heading(doc, "Risks and mitigations", 1)
        if inputs.risks:
            self._table(
                doc,
                ["Risk", "Likelihood", "Impact", "Mitigation"],
                [
                    [
                        str(r.get("description", ""))[:110],
                        r.get("likelihood", ""),
                        r.get("impact", ""),
                        str(r.get("mitigation") or "—")[:80],
                    ]
                    for r in inputs.risks
                ],
            )
        elif brief.key_risks:
            self._bullets(doc, brief.key_risks)
        else:
            self._para(doc, "None identified.", italic=True)

        # s9.3: disagreement must reach the reader.
        self._section(
            doc, "Agent disagreements and unresolved conflicts", brief.agent_disagreements
        )
        self._section(doc, "Critical assumptions", brief.critical_assumptions)
        self._section(doc, "Information still required", brief.information_required)
        self._section(doc, "Recommended next actions", brief.recommended_actions, numbered=True)

        # Decisions required
        self._heading(doc, "Decisions required from you", 1)
        if brief.decisions_required_from_user:
            self._table(
                doc,
                ["Class", "Decision", "Recommendation"],
                [
                    [
                        d.decision_class.upper(),
                        d.question,
                        d.recommendation or "—",
                    ]
                    for d in brief.decisions_required_from_user
                ],
            )
        else:
            self._para(doc, "None. This analysis is informational.", italic=True)

        # Appendix
        doc.add_page_break()
        self._heading(doc, "Appendix — specialist work products", 1)
        if inputs.results:
            for r in inputs.results:
                artifacts = ", ".join(r.artifacts) or "none"
                doc.add_paragraph(
                    f"{r.agent_id}: task {r.task_id}, artifacts: {artifacts}",
                    style="List Bullet",
                )
        else:
            self._para(doc, "None.", italic=True)
        self._para(doc, DISCLAIMER, italic=True)

        # Write. s18.3: never overwrite a prior final report.
        self.output_dir.mkdir(parents=True, exist_ok=True)
        filename = build_filename(
            inputs.project_name, inputs.report_type, inputs.run_ref, inputs.version
        )
        path = self.output_dir / filename
        if path.exists():
            raise FileExistsError(
                f"{filename} already exists; bump the version rather than overwriting"
            )
        doc.save(str(path))

        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        log.info(
            "report_generated",
            run_ref=inputs.run_ref,
            version=inputs.version,
            filename=filename,
            bytes=path.stat().st_size,
        )
        return ReportResult(path=path, filename=filename, version=inputs.version, sha256=digest)
