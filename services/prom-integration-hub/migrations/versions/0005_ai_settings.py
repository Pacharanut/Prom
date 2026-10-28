"""ai settings

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26

Phase 2 (AI assistant): per-customer LLM gateway settings, managed from Plane's
Settings → AI Assistant page via prom_bridge. The API key is Fernet-encrypted (same
SecretProvider as every other credential); NULL means "use the Hub's NT_AI_HUB_KEY default".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("encrypted_api_key", sa.LargeBinary(), nullable=True),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("customer_id", name="uq_ai_settings_customer_id"),
    )


def downgrade() -> None:
    op.drop_table("ai_settings")
