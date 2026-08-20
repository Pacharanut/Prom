"""Celery worker entrypoint. Run with: celery -A apps.worker.main.celery_app worker."""

from __future__ import annotations

from src.infrastructure.configuration.settings import get_settings

# Registers "notifications.process_prom_webhook_event", "notifications.send_debounced_notification"
# and "notifications.check_due_dates" with celery_app — without this import the worker process
# never learns these tasks exist and every apps/api/routers/prom_webhook.py send_task() call (and
# every beat-triggered check_due_dates run) would fail with "Received unregistered task".
from src.infrastructure.messaging import notification_tasks  # noqa: F401
from src.infrastructure.messaging.celery_app import celery_app
from src.infrastructure.observability.logging import configure_logging

configure_logging(get_settings().log_level)

__all__ = ["celery_app"]
