"""The pre-release report gate (PRD s32.3)."""

from __future__ import annotations

from app.reports.gate import run_report_gate
from app.schemas.report import (
    ExecutiveBriefSection,
    FinalTechnicalReport,
    InformationGap,
    Reference,
    ReportMetadata,
    ReportSection,
    ReviewIssue,
    RevisionEntry,
)

REQUEST = (
    "Provide a detailed report for the Quality Target Product Profile and Critical "
    "Quality Framework for Midostaurin 25mg soft gelatine capsules. Take into account "
    "the regulatory submission criteria for an ANDA or CTD. Prepare a table of "
    "contents. Undertake a critical review and revise it accordingly. Ensure all "
    "references are double checked."
)


def brief_section(**kw) -> ExecutiveBriefSection:
    base = {
        "executive_summary": "The framework is workable under stated controls.",
        "objective_and_scope": "Define the QTPP and quality framework.",
        "overall_recommendation": "Proceed under provisional controls.",
        "confidence_and_evidence_status": "Medium; 12 evidence items.",
    }
    return ExecutiveBriefSection(**{**base, **kw})


def report(**kw) -> FinalTechnicalReport:
    base = {
        "metadata": ReportMetadata(
            document_title="QTPP and Critical Quality Framework",
            run_ref="RUN-1",
            generated_at="2026-09-04 10:00 UTC",
        ),
        "executive_brief": brief_section(),
        "body_sections": [
            ReportSection(heading="Regulatory submission framework", body="A" * 4000),
            ReportSection(heading="Quality target product profile", body="B" * 4000),
        ],
        "critical_review": [
            ReviewIssue(
                issue="Classification conflict",
                why_it_mattered="It drives the BE approach.",
                resolution="Primary source retrieved.",
                final_position="The supported classification is used.",
            )
        ],
        "revision_crosswalk": [
            RevisionEntry(
                issue="Classification conflict",
                original="An earlier draft was wrong.",
                critique="Evidence inconsistent.",
                action_taken="Retrieved the primary source.",
                revision="Corrected.",
            )
        ],
        "conclusions": "The programme can proceed under the stated controls.",
        "references": [
            Reference(
                citation_id="R1",
                organisation="FDA",
                title="Product-Specific Guidance for Midostaurin",
                document_identifier="NDA 207997",
                evidence_ref="E-1",
                verification="primary_source_verified",
            )
        ],
    }
    return FinalTechnicalReport(**{**base, **kw})


def gate(rep: FinalTechnicalReport, **kw):
    params = {
        "user_request": REQUEST,
        "known_evidence_refs": {"E-1", "E-2"},
        "superseded_refs": set(),
    }
    return run_report_gate(rep, **{**params, **kw})


def named(result, name):
    return next(c for c in result.checks if c.name == name)


def test_a_complete_report_passes_every_mandatory_check():
    result = gate(report())
    assert not result.mandatory_failures, result.reasons
    assert result.review_required is False


def test_regulatory_section_must_precede_qtpp_when_requested():
    rep = report(
        body_sections=[
            ReportSection(heading="Quality target product profile", body="B" * 4000),
            ReportSection(heading="Regulatory submission framework", body="A" * 4000),
        ]
    )
    assert named(gate(rep), "regulatory_precedes_derived_work").passed is False


def test_superseded_material_in_the_body_fails():
    rep = report(
        body_sections=[
            ReportSection(heading="Regulatory submission framework", body="A" * 3000),
            ReportSection(
                heading="Quality target product profile",
                body="As stated in ART-7 the classification is unchanged." + "B" * 3000,
            ),
        ]
    )
    result = gate(rep, superseded_refs={"ART-7"})
    assert named(result, "no_superseded_material_in_body").passed is False
    assert result.review_required is True


def test_a_superseded_ref_in_the_audit_appendix_is_fine():
    from app.schemas.report import WorkProductReference

    rep = report(
        work_products=[
            WorkProductReference(
                artifact_ref="ART-7",
                title="QTPP Rev 0",
                status="superseded",
                superseded_by_ref="ART-9",
            )
        ]
    )
    assert named(gate(rep, superseded_refs={"ART-7"}), "no_superseded_material_in_body").passed


def test_dangling_evidence_ids_fail():
    rep = report(
        body_sections=[
            ReportSection(heading="Regulatory submission framework", body="A" * 4000),
            ReportSection(
                heading="Quality target product profile",
                body="B" * 4000,
                evidence_refs=["E-1", "E-404"],
            ),
        ]
    )
    result = gate(rep)
    assert named(result, "evidence_ids_resolve").passed is False
    assert "E-404" in (named(result, "evidence_ids_resolve").detail or "")


def test_missing_critical_review_fails_when_the_request_asked_for_one():
    rep = report(critical_review=[], review_summary=None, revision_crosswalk=[])
    result = gate(rep)
    assert named(result, "critical_review_documented").passed is False
    assert result.review_required is True


def test_a_review_without_a_revision_crosswalk_fails():
    rep = report(revision_crosswalk=[])
    assert named(gate(rep), "revision_documented").passed is False


def test_an_open_conflict_must_be_disclosed():
    result = gate(report(), open_conflicts=2)
    assert named(result, "open_conflicts_disclosed").passed is False
    assert result.review_required is True


def test_a_disclosed_conflict_passes():
    rep = report(unresolved_conflicts=["EU orphan status remains unconfirmed."])
    assert named(gate(rep, open_conflicts=2), "open_conflicts_disclosed").passed


def test_an_imprecise_reference_is_flagged_without_blocking():
    rep = report(
        references=[
            Reference(
                citation_id="R1", organisation="FDA", title="FDA guidance", evidence_ref="E-1"
            )
        ]
    )
    result = gate(rep)
    check = named(result, "references_are_precise")
    assert check.passed is False
    assert check.mandatory is False
    assert result.review_required is False


def test_a_numeric_specification_with_no_evidence_is_flagged():
    rep = report(
        body_sections=[
            ReportSection(heading="Regulatory submission framework", body="A" * 4000),
            ReportSection(
                heading="Quality target product profile",
                body="Dissolution acceptance is set at 75% in 45 min." + "B" * 3000,
            ),
        ]
    )
    assert named(gate(rep), "numeric_specifications_are_sourced").passed is False


def test_the_same_specification_with_evidence_passes():
    rep = report(
        body_sections=[
            ReportSection(heading="Regulatory submission framework", body="A" * 4000),
            ReportSection(
                heading="Quality target product profile",
                body="Dissolution acceptance is set at 75% in 45 min." + "B" * 3000,
                evidence_refs=["E-1"],
            ),
        ]
    )
    assert named(gate(rep), "numeric_specifications_are_sourced").passed


def test_a_completion_blocking_gap_forces_review():
    rep = report(
        information_gaps=[
            InformationGap(
                category="regulatory",
                gap="No verified product-specific guidance.",
                blocks_completion=True,
            )
        ]
    )
    result = gate(rep)
    assert named(result, "no_completion_blocking_gap").passed is False
    assert result.review_required is True


def test_a_report_with_no_body_fails():
    assert gate(report(body_sections=[])).review_required is True


def test_a_pdf_that_was_requested_but_not_rendered_is_noted():
    result = gate(report(), pdf_expected=True, pdf_rendered=False)
    check = named(result, "pdf_rendered")
    assert check.passed is False and check.mandatory is False
