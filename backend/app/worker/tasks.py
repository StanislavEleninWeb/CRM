"""System tasks. Domain tasks register their own modules as they are added."""

import redis

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.time import utcnow
from app.worker.celery_app import celery_app

log = get_logger(__name__)
LAST_POLL_KEY = "crm:scheduler:last_poll"


@celery_app.task(name="system.ping")
def ping(payload: str = "pong") -> dict[str, str]:
    """A harmless job used to prove the worker runs."""
    return {"echo": payload, "handled_at": utcnow().isoformat()}


@celery_app.task(name="scheduler.poll_due_rows")
def poll_due_rows() -> dict[str, int]:
    """Claim a bounded batch of due rows. Handlers are registered by later phases."""
    from app.worker.due import run_due_handlers

    claimed = run_due_handlers(get_settings().due_poll_batch_size)
    redis.Redis.from_url(get_settings().redis_url).set(LAST_POLL_KEY, utcnow().isoformat(), ex=3600)
    return {"claimed": claimed}


@celery_app.task(name="scheduler.run_due_job", max_retries=0)
def run_due_job(job: dict[str, object]) -> str:
    """Execute one claimed due row. Short and immediate: retries are scheduled in the database."""
    from uuid import UUID

    from app.worker.due import run_claimed

    claimed = {
        **job,
        "id": UUID(str(job["id"])),
        "tenant_id": UUID(str(job["tenant_id"])),
        "ref_id": UUID(str(job["ref_id"])) if job.get("ref_id") else None,
        "lease_token": UUID(str(job["lease_token"])),
    }
    return run_claimed(claimed)
