"""Database engine and sessions.

The application connects as a non-owner role without BYPASSRLS. Row-level
security policies read the tenant from a transaction-local setting, so the
tenant must be set inside every transaction and never leaks through the pool.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

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


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction without tenant context. Tenant-owned tables return nothing."""
    session = _session_factory()()
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
