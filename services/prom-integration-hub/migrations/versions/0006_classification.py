"""classification (auto-tagging)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26

Phase 2C (AI MVP item 8): per-workspace auto-tagging switches and keyword → label/priority
rules, edited from Plane's Settings → AI Assistant page.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "classification_settings",
        sa.Column(
            "workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            primary_key=True,
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("use_ai_fallback", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "classification_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("keyword", sa.String(100), nullable=False),
        sa.Column("label_name", sa.String(255), nullable=True),
        sa.Column("priority", sa.String(16), nullable=True),
    )
    op.create_index(
        "ix_classification_rules_workspace_connection_id",
        "classification_rules",
        ["workspace_connection_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_classification_rules_workspace_connection_id", "classification_rules")
    op.drop_table("classification_rules")
    op.drop_table("classification_settings")
