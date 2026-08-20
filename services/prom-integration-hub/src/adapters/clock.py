"""Real-time Clock implementation. Tests use a FakeClock instead (see tests/fakes)."""

from __future__ import annotations

from datetime import UTC, datetime

from src.application.account_linking.ports import Clock


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)
