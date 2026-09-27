"""ATHENA-90 (D9/M5): regression tests for migration 059 (seed the
search_transit tool_registry row).

Tests run against a scratch SQLite engine with a real `alembic
upgrade`/`downgrade` invocation -- same pattern as
admin/backend/tests/test_migration_058.py -- not a hand-simulated "pretend
the SQL ran" assertion. That's what actually proves idempotency (M059b)
and the DO-NOTHING-preserves-an-operator-row guarantee (M059c).

Contract: M059a-d, .mozart/plans/active/
2026-09-27-deliver-athena-transit-and-base-knowledge.test-contract.md
"""
import json
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ADMIN_BACKEND = _REPO_ROOT / "admin" / "backend"
if str(_ADMIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(_ADMIN_BACKEND))

ALEMBIC_INI = _ADMIN_BACKEND / "alembic.ini"

# Only the columns migration 059's INSERT names, plus UNIQUE(tool_name) so
# M059b's idempotency and M059c's DO-NOTHING both have something to prove
# against.
_TOOL_REGISTRY_DDL = """\
CREATE TABLE tool_registry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name VARCHAR(100) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL,
    description TEXT NOT NULL,
    category VARCHAR(50) NOT NULL,
    service_url VARCHAR(500),
    enabled BOOLEAN NOT NULL DEFAULT 1,
    guest_mode_allowed BOOLEAN NOT NULL DEFAULT 0,
    timeout_seconds INTEGER NOT NULL DEFAULT 30,
    source VARCHAR(20) DEFAULT 'static',
    function_schema TEXT NOT NULL
)"""

_ALEMBIC_VERSION_DDL = """\
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL
)"""


def _make_engine(tmp_path, stamp="058"):
    db_url = f"sqlite:///{tmp_path / 'm059.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(_TOOL_REGISTRY_DDL))
        conn.execute(text(_ALEMBIC_VERSION_DDL))
        conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": stamp})
    return engine, db_url


def _scratch_alembic_config(db_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str((ALEMBIC_INI.parent / "alembic").resolve()))
    return cfg


def _run_upgrade(monkeypatch, db_url: str, target: str = "059") -> Config:
    cfg = _scratch_alembic_config(db_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        command.upgrade(cfg, target)
    finally:
        monkeypatch.undo()
    return cfg


def _rows(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT tool_name, display_name, enabled, source, function_schema FROM tool_registry WHERE tool_name = 'search_transit'")
        ).fetchall()


def test_M059a_upgrade_inserts_one_enabled_static_row(tmp_path, monkeypatch):
    engine, db_url = _make_engine(tmp_path)
    _run_upgrade(monkeypatch, db_url)

    rows = _rows(engine)
    assert len(rows) == 1
    row = rows[0]
    assert bool(row.enabled) is True
    assert row.source == "static"
    schema = json.loads(row.function_schema)
    assert schema["function"]["name"] == "search_transit"


def test_M059b_second_upgrade_is_idempotent(tmp_path, monkeypatch):
    engine, db_url = _make_engine(tmp_path)
    _run_upgrade(monkeypatch, db_url)
    first = _rows(engine)

    # Re-running upgrade on an already-at-059 DB is a no-op for alembic
    # itself, so drive the migration's own upgrade() a second time via the
    # same SQL path by re-stamping to 058 and upgrading again.
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = '058'"))
    _run_upgrade(monkeypatch, db_url)
    second = _rows(engine)

    assert len(second) == 1
    assert first[0] == second[0]


def test_M059c_preexisting_disabled_row_is_preserved(tmp_path, monkeypatch):
    engine, db_url = _make_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tool_registry "
                "(tool_name, display_name, description, category, service_url, "
                " enabled, guest_mode_allowed, timeout_seconds, source, function_schema) "
                "VALUES ('search_transit', 'Transit & Transportation', 'x', 'rag', "
                " 'http://athena-rag-transportation:8025', 0, 1, 20, 'static', '{}')"
            )
        )

    _run_upgrade(monkeypatch, db_url)

    rows = _rows(engine)
    assert len(rows) == 1
    assert bool(rows[0].enabled) is False


def test_M059d_downgrade_removes_static_row_even_if_operator_preseeded(tmp_path, monkeypatch):
    """The accepted risk in the plan's Risks section: downgrade removes any
    source='static' search_transit row, including one an operator manually
    inserted before the migration ever ran (the migration's own upgrade is
    ON CONFLICT DO NOTHING, so it never touches this row -- downgrade still
    deletes it, by tool_name + source alone)."""
    engine, db_url = _make_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tool_registry "
                "(tool_name, display_name, description, category, service_url, "
                " enabled, guest_mode_allowed, timeout_seconds, source, function_schema) "
                "VALUES ('search_transit', 'Operator Row', 'x', 'rag', "
                " 'http://athena-rag-transportation:8025', 1, 1, 20, 'static', '{}')"
            )
        )

    cfg = _run_upgrade(monkeypatch, db_url)
    assert len(_rows(engine)) == 1

    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        command.downgrade(cfg, "058")
    finally:
        monkeypatch.undo()

    assert len(_rows(engine)) == 0
