"""Purge SQL and its lease on a real Postgres.

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        python -m pytest -m integration tests/integration/test_conversation_purge_postgres.py

Without a reachable server every test fails (never skips).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from app.services import conversation_capture as capture
from app.services import settings_lease
from shared import conversation_capture as cc
from tests.integration._capture_pg import Database, require_server

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _server():
    require_server()


@pytest.fixture
def db():
    database = Database()
    database.set_flag(cc.RETENTION_KEY, False)  # placeholder row; value replaced below
    with database.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE system_settings SET value = '30' WHERE key = 'conversation_retention_days'")
    try:
        yield database
    finally:
        database.drop()


@pytest.fixture
def factory(db):
    return sessionmaker(bind=db.engine, autoflush=False)


def add_turn(db, turn_id, conv_id, age_sql, *, number=1, conv_age_sql=None):
    """A turn created ``age_sql`` ago (a SQL interval expression) in conversation ``conv_id``."""
    conv_age_sql = conv_age_sql or age_sql
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            f"INSERT INTO conversations (id, session_id, turn_count, started_at, ended_at, session_class) "
            f"VALUES ('{conv_id}', 'own-{conv_id}', 1, NOW() - ({conv_age_sql}), NOW() - ({conv_age_sql}), 'owner') "
            f"ON CONFLICT (id) DO NOTHING")
        conn.exec_driver_sql(
            f"INSERT INTO conversation_turns (id, conversation_id, turn_number, query_text, source, created_at) "
            f"VALUES ('{turn_id}', '{conv_id}', {number}, 'q', 'analytics', NOW() - ({age_sql}))")


def turn_ids(db):
    return {r["id"] for r in db.rows("SELECT id FROM conversation_turns")}


def conv_ids(db):
    return {r["id"] for r in db.rows("SELECT id FROM conversations")}


def test_expired_removes_only_old_turns_and_the_now_empty_conversations(db, factory):
    add_turn(db, "t-old", "c-old", "interval '31 days'")
    add_turn(db, "t-mixed-old", "c-mixed", "interval '40 days'", conv_age_sql="interval '1 day'")
    add_turn(db, "t-mixed-new", "c-mixed", "interval '1 day'", number=2)
    add_turn(db, "t-new", "c-new", "interval '1 hour'")
    result = capture.purge(factory, "expired")
    assert (result.turns, result.conversations, result.aborted) == (2, 1, None)
    assert turn_ids(db) == {"t-mixed-new", "t-new"}
    assert conv_ids(db) == {"c-mixed", "c-new"}
    assert db.scalar("SELECT count(*) FROM system_settings WHERE key = :k", k=cc.PURGE_LEASE_KEY) == 0  # released


def test_the_cutoff_is_exact_on_the_database_clock(db, factory):
    add_turn(db, "t-just-expired", "c1", "make_interval(days => 30) + interval '1 second'")
    add_turn(db, "t-just-kept", "c2", "make_interval(days => 30) - interval '2 seconds'")
    capture.purge(factory, "expired")
    assert turn_ids(db) == {"t-just-kept"}


def test_preview_counts_what_a_purge_would_delete(db, factory):
    add_turn(db, "t-a", "c1", "make_interval(days => 30) + interval '1 second'")
    add_turn(db, "t-b", "c2", "make_interval(days => 30) - interval '2 seconds'")
    add_turn(db, "t-c", "c3", "interval '5 days'")
    with factory() as session:
        assert capture.preview_delete_turns(session, 30) == 1
        assert capture.preview_delete_turns(session, 6) == 2
        assert capture.preview_delete_turns(session, 4) == 3
        assert capture.preview_delete_turns(session, 3650) == 0


def test_scope_all_removes_everything_and_cascades_evaluations(db, factory):
    add_turn(db, "t1", "c1", "interval '1 hour'")
    add_turn(db, "t2", "c2", "interval '40 days'")
    with db.engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO turn_evaluations (id, turn_id, rating) VALUES ('e1', 't1', 4), ('e2', 't2', 2)")
    result = capture.purge(factory, "all")
    assert (result.turns, result.conversations) == (2, 2)
    assert db.scalar("SELECT count(*) FROM turn_evaluations") == 0
    assert turn_ids(db) == set() and conv_ids(db) == set()


def test_stale_writer_status_rows_go_and_fresh_ones_stay(db, factory):
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO system_settings (key, value, category, updated_at) VALUES "
            "('conversation_capture.writer_status.old', '{}', 'conversation_capture', NOW() - interval '25 hours'),"
            "('conversation_capture.writer_status.new', '{}', 'conversation_capture', NOW() - interval '1 hour')")
    capture.purge(factory, "all")
    keys = {r["key"] for r in db.rows("SELECT key FROM system_settings WHERE key LIKE 'conversation_capture.writer_status.%'")}
    assert keys == {"conversation_capture.writer_status.new"}


def test_batches_renew_the_lease_each_time(db, factory):
    for i in range(7):
        add_turn(db, f"t{i}", f"c{i}", "interval '40 days'")
    renewals = []

    def renew(session_factory, lease, *, ttl):
        renewals.append(ttl)
        return settings_lease.renew(session_factory, lease, ttl=ttl)

    result = capture.purge(factory, "expired", batch_size=2, renew=renew)
    assert (result.turns, result.conversations) == (7, 7)
    assert len(renewals) >= 4 + 4  # one renewal before each of the 4 + 4 delete batches


def test_a_lost_lease_stops_before_the_next_delete_and_leaves_later_batches_untouched(db, factory):
    for i in range(7):
        add_turn(db, f"t{i}", f"c{i}", "interval '40 days'")
    allowed = {"n": 1}

    def renew(*_a, **_k):
        allowed["n"] -= 1
        return allowed["n"] >= 0

    progress = capture.PurgeResult()
    result = capture.purge(factory, "expired", batch_size=2, renew=renew, progress=progress)
    assert result is progress
    assert result.aborted == "lease_lost" and result.turns == 2 and result.conversations == 0
    assert len(turn_ids(db)) == 5 and len(conv_ids(db)) == 7


def test_losing_the_lease_before_the_first_batch_deletes_nothing(db, factory):
    add_turn(db, "t1", "c1", "interval '40 days'")
    result = capture.purge(factory, "expired", renew=lambda *a, **k: False)
    assert result.aborted == "lease_lost" and result.turns == 0
    assert turn_ids(db) == {"t1"}


def test_the_lease_really_is_exclusive_and_busy_is_reported(db, factory):
    lease = settings_lease.acquire(factory, cc.PURGE_LEASE_KEY, category="conversation_capture", ttl=60, busy_message="x")
    add_turn(db, "t1", "c1", "interval '40 days'")
    with pytest.raises(settings_lease.LeaseBusy):
        capture.purge(factory, "expired")
    assert turn_ids(db) == {"t1"}
    settings_lease.release(factory, lease)
    assert capture.purge(factory, "expired").turns == 1


@pytest.mark.parametrize("value", ["abc", "0", "3651", "", "30d", " 30"])
def test_invalid_stored_retention_purges_nothing(db, factory, value):
    add_turn(db, "t1", "c1", "interval '4000 days'")
    with db.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE system_settings SET value = %s WHERE key = 'conversation_retention_days'", (value,))
    with pytest.raises(capture.RetentionInvalid):
        capture.purge(factory, "expired")
    assert capture.run_scheduled_purge(factory) is None
    assert turn_ids(db) == {"t1"}
    assert db.scalar("SELECT count(*) FROM system_settings WHERE key = :k", k=cc.PURGE_LEASE_KEY) == 0


def test_a_missing_retention_row_purges_nothing(db, factory):
    add_turn(db, "t1", "c1", "interval '4000 days'")
    with db.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM system_settings WHERE key = 'conversation_retention_days'")
    with pytest.raises(capture.RetentionInvalid):
        capture.purge(factory, "expired")
    assert turn_ids(db) == {"t1"}


def test_a_turn_a_writer_adds_during_the_sweep_survives(db, factory):
    """A writer extends a conversation the sweep is about to delete.

    The writer's statement is open (uncommitted) while the sweep runs; the sweep
    blocks on the conversation row, and when the writer commits the re-check
    sees the fresh ended_at and leaves the conversation, and the new turn, alone.
    """
    import asyncpg

    db.set_flag(cc.ANALYTICS_KEY, True)
    add_turn(db, "t-old", "c-race", "interval '40 days'")
    record = cc.sanitize_record(cc.CaptureRecord(
        session_id="own-c-race", query_text="a brand new question", response_text="an answer", route="query",
        channel="speech", audience_class="owner", session_class="owner"))

    async def run():
        conn = await asyncpg.connect(db.dsn)
        tr = conn.transaction()
        await tr.start()
        await conn.fetchrow(cc.WRITE_TURN_SQL, *cc.write_turn_params(record, conversation_id="c-new", turn_id="t-new"))
        sweep = asyncio.create_task(asyncio.to_thread(capture.purge, factory, "expired"))
        await asyncio.sleep(1.5)
        assert not sweep.done(), "the sweep should be waiting on the writer's row lock"
        await tr.commit()
        result = await sweep
        await conn.close()
        return result

    result = asyncio.run(run())
    assert result.turns == 1 and result.conversations == 0
    assert turn_ids(db) == {"t-new"}
    assert conv_ids(db) == {"c-race"}
    assert db.scalar("SELECT turn_count FROM conversations WHERE id = 'c-race'") == 2
