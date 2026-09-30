"""Migration 063: voice_automations.calendar_event_id (nullable, indexed)
and a unique twilio_sid on sms_incoming. Real alembic upgrade/downgrade on
SQLite; the Postgres run is tests/integration/test_migration_063_postgres.py.

Duplicate twilio_sids that predate the constraint (Twilio retries answered
twice) keep the SID on their earliest row; the later rows keep every other
column and lose only the SID.
"""
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, exc, text

_ADMIN_BACKEND = Path(__file__).resolve().parents[1]
if str(_ADMIN_BACKEND) not in sys.path:
    sys.path.insert(0, str(_ADMIN_BACKEND))

ALEMBIC_INI = _ADMIN_BACKEND / "alembic.ini"

OLD_TABLES = (
    "CREATE TABLE calendar_events (id INTEGER PRIMARY KEY, external_id VARCHAR(255))",
    """CREATE TABLE voice_automations (
        id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, owner_type VARCHAR(20) NOT NULL,
        guest_name VARCHAR(255), status VARCHAR(20) NOT NULL DEFAULT 'active')""",
    """CREATE TABLE sms_incoming (
        id INTEGER PRIMARY KEY, phone_number VARCHAR(50) NOT NULL, message TEXT NOT NULL,
        twilio_sid VARCHAR(100), received_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
)
SID = "SM" + "a" * 32


def _engine(tmp_path, *, with_tables=True):
    url = f"sqlite:///{tmp_path / 'm063.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('062')"))
        if with_tables:
            for ddl in OLD_TABLES:
                conn.execute(text(ddl))
            conn.execute(text("INSERT INTO voice_automations (id, name, owner_type, guest_name) VALUES (1, 'a', 'guest', 'Sam')"))
            for row_id, sid in ((1, SID), (2, SID), (3, SID), (4, "SM" + "b" * 32), (5, None), (6, None)):
                conn.execute(text("INSERT INTO sms_incoming (id, phone_number, message, twilio_sid) VALUES (:i, '+1', 'm', :s)"),
                             {"i": row_id, "s": sid})
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


def _columns(engine, table):
    with engine.connect() as conn:
        return {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()}


def _indexes(engine, table):
    with engine.connect() as conn:
        return {r[1]: bool(r[2]) for r in conn.execute(text(f"PRAGMA index_list({table})")).fetchall()}


def _sids(engine):
    with engine.connect() as conn:
        return dict(conn.execute(text("SELECT id, twilio_sid FROM sms_incoming ORDER BY id")).fetchall())


def test_upgrade_adds_the_stay_column_and_a_unique_sid(tmp_path, monkeypatch):
    engine, url = _engine(tmp_path)
    _run(monkeypatch, url, command.upgrade, "063")
    assert "calendar_event_id" in _columns(engine, "voice_automations")
    assert _indexes(engine, "voice_automations").get("idx_voice_automations_stay") is False
    assert _indexes(engine, "sms_incoming").get("uq_sms_incoming_twilio_sid") is True
    with engine.connect() as conn:
        assert conn.execute(text("SELECT calendar_event_id FROM voice_automations WHERE id = 1")).scalar() is None
    # The earliest duplicate keeps the SID; the others keep their rows.
    assert _sids(engine) == {1: SID, 2: None, 3: None, 4: "SM" + "b" * 32, 5: None, 6: None}
    with pytest.raises(exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO sms_incoming (phone_number, message, twilio_sid) VALUES ('+2', 'x', :s)"), {"s": SID})


def test_downgrade_removes_both(tmp_path, monkeypatch):
    engine, url = _engine(tmp_path)
    _run(monkeypatch, url, command.upgrade, "063")
    _run(monkeypatch, url, command.downgrade, "062")
    assert "calendar_event_id" not in _columns(engine, "voice_automations")
    assert "uq_sms_incoming_twilio_sid" not in _indexes(engine, "sms_incoming")
    assert "idx_voice_automations_stay" not in _indexes(engine, "voice_automations")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sms_incoming (phone_number, message, twilio_sid) VALUES ('+2', 'x', :s)"), {"s": SID})


def test_upgrade_is_idempotent_and_skips_missing_tables(tmp_path, monkeypatch):
    engine, url = _engine(tmp_path, with_tables=False)
    _run(monkeypatch, url, command.upgrade, "063")
    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "063"
