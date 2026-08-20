"""SQLAlchemy declarative base. No models registered yet — Gate 3 adds persistence models."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for all ORM models in this service."""
