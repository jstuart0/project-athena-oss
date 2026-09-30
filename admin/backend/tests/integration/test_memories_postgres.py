"""memories.vector_status on real Postgres: the boot-time compat ALTER
(idempotent, lock-timeout bounded) and migration 062 (including the
column-present branch after the compat ALTER ran).

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        pytest -m integration tests/integration/test_memories_postgres.py

Without a reachable server every test fails (never skips).
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from structlog.testing import capture_logs

import app.database as database

pytestmark = pytest.mark.integration

URL = os.environ.get("POSTGRES_TEST_URL", "")
MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "062_add_memory_vector_status.py"
OLD_SHAPE = """
CREATE TABLE memories (
    id SERIAL PRIMARY KEY,
    content TEXT NOT NULL,
    scope VARCHAR(20) NOT NULL,
    vector_id VARCHAR(36),
    is_deleted BOOLEAN NOT NULL DEFAULT false
)"""


@pytest.fixture(scope="module", autouse=True)
def _server_required():
    reachable = False
    if URL:
        try:
            with create_engine(URL).connect() as conn:
                reachable = conn.execute(text("SELECT 1")).scalar() == 1
        except Exception:
            reachable = False
    if not reachable:
        pytest.fail("POSTGRES_TEST_URL unset or server unreachable: point it at a running Postgres "
                    "to run the real-database tier", pytrace=False)


@pytest.fixture
def pg():
    """An engine whose search_path is a fresh schema holding an old-shape
    memories table with one row."""
    schema = f"it_{uuid.uuid4().hex[:10]}"
    admin = create_engine(URL)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(URL, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        conn.execute(text(OLD_SHAPE))
        conn.execute(text("INSERT INTO memories (content, scope, vector_id) VALUES ('a', 'owner', 'v-1')"))
    statements = []
    event.listen(engine, "before_cursor_execute", lambda c, cur, stmt, *a: statements.append(stmt))
    engine.statements = statements
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def _column(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'memories' AND column_name = 'vector_status'"
        )).first()


def _alters(engine):
    return [s for s in engine.statements if "ALTER TABLE" in s.upper()]


def _migration():
    spec = importlib.util.spec_from_file_location("migration_062", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_migration(engine, direction):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    module = _migration()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(module, direction)()


def test_compat_adds_column_once(pg):
    assert database._ensure_memories_table_compatibility(engine=pg, dev_mode=False) is True
    nullable, default = _column(pg)
    assert nullable == "NO" and "pending" in default
    with pg.connect() as conn:
        assert conn.execute(text("SELECT vector_status FROM memories")).scalar() == "pending"
    pg.statements.clear()
    assert database._ensure_memories_table_compatibility(engine=pg, dev_mode=False) is False
    assert _alters(pg) == []


def test_compat_gives_up_on_a_held_lock(pg):
    locked = threading.Event()
    release = threading.Event()

    def _hold():
        with pg.connect() as conn:
            trans = conn.begin()
            conn.execute(text("LOCK TABLE memories IN ACCESS EXCLUSIVE MODE"))
            locked.set()
            release.wait(30)
            trans.rollback()

    holder = threading.Thread(target=_hold)
    holder.start()
    assert locked.wait(10)
    try:
        started = time.monotonic()
        with capture_logs() as logs:
            result = database._ensure_memories_table_compatibility(engine=pg, dev_mode=False)
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(30)
    assert result is False and elapsed < 15
    assert "memories_schema_compat_failed" in [e["event"] for e in logs]


def test_062_upgrade_adds_not_null_pending(pg):
    _run_migration(pg, "upgrade")
    nullable, default = _column(pg)
    assert nullable == "NO" and "pending" in default


def test_062_upgrade_after_compat_executes_no_alter(pg):
    assert database._ensure_memories_table_compatibility(engine=pg, dev_mode=False) is True
    pg.statements.clear()
    _run_migration(pg, "upgrade")
    assert _alters(pg) == []
    assert _column(pg)[0] == "NO"


def test_062_downgrade_drops_column(pg):
    _run_migration(pg, "upgrade")
    _run_migration(pg, "downgrade")
    assert _column(pg) is None


def test_062_upgrade_gives_up_on_a_held_lock(pg):
    """The migration's ALTER is lock-timeout bounded like the compat ALTER."""
    locked = threading.Event()
    release = threading.Event()

    def _hold():
        with pg.connect() as conn:
            trans = conn.begin()
            conn.execute(text("LOCK TABLE memories IN ACCESS EXCLUSIVE MODE"))
            locked.set()
            release.wait(60)
            trans.rollback()

    holder = threading.Thread(target=_hold)
    holder.start()
    assert locked.wait(10)
    try:
        started = time.monotonic()
        with pytest.raises(Exception) as info:
            _run_migration(pg, "upgrade")
        elapsed = time.monotonic() - started
    finally:
        release.set()
        holder.join(60)
    assert elapsed < 15
    assert "lock timeout" in str(info.value).lower()
