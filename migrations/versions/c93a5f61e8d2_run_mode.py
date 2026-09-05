"""run mode: standard, full or direct

A user can ask for the whole ecosystem, or for one named specialist. The mode is
recorded on the run because it changes what the planner is allowed to do, and a
report has to be able to say which was used (PRD s9.1, s12.2).

Revision ID: c93a5f61e8d2
Revises: b2e8c41d77a9
Create Date: 2026-09-05
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c93a5f61e8d2"
down_revision: str | None = "b2e8c41d77a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("mode", sa.String(length=16), nullable=False, server_default="standard"),
    )
    op.add_column(
        "runs",
        sa.Column(
            "requested_agents",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "requested_agents")
    op.drop_column("runs", "mode")
