"""notifications and webhooks

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-18

Gate 7 adds: webhook_secret_encrypted on workspace_connections (nullable — set later via
PATCH /api/v1/workspace-connections/{id}/webhook-secret, a manual admin action since Plane's
webhook CRUD only exists in its session-authenticated internal API, not the public X-Api-Key
REST API Hub uses everywhere else), plus three new tables: notification_preferences,
webhook_event_records (inbound Prom webhook idempotency/audit journal), and
outbound_message_records (debounce buffer + retry/dead-letter ledger for the actual
LINE/Discord send).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "workspace_connections",
        sa.Column("webhook_secret_encrypted", sa.LargeBinary(), nullable=True),
    )

    op.create_table(
        "notification_preferences",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column(
            "linked_identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("linked_identities.id"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "linked_identity_id",
            "event_type",
            name="uq_notification_preferences_identity_type",
        ),
    )
    op.create_index(
        "ix_notification_preferences_customer_id", "notification_preferences", ["customer_id"]
    )
    op.create_index(
        "ix_notification_preferences_linked_identity_id",
        "notification_preferences",
        ["linked_identity_id"],
    )

    op.create_table(
        "webhook_event_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            nullable=False,
        ),
        sa.Column("delivery_id", sa.String(64), nullable=False),
        sa.Column("event", sa.String(32), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.UniqueConstraint("delivery_id", name="uq_webhook_event_records_delivery_id"),
    )
    op.create_index(
        "ix_webhook_event_records_workspace_connection_id",
        "webhook_event_records",
        ["workspace_connection_id"],
    )

    op.create_table(
        "outbound_message_records",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "linked_identity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("linked_identities.id"),
            nullable=False,
        ),
        sa.Column("work_item_id", sa.String(64), nullable=False),
        sa.Column("event_types_batched", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_outbound_message_records_linked_identity_id",
        "outbound_message_records",
        ["linked_identity_id"],
    )
    op.create_index(
        "ix_outbound_message_records_pending_lookup",
        "outbound_message_records",
        ["linked_identity_id", "work_item_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("outbound_message_records")
    op.drop_table("webhook_event_records")
    op.drop_index("ix_notification_preferences_linked_identity_id", "notification_preferences")
    op.drop_index("ix_notification_preferences_customer_id", "notification_preferences")
    op.drop_table("notification_preferences")
    op.drop_column("workspace_connections", "webhook_secret_encrypted")
