"""CaptureSink + AsyncpgCaptureStore end to end on a real Postgres.

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        python -m pytest -m integration tests/integration/test_capture_sink_postgres.py

Without a reachable server every test fails (never skips).
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text

from shared import conversation_capture as cc
from tests.integration._capture_pg import Database, require_server

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _server():
    require_server()


@pytest.fixture
def db():
    database = Database()
    try:
        yield database
    finally:
        database.drop()


def rec(**over):
    base = dict(
        session_id="own-sink1", query_text="turn on the porch light", response_text="Done.",
        route="query", channel="speech", audience_class="owner", session_class="owner",
        model_used="m1", debug_detail={"cache_hit": False},
    )
    base.update(over)
    return cc.CaptureRecord(**base)


async def started(db, **kw):
    store = cc.AsyncpgCaptureStore(db.dsn, replica="sinktest")
    sink = cc.CaptureSink(store, flag_ttl=kw.pop("flag_ttl", 0.05), **kw)
    sink.start()
    for _ in range(200):
        if sink._flags is not None:
            return sink
        await asyncio.sleep(0.01)
    raise AssertionError("sink never read the flags")


async def drained(sink, turns, db):
    for _ in range(300):
        if db.scalar("SELECT count(*) FROM conversation_turns") >= turns:
            return
        await asyncio.sleep(0.02)


def test_enqueue_drain_writes_one_conversation_and_one_turn(db):
    db.set_flag(cc.ANALYTICS_KEY, True)

    async def run():
        sink = await started(db)
        assert sink.enqueue(rec()) == cc.RESULT_QUEUED
        await drained(sink, 1, db)
        await sink.stop()
        return sink.status()

    status = asyncio.run(run())
    assert (db.scalar("SELECT count(*) FROM conversations"), db.scalar("SELECT count(*) FROM conversation_turns")) == (1, 1)
    assert status["writer_state"] == "stopped" and status["dropped"] == 0 and status["last_ok_at"]


def test_read_flags_follows_a_settings_change_after_one_refresh(db):
    async def run():
        sink = await started(db, flag_ttl=0.05)
        assert sink.enqueue(rec()) == cc.RESULT_DISABLED  # 052 seeded the flag as false
        db.set_flag(cc.ANALYTICS_KEY, True)
        for _ in range(100):
            if sink._flags.analytics:
                break
            await asyncio.sleep(0.02)
        assert sink.enqueue(rec()) == cc.RESULT_QUEUED
        db.set_flag(cc.ANALYTICS_KEY, False)
        for _ in range(100):
            if not sink._flags.analytics:
                break
            await asyncio.sleep(0.02)
        assert sink.enqueue(rec()) == cc.RESULT_DISABLED
        await sink.stop()

    asyncio.run(run())


def test_report_writes_a_per_replica_status_row_only_while_analytics_is_on(db):
    async def run():
        sink = await started(db, report_interval=0.0)
        await asyncio.sleep(0.2)
        assert db.scalar("SELECT count(*) FROM system_settings WHERE category = 'conversation_capture'") == 0  # off: no row
        db.set_flag(cc.ANALYTICS_KEY, True)
        for _ in range(100):
            if db.scalar("SELECT count(*) FROM system_settings WHERE category = 'conversation_capture'"):
                break
            await asyncio.sleep(0.02)
        await sink.stop()

    asyncio.run(run())
    rows = db.rows("SELECT key, value FROM system_settings WHERE category = 'conversation_capture'")
    assert len(rows) == 1 and rows[0]["key"] == cc.writer_status_key("sinktest")
    import json
    assert set(json.loads(rows[0]["value"])) == {"last_ok_at", "last_error_at", "last_error_class", "dropped"}


def test_flag_turned_off_after_enqueue_drops_the_record_and_leaves_no_conversation_row(db):
    db.set_flag(cc.ANALYTICS_KEY, True)

    async def run():
        sink = await started(db, flag_ttl=60)  # the cache will still say "on"
        control = sink.enqueue(rec(session_id="own-control"))
        await drained(sink, 1, db)
        assert control == cc.RESULT_QUEUED
        db.set_flag(cc.ANALYTICS_KEY, False)
        assert sink.enqueue(rec(session_id="own-late", query_text="must not be stored")) == cc.RESULT_QUEUED
        await asyncio.sleep(0.3)
        await sink.stop()

    asyncio.run(run())
    assert db.scalar("SELECT count(*) FROM conversations WHERE session_id = 'own-control'") == 1  # flag-on control
    assert db.scalar("SELECT count(*) FROM conversations WHERE session_id = 'own-late'") == 0
    assert db.scalar("SELECT count(*) FROM conversation_turns WHERE query_text = 'must not be stored'") == 0


def test_debug_flipped_between_two_turns(db):
    db.set_flag(cc.ANALYTICS_KEY, True)

    async def run():
        sink = await started(db)
        sink.enqueue(rec(query_text="first"))
        await drained(sink, 1, db)
        db.set_flag(cc.DEBUG_KEY, True)
        sink.enqueue(rec(query_text="second"))
        await drained(sink, 2, db)
        await sink.stop()

    asyncio.run(run())
    first, second = db.rows("SELECT query_text, source, model_used, debug_detail FROM conversation_turns ORDER BY turn_number")
    assert (first["source"], first["model_used"], first["debug_detail"]) == ("analytics", None, None)
    assert (second["source"], second["model_used"], second["debug_detail"]) == ("debug", "m1", {"cache_hit": False})


def test_sticky_withholding_across_turns(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    marker = cc.CaptureRecord(session_id="own-sink1", query_text="", route="query", channel="speech",
                              audience_class="owner", session_class="owner", outcome="withheld", text_withheld=True)

    async def run():
        sink = await started(db)
        for r in (marker, rec(query_text="that one please"), rec(query_text="again"), rec(query_text="what time is it")):
            sink.enqueue(r)
        await drained(sink, 4, db)
        await sink.stop()

    asyncio.run(run())
    rows = db.rows("SELECT query_text, text_withheld FROM conversation_turns ORDER BY turn_number")
    assert [r["text_withheld"] for r in rows] == [True, True, True, True]
    assert all(r["query_text"] == "" for r in rows)


def test_digits_are_redacted_and_pin_shaped_queries_withheld_in_the_stored_row(db):
    db.set_flag(cc.ANALYTICS_KEY, True)

    async def run():
        sink = await started(db)
        sink.enqueue(rec(query_text="meet me in 2026", response_text="Room 4412 it is."))
        sink.enqueue(rec(session_id="own-pin", query_text="the code is one two three four five six", response_text="ok"))
        await drained(sink, 2, db)
        await sink.stop()

    asyncio.run(run())
    stored = {r["query_text"]: r for r in db.rows("SELECT query_text, response_text, text_withheld FROM conversation_turns")}
    assert stored["meet me in [number]"]["response_text"] == "Room [number] it is."
    withheld = stored[""]
    assert withheld["text_withheld"] is True and withheld["response_text"] is None
    assert "one two three" not in " ".join(str(v) for r in stored.values() for v in r.values())


def test_a_stalled_store_with_a_full_queue_counts_overflow_and_stops_within_the_drain(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    holder = db.engine.connect()
    holder.execute(text("BEGIN"))
    holder.execute(text("LOCK TABLE conversations IN ACCESS EXCLUSIVE MODE"))
    try:
        async def run():
            sink = await started(db, queue_max=4)
            outcomes = [sink.enqueue(rec(session_id=f"own-{i}")) for i in range(12)]
            await asyncio.sleep(0.2)
            begun = time.perf_counter()
            await sink.stop(drain_s=1.0)
            return outcomes, time.perf_counter() - begun, sink.status()

        outcomes, elapsed, status = asyncio.run(run())
    finally:
        holder.execute(text("ROLLBACK"))
        holder.close()
    assert outcomes.count(cc.RESULT_QUEUE_FULL) >= 7
    assert elapsed < 1.0 + 3.0
    assert status["dropped"] >= outcomes.count(cc.RESULT_QUEUE_FULL) + 4
    assert db.scalar("SELECT count(*) FROM conversation_turns") == 0


def test_stop_writes_a_final_status_row_even_when_the_report_interval_is_long(db):
    db.set_flag(cc.ANALYTICS_KEY, True)

    async def run():
        sink = await started(db, report_interval=3600)
        await asyncio.sleep(0.2)  # the first periodic report has been sent
        sink.enqueue(rec())
        await drained(sink, 1, db)
        await sink.stop()

    asyncio.run(run())
    import json

    row = db.rows("SELECT value FROM system_settings WHERE key = :k", k=cc.writer_status_key("sinktest"))[0]
    assert json.loads(row["value"])["last_ok_at"], "the final report carries the last successful write"
