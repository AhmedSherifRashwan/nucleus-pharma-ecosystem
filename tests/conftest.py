"""Shared test fixtures."""

from __future__ import annotations

import os
import tempfile

# Point the session at a dedicated database BEFORE app modules read settings.
# Integration tests truncate tables, so they must never touch the dev database.
TEST_DB = os.environ.get("TEST_DATABASE_NAME", "pharma_ai_test")
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    f"postgresql+psycopg://ai:change-me@localhost:5432/{TEST_DB}",
)
os.environ["LANGGRAPH_DATABASE_URL"] = os.environ.get(
    "TEST_LANGGRAPH_DATABASE_URL",
    f"postgresql://ai:change-me@localhost:5432/{TEST_DB}",
)
os.environ.setdefault("APP_ENV", "development")
# Reports are real files. Keep test output out of the working tree.
os.environ.setdefault("ARTIFACT_LOCAL_PATH", tempfile.mkdtemp(prefix="neucleus-test-artifacts-"))
os.environ.setdefault("OPENCLAW_GATEWAY_TOKEN", "test-token-not-real")

import pytest  # noqa: E402

from app.config import get_settings  # noqa: E402


@pytest.fixture(scope="session")
def settings():
    return get_settings()
