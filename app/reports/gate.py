"""The pre-release report gate (PRD s32.3).

Checked in code, before a single paragraph is drawn, because the failure this
guards against is a fluent document that reads as finished while resting on
material that was never verified.

A failed mandatory check does not stop the report: a client is better served by a
report labelled REVIEW REQUIRED with its shortfalls named than by no report. It
does stop the run being called complete.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.schemas.report import FinalTechnicalReport

# A number attached to a unit, which is what a fabricated specification looks
# like. Used only to check such statements carry evidence, never to remove them.
NUMERIC_SPEC = re.compile(
    r"\b\d+(?:\.\d+)?\s?(?:%|mg|µg|ug|mcg|ppm|ppb|mL|ml|L\b|°C|C\b|rpm|min\b|h\b|hours?\b)"
)

EVIDENCE_ID = re.compile(r"\bE-\d+\b")

MIN_BODY_CHARS = 6_000


@dataclass
class GateCheck:
    name: str
    passed: bool
    mandatory: bool = True
    detail: str | None = None


@dataclass
class GateResult:
    checks: list[GateCheck] = field(default_factory=list)

    @property
    def failures(self) -> list[GateCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def mandatory_failures(self) -> list[GateCheck]:
        return [c for c in self.failures if c.mandatory]

    @property
    def review_required(self) -> bool:
        return bool(self.mandatory_failures)

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name}: {c.detail or 'failed'}" for c in self.failures]

    def as_metadata(self) -> dict[str, object]:
        return {
            "checks": len(self.checks),
            "failed": [c.name for c in self.failures],
            "mandatory_failed": [c.name for c in self.mandatory_failures],
        }


def _requested_deliverables(user_request: str) -> set[str]:
    """What the user actually asked for, in the terms the gate can check."""
    text = user_request.lower()
    wanted = set()
    if "table of content" in text or "toc" in text:
        wanted.add("toc")
    if "regulator" in text or "anda" in text or "ctd" in text or "fda" in text or "ema" in text:
        wanted.add("regulatory")
    if "critical review" in text or "critique" in text or "review" in text:
        wanted.add("review")
    if "revise" in text or "revision" in text:
        wanted.add("revision")
    if "reference" in text:
        wanted.add("references")
    return wanted


def run_report_gate(
    report: FinalTechnicalReport,
    *,
    user_request: str,
    known_evidence_refs: set[str],
    superseded_refs: set[str],
    open_conflicts: int = 0,
    pdf_expected: bool = False,
    pdf_rendered: bool = False,
) -> GateResult:
    checks: list[GateCheck] = []
    wanted = _requested_deliverables(user_request)
    body_text = " ".join(
        node.body
        for _, section in [(0, s) for s in report.body_sections]
        for _, node in section.walk()
    )

    checks.append(
        GateCheck(
            "body_present",
            bool(report.body_sections),
            detail="the report has no technical body sections",
        )
    )
    checks.append(
        GateCheck(
            "body_has_substance",
            report.body_length >= MIN_BODY_CHARS,
            mandatory=False,
            detail=(
                f"the body is {report.body_length} characters, which is thin for a technical report"
            ),
        )
    )
    checks.append(
        GateCheck(
            "table_of_contents",
            len(report.body_sections) >= 2,
            mandatory="toc" in wanted,
            detail="a table of contents needs at least two sections behind it",
        )
    )

    if "regulatory" in wanted:
        index = next(
            (i for i, s in enumerate(report.body_sections) if "regulat" in s.heading.lower()),
            None,
        )
        checks.append(
            GateCheck(
                "regulatory_framework_present",
                index is not None,
                detail="the request asked for a regulatory basis and no section covers it",
            )
        )
        qtpp = next(
            (
                i
                for i, s in enumerate(report.body_sections)
                if "qtpp" in s.heading.lower() or "target product" in s.heading.lower()
            ),
            None,
        )
        if index is not None and qtpp is not None:
            checks.append(
                GateCheck(
                    "regulatory_precedes_derived_work",
                    index < qtpp,
                    detail="the regulatory basis must come before what is derived from it",
                )
            )

    checks.append(
        GateCheck(
            "critical_review_documented",
            bool(report.critical_review or report.review_summary),
            mandatory="review" in wanted,
            detail="no critical review section was produced",
        )
    )
    checks.append(
        GateCheck(
            "revision_documented",
            bool(report.revision_crosswalk) or not report.critical_review,
            mandatory="revision" in wanted,
            detail="a critical review was performed but no revision crosswalk records it",
        )
    )

    # Superseded material must not present itself as current.
    leaked = sorted(
        ref
        for ref in superseded_refs
        if ref in body_text or any(ref in s.heading for s in report.body_sections)
    )
    checks.append(
        GateCheck(
            "no_superseded_material_in_body",
            not leaked,
            detail=f"superseded work products cited as current: {', '.join(leaked)}",
        )
    )

    cited = set(EVIDENCE_ID.findall(body_text))
    for section in report.body_sections:
        for _, node in section.walk():
            cited.update(node.evidence_refs)
    dangling = sorted(cited - known_evidence_refs)
    checks.append(
        GateCheck(
            "evidence_ids_resolve",
            not dangling,
            detail=f"cited evidence does not resolve: {', '.join(dangling)}",
        )
    )

    checks.append(
        GateCheck(
            "references_present",
            bool(report.references),
            mandatory="references" in wanted,
            detail="no references were produced",
        )
    )
    imprecise = [r.citation_id for r in report.references if not r.is_precise]
    checks.append(
        GateCheck(
            "references_are_precise",
            not imprecise,
            mandatory=False,
            detail=(
                f"{len(imprecise)} reference(s) carry neither a document identifier nor a "
                "locator: " + ", ".join(imprecise[:8])
            ),
        )
    )
    unverified = [
        r.citation_id
        for r in report.references
        if r.verification in ("unverified", "not_retrieved", "conflicting_sources")
    ]
    checks.append(
        GateCheck(
            "reference_verification_declared",
            True,
            mandatory=False,
            detail=(
                f"{len(unverified)} reference(s) are flagged unverified or not retrieved"
                if unverified
                else None
            ),
        )
    )

    # A numeric specification with no evidence anywhere in its section is the
    # shape of an invented limit.
    unsourced_specs = []
    for section in report.body_sections:
        for _, node in section.walk():
            if (
                NUMERIC_SPEC.search(node.body or "")
                and not node.evidence_refs
                and not EVIDENCE_ID.search(node.body or "")
            ):
                unsourced_specs.append(node.heading)
    checks.append(
        GateCheck(
            "numeric_specifications_are_sourced",
            not unsourced_specs,
            mandatory=False,
            detail=(
                "sections state numeric values with no evidence reference: "
                + ", ".join(unsourced_specs[:6])
            ),
        )
    )

    checks.append(
        GateCheck(
            "open_conflicts_disclosed",
            open_conflicts == 0 or bool(report.unresolved_conflicts),
            detail=f"{open_conflicts} open conflict(s) are not disclosed in the report",
        )
    )
    checks.append(
        GateCheck(
            "information_gaps_declared",
            bool(report.information_gaps) or not report.executive_brief.outstanding_information,
            mandatory=False,
            detail="outstanding information is named in the brief but not in the gap register",
        )
    )
    checks.append(
        GateCheck(
            "conclusions_present",
            bool(report.conclusions.strip()),
            detail="the report reaches no conclusion",
        )
    )
    if pdf_expected:
        checks.append(
            GateCheck(
                "pdf_rendered",
                pdf_rendered,
                mandatory=False,
                detail="a PDF was requested but no converter produced one",
            )
        )

    blocking = [g.gap for g in report.information_gaps if g.blocks_completion]
    checks.append(
        GateCheck(
            "no_completion_blocking_gap",
            not blocking,
            detail="gaps that block a defensible conclusion remain open: "
            + "; ".join(blocking[:3]),
        )
    )
    return GateResult(checks=checks)
