"""Rendering the final technical report (PRD s18, s32)."""

from __future__ import annotations

import pytest
from docx import Document

from app.reports.naming import document_name, is_auto_named, subject_from_request
from app.reports.technical_builder import TechnicalReportRenderer
from app.schemas.report import (
    Action,
    ExecutiveBriefSection,
    FinalTechnicalReport,
    InformationGap,
    Reference,
    ReportMetadata,
    ReportSection,
    ReportTable,
    ReviewIssue,
    RevisionEntry,
    WorkProductReference,
)

DETAIL = (
    "The fill is a lipid-based system in which the API is held in solution. "
    "Precipitation on storage would change the dissolution profile without any "
    "change in assay, so the discriminating test is a paired dissolution study. "
)


def report(**kw) -> FinalTechnicalReport:
    base = {
        "metadata": ReportMetadata(
            document_title="QTPP and Critical Quality Framework — Midostaurin 25 mg Softgel",
            product="Midostaurin",
            strength="25 mg",
            dosage_form="Soft gelatin capsule",
            run_ref="RUN-2026-0006",
            generated_at="2026-09-04 11:51 UTC",
            status="REVIEW REQUIRED",
            confidence="medium",
        ),
        "executive_brief": ExecutiveBriefSection(
            executive_summary="The framework is workable under provisional controls.",
            objective_and_scope="Define the QTPP and the critical quality framework.",
            overall_recommendation="Proceed as a US-focused working draft.",
            confidence_and_evidence_status="Medium confidence; 31 evidence items.",
            major_risks=["EU timing rests on an unconfirmed assumption."],
            outstanding_information=["EU orphan status not independently verified."],
        ),
        "body_sections": [
            ReportSection(
                heading="Regulatory submission framework",
                body=DETAIL * 6,
                subsections=[
                    ReportSection(heading="United States pathway", body=DETAIL * 3),
                    ReportSection(heading="European pathway", body=DETAIL * 3),
                ],
                evidence_refs=["E-1"],
            ),
            ReportSection(
                heading="Quality target product profile",
                body=DETAIL * 6,
                status="provisional",
                tables=[
                    ReportTable(
                        title="QTPP register",
                        columns=["Element", "Target", "Rationale", "Status"],
                        rows=[
                            [
                                "Dissolution",
                                "Q=75% at 45 min",
                                "Release-controlling",
                                "Provisional",
                            ],
                            ["Assay", "95.0-105.0%", "Compendial", "Provisional"],
                        ],
                    )
                ],
            ),
        ],
        "critical_review": [
            ReviewIssue(
                issue="BCS classification conflict",
                severity="high",
                why_it_mattered="It determines the bioequivalence strategy.",
                original_position="An earlier draft stated an unsupported classification.",
                resolution="Primary regulatory evidence was retrieved.",
                final_position="The evidence-supported classification is used throughout.",
                residual_uncertainty="Permeability data remain indirect.",
            )
        ],
        "revision_crosswalk": [
            RevisionEntry(
                issue="BCS classification conflict",
                original="An earlier draft stated an unsupported classification.",
                critique="The cited evidence did not support the claim.",
                action_taken="Primary regulatory source retrieved.",
                revision="The report states only the supported position.",
                superseded_artifact_refs=["ART-7"],
            )
        ],
        "information_gaps": [
            InformationGap(
                category="regulatory",
                gap="EU orphan status not independently confirmed.",
                why_it_matters="It affects filing timing.",
                how_to_close="Request confirmation from the authority.",
            )
        ],
        "conclusions": "The programme can proceed under the stated provisional controls.",
        "recommended_actions": [Action(priority="high", action="Confirm EU orphan status.")],
        "references": [
            Reference(
                citation_id="R1",
                organisation="U.S. Food and Drug Administration",
                title="Product-Specific Guidance for Midostaurin Oral Capsule",
                document_identifier="Reference NDA 207997",
                published_or_revised="Revised May 2022",
                relevant_locator="Recommended Studies",
                url_or_locator="https://www.accessdata.fda.gov/",
                retrieved_at="2026-09-04",
                evidence_ref="E-1",
                reliability_level=1,
                verification="primary_source_verified",
            ),
            Reference(
                citation_id="R2",
                organisation="European Medicines Agency",
                title="Orphan designation register entry",
                evidence_ref="E-2",
                verification="not_retrieved",
            ),
        ],
        "work_products": [
            WorkProductReference(artifact_ref="ART-9", title="QTPP Revision 1", status="current"),
            WorkProductReference(
                artifact_ref="ART-7",
                title="QTPP Revision 0",
                status="superseded",
                superseded_by_ref="ART-9",
            ),
        ],
    }
    return FinalTechnicalReport(**{**base, **kw})


def text_of(path) -> str:
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        for row in table.rows:
            parts.extend(cell.text for cell in row.cells)
    return "\n".join(parts)


def headings_of(path) -> list[str]:
    doc = Document(str(path))
    return [p.text for p in doc.paragraphs if p.style.name.startswith("Heading")]


def render(tmp_path, rep=None):
    return TechnicalReportRenderer(tmp_path).render(
        rep or report(), project_name="Midostaurin 25 mg Softgel"
    )


# -- structure ---------------------------------------------------------------


def test_the_document_carries_every_required_section(tmp_path):
    out = render(tmp_path)
    headings = " | ".join(headings_of(out.path))
    for expected in (
        "Document control",
        "Executive Decision Brief",
        "Table of contents",
        "Regulatory submission framework",
        "Quality target product profile",
        "Critical review",
        "Revision following critical review",
        "Outstanding information",
        "Conclusions",
        "Recommended next actions",
        "References",
        "Evidence and provenance appendix",
        "Specialist work product and audit appendix",
    ):
        assert expected in headings, f"missing: {expected}"


def test_sections_are_numbered_in_document_order(tmp_path):
    headings = headings_of(render(tmp_path).path)
    assert "1 Regulatory submission framework" in headings
    assert "1.1 United States pathway" in headings
    assert "2 Quality target product profile" in headings


def test_an_actual_table_of_contents_is_generated(tmp_path):
    body = text_of(render(tmp_path).path)
    assert "1. Regulatory submission framework" in body
    assert "1.1  United States pathway" in body
    assert "2. Quality target product profile" in body


def test_the_document_has_no_agent_chronology(tmp_path):
    """s18: the reader receives a report, not a transcript."""
    body = text_of(render(tmp_path).path)
    for leak in ("A02 —", "A03 —", "task_id", "T1 ", "specialist result"):
        assert leak not in body, f"execution detail leaked: {leak}"


def test_detailed_specialist_content_is_preserved(tmp_path):
    out = render(tmp_path)
    body = text_of(out.path)
    assert "held in solution" in body
    assert out.word_count > 400


def test_tables_are_rendered_with_their_rows(tmp_path):
    body = text_of(render(tmp_path).path)
    assert "Q=75% at 45 min" in body
    assert "95.0-105.0%" in body


def test_a_provisional_section_says_so(tmp_path):
    assert "PROVISIONAL" in text_of(render(tmp_path).path)


# -- supersession ------------------------------------------------------------


def test_a_superseded_draft_appears_only_in_the_audit_appendix(tmp_path):
    doc = Document(str(render(tmp_path).path))
    body_paragraphs = "\n".join(p.text for p in doc.paragraphs)
    # ART-7 is named in the appendix table, never in the report's prose.
    assert "ART-7" not in body_paragraphs
    appendix = "\n".join(cell.text for t in doc.tables for row in t.rows for cell in row.cells)
    assert "ART-7" in appendix
    assert "Superseded" in appendix


def test_the_critical_review_states_that_an_earlier_draft_was_corrected(tmp_path):
    body = text_of(render(tmp_path).path)
    assert "An earlier draft stated an unsupported classification." in body
    assert "The evidence-supported classification is used throughout." in body


# -- references --------------------------------------------------------------


def test_a_reference_carries_its_precise_identifiers(tmp_path):
    body = text_of(render(tmp_path).path)
    assert "Reference NDA 207997" in body
    assert "Revised May 2022" in body
    assert "Retrieved 2026-09-04" in body
    assert "Evidence E-1" in body


def test_an_unretrieved_reference_is_flagged(tmp_path):
    assert "NOT RETRIEVED" in text_of(render(tmp_path).path)


# -- the two documents -------------------------------------------------------


def test_the_brief_is_a_separate_shorter_document(tmp_path):
    renderer = TechnicalReportRenderer(tmp_path)
    rep = report()
    full = renderer.render(rep, project_name="Midostaurin 25 mg Softgel")
    brief = renderer.render_brief(rep, project_name="Midostaurin 25 mg Softgel")

    assert full.path != brief.path
    assert "Technical_Report" in full.filename
    assert "Executive_Decision_Brief" in brief.filename
    assert brief.word_count < full.word_count
    # The brief still carries the decision material.
    body = text_of(brief.path)
    assert "Proceed as a US-focused working draft." in body
    # And it does not carry the technical body.
    assert "held in solution" not in body


def test_a_rendered_report_is_never_overwritten(tmp_path):
    renderer = TechnicalReportRenderer(tmp_path)
    renderer.render(report(), project_name="P")
    with pytest.raises(FileExistsError):
        renderer.render(report(), project_name="P")


def test_review_required_is_stamped_on_the_cover(tmp_path):
    assert "REVIEW REQUIRED" in text_of(render(tmp_path).path)


# -- naming ------------------------------------------------------------------


def test_a_thread_named_project_does_not_reach_the_filename():
    assert is_auto_named("Slack C0BUYN3KV50/1788515577.573649")
    name = document_name(
        "Slack C0BUYN3KV50/1788515577.573649",
        "Provide a detailed report for the Quality Target Product profile for Midostaurin",
    )
    assert not name.startswith("Slack")
    assert "Quality Target Product" in name


def test_a_real_project_name_is_kept():
    assert document_name("Midostaurin Generic Programme", "anything") == (
        "Midostaurin Generic Programme"
    )


def test_the_subject_comes_from_the_users_own_words():
    subject = subject_from_request("Assess the dissolution slow-down at 6 months.")
    assert "dissolution" in subject.lower()
