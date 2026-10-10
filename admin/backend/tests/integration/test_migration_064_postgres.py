"""Revision 064 on a real Postgres: shape, backfill, idempotence, seeds.

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        python -m pytest -m integration tests/integration/test_migration_064_postgres.py

Without a reachable server every test fails (never skips).
"""
from __future__ import annotations

import asyncio

import pytest

from shared import conversation_capture as cc
from tests.integration._capture_pg import M064, Database, require_server, run_migration

pytestmark = pytest.mark.integration

NEW_TURN_COLUMNS = {"route", "channel", "audience_class", "outcome", "text_withheld", "debug_detail"}
INDEXES = {"idx_conversations_class_started_m", "idx_conversations_started_id_m"}


@pytest.fixture(autouse=True)
def _server():
    require_server()


def _legacy_rows(conn):
    conn.exec_driver_sql("""
        INSERT INTO conversations (id, session_id, turn_count) VALUES
          ('c-own', 'own-1', 1), ('c-gst', 'gst-1', 1), ('c-pub', 'pub-1', 1),
          ('c-sms', 'sms_abc', 1), ('c-dev', 'ha-device-9', 1)
    """)
    conn.exec_driver_sql("INSERT INTO system_settings (key, value, category) VALUES ('conversation_retention_days', '365', 'privacy')")


@pytest.fixture
def old_shape():
    database = Database(upgrade_064=False, pre_064=_legacy_rows)
    try:
        yield database
    finally:
        database.drop()


def _columns(db, table):
    return {r["column_name"] for r in db.rows(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = :t", t=table)}


def _indexes(db):
    return {r["indexname"] for r in db.rows("SELECT indexname FROM pg_indexes WHERE schemaname = current_schema()")}


def test_upgrade_adds_columns_indexes_and_seeds_without_rewriting_rows(old_shape):
    db = old_shape
    assert not (NEW_TURN_COLUMNS & _columns(db, "conversation_turns"))
    run_migration(db.engine, M064, "upgrade")
    assert NEW_TURN_COLUMNS <= _columns(db, "conversation_turns")
    assert "session_class" in _columns(db, "conversations")
    assert INDEXES <= _indexes(db)
    # no backfill: existing rows keep NULL and are adopted by the next turn written to them
    assert db.scalar("SELECT count(*) FROM conversations WHERE session_class IS NOT NULL") == 0
    assert db.scalar("SELECT bool_and(indisvalid) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                     "WHERE c.relname IN ('idx_conversations_class_started_m', 'idx_conversations_started_id_m')") is True
    assert db.scalar("SELECT value FROM system_settings WHERE key = 'conversation_debug_mode_enabled'") == "false"
    assert db.scalar("SELECT category FROM system_settings WHERE key = 'conversation_debug_mode_enabled'") == "privacy"


def test_an_operators_retention_survives_the_seed(old_shape):
    run_migration(old_shape.engine, M064, "upgrade")
    assert old_shape.scalar("SELECT value FROM system_settings WHERE key = 'conversation_retention_days'") == "365"


def test_fresh_install_gets_30_days():
    db = Database(upgrade_064=True)
    try:
        assert db.scalar("SELECT value FROM system_settings WHERE key = 'conversation_retention_days'") == "30"
    finally:
        db.drop()


def test_up_down_up_is_idempotent_and_downgrade_keeps_operator_values(old_shape):
    db = old_shape
    run_migration(db.engine, M064, "upgrade")
    run_migration(db.engine, M064, "upgrade")  # re-run is a no-op
    with db.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE system_settings SET value = 'true' WHERE key = 'conversation_debug_mode_enabled'")
    run_migration(db.engine, M064, "downgrade")
    assert not (NEW_TURN_COLUMNS & _columns(db, "conversation_turns"))
    assert "session_class" not in _columns(db, "conversations")
    assert not (INDEXES & _indexes(db))
    # the retention value is the operator's (365), the debug value was changed: neither is deleted
    assert db.scalar("SELECT value FROM system_settings WHERE key = 'conversation_retention_days'") == "365"
    assert db.scalar("SELECT value FROM system_settings WHERE key = 'conversation_debug_mode_enabled'") == "true"
    run_migration(db.engine, M064, "downgrade")  # idempotent
    run_migration(db.engine, M064, "upgrade")
    assert NEW_TURN_COLUMNS <= _columns(db, "conversation_turns")


def test_downgrade_removes_untouched_seeds():
    db = Database(upgrade_064=True)
    try:
        run_migration(db.engine, M064, "downgrade")
        assert db.scalar("SELECT count(*) FROM system_settings WHERE key IN ('conversation_debug_mode_enabled', 'conversation_retention_days')") == 0
        assert db.scalar("SELECT count(*) FROM system_settings WHERE key = 'analytics_mode_enabled'") == 1
    finally:
        db.drop()


def test_a_legacy_null_class_conversation_takes_new_turns_and_is_stamped(old_shape):
    db = old_shape
    run_migration(db.engine, M064, "upgrade")
    db.set_flag(cc.ANALYTICS_KEY, True)
    record = cc.sanitize_record(cc.CaptureRecord(
        session_id="sms_abc", query_text="hello", response_text="hi", route="query", channel="text",
        audience_class="other", session_class="other"))

    async def run():
        store = cc.AsyncpgCaptureStore(db.dsn, replica="t")
        try:
            return await store.write(record)
        finally:
            await store.close()

    assert asyncio.run(run()) == cc.RESULT_WRITTEN
    assert db.scalar("SELECT turn_count FROM conversations WHERE session_id = 'sms_abc'") == 2
    assert db.scalar("SELECT session_class FROM conversations WHERE session_id = 'sms_abc'") == "other"


def test_after_064_an_old_orchestrator_insert_makes_a_null_class_row_the_next_new_turn_stamps(old_shape):
    db = old_shape
    run_migration(db.engine, M064, "upgrade")
    db.set_flag(cc.ANALYTICS_KEY, True)
    with db.engine.begin() as conn:  # old column list, no session_class
        conn.exec_driver_sql("INSERT INTO conversations (id, session_id, turn_count) VALUES ('c-late', 'own-late', 1)")
        conn.exec_driver_sql("INSERT INTO conversation_turns (id, conversation_id, turn_number, query_text, source) "
                             "VALUES ('t-late', 'c-late', 1, 'q', 'analytics')")
    assert db.scalar("SELECT session_class FROM conversations WHERE session_id = 'own-late'") is None
    record = cc.sanitize_record(cc.CaptureRecord(
        session_id="own-late", query_text="next", response_text="ok", route="query", channel="speech",
        audience_class="owner", session_class="owner"))

    async def run():
        store = cc.AsyncpgCaptureStore(db.dsn, replica="t")
        try:
            return await store.write(record)
        finally:
            await store.close()

    assert asyncio.run(run()) == cc.RESULT_WRITTEN
    assert db.scalar("SELECT session_class FROM conversations WHERE session_id = 'own-late'") == "owner"
    assert db.scalar("SELECT count(*) FROM conversation_turns WHERE conversation_id = 'c-late'") == 2


def test_readers_and_writers_are_not_blocked_while_the_indexes_build():
    """The indexes are built CONCURRENTLY outside the DDL transaction: a reader
    writing to the table throughout never waits on the build (a plain
    CREATE INDEX would block writes for its whole duration)."""
    import threading
    import time

    from sqlalchemy import create_engine

    from tests.integration._capture_pg import URL

    db = Database(upgrade_064=False, pre_064=lambda conn: conn.exec_driver_sql(
        "INSERT INTO conversations (id, session_id, turn_count) "
        "SELECT 'c' || g, 'sms_' || g, 1 FROM generate_series(1, 400000) g"))
    stop, latencies, errors = threading.Event(), [], []

    def reader():
        engine = create_engine(URL, connect_args={"options": f"-csearch_path={db.schema}"}, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            while not stop.is_set():
                begun = time.perf_counter()
                try:
                    conn.exec_driver_sql("UPDATE conversations SET turn_count = turn_count WHERE id = 'c77'")
                except Exception as exc:  # pragma: no cover - failure path
                    errors.append(type(exc).__name__)
                latencies.append(time.perf_counter() - begun)
        engine.dispose()

    thread = threading.Thread(target=reader)
    try:
        thread.start()
        time.sleep(0.2)
        run_migration(db.engine, M064, "upgrade")
        stop.set()
        thread.join()
        assert not errors
        assert len(latencies) > 20
        assert max(latencies) < 0.5, max(latencies)
        assert INDEXES <= _indexes(db)
        run_migration(db.engine, M064, "downgrade")
        run_migration(db.engine, M064, "upgrade")
        assert INDEXES <= _indexes(db)
    finally:
        stop.set()
        thread.join()
        db.drop()
