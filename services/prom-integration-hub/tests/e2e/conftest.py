"""Re-exports the integration tier's real-Postgres/Redis fixtures — pytest fixtures defined in a
conftest.py are only auto-discovered by tests in that directory and its subdirectories, not
siblings, so `tests/e2e/` needs its own thin re-export rather than duplicating the fixture.
"""

from __future__ import annotations

from tests.integration.conftest import db_session, redis_client

__all__ = ["db_session", "redis_client"]
