"""baseline (no-op)

Revision ID: 0001
Revises:
Create Date: 2026-08-18

No domain models exist yet — this establishes the migration chain. Gate 3 adds the first real
schema (customers, instances, workspace_connections, channel_installations, ...).
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
