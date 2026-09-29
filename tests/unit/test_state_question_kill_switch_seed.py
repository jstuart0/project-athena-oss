"""The state_question_routing_kill_switch flag is seeded disabled by both
seed paths: alembic migration 061 (existing deployments) and
database.py::seed_oss_features (fresh DEV_MODE/SQLite installs).

The orchestrator reads a missing flag as enabled=False, so "disabled"
and "missing" behave the same there; the seed exists so the switch is
visible and toggleable in the Admin UI.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "admin/backend/alembic/versions/061_seed_state_question_routing_kill_switch.py"
DATABASE_PY = REPO_ROOT / "admin/backend/app/database.py"
FLAG = "state_question_routing_kill_switch"

_FEATURES_DDL = """
CREATE TABLE features (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    description TEXT,
    category VARCHAR(50) NOT NULL,
    enabled BOOLEAN NOT NULL,
    avg_latency_ms FLOAT,
    required BOOLEAN,
    priority INTEGER,
    config TEXT
)
"""


def _load_migration():
    spec = importlib.util.spec_from_file_location("_migration_061", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(conn, module):
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        module.upgrade()


@pytest.fixture
def conn():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(sa.text(_FEATURES_DDL))
        yield c


def _row(conn):
    return conn.execute(
        sa.text("SELECT enabled, category, description FROM features WHERE name = :n"), {"n": FLAG}
    ).fetchall()


def test_kill_switch_seed_migration_inserts_disabled_row(conn):
    module = _load_migration()
    assert module.revision == "061" and module.down_revision == "060"
    _run_upgrade(conn, module)
    rows = _row(conn)
    assert len(rows) == 1
    enabled, category, description = rows[0]
    assert not enabled
    assert category == "routing"
    assert description.startswith("Enable to DISABLE state-question routing")


def test_kill_switch_seed_migration_never_overwrites_operator_row(conn):
    conn.execute(
        sa.text(
            "INSERT INTO features (name, display_name, description, category, enabled, required, priority) "
            "VALUES (:n, 'mine', 'mine', 'routing', 1, 0, 1)"
        ),
        {"n": FLAG},
    )
    module = _load_migration()
    _run_upgrade(conn, module)
    _run_upgrade(conn, module)
    rows = _row(conn)
    assert len(rows) == 1
    assert rows[0][0]  # the operator's enabled=True survives


def test_kill_switch_seed_migration_downgrade_removes_row(conn):
    module = _load_migration()
    _run_upgrade(conn, module)
    ctx = MigrationContext.configure(conn)
    with Operations.context(ctx):
        module.downgrade()
    assert _row(conn) == []


def _seed_oss_features_rows():
    tree = ast.parse(DATABASE_PY.read_text(encoding="utf-8"))
    func = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "seed_oss_features"
    )
    features = next(
        n.value for n in ast.walk(func)
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "features" for t in n.targets)
    )
    return [ast.literal_eval(elt) for elt in features.elts]


def test_kill_switch_seed_database_py_seeds_disabled():
    rows = _seed_oss_features_rows()
    assert len(rows) >= 10
    assert any(r[0] == "status_bulk_query" for r in rows)  # named member: the parse found the real list
    matches = [r for r in rows if r[0] == FLAG]
    assert len(matches) == 1
    name, display_name, description, category, enabled, required, priority, config = matches[0]
    assert enabled is False
    assert required is False
    assert category == "routing"
    assert description.startswith("Enable to DISABLE state-question routing")


def test_kill_switch_seed_description_matches_across_paths():
    module = _load_migration()
    row = next(r for r in _seed_oss_features_rows() if r[0] == FLAG)
    assert row[2] == module.KILL_SWITCH_DESCRIPTION
    assert row[1] == module.KILL_SWITCH_DISPLAY_NAME


def test_kill_switch_description_states_questions_never_write_silently():
    """The admin UI text says what the switch keeps: with routing reverted,
    a question that would change a device is confirmed or reworded."""
    module = _load_migration()
    assert "never made silently" in module.KILL_SWITCH_DESCRIPTION
