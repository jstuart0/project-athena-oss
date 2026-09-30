"""Migration 062: memories.vector_status (real alembic upgrade/downgrade on
SQLite; the Postgres run is tests/integration/test_memories_postgres.py)."""
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

_ADMIN_BACKEND = Path(__file__).resolve().parents[1]
if str(_ADMIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(_ADMIN_BACKEND))

ALEMBIC_INI = _ADMIN_BACKEND / "alembic.ini"

OLD_SHAPE = """\
CREATE TABLE memories (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL,
    scope VARCHAR(20) NOT NULL,
    vector_id VARCHAR(36),
    is_deleted BOOLEAN NOT NULL DEFAULT 0
)"""


def _engine(tmp_path, *, with_table=True, with_column=False):
    url = f"sqlite:///{tmp_path / 'm062.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('061')"))
        if with_table:
            conn.execute(text(OLD_SHAPE))
            if with_column:
                conn.execute(text("ALTER TABLE memories ADD COLUMN vector_status VARCHAR(16) NOT NULL DEFAULT 'stored'"))
            conn.execute(text("INSERT INTO memories (id, content, scope, vector_id) VALUES (1, 'a', 'owner', 'v-1')"))
            conn.execute(text("INSERT INTO memories (id, content, scope, vector_id) VALUES (2, 'b', 'owner', NULL)"))
    return engine, url


def _cfg(url):
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.set_main_option("script_location", str(_ADMIN_BACKEND / "alembic"))
    return cfg


def _run(monkeypatch, url, fn, target):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        fn(_cfg(url), target)
    finally:
        monkeypatch.undo()


def _columns(engine):
    with engine.connect() as conn:
        return {r[1]: r for r in conn.execute(text("PRAGMA table_info(memories)")).fetchall()}


def test_upgrade_adds_not_null_pending_column_and_backfills_vector_ids(tmp_path, monkeypatch):
    engine, url = _engine(tmp_path)
    _run(monkeypatch, url, command.upgrade, "062")
    cols = _columns(engine)
    assert "vector_status" in cols
    _, _, coltype, notnull, default, _ = cols["vector_status"]
    assert notnull == 1 and "pending" in str(default)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id, vector_status, vector_id FROM memories ORDER BY id")).fetchall()
    assert [r[1] for r in rows] == ["pending", "pending"]
    assert rows[0][2] == "v-1" and rows[1][2] and len(rows[1][2]) == 36


def test_upgrade_skips_when_column_present(tmp_path, monkeypatch):
    engine, url = _engine(tmp_path, with_column=True)
    _run(monkeypatch, url, command.upgrade, "062")
    with engine.connect() as conn:
        statuses = [r[0] for r in conn.execute(text("SELECT vector_status FROM memories ORDER BY id"))]
    assert statuses == ["stored", "stored"]


def test_upgrade_skips_when_table_absent(tmp_path, monkeypatch):
    engine, url = _engine(tmp_path, with_table=False)
    _run(monkeypatch, url, command.upgrade, "062")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "062"
        assert conn.execute(text("SELECT name FROM sqlite_master WHERE name='memories'")).fetchone() is None


def test_downgrade_drops_column(tmp_path, monkeypatch):
    engine, url = _engine(tmp_path)
    _run(monkeypatch, url, command.upgrade, "062")
    _run(monkeypatch, url, command.downgrade, "061")
    assert "vector_status" not in _columns(engine)
