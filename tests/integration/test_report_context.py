"""The report context: current material only (PRD s18, s32)."""

from __future__ import annotations

import uuid

import pytest

from app.reports.context import build_context
from app.schemas.executive import ExecutiveDecisionBrief
from app.storage.db import session_scope
from app.storage.repositories import ArtifactRepository, EvidenceRepository, RunRepository
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


def brief() -> ExecutiveDecisionBrief:
    return ExecutiveDecisionBrief(
        objective="Define the QTPP.",
        executive_conclusion="Workable under controls.",
        recommendation="Proceed.",
        confidence="medium",
    )


async def _run() -> tuple[uuid.UUID, str]:
    async with session_scope() as s:
        run = await RunRepository(s).create(user_request="Define the QTPP.")
        return run.id, run.run_ref


async def _artifact(run_id: uuid.UUID, tmp_path, ref: str, title: str, content: str, agent="A03"):
    path = tmp_path / f"{ref}.md"
    path.write_text(content, encoding="utf-8")
    async with session_scope() as s:
        await ArtifactRepository(s).create(
            title=title,
            kind="work_product",
            storage_path=str(path),
            size_bytes=len(content.encode()),
            run_id=run_id,
            created_by_agent=agent,
            artifact_ref=ref,
        )


async def _context(run_id, run_ref):
    return await build_context(
        run_id=run_id,
        run_ref=run_ref,
        user_request="Define the QTPP.",
        plan=None,
        brief=brief(),
        results=[],
        agent_names={"A03": "QTPP Expert"},
    )


async def test_current_artifacts_are_loaded_with_their_content(tmp_path):
    run_id, run_ref = await _run()
    await _artifact(run_id, tmp_path, "ART-1", "QTPP draft", "The QTPP targets are as follows.")

    context = await _context(run_id, run_ref)

    assert [a.artifact_ref for a in context.current_work_products] == ["ART-1"]
    assert "QTPP targets" in context.current_work_products[0].content
    assert "QTPP targets" in context.work_product_digest()


async def test_a_superseded_draft_is_excluded_from_the_composer_material(tmp_path):
    run_id, run_ref = await _run()
    await _artifact(run_id, tmp_path, "ART-1", "QTPP Rev 0", "The classification is wrong here.")
    await _artifact(run_id, tmp_path, "ART-2", "QTPP Rev 1", "The corrected classification.")
    async with session_scope() as s:
        assert await ArtifactRepository(s).supersede(
            old_ref="ART-1", new_ref="ART-2", run_id=run_id
        )

    context = await _context(run_id, run_ref)

    assert [a.artifact_ref for a in context.current_work_products] == ["ART-2"]
    assert "wrong here" not in context.work_product_digest()
    # It is still available for the audit appendix and the crosswalk.
    assert context.superseded_refs() == ["ART-1"]
    assert context.superseded_work_products[0].superseded_by_ref == "ART-2"


async def test_supersession_cannot_reach_across_runs(tmp_path):
    run_a, _ = await _run()
    run_b, _ = await _run()
    await _artifact(run_a, tmp_path, "ART-1", "Theirs", "Another run's work.")
    await _artifact(run_b, tmp_path, "ART-2", "Ours", "This run's work.")

    async with session_scope() as s:
        assert not await ArtifactRepository(s).supersede(
            old_ref="ART-1", new_ref="ART-2", run_id=run_b
        )


async def test_evidence_reaches_the_context_with_its_provenance(tmp_path):
    run_id, run_ref = await _run()
    async with session_scope() as s:
        await EvidenceRepository(s).create(
            source_type="regulatory",
            source_title="FDA Product-Specific Guidance, Midostaurin",
            reliability_level=1,
            run_id=run_id,
            source_url_or_document_id="https://www.accessdata.fda.gov/psg",
            excerpt_or_locator="Recommended Studies",
            originating_agent="A02",
        )

    context = await _context(run_id, run_ref)
    item = context.evidence[0]
    assert item["source_url_or_document_id"].endswith("/psg")
    assert item["excerpt_or_locator"] == "Recommended Studies"
    assert item["retrieved_at"]
    assert context.evidence_refs == {item["evidence_ref"]}


async def test_a_very_long_artifact_is_truncated_and_says_so(tmp_path, monkeypatch):
    from app.reports import context as context_mod

    monkeypatch.setattr(context_mod, "MAX_ARTIFACT_CHARS", 500)
    run_id, run_ref = await _run()
    await _artifact(run_id, tmp_path, "ART-1", "Long", "x" * 5000)

    context = await _context(run_id, run_ref)
    loaded = context.current_work_products[0]
    assert loaded.truncated is True
    assert "truncated" in loaded.content
