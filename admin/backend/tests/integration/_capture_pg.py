"""Shared harness for the conversation-capture Postgres tests.

Each test gets a fresh schema holding a minimal ``system_settings`` table
(asserted against ``app.models.SystemSetting``), revision 052's tables and,
unless asked for the old shape, revision 064's columns. Both migrations are
loaded from file and run through alembic's Operations, the pattern of
test_migration_063_postgres.py; the full chain is not run (it has never run
from an empty database in CI).
"""
from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import create_engine, text

URL = os.environ.get("POSTGRES_TEST_URL", "")
VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"

SYSTEM_SETTINGS_DDL = """
CREATE TABLE system_settings (
    id          SERIAL PRIMARY KEY,
    key         VARCHAR(255) NOT NULL UNIQUE,
    value       TEXT NOT NULL,
    description TEXT,
    category    VARCHAR(100) DEFAULT 'general',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
)
"""


def require_server() -> None:
    reachable = False
    if URL:
        try:
            with create_engine(URL).connect() as conn:
                reachable = conn.execute(text("SELECT 1")).scalar() == 1
        except Exception:
            reachable = False
    if not reachable:
        pytest.fail("POSTGRES_TEST_URL unset or server unreachable", pytrace=False)


def run_migration(engine, filename: str, direction: str) -> None:
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    spec = importlib.util.spec_from_file_location(f"capture_{filename[:3]}", VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            with ctx.begin_transaction():
                getattr(module, direction)()


M052 = "052_add_conversation_analytics_tables.py"
M064 = "064_conversation_capture_columns.py"


def assert_fixture_matches_model(engine, schema: str) -> None:
    """The minimal system_settings table must have the model's shape."""
    from app.models import SystemSetting

    model = SystemSetting.__table__
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_schema = :s AND table_name = 'system_settings'"), {"s": schema}).all()
        unique = conn.execute(text(
            "SELECT count(*) FROM pg_indexes WHERE schemaname = :s AND tablename = 'system_settings' "
            "AND indexdef ILIKE '%UNIQUE%(key)%'"), {"s": schema}).scalar()
    assert {name for name, _ in rows} == {c.name for c in model.columns}
    assert dict(rows)["value"] == "NO" and not model.c.value.nullable
    assert model.c.key.unique and unique >= 1


class Database:
    """A throwaway schema; ``dsn`` points an asyncpg pool at it."""

    def __init__(self, upgrade_064: bool = True, pre_064=None):
        self.schema = f"cap_{uuid.uuid4().hex[:10]}"
        self.admin = create_engine(URL)
        with self.admin.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA {self.schema}"))
        self.engine = create_engine(URL, connect_args={"options": f"-csearch_path={self.schema}"})
        with self.engine.begin() as conn:
            conn.execute(text(SYSTEM_SETTINGS_DDL))
        assert_fixture_matches_model(self.engine, self.schema)
        run_migration(self.engine, M052, "upgrade")
        if pre_064 is not None:
            with self.engine.begin() as conn:
                pre_064(conn)
        if upgrade_064:
            run_migration(self.engine, M064, "upgrade")
        parts = urlsplit(URL)
        self.dsn = urlunsplit((parts.scheme, parts.netloc, parts.path, f"search_path={self.schema}", ""))

    def set_flag(self, key: str, value: bool) -> None:
        with self.engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO system_settings (key, value, category) VALUES (:k, :v, 'privacy') "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value"), {"k": key, "v": str(value).lower()})

    def rows(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).mappings().all()

    def scalar(self, sql: str, **params):
        with self.engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def drop(self) -> None:
        self.engine.dispose()
        with self.admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {self.schema} CASCADE"))
        self.admin.dispose()
