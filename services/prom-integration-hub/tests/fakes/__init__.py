"""In-memory fake port implementations used by the application-layer unit test tier. These make
tests/application/* infra-free (no Docker/Postgres/Redis needed) — the primary, always-run test
tier. See tests/integration/ for tests against real infrastructure.
"""
