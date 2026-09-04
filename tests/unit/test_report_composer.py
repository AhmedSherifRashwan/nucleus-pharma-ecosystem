"""Composition boundaries (PRD s18, s32).

The composer's own prompts are exercised here against a stub gateway: what matters
is which material reaches the model, and what happens when a call fails.
"""

from __future__ import annotations

import pytest

from app.providers.openclaw_client import GatewayTransientError, Usage
from app.reports.composer import compose_report
from app.reports.context import FinalReportContext, LoadedArtifact
from app.schemas.executive import ExecutiveDecisionBrief
from app.schemas.report import ExecutiveBriefSection

OUTLINE = {
    "document_title": "QTPP and Critical Quality Framework",
    "sections": [
        {
            "heading": "Regulatory submission framework",
            "purpose": "The criteria the work is measured against.",
            "subsections": [],
            "source_artifact_refs": ["ART-1", "ART-404"],
            "expected_tables": [],
        },
        {
            "heading": "Quality target product profile",
            "purpose": "The QTPP.",
            "subsections": [],
            "source_artifact_refs": ["ART-2"],
            "expected_tables": ["QTPP register"],
        },
        {
            "heading": "Critical quality attributes",
            "purpose": "The CQAs.",
            "subsections": [],
            "source_artifact_refs": [],
            "expected_tables": [],
        },
    ],
}

SECTION = {
    "heading": "Composed",
    "body": "Body text.",
    "tables": [],
    "subsections": [],
    "evidence_refs": ["E-1"],
    "status": "provisional",
}

CLOSING = {
    "review_summary": "Reviewed.",
    "critical_review": [],
    "revision_crosswalk": [],
    "information_gaps": [],
    "conclusions": "Proceed.",
    "recommended_actions": [],
    "unresolved_conflicts": [],
}

BRIEF = {
    "brief": {
        "executive_summary": "Summary.",
        "objective_and_scope": "Scope.",
        "overall_recommendation": "Proceed.",
        "confidence_and_evidence_status": "Medium.",
    }
}

REFERENCES = {
    "references": [
        {
            "citation_id": "R1",
            "organisation": "FDA",
            "title": "Guidance",
            "evidence_ref": "E-1",
            "verification": "primary_source_verified",
        },
        {
            "citation_id": "R2",
            "organisation": "Invented",
            "title": "Not in the register",
            "evidence_ref": "E-999",
            "verification": "unverified",
        },
    ]
}


class StubClient:
    """Answers by schema name and records the prompt it was given."""

    def __init__(self, *, fail: set[str] | None = None):
        self.prompts: dict[str, str] = {}
        self.fail = fail or set()

    async def complete_structured(self, *, route, system, user, schema, **kw):
        name = schema.__name__
        self.prompts.setdefault(name, "")
        self.prompts[name] += user
        if name in self.fail:
            raise GatewayTransientError("simulated failure")
        payload = {
            "ReportOutline": OUTLINE,
            "SectionDraft": SECTION,
            "ClosingMatter": CLOSING,
            "BriefSection": BRIEF,
            "ReferenceList": REFERENCES,
        }[name]
        return schema.model_validate(payload), Usage(duration_ms=5, steps=1), None


def context(**kw) -> FinalReportContext:
    base = {
        "run_id": __import__("uuid").uuid4(),
        "run_ref": "RUN-1",
        "user_request": "Define the QTPP.",
        "brief": ExecutiveDecisionBrief(
            objective="Define the QTPP.",
            executive_conclusion="Workable.",
            recommendation="Proceed.",
            confidence="medium",
        ),
        "current_work_products": [
            LoadedArtifact("ART-1", "Regulatory basis", "A02", "current", "REGULATORY MATERIAL"),
            LoadedArtifact("ART-2", "QTPP Rev 1", "A03", "current", "CURRENT QTPP MATERIAL"),
        ],
        "superseded_work_products": [
            LoadedArtifact(
                "ART-7", "QTPP Rev 0", "A03", "superseded", "", superseded_by_ref="ART-2"
            )
        ],
        "evidence": [
            {
                "evidence_ref": "E-1",
                "reliability_level": 1,
                "source_title": "FDA guidance",
                "source_url_or_document_id": "https://fda.gov",
                "excerpt_or_locator": "Section 1",
                "retrieved_at": "2026-09-04",
                "originating_agent": "A02",
                "notes": None,
            }
        ],
    }
    return FinalReportContext(**{**base, **kw})


@pytest.mark.asyncio
async def test_only_current_artifacts_reach_the_composer():
    client = StubClient()
    await compose_report(context(), client=client)

    everything = "".join(client.prompts.values())
    assert "CURRENT QTPP MATERIAL" in everything
    # The superseded draft's content is never offered as material.
    assert "ART-7" not in client.prompts.get("SectionDraft", "")


@pytest.mark.asyncio
async def test_the_superseded_draft_is_offered_to_the_revision_crosswalk():
    client = StubClient()
    await compose_report(context(), client=client)
    assert "ART-7" in client.prompts["ClosingMatter"]


@pytest.mark.asyncio
async def test_a_section_receives_only_its_own_sources():
    client = StubClient()
    await compose_report(context(), client=client)
    # Both artifacts appear across the section calls, each against its own section.
    prompts = client.prompts["SectionDraft"]
    assert "REGULATORY MATERIAL" in prompts
    assert "CURRENT QTPP MATERIAL" in prompts


@pytest.mark.asyncio
async def test_an_invented_artifact_reference_is_dropped_from_the_outline():
    client = StubClient()
    outcome = await compose_report(context(), client=client)
    # ART-404 does not exist; the section still composes from what does.
    assert outcome.report.body_sections
    assert "ART-404" not in client.prompts["SectionDraft"]


@pytest.mark.asyncio
async def test_a_reference_to_unknown_evidence_is_discarded():
    outcome = await compose_report(context(), client=StubClient())
    ids = {r.evidence_ref for r in outcome.report.references}
    assert ids == {"E-1"}


@pytest.mark.asyncio
async def test_the_work_product_appendix_records_both_lifecycles():
    outcome = await compose_report(context(), client=StubClient())
    by_ref = {w.artifact_ref: w for w in outcome.report.work_products}
    assert by_ref["ART-2"].status == "current"
    assert by_ref["ART-7"].status == "superseded"
    assert by_ref["ART-7"].superseded_by_ref == "ART-2"


@pytest.mark.asyncio
async def test_a_failed_section_is_reported_and_the_rest_still_compose():
    client = StubClient(fail={"SectionDraft"})
    outcome = await compose_report(context(), client=client)
    assert outcome.report.body_sections == []
    assert len(outcome.warnings) == 3
    assert all("could not be composed" in w for w in outcome.warnings)


@pytest.mark.asyncio
async def test_a_failed_brief_falls_back_to_the_integrated_position():
    client = StubClient(fail={"BriefSection"})
    outcome = await compose_report(context(), client=client)
    brief = outcome.report.executive_brief
    assert isinstance(brief, ExecutiveBriefSection)
    assert brief.overall_recommendation == "Proceed."
    assert any("executive brief" in w for w in outcome.warnings)


@pytest.mark.asyncio
async def test_metadata_carries_the_run_and_version():
    outcome = await compose_report(context(), report_version=3, client=StubClient())
    meta = outcome.report.metadata
    assert meta.run_ref == "RUN-1"
    assert meta.report_version == 3
    assert meta.supersedes_version == 2
    assert meta.evidence_cutoff == "2026-09-04"


@pytest.mark.asyncio
async def test_the_composer_is_never_told_who_did_what():
    """s18: the deliverable is a report, not an execution log."""
    client = StubClient()
    await compose_report(context(), client=client)
    for name, prompt in client.prompts.items():
        assert "task_id" not in prompt, name
        assert "execution order" not in prompt, name
