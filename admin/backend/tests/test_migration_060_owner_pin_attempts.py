"""ATHENA-69 D25/D35: regression tests for migration 060 (owner_pin_attempts).

Same real-alembic-upgrade/downgrade pattern as test_migration_059_search_transit.py
and test_migration_058.py -- not a hand-simulated "pretend the SQL ran"
assertion.
"""
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

_ALEMBIC_VERSION_DDL = """\
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL
)"""


def _make_engine(tmp_path, stamp="059"):
    db_url = f"sqlite:///{tmp_path / 'm060.db'}"
    engine = create_engine(db_url, connect_args={"check_same_thread": False})
    with engine.begin() as conn:
        conn.execute(text(_ALEMBIC_VERSION_DDL))
        conn.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": stamp})
    return engine, db_url


def _scratch_alembic_config(db_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option("script_location", str((ALEMBIC_INI.parent / "alembic").resolve()))
    return cfg


def _run_upgrade(monkeypatch, db_url: str, target: str = "060") -> Config:
    cfg = _scratch_alembic_config(db_url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        command.upgrade(cfg, target)
    finally:
        monkeypatch.undo()
    return cfg


def _has_table(engine, name: str) -> bool:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"), {"n": name}
        ).fetchone()
    return row is not None


def _columns(engine, table: str) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return {r[1] for r in rows}


def test_M060a_upgrade_creates_table_with_expected_columns(tmp_path, monkeypatch):
    engine, db_url = _make_engine(tmp_path)
    _run_upgrade(monkeypatch, db_url)

    assert _has_table(engine, "owner_pin_attempts")
    assert _columns(engine, "owner_pin_attempts") == {"tier", "failed_count", "locked_until", "updated_at"}


def test_M060b_upgrade_is_a_noop_when_table_preexists(tmp_path, monkeypatch):
    """Simulates production init_db()'s create_all having already created
    the table from the SQLAlchemy model before this migration ever runs."""
    engine, db_url = _make_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE owner_pin_attempts ("
            "tier VARCHAR(32) PRIMARY KEY, "
            "failed_count INTEGER NOT NULL DEFAULT 0, "
            "locked_until TIMESTAMP, "
            "updated_at TIMESTAMP)"
        ))
        conn.execute(text(
            "INSERT INTO owner_pin_attempts (tier, failed_count) VALUES ('household', 3)"
        ))

    _run_upgrade(monkeypatch, db_url)

    with engine.connect() as conn:
        row = conn.execute(text("SELECT failed_count FROM owner_pin_attempts WHERE tier = 'household'")).fetchone()
    assert row[0] == 3


def test_M060c_downgrade_drops_table(tmp_path, monkeypatch):
    engine, db_url = _make_engine(tmp_path)
    cfg = _run_upgrade(monkeypatch, db_url)
    assert _has_table(engine, "owner_pin_attempts")

    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        command.downgrade(cfg, "059")
    finally:
        monkeypatch.undo()

    assert not _has_table(engine, "owner_pin_attempts")


def test_M060d_downgrade_when_table_absent_is_a_noop(tmp_path, monkeypatch):
    engine, db_url = _make_engine(tmp_path)
    cfg = _run_upgrade(monkeypatch, db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE owner_pin_attempts"))

    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        command.downgrade(cfg, "059")
    finally:
        monkeypatch.undo()

    assert not _has_table(engine, "owner_pin_attempts")
