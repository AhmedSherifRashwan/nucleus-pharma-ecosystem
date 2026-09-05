"""Document ingestion and retrieval (PRD s14, s17.3, s19)."""

from __future__ import annotations

import uuid

import pytest

from app.ingest.extract import UnsupportedDocument, extract
from app.ingest.service import ingest_file, ingest_path
from app.storage.db import session_scope
from app.storage.models import DocumentChunk, Project, SourceDocument
from app.tools import store
from app.tools.context import ToolContext
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]

GUIDANCE = """# Product-Specific Guidance for Midostaurin Oral Capsule

Reference NDA: 207997. Revised May 2022.

## Recommended Studies

Two studies are recommended: a fasting study and a fed study, each a single-dose
two-way crossover in-vivo bioequivalence study in healthy volunteers.

## Dissolution

No product-specific dissolution method is recommended in this guidance. The
applicant should develop a method with demonstrated discriminatory capability.
"""


async def a_project(name: str = "Ingestion Test") -> uuid.UUID:
    async with session_scope() as s:
        project = Project(name=f"{name} {uuid.uuid4().hex[:6]}")
        s.add(project)
        await s.flush()
        return project.id


def context_for(project_id: uuid.UUID) -> ToolContext:
    return ToolContext(
        run_id=uuid.uuid4(),
        run_ref="RUN-TEST",
        task_row_id=uuid.uuid4(),
        planner_task_id="T1",
        agent_id="A02",
        project_id=project_id,
        allowed_tools=("project_search",),
        knowledge_scopes=("regulatory_sources",),
    )


async def test_an_ingested_document_becomes_retrievable(tmp_path):
    """project_search returned nothing on every run because nothing was loaded."""
    path = tmp_path / "psg.md"
    path.write_text(GUIDANCE)
    project_id = await a_project()

    result = await ingest_file(path, project_id=project_id)
    assert result.status == "ingested"
    assert result.chunks >= 2

    found = await store.project_search(context_for(project_id), query="dissolution method")
    assert found["result_count"] >= 1
    assert "discriminatory" in " ".join(r["content"] for r in found["results"])


async def test_every_chunk_carries_a_locator_a_reader_can_follow(tmp_path):
    path = tmp_path / "psg.md"
    path.write_text(GUIDANCE)
    project_id = await a_project()
    await ingest_file(path, project_id=project_id)

    found = await store.project_search(context_for(project_id), query="bioequivalence studies")
    locators = [r["locator"] for r in found["results"]]
    assert all(locators), "a chunk with no locator is not provenance"
    assert any("Recommended Studies" in loc for loc in locators)


async def test_another_projects_documents_stay_invisible(tmp_path):
    """s19: authorisation is applied before the search, not after."""
    path = tmp_path / "psg.md"
    path.write_text(GUIDANCE)
    theirs = await a_project("Theirs")
    ours = await a_project("Ours")
    await ingest_file(path, project_id=theirs)

    found = await store.project_search(context_for(ours), query="dissolution")
    assert found["result_count"] == 0


async def test_re_ingesting_an_unchanged_file_does_not_duplicate_it(tmp_path):
    path = tmp_path / "psg.md"
    path.write_text(GUIDANCE)
    project_id = await a_project()

    first = await ingest_file(path, project_id=project_id)
    second = await ingest_file(path, project_id=project_id)

    assert second.status == "unchanged"
    assert second.document_id == first.document_id


async def test_a_revised_document_supersedes_the_old_one(tmp_path):
    """Two versions of one guidance is how a specialist cites the wrong one."""
    path = tmp_path / "psg.md"
    path.write_text(GUIDANCE)
    project_id = await a_project()
    await ingest_file(path, project_id=project_id)

    path.write_text(GUIDANCE.replace("Revised May 2022", "Revised March 2026"))
    second = await ingest_file(path, project_id=project_id)
    assert second.status == "replaced"

    async with session_scope() as s:
        from sqlalchemy import select

        documents = (
            (await s.execute(select(SourceDocument).where(SourceDocument.project_id == project_id)))
            .scalars()
            .all()
        )
        chunks = (
            (await s.execute(select(DocumentChunk).where(DocumentChunk.project_id == project_id)))
            .scalars()
            .all()
        )

    assert len(documents) == 1
    assert documents[0].version == 2
    assert all(c.document_id == documents[0].id for c in chunks)

    found = await store.project_search(context_for(project_id), query="Revised")
    text = " ".join(r["content"] for r in found["results"])
    assert "March 2026" in text
    assert "May 2022" not in text


async def test_keeping_old_versions_is_a_deliberate_choice(tmp_path):
    path = tmp_path / "psg.md"
    path.write_text(GUIDANCE)
    project_id = await a_project()
    await ingest_file(path, project_id=project_id)
    path.write_text(GUIDANCE.replace("May 2022", "March 2026"))
    await ingest_file(path, project_id=project_id, replace=False)

    async with session_scope() as s:
        from sqlalchemy import select

        documents = (
            (await s.execute(select(SourceDocument).where(SourceDocument.project_id == project_id)))
            .scalars()
            .all()
        )
    assert len(documents) == 2


async def test_a_directory_is_ingested_whole(tmp_path):
    (tmp_path / "a.md").write_text(GUIDANCE)
    (tmp_path / "b.txt").write_text("Stability data for batch B221. Assay 99.4%.")
    (tmp_path / "ignore.bin").write_bytes(b"\x00\x01")
    project_id = await a_project()

    report = await ingest_path(tmp_path, project_id=project_id)
    assert len(report.ingested) == 2
    assert report.total_chunks >= 2
    assert not report.failed


async def test_an_unreadable_file_fails_loudly_without_stopping_the_batch(tmp_path):
    (tmp_path / "good.md").write_text(GUIDANCE)
    (tmp_path / "empty.txt").write_text("   ")
    project_id = await a_project()

    report = await ingest_path(tmp_path, project_id=project_id)
    assert len(report.ingested) == 1
    assert len(report.failed) == 1
    assert report.failed[0].detail


async def test_an_unsupported_file_type_says_so(tmp_path):
    path = tmp_path / "thing.xyz"
    path.write_text("data")
    with pytest.raises(UnsupportedDocument, match="not supported"):
        extract(path)


async def test_a_short_block_carrying_an_identifier_is_kept(tmp_path):
    """The header holding "Reference NDA: 207997" was being dropped as too short."""
    path = tmp_path / "psg.md"
    path.write_text(GUIDANCE)
    project_id = await a_project()
    await ingest_file(path, project_id=project_id)

    found = await store.project_search(context_for(project_id), query="Reference NDA 207997")
    assert found["result_count"] >= 1
    assert "207997" in " ".join(r["content"] for r in found["results"])
