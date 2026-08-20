"""Celery application instance. Business tasks are registered in later Gates; this Gate only
wires the broker/backend and a healthcheck task used by the foundation test suite.
"""

from __future__ import annotations

from celery import Celery

from src.infrastructure.configuration.settings import get_settings

_settings = get_settings()

celery_app = Celery(
    "prom_integration_hub",
    broker=_settings.celery_broker_url,
    backend=_settings.celery_result_backend,
)


@celery_app.task(name="foundation.healthcheck")  # type: ignore[untyped-decorator] # celery has no py.typed stubs
def healthcheck() -> str:
    """Trivial task proving the worker can receive and execute a task."""
    return "ok"
