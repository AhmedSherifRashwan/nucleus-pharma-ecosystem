"""Deterministic rendering of the final technical report (PRD s18, s32).

The model wrote the content; this module draws the document and nothing else. No
paraphrasing, no reordering, no judgement about what to include: given the same
FinalTechnicalReport it produces the same file, which is what makes the
deliverable defensible.

Two documents come out of one report object: the full technical report, and the
Executive Decision Brief as a standalone short document for a reader who will not
open the technical body.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from docx import Document as new_document
from docx.document import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from app.audit.logger import get_logger
from app.reports.builder import DISCLAIMER, RELIABILITY_LABEL, build_filename
from app.schemas.report import (
    FinalTechnicalReport,
    ReportSection,
    ReportTable,
)

log = get_logger(__name__)

GENERATOR_VERSION = "2.0.0"
TEMPLATE_VERSION = "technical-1.0.0"

TABLE_STYLE = "Light Grid Accent 1"

VERIFICATION_LABEL = {
    "primary_source_verified": "Primary source verified",
    "secondary_verified": "Secondary source verified",
    "partially_verified": "PARTIALLY VERIFIED",
    "unverified": "UNVERIFIED",
    "not_retrieved": "NOT RETRIEVED",
    "conflicting_sources": "CONFLICTING SOURCES",
}

STATUS_LABEL = {
    "confirmed": "Confirmed",
    "provisional": "PROVISIONAL",
    "proposed": "Proposed",
    "screening": "Screening-level",
    "unverified": "UNVERIFIED",
}


@dataclass
class RenderedReport:
    path: Path
    filename: str
    sha256: str
    section_count: int
    word_count: int


def _field_code(paragraph, instruction: str, placeholder: str) -> None:
    """Insert a Word field. Used for page numbers and the table of contents."""
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    text = OxmlElement("w:t")
    text.text = placeholder
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for element in (begin, instr, separate, text, end):
        run._r.append(element)


class TechnicalReportRenderer:
    def __init__(self, output_dir: Path | str):
        self.output_dir = Path(output_dir).expanduser()

    # -- primitives ---------------------------------------------------------

    def _para(self, doc: DocxDocument, text: str, *, italic: bool = False, bold: bool = False):
        p = doc.add_paragraph()
        run = p.add_run(text)
        run.italic = italic
        run.bold = bold
        return p

    def _body(self, doc: DocxDocument, text: str) -> None:
        """Render composed prose. Blank lines separate paragraphs; '- ' is a bullet."""
        for block in (text or "").split("\n\n"):
            block = block.strip()
            if not block:
                continue
            lines = block.split("\n")
            if all(line.strip().startswith(("- ", "* ", "• ")) for line in lines if line.strip()):
                for line in lines:
                    if line.strip():
                        doc.add_paragraph(line.strip()[2:].strip(), style="List Bullet")
            elif (
                all(line.strip()[:2].rstrip(".").isdigit() for line in lines if line.strip())
                and len(lines) > 1
            ):
                for line in lines:
                    if line.strip():
                        doc.add_paragraph(line.strip().split(" ", 1)[-1], style="List Number")
            else:
                doc.add_paragraph(" ".join(line.strip() for line in lines))

    def _table(
        self,
        doc: DocxDocument,
        columns: list[str],
        rows: list[list[str]],
        *,
        title: str | None = None,
    ) -> None:
        if title:
            caption = doc.add_paragraph()
            run = caption.add_run(title)
            run.bold = True
            run.font.size = Pt(9)
        table = doc.add_table(rows=1, cols=len(columns))
        table.style = TABLE_STYLE
        for cell, header in zip(table.rows[0].cells, columns, strict=True):
            cell.text = header
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.bold = True
                    run.font.size = Pt(9)
        for row in rows:
            cells = table.add_row().cells
            for cell, value in zip(cells, row, strict=True):
                cell.text = str(value)
                for paragraph in cell.paragraphs:
                    for run in paragraph.runs:
                        run.font.size = Pt(9)

    def _status_block(self, doc: DocxDocument, section: ReportSection) -> None:
        """The section's standing, once, under its heading.

        Everything a reader needs to calibrate the section is here — status,
        evidence confidence, and the gaps it depends on — so the prose beneath can
        be written plainly. A caveat repeated in every paragraph stops being read;
        one stated where the reader starts does not.
        """
        parts: list[str] = []
        if section.status:
            parts.append(f"Section status: {STATUS_LABEL.get(section.status, section.status)}")
        if section.evidence_confidence:
            parts.append(
                f"Evidence confidence: {section.evidence_confidence.replace('_', ' ').title()}"
            )
        if section.open_gap_refs:
            parts.append(f"Unresolved gaps: {', '.join(section.open_gap_refs)}")
        if not parts and not section.status_note:
            return

        paragraph = doc.add_paragraph()
        if parts:
            run = paragraph.add_run(" · ".join(parts))
            run.bold = True
            run.font.size = Pt(9)
        if section.status_note:
            if parts:
                paragraph.add_run("\n")
            note = paragraph.add_run(section.status_note)
            note.italic = True
            note.font.size = Pt(9)

    def _report_table(self, doc: DocxDocument, table: ReportTable) -> None:
        self._table(doc, table.columns, table.rows, title=table.title)
        if table.note:
            self._para(doc, table.note, italic=True)

    def _section(self, doc: DocxDocument, section: ReportSection, level: int, number: str) -> None:
        doc.add_heading(f"{number} {section.heading}", level=min(level, 4))
        self._status_block(doc, section)
        self._body(doc, section.body)
        for table in section.tables:
            self._report_table(doc, table)
        if section.evidence_refs:
            self._para(doc, "Evidence: " + ", ".join(section.evidence_refs), italic=True)
        for index, child in enumerate(section.subsections, start=1):
            self._section(doc, child, level + 1, f"{number}.{index}")

    def _page_numbers(self, doc: DocxDocument, run_ref: str) -> None:
        section = doc.sections[0]
        header = section.header.paragraphs[0]
        header.text = run_ref
        header.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        footer = section.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        footer.add_run("Page ")
        _field_code(footer, "PAGE", "1")
        footer.add_run(" of ")
        _field_code(footer, "NUMPAGES", "1")

    # -- documents ----------------------------------------------------------

    def _cover(self, doc: DocxDocument, report: FinalTechnicalReport) -> None:
        meta = report.metadata
        title = doc.add_heading(meta.document_title, level=0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER

        if meta.status.upper().startswith("REVIEW"):
            warning = doc.add_paragraph()
            warning.alignment = WD_ALIGN_PARAGRAPH.CENTER
            flag = warning.add_run("REVIEW REQUIRED")
            flag.bold = True
            flag.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)

        rows = [
            ["Product", meta.product or "—"],
            ["Strength", meta.strength or "—"],
            ["Dosage form", meta.dosage_form or "—"],
            ["Run reference", meta.run_ref],
            ["Report version", str(meta.report_version)],
            ["Generated", meta.generated_at],
            ["Status", meta.status],
            ["Overall confidence", meta.confidence.replace("_", " ").title()],
            ["Human review", meta.human_review_status],
        ]
        self._table(doc, ["Field", "Value"], rows)
        self._para(doc, DISCLAIMER, italic=True)
        doc.add_page_break()

    def _document_control(self, doc: DocxDocument, report: FinalTechnicalReport) -> None:
        meta = report.metadata
        doc.add_heading("Document control", level=1)
        rows = [
            ["Report version", str(meta.report_version)],
            ["Supersedes", str(meta.supersedes_version) if meta.supersedes_version else "—"],
            ["Generated", meta.generated_at],
            ["Evidence cut-off", meta.evidence_cutoff or "—"],
            ["Template version", meta.template_version or TEMPLATE_VERSION],
            ["Generator version", meta.generator_version or GENERATOR_VERSION],
            ["Review status", meta.human_review_status],
            [
                "Contributing profiles",
                ", ".join(f"{k} {v}" for k, v in sorted(meta.agent_versions.items())) or "—",
            ],
        ]
        self._table(doc, ["Field", "Value"], rows)

    def _brief(self, doc: DocxDocument, report: FinalTechnicalReport) -> None:
        brief = report.executive_brief
        doc.add_heading("Executive Decision Brief", level=1)
        doc.add_heading("Executive summary", level=2)
        self._body(doc, brief.executive_summary)
        doc.add_heading("Objective and scope", level=2)
        self._body(doc, brief.objective_and_scope)
        doc.add_heading("Overall recommendation", level=2)
        self._body(doc, brief.overall_recommendation)
        doc.add_heading("Confidence and evidence status", level=2)
        self._body(doc, brief.confidence_and_evidence_status)

        for heading, items in (
            ("Most important findings", brief.key_findings),
            ("Major risks", brief.major_risks),
            ("Critical assumptions", brief.critical_assumptions),
            ("Outstanding information", brief.outstanding_information),
            ("Decisions required from you", brief.decisions_required),
        ):
            doc.add_heading(heading, level=2)
            if items:
                for item in items:
                    doc.add_paragraph(item, style="List Bullet")
            else:
                self._para(doc, "None identified.", italic=True)

    def _toc(self, doc: DocxDocument, report: FinalTechnicalReport) -> None:
        """A real Word TOC field, plus a rendered listing.

        The field populates when the reader opens the document in Word; the
        listing means the contents are legible in a viewer that never updates
        fields, which is most of them.
        """
        doc.add_heading("Table of contents", level=1)
        paragraph = doc.add_paragraph()
        _field_code(paragraph, r'TOC \o "1-3" \h \z \u', "Right-click and update this field.")

        for index, section in enumerate(report.body_sections, start=1):
            doc.add_paragraph(f"{index}. {section.heading}")
            for sub_index, sub in enumerate(section.subsections, start=1):
                p = doc.add_paragraph(f"    {index}.{sub_index}  {sub.heading}")
                p.paragraph_format.left_indent = Pt(18)
        tail = len(report.body_sections)
        for offset, heading in enumerate(_closing_headings(report), start=1):
            doc.add_paragraph(f"{tail + offset}. {heading}")

    def _closing(self, doc: DocxDocument, report: FinalTechnicalReport, start: int) -> None:
        number = start

        if report.critical_review or report.review_summary:
            doc.add_heading(f"{number}. Critical review", level=1)
            if report.review_summary:
                self._body(doc, report.review_summary)
            for issue in report.critical_review:
                doc.add_heading(issue.issue, level=2)
                self._table(
                    doc,
                    ["Field", "Detail"],
                    [
                        ["Severity", issue.severity.title()],
                        ["Why it mattered", issue.why_it_mattered],
                        ["Original position", issue.original_position or "—"],
                        ["Evidence reviewed", ", ".join(issue.evidence_reviewed) or "—"],
                        ["Resolution", issue.resolution],
                        ["Final position", issue.final_position],
                        ["Residual uncertainty", issue.residual_uncertainty or "None stated"],
                        ["State", issue.state.replace("_", " ").title()],
                    ],
                )
            number += 1

        if report.revision_crosswalk:
            doc.add_heading(f"{number}. Revision following critical review", level=1)
            self._table(
                doc,
                ["Issue", "Critique", "Action taken", "Revision", "State"],
                [
                    [r.issue, r.critique, r.action_taken, r.revision, r.state.replace("_", " ")]
                    for r in report.revision_crosswalk
                ],
            )
            number += 1

        if report.unresolved_conflicts:
            doc.add_heading(f"{number}. Unresolved disagreements", level=1)
            for item in report.unresolved_conflicts:
                doc.add_paragraph(item, style="List Bullet")
            number += 1

        if report.information_gaps:
            doc.add_heading(f"{number}. Outstanding information and development gaps", level=1)
            by_category: dict[str, list] = {}
            for gap in report.information_gaps:
                by_category.setdefault(gap.category, []).append(gap)
            for category, gaps in by_category.items():
                doc.add_heading(category.title(), level=2)
                self._table(
                    doc,
                    ["Gap", "Why it matters", "How to close it", "Blocks completion"],
                    [
                        [
                            g.gap,
                            g.why_it_matters or "—",
                            g.how_to_close or "—",
                            "Yes" if g.blocks_completion else "No",
                        ]
                        for g in gaps
                    ],
                )
            number += 1

        doc.add_heading(f"{number}. Conclusions", level=1)
        if report.conclusions:
            self._body(doc, report.conclusions)
        else:
            self._para(doc, "No integrated conclusion was produced.", italic=True)
        number += 1

        doc.add_heading(f"{number}. Recommended next actions", level=1)
        if report.recommended_actions:
            self._table(
                doc,
                ["Priority", "Action", "Rationale", "Owner function"],
                [
                    [a.priority.title(), a.action, a.rationale or "—", a.owner_function or "—"]
                    for a in report.recommended_actions
                ],
            )
        else:
            self._para(doc, "None identified.", italic=True)
        number += 1

        doc.add_heading(f"{number}. References", level=1)
        if report.references:
            for reference in report.references:
                paragraph = doc.add_paragraph()
                paragraph.add_run(f"[{reference.citation_id}] ").bold = True
                bits = [reference.organisation, reference.title]
                for value in (
                    reference.document_identifier,
                    reference.version_or_revision,
                    reference.published_or_revised,
                    reference.authority,
                    reference.relevant_locator,
                ):
                    if value:
                        bits.append(value)
                paragraph.add_run(". ".join(b.rstrip(".") for b in bits if b) + ".")
                detail = []
                if reference.url_or_locator:
                    detail.append(reference.url_or_locator)
                if reference.retrieved_at:
                    detail.append(f"Retrieved {reference.retrieved_at}")
                if reference.evidence_ref:
                    detail.append(f"Evidence {reference.evidence_ref}")
                detail.append(
                    VERIFICATION_LABEL.get(reference.verification, reference.verification)
                )
                self._para(doc, " · ".join(detail), italic=True)
        else:
            self._para(doc, "No references were recorded for this report.", italic=True)
        number += 1

        doc.add_page_break()
        doc.add_heading(f"{number}. Evidence and provenance appendix", level=1)
        self._evidence_appendix(doc, report)
        number += 1

        doc.add_heading(f"{number}. Specialist work product and audit appendix", level=1)
        self._para(
            doc,
            "The complete execution record — every task, result, correction and "
            "superseded draft — is retained in the system of record. This appendix "
            "lists the work products behind this report. Superseded drafts are shown "
            "for traceability; their content does not inform the report above.",
            italic=True,
        )
        if report.work_products:
            self._table(
                doc,
                ["Reference", "Title", "Status", "Superseded by"],
                [
                    [w.artifact_ref, w.title[:90], w.status.title(), w.superseded_by_ref or "—"]
                    for w in report.work_products
                ],
            )
        else:
            self._para(doc, "None recorded.", italic=True)

    def _evidence_appendix(self, doc: DocxDocument, report: FinalTechnicalReport) -> None:
        if not report.references:
            self._para(doc, "No evidence items were recorded for this run.", italic=True)
            return
        self._table(
            doc,
            ["Evidence", "Source", "Identifier", "Reliability", "Verification"],
            [
                [
                    r.evidence_ref or "—",
                    f"{r.organisation} — {r.title}"[:90],
                    r.document_identifier or "—",
                    RELIABILITY_LABEL.get(r.reliability_level or 0, "—"),
                    VERIFICATION_LABEL.get(r.verification, r.verification),
                ]
                for r in report.references
            ],
        )

    # -- entry points -------------------------------------------------------

    def render(
        self,
        report: FinalTechnicalReport,
        *,
        project_name: str,
        report_type: str = "Technical_Report",
    ) -> RenderedReport:
        doc = new_document()
        self._page_numbers(doc, report.metadata.run_ref)
        self._cover(doc, report)
        self._document_control(doc, report)
        self._brief(doc, report)
        doc.add_page_break()
        self._toc(doc, report)
        doc.add_page_break()

        for index, section in enumerate(report.body_sections, start=1):
            self._section(doc, section, 1, str(index))

        self._closing(doc, report, len(report.body_sections) + 1)
        return self._save(doc, report, project_name, report_type)

    def render_brief(self, report: FinalTechnicalReport, *, project_name: str) -> RenderedReport:
        """The Executive Decision Brief as a document of its own."""
        doc = new_document()
        self._page_numbers(doc, report.metadata.run_ref)
        self._cover(doc, report)
        self._brief(doc, report)
        doc.add_heading("Where the detail is", level=1)
        self._para(
            doc,
            "This brief accompanies the full technical report for the same run "
            f"({report.metadata.run_ref}, version {report.metadata.report_version}), "
            "which carries the supporting analysis, the evidence register and the "
            "precise references.",
        )
        if report.body_sections:
            for index, section in enumerate(report.body_sections, start=1):
                doc.add_paragraph(f"{index}. {section.heading}", style="List Bullet")
        return self._save(doc, report, project_name, "Executive_Decision_Brief")

    def _save(
        self, doc: DocxDocument, report: FinalTechnicalReport, project_name: str, report_type: str
    ) -> RenderedReport:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        filename = build_filename(
            project_name, report_type, report.metadata.run_ref, report.metadata.report_version
        )
        path = self.output_dir / filename
        if path.exists():
            raise FileExistsError(
                f"{filename} already exists; bump the version rather than overwriting"
            )
        doc.save(str(path))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        words = sum(len(p.text.split()) for p in doc.paragraphs)
        log.info(
            "technical_report_rendered",
            filename=filename,
            sections=len(report.body_sections),
            words=words,
            bytes=path.stat().st_size,
        )
        return RenderedReport(
            path=path,
            filename=filename,
            sha256=digest,
            section_count=len(report.body_sections),
            word_count=words,
        )


def _closing_headings(report: FinalTechnicalReport) -> list[str]:
    headings = []
    if report.critical_review or report.review_summary:
        headings.append("Critical review")
    if report.revision_crosswalk:
        headings.append("Revision following critical review")
    if report.unresolved_conflicts:
        headings.append("Unresolved disagreements")
    if report.information_gaps:
        headings.append("Outstanding information and development gaps")
    headings += [
        "Conclusions",
        "Recommended next actions",
        "References",
        "Evidence and provenance appendix",
        "Specialist work product and audit appendix",
    ]
    return headings
