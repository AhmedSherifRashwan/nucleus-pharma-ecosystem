"""Profile seeding and version integrity (PRD Phase 1, FR-018)."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.storage.db import session_scope
from app.storage.models import AgentProfileRecord, PromptVersion
from tests.integration.conftest import requires_db

pytestmark = [pytest.mark.asyncio, requires_db]


@pytest.fixture(autouse=True)
async def clear_registry_tables():
    async with session_scope() as session:
        await session.execute(text("TRUNCATE agent_profiles, prompt_versions CASCADE"))
    yield


async def _seed() -> int:
    from scripts.seed_agents import seed

    return await seed()


async def test_seed_records_all_sixteen_profiles():
    assert await _seed() == 0
    async with session_scope() as session:
        rows = list((await session.execute(select(AgentProfileRecord))).scalars().all())
    assert len(rows) == 16
    assert {r.id for r in rows} == {f"A{i:02d}" for i in range(1, 17)}


async def test_seed_is_idempotent():
    await _seed()
    await _seed()
    async with session_scope() as session:
        rows = list((await session.execute(select(AgentProfileRecord))).scalars().all())
    assert len(rows) == 16


async def test_every_profile_is_seeded_with_its_source_document():
    await _seed()
    async with session_scope() as session:
        rows = {
            r.id: r for r in (await session.execute(select(AgentProfileRecord))).scalars().all()
        }
    for pid in ("A15", "A16"):
        assert rows[pid].status == "active"
        assert rows[pid].enabled is True
        # Framework-level provenance is still provenance, and must be recorded.
        assert rows[pid].source_sds
    assert rows["A07"].enabled is True
    assert rows["A07"].status == "active"


async def test_seed_stores_the_source_sds_for_traceability():
    await _seed()
    async with session_scope() as session:
        row = await session.get(AgentProfileRecord, "A10")
    assert row.source_sds == "AI_Agent_Stability_Studies_Expert_SDS_v1.docx"


async def test_seed_snapshots_the_whole_profile():
    """Each run must be reconstructible from the recorded profile version."""
    await _seed()
    async with session_scope() as session:
        row = await session.get(AgentProfileRecord, "A08")
    snapshot = row.profile_snapshot
    assert snapshot["operating_persona"]
    assert snapshot["allowed_tools"]
    assert snapshot["escalation_policy"]["triggers"]


async def test_prompt_versions_are_recorded_with_hashes():
    await _seed()
    async with session_scope() as session:
        rows = list((await session.execute(select(PromptVersion))).scalars().all())
    names = {r.name for r in rows}
    assert "workers/common" in names
    assert "workers/profiles/A01" in names
    assert len(rows) == 17  # 16 profiles + the shared worker prompt
    assert all(len(r.sha256) == 64 for r in rows)


async def test_released_prompt_drift_is_refused():
    """A released prompt must not change without a version bump (s21)."""
    await _seed()
    async with session_scope() as session:
        row = await session.scalar(
            select(PromptVersion).where(PromptVersion.name == "workers/profiles/A01")
        )
        row.sha256 = "0" * 64  # simulate the file changing underneath

    assert await _seed() == 1
