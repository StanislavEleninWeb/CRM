"""Tenant context for background jobs.

A job message carries the tenant identifier that server code put there. The
job re-checks, at execution time, that the tenant still exists and that the
acting member (when there is one) still belongs to it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import RlsContext, session_scope
from app.core.errors import PermissionDeniedError


@contextmanager
def tenant_job(
    tenant_id: UUID | str | None, actor_user_id: UUID | str | None = None
) -> Iterator[Session]:
    if not tenant_id:
        raise PermissionDeniedError("A background job requires a tenant.")
    tenant = UUID(str(tenant_id))
    actor = UUID(str(actor_user_id)) if actor_user_id else None
    with session_scope(RlsContext(user_id=actor, tenant_id=tenant)) as session:
        if (
            session.execute(text("SELECT 1 FROM tenants WHERE id = :t"), {"t": tenant}).first()
            is None
        ):
            raise PermissionDeniedError("The tenant for this job no longer exists.")
        if actor is not None:
            member = session.execute(
                text("SELECT 1 FROM memberships WHERE tenant_id = :t AND user_id = :u"),
                {"t": tenant, "u": actor},
            ).first()
            if member is None:
                raise PermissionDeniedError("The member who started this job was removed.")
        yield session
