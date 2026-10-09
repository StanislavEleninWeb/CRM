import os

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.core.logging import REDACTED, redact_processor, redact_value
from app.core.time import today_in


def test_liveness(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "alive"}


def test_readiness_reports_database_and_redis(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": "ok", "redis": "ok"}}


def test_readiness_fails_when_database_is_unreachable(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import db

    broken = create_engine("postgresql+psycopg://crm_app:wrong@127.0.0.1:1/none")
    monkeypatch.setattr(db, "get_engine", lambda: broken)
    db._session_factory.cache_clear()
    try:
        response = client.get("/readyz")
    finally:
        db._session_factory.cache_clear()
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "failed"


def test_runtime_role_cannot_bypass_rls_or_alter_schema() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        assert row.current_user == "crm_app"
        assert row.rolsuper is False
        assert row.rolbypassrls is False
        with pytest.raises(Exception, match="permission denied"):
            conn.execute(text("CREATE TABLE should_not_exist (id int)"))
    engine.dispose()


def test_missing_tenant_setting_resolves_to_null() -> None:
    engine = create_engine(os.environ["DATABASE_URL"])
    with engine.connect() as conn:
        assert conn.execute(text("SELECT app_current_tenant()")).scalar() is None
    engine.dispose()


def test_error_envelope_and_correlation_id(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist", headers={"X-Correlation-ID": "corr-12345678"})
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["correlation_id"] == "corr-12345678"
    assert response.headers["x-correlation-id"] == "corr-12345678"
    assert len(response.headers["x-request-id"]) == 32


def test_malformed_correlation_id_is_replaced(client: TestClient) -> None:
    response = client.get("/healthz", headers={"X-Correlation-ID": "bad id\nwith newline"})
    assert response.headers["x-correlation-id"] == response.headers["x-request-id"]


def test_openapi_is_served_under_v1(client: TestClient) -> None:
    schema = client.get("/api/v1/openapi.json").json()
    assert "/api/v1/system/info" in schema["paths"]
    assert schema["paths"]["/api/v1/system/info"]["get"]["operationId"] == "getSystemInfo"


def test_system_info_defaults(client: TestClient) -> None:
    body = client.get("/api/v1/system/info").json()
    assert body["default_currency"] == "EUR"
    assert body["default_timezone"] == "Europe/Sofia"


def test_log_redaction() -> None:
    event = redact_processor(
        None,
        "info",
        {
            "password": "hunter2",
            "headers": {"Authorization": "Bearer abcdefghijklmnop", "accept": "json"},
            "note": "sent Bearer abcdefghijklmnop to upstream",
            "url": "postgresql://user:pw@db:5432/crm",
        },
    )
    assert event["password"] == REDACTED
    assert event["headers"] == {"Authorization": REDACTED, "accept": "json"}
    assert "abcdefghijklmnop" not in event["note"]
    assert "pw" not in event["url"]
    assert redact_value(42) == 42
    for url in (
        "https://api.example.test/v1/places?key=AIzaSyRealLookingKey123&fields=name",
        "https://x.test/cb?state=abc&code=4/0AbCdEf&scope=openid",
        "GET /hook?access_token=ya29.secret HTTP/1.1",
    ):
        cleaned = redact_value(url)
        assert "AIzaSyRealLookingKey123" not in cleaned and "4/0AbCdEf" not in cleaned and "ya29.secret" not in cleaned
        assert "fields=name" in cleaned or "state=abc" in cleaned or "/hook?" in cleaned  # the rest of the URL is kept


def test_settings_reject_placeholders_in_production() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+psycopg://crm_app:change-me-app@db/crm",
            session_secret="x" * 40,
        )


def test_settings_reject_non_postgres_and_short_secret() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="sqlite:///x.db", session_secret="x" * 40)
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql+psycopg://a:b@db/crm", session_secret="short")


def test_tenant_date_follows_tenant_timezone() -> None:
    from datetime import UTC, datetime

    late_utc = datetime(2026, 10, 7, 22, 30, tzinfo=UTC)
    assert today_in("Europe/Sofia", now=late_utc).isoformat() == "2026-10-08"
    assert today_in("America/Los_Angeles", now=late_utc).isoformat() == "2026-10-07"


def test_worker_task_runs_eagerly() -> None:
    from app.worker.tasks import ping

    result = ping.apply(args=["hello"]).get()
    assert result["echo"] == "hello"


def test_beat_schedule_has_no_long_eta_jobs() -> None:
    from app.worker.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert list(schedule) == ["poll-due-rows"]
    assert schedule["poll-due-rows"]["schedule"] <= 3600
