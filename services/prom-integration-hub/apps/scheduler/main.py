"""Celery beat scheduler entrypoint. Run with: celery -A apps.scheduler.main.celery_app beat."""

from __future__ import annotations

from src.infrastructure.configuration.settings import get_settings

# Importing notification_tasks registers "notifications.check_due_dates" with celery_app — the
# task module must be imported somewhere before beat can schedule it by name; this is that "later
# gates register business tasks here" hook flagged in celery_app.py's module docstring.
from src.infrastructure.messaging import notification_tasks  # noqa: F401
from src.infrastructure.messaging.celery_app import celery_app
from src.infrastructure.observability.logging import configure_logging

configure_logging(get_settings().log_level)

celery_app.conf.beat_schedule = {
    "check-due-date-notifications": {
        "task": "notifications.check_due_dates",
        # Hourly — the task itself no-ops unless Asia/Bangkok's local hour matches
        # _DUE_CHECK_LOCAL_HOUR (see notification_tasks.py), so an hourly beat interval is what
        # actually gets "once a day, at a sensible morning time" without a timezone-aware cron
        # dependency. Gate 7 scope: Asia/Bangkok only, not a per-customer schedule.
        "schedule": 3600.0,
    },
}

__all__ = ["celery_app"]
