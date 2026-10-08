"""Test configuration.

Tests run against real PostgreSQL. The schema is migrated by the migrator role;
every test connection uses the runtime role ``crm_app``, which cannot bypass
row-level security.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

_app_url = make_url(os.environ["DATABASE_URL"])
_migrator_url = make_url(os.environ["MIGRATION_DATABASE_URL"])
_database = str(_app_url.database)
_test_db = _database if _database.endswith("_test") else f"{_database}_test"
TEST_DATABASE_URL = _app_url.set(database=_test_db).render_as_string(hide_password=False)
TEST_MIGRATION_URL = _migrator_url.set(database=_test_db).render_as_string(hide_password=False)

os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["MIGRATION_DATABASE_URL"] = TEST_MIGRATION_URL
os.environ["ENVIRONMENT"] = "test"
if not os.environ.get("SECRET_ENCRYPTION_KEYS"):
    # A fixed key for tests only. Real environments generate their own.
    os.environ["SECRET_ENCRYPTION_KEYS"] = "test1:" + "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU="

PRIVATE_FIXTURES = Path(os.environ.get("PRIVATE_FIXTURE_DIR", "/fixtures/private"))
REFERENCE_WORKBOOK = PRIVATE_FIXTURES / "SEWEB_prospects_Bulgaria_2026-10-07.xlsx"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if REFERENCE_WORKBOOK.exists():
        return
    skip = pytest.mark.skip(reason="reference fixture absent")
    for item in items:
        if "reference_fixture" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def reference_workbook() -> Path:
    return REFERENCE_WORKBOOK


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> Iterator[None]:
    """Rebuild the test schema from an empty database using the real migrations."""
    from alembic import command
    from alembic.config import Config

    engine = create_engine(TEST_MIGRATION_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public AUTHORIZATION crm_migrator"))
        conn.execute(text("GRANT USAGE ON SCHEMA public TO crm_app"))
        conn.execute(
            text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO crm_app")
        )
        conn.execute(text("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO crm_app"))
    engine.dispose()

    config = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).parent.parent / "migrations"))
    command.upgrade(config, "head")
    yield


@pytest.fixture(scope="session")
def migrator_engine(migrated_database: None) -> Iterator[object]:
    engine = create_engine(TEST_MIGRATION_URL)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app(migrated_database: None) -> object:
    from app.main import create_app

    return create_app()


@pytest.fixture
def client(app: object) -> Iterator[object]:
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:  # type: ignore[arg-type]
        yield test_client


@pytest.fixture
def make_client(app: object) -> Iterator[object]:
    """Independent browsers: each client has its own cookie jar."""
    from fastapi.testclient import TestClient

    clients: list[TestClient] = []

    def factory() -> TestClient:
        test_client = TestClient(app)  # type: ignore[arg-type]
        clients.append(test_client)
        return test_client

    yield factory
    for test_client in clients:
        test_client.close()


@pytest.fixture(autouse=True)
def clean_tables(migrator_engine: object) -> Iterator[None]:
    """Each test starts with empty tables. Runs as the schema owner."""
    yield
    with migrator_engine.begin() as conn:  # type: ignore[attr-defined]
        tables = conn.execute(
            text(
                "SELECT string_agg(format('%I', tablename), ', ') FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).scalar()
        if tables:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
