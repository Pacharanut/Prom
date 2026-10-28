"""Ports for auto-tagging (Phase 2C)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from src.domain.classification.entities import ClassificationSettings


class ClassificationRepository(Protocol):
    async def get(self, workspace_connection_id: UUID) -> ClassificationSettings:
        """Never None — an unsaved workspace gets the defaults (updated_at=None)."""
        ...

    async def save(self, settings: ClassificationSettings) -> None:
        """Upserts the switches and replaces the whole rule list, keeping its order."""
        ...
