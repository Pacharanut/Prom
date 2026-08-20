"""installation and account-linking schema

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-18

Creates the Gate 3 tables: customers, prom_instances, workspace_connections,
channel_installations, installation_workspaces, linked_identities, linking_codes,
delegated_credentials, audit_logs. Mirrors src/adapters/persistence/models.py exactly.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "prom_instances",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_prom_instances_customer_id", "prom_instances", ["customer_id"])

    op.create_table(
        "workspace_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column(
            "prom_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prom_instances.id"),
            nullable=False,
        ),
        sa.Column("prom_workspace_id", sa.String(255), nullable=False),
        sa.Column("prom_workspace_slug", sa.String(255), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "prom_instance_id",
            "prom_workspace_id",
            name="uq_workspace_connections_instance_workspace",
        ),
    )
    op.create_index(
        "ix_workspace_connections_customer_id", "workspace_connections", ["customer_id"]
    )
    op.create_index(
        "ix_workspace_connections_prom_instance_id", "workspace_connections", ["prom_instance_id"]
    )

    op.create_table(
        "channel_installations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column("channel_type", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=False),
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=False),
        sa.Column(
            "default_workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_channel_installations_customer_id", "channel_installations", ["customer_id"]
    )

    op.create_table(
        "installation_workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "channel_installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_installations.id"),
            nullable=False,
        ),
        sa.Column(
            "workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "channel_installation_id",
            "workspace_connection_id",
            name="uq_installation_workspaces_pair",
        ),
    )
    op.create_index(
        "ix_installation_workspaces_channel_installation_id",
        "installation_workspaces",
        ["channel_installation_id"],
    )
    op.create_index(
        "ix_installation_workspaces_workspace_connection_id",
        "installation_workspaces",
        ["workspace_connection_id"],
    )

    op.create_table(
        "linked_identities",
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
        sa.Column("channel_type", sa.String(32), nullable=False),
        sa.Column("channel_user_id", sa.String(255), nullable=False),
        sa.Column(
            "prom_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prom_instances.id"),
            nullable=False,
        ),
        sa.Column("prom_user_id", sa.String(255), nullable=False),
        sa.Column(
            "default_workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("unlinked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_linked_identities_customer_id", "linked_identities", ["customer_id"])
    op.create_index(
        "ix_linked_identities_channel_installation_id",
        "linked_identities",
        ["channel_installation_id"],
    )
    op.create_index(
        "ix_linked_identities_prom_instance_id", "linked_identities", ["prom_instance_id"]
    )
    op.create_index(
        "uq_linked_identities_active_channel_user",
        "linked_identities",
        ["channel_installation_id", "channel_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "uq_linked_identities_active_prom_user",
        "linked_identities",
        ["channel_installation_id", "prom_user_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "linking_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column(
            "prom_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prom_instances.id"),
            nullable=False,
        ),
        sa.Column("prom_user_id", sa.String(255), nullable=False),
        sa.Column(
            "channel_installation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("channel_installations.id"),
            nullable=False,
        ),
        sa.Column("code", sa.String(6), nullable=False),
        sa.Column(
            "default_workspace_connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace_connections.id"),
            nullable=True,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_linking_codes_customer_id", "linking_codes", ["customer_id"])
    op.create_index(
        "ix_linking_codes_channel_installation_id", "linking_codes", ["channel_installation_id"]
    )
    op.create_index(
        "ix_linking_codes_installation_code", "linking_codes", ["channel_installation_id", "code"]
    )

    op.create_table(
        "delegated_credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=False,
        ),
        sa.Column(
            "prom_instance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prom_instances.id"),
            nullable=False,
        ),
        sa.Column("prom_user_id", sa.String(255), nullable=False),
        sa.Column("encrypted_token", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "prom_instance_id", "prom_user_id", name="uq_delegated_credentials_instance_user"
        ),
    )
    op.create_index(
        "ix_delegated_credentials_customer_id", "delegated_credentials", ["customer_id"]
    )

    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "customer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("customers.id"),
            nullable=True,
        ),
        sa.Column("prom_instance_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("installation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("channel_type", sa.String(32), nullable=True),
        sa.Column("channel_user_id", sa.String(255), nullable=True),
        sa.Column("prom_user_id", sa.String(255), nullable=True),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=True),
        sa.Column("target_id", sa.String(255), nullable=True),
        sa.Column("correlation_id", sa.String(64), nullable=False),
        sa.Column("result_status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_logs_customer_id", "audit_logs", ["customer_id"])
    op.create_index("ix_audit_logs_installation_id", "audit_logs", ["installation_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    op.create_index("ix_audit_logs_correlation_id", "audit_logs", ["correlation_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("delegated_credentials")
    op.drop_table("linking_codes")
    op.drop_table("linked_identities")
    op.drop_table("installation_workspaces")
    op.drop_table("channel_installations")
    op.drop_table("workspace_connections")
    op.drop_table("prom_instances")
    op.drop_table("customers")
