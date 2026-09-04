"""widen artifact content_type for office MIME types

The OOXML MIME type for a .docx is 71 characters, which did not fit the original
64-character column (PRD s18).

Revision ID: a1c7f2d90b14
Revises: d3b2af655913
Create Date: 2026-09-04
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "a1c7f2d90b14"
down_revision: str | None = "d3b2af655913"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "artifacts",
        "content_type",
        existing_type=sa.String(length=64),
        type_=sa.String(length=128),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "artifacts",
        "content_type",
        existing_type=sa.String(length=128),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
