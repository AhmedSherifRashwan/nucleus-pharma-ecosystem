"""The final technical report contract (PRD s18, s32).

A report is a structured object before it is a document. The model composes this
structure section by section; Python renders it. Nothing here describes DOCX:
that separation is what keeps the deliverable deterministic and lets the quality
gate inspect the report before a single paragraph is drawn.

The shape is domain-shaped, not execution-shaped. There is no section per agent
and no chronology: the reader receives one coherent report, and the record of how
it got there lives in the critical review, the revision crosswalk and the audit
appendix.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from app.schemas.common import ConfidenceLevel, ReliabilityLevel, StrictModel

# How solid a stated position is. `provisional` is the honest middle: derived
# from screening-level work, usable, not yet a commitment.
ClaimStatus = Literal["confirmed", "provisional", "proposed", "screening", "unverified"]

# s32.3 reference verification. A citation nobody retrieved must say so.
VerificationStatus = Literal[
    "primary_source_verified",
    "secondary_verified",
    "partially_verified",
    "unverified",
    "not_retrieved",
    "conflicting_sources",
]

Criticality = Literal["high", "medium", "low", "undetermined"]


class ReportTable(StrictModel):
    """A table the renderer draws verbatim. Rows must match the header width."""

    title: str | None = None
    columns: list[str] = Field(min_length=1, max_length=12)
    rows: list[list[str]] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _square_the_rows(cls, data: object) -> object:
        """Make every row the width of the header.

        A ragged row is a formatting slip, not a reason to lose the section it
        sits in - which is what rejecting the table used to cost. A short row is
        padded; a long one has its surplus folded into the final cell, so no
        content is dropped on the way to the page.
        """
        if not isinstance(data, dict):
            return data
        columns = data.get("columns") or []
        rows = data.get("rows") or []
        width = len(columns)
        if not width or not isinstance(rows, list):
            return data

        squared: list[list[str]] = []
        for row in rows:
            cells = [str(c) for c in row] if isinstance(row, list) else [str(row)]
            if len(cells) < width:
                cells = cells + ["—"] * (width - len(cells))
            elif len(cells) > width:
                cells = cells[: width - 1] + [" ".join(cells[width - 1 :])]
            squared.append(cells)
        return {**data, "rows": squared}

    @model_validator(mode="after")
    def _rows_match_columns(self) -> ReportTable:
        width = len(self.columns)
        for index, row in enumerate(self.rows):
            if len(row) != width:
                raise ValueError(
                    f"row {index} has {len(row)} cells but the table has {width} columns"
                )
        return self


class ReportSection(StrictModel):
    """One numbered section. Subsections nest one level (Heading 2 / Heading 3)."""

    heading: str = Field(min_length=1, max_length=200)
    body: str = ""
    tables: list[ReportTable] = Field(default_factory=list)
    subsections: list[ReportSection] = Field(default_factory=list)
    # Evidence ids supporting this section, rendered as a citation line.
    evidence_refs: list[str] = Field(default_factory=list)
    status: ClaimStatus | None = None
    # The writer's own words about the section's standing, kept verbatim. A
    # section is rarely uniformly confirmed or provisional, and flattening
    # "established mechanism, provisional limits" to one code loses the part a
    # reader needs.
    status_note: str | None = Field(default=None, max_length=400)
    # Stated once in the section's status block so the prose does not have to
    # hedge every sentence. A caveat repeated forty times stops being read.
    evidence_confidence: ConfidenceLevel | None = None
    open_gap_refs: list[str] = Field(default_factory=list)

    def walk(self) -> list[tuple[int, ReportSection]]:
        """This section and its descendants, with depth (0 for self)."""
        out: list[tuple[int, ReportSection]] = [(0, self)]
        for child in self.subsections:
            out.extend((depth + 1, node) for depth, node in child.walk())
        return out

    @property
    def text_length(self) -> int:
        total = len(self.body)
        for table in self.tables:
            total += sum(len(cell) for row in table.rows for cell in row)
        return total + sum(s.text_length for s in self.subsections)


class Reference(StrictModel):
    """A precise citation (s32.3).

    Vague references are the failure mode this model exists to prevent: a report
    that says "FDA guidance" cannot be checked by the person who has to defend it.
    """

    citation_id: str = Field(min_length=1, max_length=16)
    organisation: str = Field(min_length=1)
    title: str = Field(min_length=1)
    document_identifier: str | None = None
    version_or_revision: str | None = None
    published_or_revised: str | None = None
    authority: str | None = None
    relevant_locator: str | None = None
    url_or_locator: str | None = None
    retrieved_at: str | None = None
    evidence_ref: str | None = None
    reliability_level: ReliabilityLevel | None = None
    verification: VerificationStatus = "unverified"
    note: str | None = None

    @property
    def is_precise(self) -> bool:
        """Enough detail for a third party to find the exact document."""
        return bool(self.document_identifier or self.url_or_locator)


class ReviewIssue(StrictModel):
    """One material issue raised by the adversarial review, and its disposition."""

    issue: str = Field(min_length=1)
    severity: Literal["critical", "high", "medium", "low"] = "medium"
    why_it_mattered: str = Field(min_length=1)
    original_position: str | None = None
    evidence_reviewed: list[str] = Field(default_factory=list)
    resolution: str = Field(min_length=1)
    final_position: str = Field(min_length=1)
    residual_uncertainty: str | None = None
    state: Literal["resolved", "partially_resolved", "open"] = "resolved"


class RevisionEntry(StrictModel):
    """A crosswalk row: what changed after the critique, and why."""

    issue: str = Field(min_length=1)
    original: str = Field(min_length=1)
    critique: str = Field(min_length=1)
    action_taken: str = Field(min_length=1)
    revision: str = Field(min_length=1)
    state: Literal["resolved", "partially_resolved", "open"] = "resolved"
    superseded_artifact_refs: list[str] = Field(default_factory=list)


class InformationGap(StrictModel):
    # Assigned deterministically after composition (G-01, G-02, ...) so a section
    # can point at a gap instead of restating it.
    gap_id: str | None = None
    category: Literal[
        "regulatory", "scientific", "analytical", "formulation", "manufacturing", "quality"
    ]
    gap: str = Field(min_length=1)
    why_it_matters: str | None = None
    how_to_close: str | None = None
    blocks_completion: bool = False


class Action(StrictModel):
    priority: Literal["immediate", "high", "medium", "low"] = "medium"
    action: str = Field(min_length=1)
    rationale: str | None = None
    owner_function: str | None = None
    closes_gap: str | None = None


class WorkProductReference(StrictModel):
    """An audit-appendix entry. Superseded drafts appear here and nowhere else."""

    artifact_ref: str
    title: str
    originating_agent: str | None = None
    status: Literal["current", "superseded", "draft", "approved"] = "current"
    superseded_by_ref: str | None = None
    size_bytes: int | None = None


class ReportMetadata(StrictModel):
    document_title: str = Field(min_length=1)
    subject: str | None = None
    product: str | None = None
    strength: str | None = None
    dosage_form: str | None = None
    run_ref: str
    report_version: int = 1
    generated_at: str
    status: str = "REVIEW REQUIRED"
    confidence: ConfidenceLevel = "medium"
    human_review_status: str = "Pending human review"
    supersedes_version: int | None = None
    evidence_cutoff: str | None = None
    agent_versions: dict[str, str] = Field(default_factory=dict)
    model_versions: dict[str, str] = Field(default_factory=dict)
    template_version: str | None = None
    generator_version: str | None = None
    # s11: contributors whose role definition is the client's framework role table
    # rather than a specification written for that role. Printed on the document
    # control table so a reviewer can weigh their contribution accordingly.
    framework_scoped_agents: dict[str, str] = Field(default_factory=dict)


class ExecutiveBriefSection(StrictModel):
    """The 1-3 page front section. Not a replacement for the technical report."""

    executive_summary: str = Field(min_length=1)
    objective_and_scope: str = Field(min_length=1)
    overall_recommendation: str = Field(min_length=1)
    confidence_and_evidence_status: str = Field(min_length=1)
    major_risks: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)
    critical_assumptions: list[str] = Field(default_factory=list)
    outstanding_information: list[str] = Field(default_factory=list)
    decisions_required: list[str] = Field(default_factory=list)


class FinalTechnicalReport(StrictModel):
    """The whole deliverable, as data.

    `body_sections` is dynamic: the outline is derived from what the user asked
    for and what the specialists actually produced, not from a fixed template. The
    fixed parts are the ones a regulated deliverable always needs - review,
    revision, gaps, conclusions, references, provenance, audit.
    """

    metadata: ReportMetadata
    executive_brief: ExecutiveBriefSection
    body_sections: list[ReportSection] = Field(default_factory=list)
    critical_review: list[ReviewIssue] = Field(default_factory=list)
    review_summary: str | None = None
    revision_crosswalk: list[RevisionEntry] = Field(default_factory=list)
    information_gaps: list[InformationGap] = Field(default_factory=list)
    conclusions: str = ""
    recommended_actions: list[Action] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    work_products: list[WorkProductReference] = Field(default_factory=list)
    unresolved_conflicts: list[str] = Field(default_factory=list)

    @property
    def body_length(self) -> int:
        return sum(s.text_length for s in self.body_sections)

    def section_headings(self) -> list[str]:
        return [s.heading for s in self.body_sections]
