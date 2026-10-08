"""Celery application.

Schedules are stored in PostgreSQL. The beat process only triggers a short
polling task; nothing is ever scheduled in Redis with a long ETA or countdown,
because the Redis broker redelivers tasks that outlive its visibility timeout.
"""

from celery import Celery
from celery.signals import setup_logging

from app.core.config import get_settings
from app.core.logging import configure_logging

settings = get_settings()

celery_app = Celery("crm", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=240,
    result_expires=3600,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "poll-due-rows": {
            "task": "scheduler.poll_due_rows",
            "schedule": float(settings.due_poll_interval_seconds),
            "options": {"expires": float(settings.due_poll_interval_seconds)},
        },
    },
    imports=("app.worker.tasks",),
)


@setup_logging.connect
def _configure_logging(**_kwargs: object) -> None:
    configure_logging(settings.log_level)
