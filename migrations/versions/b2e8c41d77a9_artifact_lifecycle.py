"""artifact lifecycle: status and supersession

A specialist that revises its own work produces a second artifact. Without a
lifecycle the report generator cannot tell the revision from the draft it
replaces, so both reach the reader (PRD s14, s18).

Revision ID: b2e8c41d77a9
Revises: a1c7f2d90b14
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "b2e8c41d77a9"
down_revision: str | None = "a1c7f2d90b14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "artifacts",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="current"),
    )
    op.add_column("artifacts", sa.Column("supersedes_ref", sa.String(length=64), nullable=True))
    op.add_column("artifacts", sa.Column("superseded_by_ref", sa.String(length=64), nullable=True))
    op.create_index("ix_artifacts_run_status", "artifacts", ["run_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_artifacts_run_status", table_name="artifacts")
    op.drop_column("artifacts", "superseded_by_ref")
    op.drop_column("artifacts", "supersedes_ref")
    op.drop_column("artifacts", "status")
