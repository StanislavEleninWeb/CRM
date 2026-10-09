"""Background work for imports. Each task carries the tenant and re-checks it on execution."""

from typing import Any
from uuid import UUID

from sqlalchemy import text

from app.core.db import RlsContext, session_scope
from app.core.logging import get_logger
from app.core.storage import get_storage
from app.modules.research.importer import build_preview, commit_import
from app.modules.research.workbook import WorkbookError, read_upload
from app.worker.celery_app import celery_app
from app.worker.context import tenant_job

log = get_logger(__name__)


def _fail(tenant_id: str, import_id: str, message: str) -> None:
    """Record a failure in its own transaction, after the failed work has rolled back."""
    with session_scope(RlsContext(tenant_id=UUID(tenant_id))) as db:
        db.execute(
            text("UPDATE imports SET status = 'failed', error = :e WHERE tenant_id = :t AND id = :i"),
            {"e": message[:500], "t": tenant_id, "i": import_id},
        )


@celery_app.task(name="imports.parse", max_retries=0)
def parse_import(tenant_id: str, import_id: str, actor_user_id: str | None = None) -> dict[str, Any]:
    try:
        with tenant_job(tenant_id, actor_user_id) as db:
            row = db.execute(
                text(
                    "UPDATE imports SET status = 'parsing' WHERE tenant_id = :t AND id = :i "
                    "AND status IN ('uploaded', 'ready', 'failed') RETURNING filename, storage_key, mapping"
                ),
                {"t": tenant_id, "i": import_id},
            ).one_or_none()
            if row is None:
                return {"skipped": True}  # duplicate delivery, or already committed
            data = get_storage().get(row.storage_key)
            workbook = read_upload(row.filename, data)
            override = row.mapping.get("override") if isinstance(row.mapping, dict) else None
            report = build_preview(db, UUID(tenant_id), UUID(import_id), workbook, override)
            return {"rows": report["counts"].get("rows", 0)}
    except WorkbookError as exc:
        _fail(tenant_id, import_id, str(exc))
        return {"failed": str(exc)}
    except Exception as exc:
        log.error("import_parse_failed", import_id=import_id, exc_info=exc)
        _fail(tenant_id, import_id, "The file could not be processed.")
        return {"failed": "internal"}


@celery_app.task(name="imports.commit", max_retries=0)
def commit_import_task(tenant_id: str, import_id: str, actor_user_id: str | None = None) -> dict[str, Any]:
    try:
        with tenant_job(tenant_id, actor_user_id) as db:
            claimed = db.execute(
                text(
                    "UPDATE imports SET status = 'committing' WHERE tenant_id = :t AND id = :i AND status = 'ready' "
                    "RETURNING id"
                ),
                {"t": tenant_id, "i": import_id},
            ).one_or_none()
            if claimed is None:
                return {"skipped": True}  # a duplicate delivery must not apply the import twice
            import json

            result = commit_import(db, UUID(tenant_id), UUID(import_id), UUID(actor_user_id) if actor_user_id else None)
            db.execute(
                text(
                    "UPDATE imports SET status = 'committed', committed_at = now(), result = CAST(:r AS jsonb) "
                    "WHERE tenant_id = :t AND id = :i"
                ),
                {"r": json.dumps(result), "t": tenant_id, "i": import_id},
            )
            db.execute(
                text(
                    "INSERT INTO audit_events (tenant_id, actor_type, actor_id, action, target_type, target_id, origin, data) "
                    "VALUES (:t, 'user', :u, 'import.committed', 'import', :i, 'worker', CAST(:r AS jsonb))"
                ),
                {"t": tenant_id, "u": actor_user_id, "i": import_id, "r": json.dumps(result)},
            )
            return result
    except Exception as exc:
        log.error("import_commit_failed", import_id=import_id, exc_info=exc)
        _fail(tenant_id, import_id, "The import could not be applied. No records were changed.")
        return {"failed": "internal"}
