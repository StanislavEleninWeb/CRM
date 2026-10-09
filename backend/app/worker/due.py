"""Database-backed scheduling.

Anything that must happen later is a row in ``due_jobs`` with a due time. One scheduler
tick claims a bounded batch of due rows with ``FOR UPDATE SKIP LOCKED`` and a lease, in a
short transaction, and hands each to a short task. Nothing is parked in the task queue
with a long delay. A worker that dies simply lets its lease expire and the row is
claimed again.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.core.logging import get_logger
from app.core.time import utcnow

log = get_logger(__name__)
LEASE_SECONDS = 120
# A handler returns None when the job is finished, or a datetime to run again at that time.
DueHandler = Callable[[Session, UUID, UUID | None, dict[str, Any]], datetime | None]
_handlers: dict[str, DueHandler] = {}


_builtin_loaded = False


def register_due_handler(kind: str, handler: DueHandler) -> None:
    _handlers[kind] = handler


def _load_builtin_handlers() -> None:
    """Connect each kind of due row to the code that runs it. Done on first use, in any process."""
    global _builtin_loaded
    if _builtin_loaded:
        return
    from app.modules.discovery import orchestrator

    _handlers.setdefault("research.run", orchestrator.run_handler)
    _handlers.setdefault("research.schedule", orchestrator.schedule_handler)
    from app.modules.email import sync

    _handlers.setdefault("mailbox.sync", sync.sync_handler)
    _handlers.setdefault("mailbox.watch", sync.watch_handler)
    from app.modules.email import dispatch

    _handlers.setdefault(dispatch.DUE_KIND, dispatch.send_handler)
    from app.modules.integrations import webhooks

    _handlers.setdefault(webhooks.DUE_KIND, webhooks.deliver_handler)
    _builtin_loaded = True


def schedule(
    db: Session,
    tenant_id: UUID,
    *,
    kind: str,
    unique_key: str,
    due_at: datetime | None = None,
    ref_id: UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Record work to do at ``due_at`` (default: now). The same key is never scheduled twice."""
    import json

    db.execute(
        text(
            "INSERT INTO due_jobs (tenant_id, kind, ref_id, unique_key, due_at, payload) "
            "VALUES (:t, :k, :r, :u, :d, CAST(:p AS jsonb)) ON CONFLICT (tenant_id, kind, unique_key) DO NOTHING"
        ),
        {
            "t": tenant_id,
            "k": kind,
            "r": ref_id,
            "u": unique_key,
            "d": due_at or utcnow(),
            "p": json.dumps(payload or {}),
        },
    )


def claim_due(batch_size: int, lease_seconds: int = LEASE_SECONDS) -> list[dict[str, Any]]:
    """Claim a batch across all tenants. Runs without tenant context, through the definer function."""
    with session_scope() as db:
        rows = (
            db.execute(text("SELECT * FROM due_jobs_claim(:n, :lease)"), {"n": batch_size, "lease": lease_seconds})
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]


def run_claimed(job: dict[str, Any]) -> str:
    """Execute one claimed job inside its tenant. Returns ``done``, ``rescheduled``, ``stale`` or ``failed``."""
    from app.worker.context import tenant_job

    _load_builtin_handlers()
    handler = _handlers.get(job["kind"])
    try:
        with tenant_job(job["tenant_id"]) as db:
            # Only the holder of the current lease may act: a slow worker whose lease expired must stop.
            held = db.execute(
                text(
                    "SELECT 1 FROM due_jobs WHERE id = :id AND lease_token = :lease AND status = 'claimed' FOR UPDATE"
                ),
                {"id": job["id"], "lease": job["lease_token"]},
            ).first()
            if held is None:
                return "stale"
            if handler is None:
                db.execute(
                    text(
                        "UPDATE due_jobs SET status = 'failed', last_error = 'no handler', finished_at = now() WHERE id = :id"
                    ),
                    {"id": job["id"]},
                )
                return "failed"
            again_at = handler(db, UUID(str(job["tenant_id"])), job["ref_id"], job["payload"] or {})
            if again_at is None:
                db.execute(
                    text(
                        "UPDATE due_jobs SET status = 'done', finished_at = now(), lease_token = NULL WHERE id = :id AND lease_token = :lease"
                    ),
                    {"id": job["id"], "lease": job["lease_token"]},
                )
                return "done"
            db.execute(
                text(
                    "UPDATE due_jobs SET status = 'pending', due_at = :d, lease_token = NULL, lease_expires_at = NULL, attempts = 0 "
                    "WHERE id = :id AND lease_token = :lease"
                ),
                {"d": again_at, "id": job["id"], "lease": job["lease_token"]},
            )
            return "rescheduled"
    except Exception as exc:
        log.error("due_job_failed", kind=job["kind"], job_id=str(job["id"]), exc_info=exc)
        _record_failure(job, str(exc))
        return "failed"


def _record_failure(job: dict[str, Any], message: str) -> None:
    """Retry later with backoff, kept in the database; give up after ``max_attempts``."""
    from app.core.db import RlsContext

    delay = timedelta(seconds=min(30 * 2 ** min(job["attempts"], 7), 3600))
    with session_scope(RlsContext(tenant_id=UUID(str(job["tenant_id"])))) as db:
        db.execute(
            text(
                "UPDATE due_jobs SET status = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END, "
                "due_at = now() + :delay, lease_token = NULL, lease_expires_at = NULL, last_error = :e, "
                "finished_at = CASE WHEN attempts >= max_attempts THEN now() END WHERE id = :id AND lease_token = :lease"
            ),
            {"delay": delay, "e": message[:500], "id": job["id"], "lease": job["lease_token"]},
        )


def run_due_handlers(batch_size: int) -> int:
    """One scheduler tick: claim due rows and hand each to a short task."""
    from app.worker.tasks import run_due_job

    jobs = claim_due(batch_size)
    for job in jobs:
        run_due_job.delay(
            {
                **job,
                "id": str(job["id"]),
                "tenant_id": str(job["tenant_id"]),
                "ref_id": str(job["ref_id"]) if job["ref_id"] else None,
                "lease_token": str(job["lease_token"]),
            }
        )
    return len(jobs)


def backlog() -> dict[str, int]:
    with session_scope() as db:
        row = db.execute(text("SELECT * FROM due_jobs_backlog()")).one()
        return {"due": row.due, "oldest_due_seconds": row.oldest_due_seconds}
