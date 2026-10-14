"""The writer statement on a real Postgres: flag gating, debug gating, the
partition guard, legacy-row adoption and whole-turn withholding.

    POSTGRES_TEST_URL=postgresql://postgres:t@localhost:5432/postgres \\
        python -m pytest -m integration tests/integration/test_conversation_capture_postgres.py

Without a reachable server every test fails (never skips).
"""
from __future__ import annotations

import asyncio

import pytest

from shared import conversation_capture as cc
from tests.integration._capture_pg import Database, require_server

pytestmark = pytest.mark.integration

DEBUG_COLUMNS = (
    "tokens_generated", "tokens_per_second", "validation_passed",
    "error_message", "complexity", "base_knowledge_populated", "debug_detail",
)
STANDARD_COLUMNS = ("model_used", "rag_tools_used", "node_timings_ms")


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


def record(**over):
    base = dict(
        session_id="own-s1", query_text="turn on the porch light", response_text="Porch light is on.",
        route="query", channel="speech", audience_class="owner", session_class="owner",
        request_id="req-1", room="porch", user_mode="owner", interface_type="voice",
        intent="control", confidence=0.91, is_fallback=False, response_time_ms=420,
        model_used="m1", rag_tools_used=["weather"], tokens_generated=12, tokens_per_second=30.5,
        validation_passed=True, error_message="oops", complexity="simple", base_knowledge_populated=True,
        node_timings_ms={"classify": 12.5}, debug_detail={"cache_hit": True, "prompt_tokens": 99},
    )
    base.update(over)
    return cc.sanitize_record(cc.CaptureRecord(**base))


def write(db, *records):
    async def run():
        store = cc.AsyncpgCaptureStore(db.dsn, replica="t")
        try:
            return [await store.write(r) for r in records]
        finally:
            await store.close()

    return asyncio.run(run())


def counts(db):
    return db.scalar("SELECT count(*) FROM conversations"), db.scalar("SELECT count(*) FROM conversation_turns")


def test_flag_off_writes_nothing_and_flag_on_control_writes(db):
    assert write(db, record()) == [cc.RESULT_DISABLED]
    assert counts(db) == (0, 0)  # not even conversation metadata

    db.set_flag(cc.ANALYTICS_KEY, True)
    assert write(db, record()) == [cc.RESULT_WRITTEN]
    assert counts(db) == (1, 1)


def test_missing_flag_row_counts_as_off(db):
    assert db.scalar("SELECT count(*) FROM system_settings WHERE key = :k", k=cc.ANALYTICS_KEY) == 1  # seeded by 052
    with db.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM system_settings WHERE key = 'analytics_mode_enabled'")
    assert write(db, record()) == [cc.RESULT_DISABLED]
    assert counts(db) == (0, 0)


def test_analytics_on_debug_off_stores_base_fields_model_tools_timings_and_nulls_the_debug_block(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    write(db, record())
    row = db.rows("SELECT * FROM conversation_turns")[0]
    assert (row["query_text"], row["response_text"]) == ("turn on the porch light", "Porch light is on.")
    assert (row["route"], row["channel"], row["audience_class"], row["outcome"]) == ("query", "speech", "owner", "answered")
    assert (row["intent"], float(row["confidence"]), row["is_fallback"], row["response_time_ms"]) == ("control", 0.91, False, 420)
    assert row["source"] == "analytics" and row["text_withheld"] is False and row["turn_number"] == 1
    assert all(row[c] is None for c in DEBUG_COLUMNS), {c: row[c] for c in DEBUG_COLUMNS}
    assert (row["model_used"], row["rag_tools_used"], row["node_timings_ms"]) == ("m1", ["weather"], {"classify": 12.5})
    conv = db.rows("SELECT * FROM conversations")[0]
    assert (conv["session_class"], conv["user_mode"], conv["room"], conv["total_tokens"], conv["turn_count"]) == ("owner", "owner", "porch", None, 1)


def test_debug_on_fills_the_debug_block(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    db.set_flag(cc.DEBUG_KEY, True)
    write(db, record())
    row = db.rows("SELECT * FROM conversation_turns")[0]
    assert row["source"] == "debug"
    assert (row["model_used"], row["rag_tools_used"], row["tokens_generated"], row["validation_passed"]) == ("m1", ["weather"], 12, True)
    assert (float(row["tokens_per_second"]), row["complexity"], row["base_knowledge_populated"]) == (30.5, "simple", True)
    assert row["node_timings_ms"] == {"classify": 12.5}
    assert row["debug_detail"] == {"cache_hit": True, "prompt_tokens": 99}
    assert row["error_message"] == "oops"
    assert db.scalar("SELECT total_tokens FROM conversations") == 12


def test_off_list_tool_names_and_timing_keys_never_reach_the_row(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    write(db, record(rag_tools_used=["weather", "tell me the pin 1234"], node_timings_ms={"classify": 1.0, "my secret": 2.0},
                     model_used="has spaces and text"))
    row = db.rows("SELECT * FROM conversation_turns")[0]
    assert (row["rag_tools_used"], row["node_timings_ms"], row["model_used"]) == (["weather"], {"classify": 1.0}, None)


def test_debug_flag_alone_never_writes(db):
    db.set_flag(cc.DEBUG_KEY, True)
    assert write(db, record()) == [cc.RESULT_DISABLED]
    assert counts(db) == (0, 0)


def test_second_turn_extends_the_same_conversation(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    write(db, record(), record(query_text="and the lamp"))
    assert counts(db) == (1, 2)
    assert [r["turn_number"] for r in db.rows("SELECT turn_number FROM conversation_turns ORDER BY turn_number")] == [1, 2]
    assert db.scalar("SELECT turn_count FROM conversations") == 2


def test_partition_mismatch_writes_no_turn_and_leaves_the_conversation_alone(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    write(db, record())
    result = write(db, record(session_class="guest", audience_class="guest", query_text="intruder"))
    assert result == [cc.RESULT_PARTITION_MISMATCH]
    assert counts(db) == (1, 1)
    assert db.scalar("SELECT turn_count FROM conversations") == 1
    assert db.scalar("SELECT session_class FROM conversations") == "owner"


def test_a_null_class_legacy_row_is_adopted_and_stamped(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    with db.engine.begin() as conn:  # what an old orchestrator's INSERT creates after 064
        conn.exec_driver_sql(
            "INSERT INTO conversations (id, session_id, turn_count) VALUES ('legacy-1', 'sms_abc', 3)")
    assert db.scalar("SELECT session_class FROM conversations") is None
    assert write(db, record(session_id="sms_abc", session_class="other", audience_class="other")) == [cc.RESULT_WRITTEN]
    assert db.scalar("SELECT session_class FROM conversations") == "other"
    assert db.rows("SELECT turn_number FROM conversation_turns")[0]["turn_number"] == 4


def test_old_shape_insert_still_works_after_064(db):
    with db.engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO conversations (id, session_id, turn_count) VALUES ('c1', 's1', 1)")
        conn.exec_driver_sql(
            "INSERT INTO conversation_turns (id, conversation_id, turn_number, query_text, source) "
            "VALUES ('t1', 'c1', 1, 'q', 'analytics')")
    row = db.rows("SELECT route, channel, audience_class, outcome, text_withheld, debug_detail FROM conversation_turns")[0]
    assert dict(row) == {"route": None, "channel": None, "audience_class": None, "outcome": None,
                         "text_withheld": False, "debug_detail": None}


def test_withheld_record_stores_no_text_and_no_debug_even_in_debug_mode(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    db.set_flag(cc.DEBUG_KEY, True)
    marker = record(query_text="", response_text=None, outcome="withheld", text_withheld=True,
                    intent=None, confidence=None)
    write(db, marker)
    row = db.rows("SELECT * FROM conversation_turns")[0]
    assert (row["query_text"], row["response_text"], row["outcome"], row["text_withheld"]) == ("", None, "withheld", True)
    assert row["intent"] is None and row["confidence"] is None
    assert all(row[c] is None for c in DEBUG_COLUMNS + STANDARD_COLUMNS)
    assert db.scalar("SELECT turn_count FROM conversations") == 1  # a marker counts as a turn


def test_sticky_withholding_chains_within_the_window(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    marker = record(query_text="", response_text=None, outcome="withheld", text_withheld=True, intent=None, confidence=None)
    write(db, marker, record(query_text="yes that one"), record(query_text="and again"), record(query_text="what is the time"))
    rows = db.rows("SELECT turn_number, query_text, response_text, text_withheld, outcome FROM conversation_turns ORDER BY turn_number")
    assert [r["text_withheld"] for r in rows] == [True, True, True, True]
    assert [r["query_text"] for r in rows] == ["", "", "", ""]
    assert all(r["response_text"] is None and r["outcome"] == "withheld" for r in rows)
    # a different session is unaffected
    write(db, record(session_id="own-s2", query_text="what is the time"))
    assert db.scalar("SELECT query_text FROM conversation_turns t JOIN conversations c ON c.id = t.conversation_id WHERE c.session_id = 'own-s2'") == "what is the time"


def test_sticky_window_expires_after_five_minutes(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    marker = record(query_text="", response_text=None, outcome="withheld", text_withheld=True, intent=None, confidence=None)
    write(db, marker)
    with db.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE conversation_turns SET created_at = NOW() - interval '6 minutes'")
    write(db, record(query_text="what is the time"))
    assert db.rows("SELECT text_withheld FROM conversation_turns ORDER BY turn_number")[1]["text_withheld"] is False


def test_digits_are_redacted_in_the_stored_row(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    db.set_flag(cc.DEBUG_KEY, True)
    write(db, record(query_text="book room 2026", response_text="Booked 4412 for you.",
                     error_message="code 55123 failed", debug_detail={"validation_reason": "id 90210 seen"}))
    row = db.rows("SELECT * FROM conversation_turns")[0]
    joined = " ".join(str(row[c]) for c in ("query_text", "response_text", "error_message", "debug_detail"))
    for digits in ("2026", "4412", "55123", "90210"):
        assert digits not in joined
    assert row["query_text"] == "book room [number]"


def test_overlong_labels_do_not_turn_a_turn_into_a_dropped_write(db):
    db.set_flag(cc.ANALYTICS_KEY, True)
    long = "x" * 400
    assert write(db, record(room=long, request_id=long, intent=long, model_used=long, session_id="own-" + long)) == [cc.RESULT_WRITTEN]
    assert db.scalar("SELECT length(room) FROM conversations") == 100
