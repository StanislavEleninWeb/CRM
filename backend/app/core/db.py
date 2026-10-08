"""Database engine and sessions.

The application connects as a non-owner role without BYPASSRLS. Row-level
security policies read the tenant from a transaction-local setting, so the
tenant must be set inside every transaction and never leaks through the pool.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, MetaData, Table, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, SessionTransaction, sessionmaker

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        future=True,
    )


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


@dataclass(frozen=True)
class RlsContext:
    """Who the current transaction acts for. Applied at the start of every transaction."""

    user_id: UUID | None = None
    tenant_id: UUID | None = None


RLS_KEY = "rls"


@event.listens_for(Session, "after_begin")
def _apply_rls_context(session: Session, _tx: SessionTransaction, connection: Connection) -> None:
    """Set the transaction-local user and tenant.

    Runs for every transaction the session begins, so a commit in the middle of
    a request does not drop the context. ``set_config(..., true)`` is scoped to
    the transaction, so nothing survives on a pooled connection.
    """
    context: RlsContext | None = session.info.get(RLS_KEY)
    if context is None:
        return
    connection.execute(
        text("SELECT set_config('app.user_id', :u, true), set_config('app.tenant_id', :t, true)"),
        {
            "u": str(context.user_id) if context.user_id else "",
            "t": str(context.tenant_id) if context.tenant_id else "",
        },
    )


@contextmanager
def session_scope(context: RlsContext | None = None) -> Iterator[Session]:
    """One transaction. Without a context, tenant-owned tables return nothing."""
    session = _session_factory()()
    if context is not None:
        session.info[RLS_KEY] = context
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    with session_scope() as session:
        yield session


def runtime_role_is_safe(session: Session) -> tuple[bool, str]:
    """The runtime role must not be able to bypass row-level security."""
    row = session.execute(
        text("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
    ).one()
    if row.rolsuper or row.rolbypassrls:
        return False, f"database role {row.rolname} can bypass row-level security"
    return True, row.rolname


_metadata = MetaData()


def table(name: str) -> Table:
    """A reflected table. The schema is defined only by the migrations."""
    existing = _metadata.tables.get(name)
    if existing is not None:
        return existing
    return Table(name, _metadata, autoload_with=get_engine())


def reset_reflection() -> None:
    _metadata.clear()


def rows(result: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in result.mappings()]
