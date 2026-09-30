"""Migration 063 on a real Postgres: upgrade and downgrade, with duplicate
twilio_sids predating the unique index.

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        python -m pytest -m integration tests/integration/test_migration_063_postgres.py
"""
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, exc, text

pytestmark = pytest.mark.integration

URL = os.environ.get("POSTGRES_TEST_URL", "")
MIGRATION = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "063_voice_automation_stay_and_unique_sms_sid.py"
SID = "SM" + "a" * 32
OLD_TABLES = (
    "CREATE TABLE calendar_events (id SERIAL PRIMARY KEY, external_id VARCHAR(255))",
    """CREATE TABLE voice_automations (
        id SERIAL PRIMARY KEY, name VARCHAR(255) NOT NULL, owner_type VARCHAR(20) NOT NULL,
        guest_name VARCHAR(255), status VARCHAR(20) NOT NULL DEFAULT 'active')""",
    """CREATE TABLE sms_incoming (
        id SERIAL PRIMARY KEY, phone_number VARCHAR(50) NOT NULL, message TEXT NOT NULL,
        twilio_sid VARCHAR(100), received_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
)


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
        pytest.fail("POSTGRES_TEST_URL unset or server unreachable", pytrace=False)


@pytest.fixture
def pg():
    schema = f"m063_{uuid.uuid4().hex[:10]}"
    admin = create_engine(URL)
    with admin.begin() as conn:
        conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_engine(URL, connect_args={"options": f"-csearch_path={schema}"})
    with engine.begin() as conn:
        for ddl in OLD_TABLES:
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO calendar_events (external_id) VALUES ('e1')"))
        conn.execute(text("INSERT INTO voice_automations (name, owner_type, guest_name) VALUES ('a', 'guest', 'Sam')"))
        for sid in (SID, SID, "SM" + "b" * 32, None, None):
            conn.execute(text("INSERT INTO sms_incoming (phone_number, message, twilio_sid) VALUES ('+1', 'm', :s)"), {"s": sid})
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        admin.dispose()


def _run(engine, direction):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    spec = importlib.util.spec_from_file_location("migration_063", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(module, direction)()


def _column(engine):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT is_nullable FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = 'voice_automations' AND column_name = 'calendar_event_id'")).scalar()


def _index_names(engine):
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()"))}


def test_upgrade_then_downgrade_on_postgres(pg):
    _run(pg, "upgrade")
    assert _column(pg) == "YES"
    assert {"idx_voice_automations_stay", "uq_sms_incoming_twilio_sid"} <= _index_names(pg)
    with pg.connect() as conn:
        sids = [r[0] for r in conn.execute(text("SELECT twilio_sid FROM sms_incoming ORDER BY id"))]
    assert sids == [SID, None, "SM" + "b" * 32, None, None]
    with pytest.raises(exc.IntegrityError):
        with pg.begin() as conn:
            conn.execute(text("INSERT INTO sms_incoming (phone_number, message, twilio_sid) VALUES ('+2', 'x', :s)"), {"s": SID})
    # The stay column follows its event: deleting the event clears it.
    with pg.begin() as conn:
        conn.execute(text("UPDATE voice_automations SET calendar_event_id = (SELECT id FROM calendar_events)"))
        conn.execute(text("DELETE FROM calendar_events"))
        assert conn.execute(text("SELECT calendar_event_id FROM voice_automations")).scalar() is None
    _run(pg, "upgrade")  # idempotent
    _run(pg, "downgrade")
    assert _column(pg) is None
    assert not ({"idx_voice_automations_stay", "uq_sms_incoming_twilio_sid"} & _index_names(pg))
