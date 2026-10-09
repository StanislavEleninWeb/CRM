"""Liveness, readiness and build information."""

from typing import Literal

import redis
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import runtime_role_is_safe, session_scope
from app.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter()
api_router = APIRouter(prefix="/system", tags=["system"])

CheckStatus = Literal["ok", "failed"]


class Readiness(BaseModel):
    status: Literal["ready", "not_ready"]
    checks: dict[str, CheckStatus]


class SystemInfo(BaseModel):
    name: str
    api_version: str
    environment: str
    default_currency: str
    default_timezone: str


@router.get("/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "alive"}


def _check_database() -> CheckStatus:
    try:
        with session_scope() as session:
            session.execute(text("SELECT 1"))
            safe, detail = runtime_role_is_safe(session)
            if not safe:
                log.error("unsafe_database_role", detail=detail)
                return "failed"
        return "ok"
    except Exception as exc:
        log.warning("database_not_ready", error=type(exc).__name__)
        return "failed"


def _check_redis() -> CheckStatus:
    try:
        client = redis.Redis.from_url(get_settings().redis_url, socket_timeout=2)
        client.ping()
        return "ok"
    except Exception as exc:
        log.warning("redis_not_ready", error=type(exc).__name__)
        return "failed"


@router.get("/readyz", response_model=Readiness, include_in_schema=False)
def readyz(response: Response) -> Readiness:
    checks: dict[str, CheckStatus] = {"database": _check_database(), "redis": _check_redis()}
    ready = all(v == "ok" for v in checks.values())
    if not ready:
        response.status_code = 503
    return Readiness(status="ready" if ready else "not_ready", checks=checks)


@api_router.get("/info", response_model=SystemInfo, operation_id="getSystemInfo")
def info() -> SystemInfo:
    settings = get_settings()
    return SystemInfo(
        name="SEWEB CRM",
        api_version="v1",
        environment=settings.environment,
        default_currency=settings.default_currency,
        default_timezone=settings.default_timezone,
    )


@api_router.get("/readiness", response_model=Readiness, operation_id="getReadiness")
def readiness(response: Response) -> Readiness:
    return readyz(response)


@router.get("/ops/metrics", include_in_schema=False)
def ops_metrics(request: Request) -> Response:
    """Counts for monitoring, across all workspaces, in the Prometheus text format. No names, addresses or contents.

    Closed unless ``OPS_METRICS_TOKEN`` is set, and then only to a caller presenting it.
    """
    from app.core.security import constant_time_equal

    token = get_settings().ops_metrics_token
    supplied = request.headers.get("Authorization", "")
    if not token or not constant_time_equal(supplied, f"Bearer {token}"):
        return Response(status_code=404)
    with session_scope() as session:
        rows = session.execute(text("SELECT name, value FROM ops_snapshot()")).all()
    lines = [f"crm_{row.name} {row.value:g}" for row in rows]
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
