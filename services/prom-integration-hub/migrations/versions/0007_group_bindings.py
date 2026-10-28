"""group bindings

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26

Phase 2D (AI MVP item 9): LINE groups / Discord channels bound to a workspace (optionally one
project) to receive created/assigned/completed work-item events.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "group_bindings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column(
            "channel_installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_installations.id"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.String(16), nullable=False),
        sa.Column("group_id", sa.String(128), nullable=False),
        sa.Column(
            "workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("project_name", sa.String(255), nullable=True),
        sa.Column("bound_by_prom_user_id", sa.String(64), nullable=False),
        sa.Column("events", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "channel_installation_id", "group_id", name="uq_group_bindings_installation_group"
        ),
    )
    op.create_index(
        "ix_group_bindings_workspace_connection_id", "group_bindings", ["workspace_connection_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_group_bindings_workspace_connection_id", "group_bindings")
    op.drop_table("group_bindings")
