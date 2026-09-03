"""Integration fixtures. Require the local PostgreSQL instance."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.storage.db import session_scope


def _db_available() -> bool:
    async def check() -> bool:
        try:
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    try:
        return asyncio.run(check())
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_available(), reason="PostgreSQL not reachable")

_TABLES = (
    "audit_events",
    "decisions",
    "approvals",
    "risks",
    "conflicts",
    "assumptions",
    "artifacts",
    "evidence_items",
    "specialist_results",
    "run_tasks",
    "run_jobs",
    "runs",
    "document_chunks",
    "source_documents",
    "projects",
)


@pytest_asyncio.fixture(autouse=True)
async def clean_database():
    """Empty transactional data between tests, guarded against non-test DBs."""
    from app.config import get_settings

    url = get_settings().database_url
    if "test" not in url:
        raise RuntimeError(f"refusing to truncate a non-test database: {url.rsplit('/', 1)[-1]}")
    async with session_scope() as session:
        await session.execute(text("TRUNCATE " + ", ".join(_TABLES) + " RESTART IDENTITY CASCADE"))
    yield
