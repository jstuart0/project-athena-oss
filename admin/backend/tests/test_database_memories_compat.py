"""The boot-time compat ALTER for memories.vector_status (D3). The real
Postgres behavior (lock timeout, idempotence) is in
tests/integration/test_memories_postgres.py."""
from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import create_engine, event, text
from structlog.testing import capture_logs

import app.database as database


class _FakeConn:
    def __init__(self, recorder, table=True, column=False):
        self._recorder = recorder
        self._table = table
        self._column = column

    def execute(self, statement, params=None):
        sql = str(statement)
        self._recorder.append(sql)
        found = None
        if "information_schema.tables" in sql:
            found = (1,) if self._table else None
        elif "information_schema.columns" in sql:
            found = (1,) if self._column else None
        return SimpleNamespace(first=lambda: found)


class _FakePostgresEngine:
    def __init__(self, **kwargs):
        self.statements = []
        self._kwargs = kwargs
        self.dialect = SimpleNamespace(name="postgresql")

    @contextmanager
    def begin(self):
        yield _FakeConn(self.statements, **self._kwargs)


def _alters(statements):
    return [s for s in statements if "ALTER TABLE" in s.upper()]


def test_init_db_calls_memories_compat(monkeypatch):
    calls = []
    monkeypatch.setattr(database, "_ensure_memories_table_compatibility", lambda *a, **k: calls.append(1))
    database.init_db()
    assert calls == [1]


def test_init_db_runs_memories_compat_even_when_users_compat_fails(monkeypatch):
    calls = []

    def _users_boom():
        raise RuntimeError("users compat failed")

    monkeypatch.setattr(database, "_ensure_memories_table_compatibility", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(database, "_ensure_users_table_compatibility", _users_boom)
    try:
        database.init_db()
    except RuntimeError:
        pass
    assert calls == [1]


def test_noop_on_sqlite():
    engine = create_engine("sqlite:///:memory:")
    statements = []
    event.listen(engine, "before_cursor_execute", lambda conn, cur, stmt, *a: statements.append(stmt))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE memories (id INTEGER PRIMARY KEY)"))
    statements.clear()
    assert database._ensure_memories_table_compatibility(engine=engine, dev_mode=False) is False
    assert statements == []


def test_noop_in_dev_mode():
    engine = _FakePostgresEngine()
    assert database._ensure_memories_table_compatibility(engine=engine, dev_mode=True) is False
    assert engine.statements == []


def test_noop_when_table_absent():
    engine = _FakePostgresEngine(table=False)
    assert database._ensure_memories_table_compatibility(engine=engine, dev_mode=False) is False
    assert _alters(engine.statements) == []


def test_noop_when_column_present():
    engine = _FakePostgresEngine(column=True)
    assert database._ensure_memories_table_compatibility(engine=engine, dev_mode=False) is False
    assert _alters(engine.statements) == []


def test_adds_column_with_lock_timeout_when_absent():
    engine = _FakePostgresEngine()
    assert database._ensure_memories_table_compatibility(engine=engine, dev_mode=False) is True
    joined = "\n".join(engine.statements)
    assert "SET LOCAL lock_timeout = '5s'" in joined
    [alter] = _alters(engine.statements)
    assert "ADD COLUMN IF NOT EXISTS vector_status VARCHAR(16) NOT NULL DEFAULT 'pending'" in alter
    assert joined.index("lock_timeout") < joined.index("ALTER TABLE")


def test_failure_is_logged_not_raised():
    class _Broken(_FakePostgresEngine):
        @contextmanager
        def begin(self):
            raise RuntimeError("canceling statement due to lock timeout")
            yield  # pragma: no cover

    with capture_logs() as logs:
        assert database._ensure_memories_table_compatibility(engine=_Broken(), dev_mode=False) is False
    assert [e["event"] for e in logs if e["log_level"] == "error"] == ["memories_schema_compat_failed"]
