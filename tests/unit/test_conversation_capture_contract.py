"""shared.conversation_capture: the contract the orchestrator writer and admin-backend share.

Stdlib-only behaviour: the DSN builder, retention parsing, digit redaction and
PIN withholding, the record sanitiser, the SQL constants' shape, and the sink's
queue behaviour against a fake store. The statements themselves run against a
real Postgres in admin/backend/tests/integration/.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import re
import time
from pathlib import Path

import pytest

from shared import conversation_capture as cc

REPO = Path(__file__).resolve().parents[2]
SECRET = "hunter2-do-not-leak"


# ---------------------------------------------------------------------------
# DSN
# ---------------------------------------------------------------------------

def _old_inline_splice(database_url: str, db_password: str) -> str:
    """The code in admin/backend/app/database.py before resolve_database_dsn."""
    if db_password and "@" in database_url:
        parts = database_url.split("@")
        user_part = parts[0].split("//")[1]
        return f"postgresql://{user_part}:{db_password}@{'@'.join(parts[1:])}"
    return database_url


LEGACY = {
    "ATHENA_DB_HOST": "legacy.db.example",
    "ATHENA_DB_NAME": "athena",
    "ATHENA_DB_USER": "svc",
    "ATHENA_DB_PASSWORD": "p@ss word",
}


def test_database_url_only():
    got = cc.resolve_database_dsn("postgresql://u:pw@db.example:5432/app", {})
    assert (got.dsn, got.source, got.legacy_disagrees) == ("postgresql://u:pw@db.example:5432/app", "database_url", False)
    assert got.configured


def test_legacy_vars_only_are_a_fallback_and_quoted():
    got = cc.resolve_database_dsn("", LEGACY)
    assert got.source == "athena_db"
    assert got.sqlalchemy_url == got.dsn
    assert got.dsn == "postgresql://svc:p%40ss%20word@legacy.db.example:5432/athena"
    assert not got.legacy_disagrees


def test_legacy_vars_incomplete_means_not_configured():
    got = cc.resolve_database_dsn("", {"ATHENA_DB_HOST": "h", "ATHENA_DB_NAME": "n"})
    assert (got.dsn, got.source) == (None, "none")
    assert not got.configured


def test_both_equal_agree():
    env = {**LEGACY, "ATHENA_DB_HOST": "db.example", "ATHENA_DB_NAME": "app"}
    got = cc.resolve_database_dsn("postgresql://u:pw@DB.example/app", env)
    assert got.source == "database_url" and not got.legacy_disagrees


@pytest.mark.parametrize("field,value", [("ATHENA_DB_HOST", "other.example"), ("ATHENA_DB_NAME", "other"), ("ATHENA_DB_PORT", "6432")])
def test_both_set_and_different_flags_disagreement_and_database_url_wins(field, value):
    env = {**LEGACY, "ATHENA_DB_HOST": "db.example", "ATHENA_DB_NAME": "app", field: value}
    got = cc.resolve_database_dsn("postgresql://u:pw@db.example:5432/app", env)
    assert got.source == "database_url"
    assert got.dsn == "postgresql://u:pw@db.example:5432/app"
    assert got.legacy_disagrees


@pytest.mark.parametrize("url", ["sqlite:///:memory:", "sqlite:////data/x.db", "mysql://u@h/d", "redis://h/0"])
def test_non_postgres_url_is_unsupported_even_with_legacy_vars(url):
    got = cc.resolve_database_dsn(url, LEGACY)
    assert (got.dsn, got.source) == (None, "unsupported")


@pytest.mark.parametrize("scheme", ["postgres", "postgresql", "postgresql+asyncpg", "postgresql+psycopg2", "postgresql+psycopg"])
def test_scheme_is_normalised_and_the_rest_preserved(scheme):
    got = cc.resolve_database_dsn(f"{scheme}://u:pw@h.example:5433/db?sslmode=require", {})
    assert got.dsn == "postgresql://u:pw@h.example:5433/db?sslmode=require"


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user@host:5432/db",
        "postgresql://user:old@host/db",
        "postgres://user@host/db?sslmode=require",
        "postgresql+psycopg2://user@host/db",
        "postgresql://user@ho@st/db",
        "postgresql://host/db",
    ],
)
def test_db_password_splice_matches_the_inline_code_for_plain_passwords(url):
    expected = _old_inline_splice(url, "s3cret")
    got = cc.resolve_database_dsn(url, {"DB_PASSWORD": "s3cret"})
    if "@" in url:
        assert got.dsn == expected  # asyncpg DSN: byte-identical to the old output
        # the SQLAlchemy URL differs only by keeping its driver suffix
        assert got.sqlalchemy_url == expected.replace("postgresql://", url.split("://")[0].replace("postgres", "postgresql", 1).replace("postgresqlql", "postgresql") + "://", 1)
    else:
        assert got.dsn == "postgresql://host/db"


def test_db_password_with_special_characters_is_url_quoted():
    got = cc.resolve_database_dsn("postgresql://user@host/db", {"DB_PASSWORD": "p@ss/w:rd?#"})
    assert got.dsn == "postgresql://user:p%40ss%2Fw%3Ard%3F%23@host/db"
    from urllib.parse import urlsplit, unquote
    assert unquote(urlsplit(got.dsn).password) == "p@ss/w:rd?#"


@pytest.mark.parametrize("scheme", ["postgresql+psycopg", "postgresql+psycopg2", "postgresql+asyncpg"])
def test_sqlalchemy_url_keeps_the_driver_suffix_and_the_asyncpg_dsn_drops_it(scheme):
    got = cc.resolve_database_dsn(f"{scheme}://u:pw@h/db", {})
    assert got.sqlalchemy_url == f"{scheme}://u:pw@h/db"
    assert got.dsn == "postgresql://u:pw@h/db"
    spliced = cc.resolve_database_dsn(f"{scheme}://u@h/db", {"DB_PASSWORD": "pw"})
    assert spliced.sqlalchemy_url == f"{scheme}://u:pw@h/db" and spliced.dsn == "postgresql://u:pw@h/db"


def test_bare_postgres_scheme_becomes_postgresql_for_sqlalchemy_too():
    got = cc.resolve_database_dsn("postgres://u:pw@h/db", {})
    assert got.sqlalchemy_url == got.dsn == "postgresql://u:pw@h/db"


def test_capture_dsn_cannot_be_pickled_or_copied():
    import copy
    import pickle

    got = cc.resolve_database_dsn(f"postgresql://u:{SECRET}@h/db", {})
    for attempt in (pickle.dumps, copy.copy, copy.deepcopy):
        with pytest.raises(TypeError):
            attempt(got)


def test_no_db_password_leaves_the_url_unchanged():
    url = "postgresql://user:pw@host/db"
    assert cc.resolve_database_dsn(url, {}).dsn == url


def test_the_dsn_never_appears_in_repr_or_str():
    got = cc.resolve_database_dsn(f"postgresql+psycopg://u:{SECRET}@h/db", {})
    assert SECRET not in repr(got) and SECRET not in str(got)
    store = cc.AsyncpgCaptureStore(got.dsn, replica="r1")
    assert SECRET not in repr(store)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [("365", 365), ("1", 1), ("3650", 3650), ("30", 30), ("0030", 30)])
def test_stored_retention_accepts_strict_integers(value, expected):
    assert cc.parse_stored_retention(value) == expected


@pytest.mark.parametrize("value", ["0", "3651", "", "30d", " 30", "30 ", "1e2", "-5", "abc", "30\n", "٣٠", "12345", None, 30])
def test_stored_retention_rejects_everything_else(value):
    assert cc.parse_stored_retention(value) is None


def test_default_and_bounds():
    assert (cc.RETENTION_DEFAULT_DAYS, cc.RETENTION_MIN_DAYS, cc.RETENTION_MAX_DAYS) == (30, 1, 3650)


def test_status_keys_are_per_replica_and_hide_the_hostname():
    rid = cc.replica_id("orchestrator-7d9f-abc")
    assert cc.writer_status_key(rid).startswith(cc.WRITER_STATUS_KEY + ".")
    assert "orchestrator" not in rid and len(rid) == 12


# ---------------------------------------------------------------------------
# Redaction and withholding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("set the lights to 40 percent", "set the lights to 40 percent"),
        ("what's the weather in 2026", "what's the weather in [number]"),
        ("call 555-123-4567 now", "call [number] now"),
        ("it's 1 2 3 4", "it's [number]"),
        ("1,2,3,4,5,6", "[number]"),
        ("room 12 and 345", "room 12 and 345"),
        ("pin 1 _ 2 __ 3 -  4", "pin [number]"),
        ("code 12 , 34", "code [number]"),
        ("id 1_2_3_4", "id [number]"),
    ],
)
def test_redact_digits(text, expected):
    assert cc.redact_digits(text) == expected


def test_redact_digits_passes_through_empty():
    assert cc.redact_digits("") == "" and cc.redact_digits(None) is None


WITHHELD_CORPUS = [
    "one two three four five six",
    "123-456",
    "12 34 56",
    "1,2,3,4,5,6",
    "pin is 4 4 4 4 4 4",
    "my code is oh one two three",
    "1 2 3 four five six",
    "one two three, four five six",
    "12 three four 56",
    "pin one two three and four five six",
    "four five six seven eight nine",
    "my passcode is 8 4",
    "the password 7",
    "it's 123456",
    "one 2 three 4",
    "1 two 3 four",
    "pin 1 _ 2 _ 3 four five",
    "one, 2, three, 4",
    "two to six seven",
    "one two three for",
    "ate ate ate ate",
    "won two too four",
]


@pytest.mark.parametrize("query", WITHHELD_CORPUS)
def test_pin_shaped_queries_are_withheld(query):
    assert cc.withhold_reason(query) is not None


@pytest.mark.parametrize("query", ["set the lights to 40 percent", "what's the weather in 2026", "turn on the kitchen light", "play track one", "play track 4 and 5", "1234 main street", "weather for 2026", "set it to 72", "remind me at 5 to 6", "to 1 2 3", "5 for 6 to 7", "for 2026 and to 2027", "", "what time is it"])
def test_ordinary_queries_are_not_withheld(query):
    assert cc.withhold_reason(query) is None


def test_owner_command_predicate_is_injected_and_fails_closed():
    assert cc.withhold_reason("switch to owner mode", lambda q: "owner mode" in q) == cc.WITHHOLD_OWNER_MODE

    def boom(_q):
        raise RuntimeError("x")

    assert cc.withhold_reason("hello", boom) == cc.WITHHOLD_OWNER_MODE


def test_spoken_digit_table_matches_mode_permission():
    tree = ast.parse((REPO / "src/orchestrator/mode_permission.py").read_text())
    table = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "word_to_digit" for t in node.targets):
            table = ast.literal_eval(node.value)
    assert table is not None, "word_to_digit not found in extract_pin_from_query"
    assert table == cc.SPOKEN_DIGIT_WORDS


# ---------------------------------------------------------------------------
# Sanitiser
# ---------------------------------------------------------------------------

def _record(**over):
    base = dict(
        session_id="own-1", query_text="turn on the porch light", response_text="Done.",
        route="query", channel="speech", audience_class="owner", session_class="owner",
    )
    base.update(over)
    return cc.CaptureRecord(**base)


def test_sanitise_redacts_every_text_field_and_caps_them():
    rec = cc.sanitize_record(_record(
        query_text="room 20261 please" + "x" * (cc.QUERY_MAX_CHARS + 10),
        response_text="code 98765 " + "y" * (cc.ANSWER_MAX_CHARS + 10),
        error_message="Boom 123456789 " + "e" * 500,
        debug_detail={"validation_reason": "saw 55555 here", "cache_hit": True},
    ))
    assert "20261" not in rec.query_text and "[number]" in rec.query_text
    assert len(rec.query_text) <= cc.QUERY_MAX_CHARS and len(rec.response_text) <= cc.ANSWER_MAX_CHARS
    assert "98765" not in rec.response_text
    assert "123456789" not in rec.error_message and len(rec.error_message) <= cc.ERROR_MAX_CHARS
    assert rec.debug_detail["validation_reason"] == "saw [number] here"
    assert rec.debug_detail["cache_hit"] is True and rec.debug_detail["answer_truncated"] is True


def test_sanitise_withholds_whole_row_and_drops_debug_text():
    rec = cc.sanitize_record(_record(
        query_text="my pin is one two three four five six", response_text="ok",
        error_message="x", debug_detail={"cache_hit": True}, rag_tools_used=["a"], node_timings_ms={"n": 1.0},
    ))
    assert (rec.query_text, rec.response_text, rec.outcome, rec.text_withheld) == ("", None, "withheld", True)
    assert rec.error_message is None and rec.debug_detail is None
    assert rec.rag_tools_used is None and rec.node_timings_ms is None


def test_sanitise_marker_stays_a_marker_and_is_idempotent():
    marker = cc.CaptureRecord(
        session_id="own-1", query_text="", route="query", channel="speech",
        audience_class="owner", session_class="owner", outcome="withheld", text_withheld=True,
    )
    once = cc.sanitize_record(marker)
    assert once == cc.sanitize_record(once)
    assert once.text_withheld and once.query_text == "" and once.response_text is None


def test_sanitise_is_idempotent_on_ordinary_rows():
    once = cc.sanitize_record(_record(debug_detail={"cache_hit": False}))
    assert cc.sanitize_record(once) == once


def test_debug_detail_is_a_closed_key_set_of_scalars():
    rec = cc.sanitize_record(_record(debug_detail={
        "cache_hit": True, "unknown_key": "q", "caller_trust_tier": "sms", "prompt_tokens": 12,
        "validation_reason": {"nested": "no"}, "degraded": [1],
    }))
    assert set(rec.debug_detail) <= cc.DEBUG_DETAIL_KEYS
    assert rec.debug_detail == {"cache_hit": True, "caller_trust_tier": "sms", "prompt_tokens": 12}


def test_labels_are_clamped_to_their_column_widths_and_nul_stripped():
    long = "z" * 500
    rec = cc.sanitize_record(_record(
        session_id=long, room=long, request_id=long, intent=long, model_used=long,
        interface_type=long, user_mode=long, route=long, query_text="a\x00b", response_text="c\x00d",
    ))
    for name, width in cc.FIELD_WIDTHS.items():
        value = getattr(rec, name)
        assert value is None or len(value) <= width, name
    assert (rec.query_text, rec.response_text) == ("ab", "cd")


def test_json_blocks_are_bounded():
    rec = cc.sanitize_record(_record(
        rag_tools_used=[f"tool{i}" for i in range(100)] + [5],
        node_timings_ms={f"n{i}": float(i) for i in range(100)} | {"bad": "x", "flag": True},
    ))
    assert len(rec.rag_tools_used) == 20
    assert len(rec.node_timings_ms) <= 40 and "bad" not in rec.node_timings_ms and "flag" not in rec.node_timings_ms


# ---------------------------------------------------------------------------
# SQL shape
# ---------------------------------------------------------------------------

SQL_CONSTANTS = ("FLAGS_READ_SQL", "WRITER_STATUS_UPSERT_SQL", "WRITE_TURN_SQL")


def test_sql_constants_are_plain_literals_never_formatted():
    tree = ast.parse((REPO / "src/shared/conversation_capture.py").read_text())
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "").endswith("_SQL"):
            found[node.targets[0].id] = node.value
    assert set(SQL_CONSTANTS) <= set(found)
    for name, value in found.items():
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), name
    source = (REPO / "src/shared/conversation_capture.py").read_text()
    assert "interval '" not in source and "make_interval(mins => 5)" in source


def test_write_params_cover_exactly_the_statement_placeholders():
    used = {int(n) for n in re.findall(r"\$(\d+)", cc.WRITE_TURN_SQL)}
    assert used == set(range(1, max(used) + 1))
    params = cc.write_turn_params(_record(), conversation_id="c", turn_id="t")
    assert len(params) == max(used)
    assert params[0] == cc.ANALYTICS_KEY and params[1] == cc.DEBUG_KEY


def test_the_statement_gates_on_the_flag_and_names_the_capture_level():
    sql = cc.WRITE_TURN_SQL
    assert "WHERE f.analytics" in sql and "CASE WHEN f.debug THEN 'debug' ELSE 'analytics' END" in sql
    assert "IS NOT DISTINCT FROM EXCLUDED.session_class" in sql and "session_class IS NULL" in sql


# ---------------------------------------------------------------------------
# Sink (fake store; the real one runs in the Postgres tier)
# ---------------------------------------------------------------------------

class FakeStore:
    def __init__(self, *, analytics=True, block=None, fail=None):
        self.flags = cc.CaptureFlags(analytics=analytics)
        self.written: list = []
        self.reports: list = []
        self.block = block
        self.fail = fail
        self.closed = False

    async def read_flags(self):
        return self.flags

    async def write(self, record):
        if self.block is not None:
            await self.block.wait()
        if self.fail is not None:
            raise self.fail
        self.written.append(record)
        return cc.RESULT_WRITTEN

    async def report(self, status):
        self.reports.append(dict(status))

    async def close(self):
        self.closed = True


async def _ready(sink):
    sink.start()
    for _ in range(100):
        if sink._flags is not None:
            return
        await asyncio.sleep(0.005)
    raise AssertionError("flags never loaded")


def test_enqueue_is_synchronous_and_cheap():
    assert not asyncio.iscoroutinefunction(cc.CaptureSink.enqueue)

    async def run():
        sink = cc.CaptureSink(FakeStore())
        await _ready(sink)
        rec = _record()
        start = time.perf_counter()
        for _ in range(500):
            sink.enqueue(rec)
        per_call_ms = (time.perf_counter() - start) * 1000 / 500
        await sink.stop(drain_s=0.2)
        return per_call_ms

    assert asyncio.run(run()) < 1.0


def test_unknown_or_off_flags_drop_without_queueing():
    async def run():
        results = []
        store = FakeStore(analytics=False)
        sink = cc.CaptureSink(store, on_result=results.append)
        assert sink.enqueue(_record()) == cc.RESULT_UNCONFIGURED  # not started
        await _ready(sink)
        assert sink.enqueue(_record(query_text="secret words")) == cc.RESULT_DISABLED
        assert sink._queue.qsize() == 0
        await sink.stop(drain_s=0.2)
        assert store.written == []
        assert results == [cc.RESULT_UNCONFIGURED, cc.RESULT_DISABLED]

    asyncio.run(run())


def test_unconfigured_sink_drops_everything():
    sink = cc.CaptureSink(None)
    sink.start()
    assert sink.enqueue(_record()) == cc.RESULT_UNCONFIGURED
    assert sink.status() == {"configured": False, "writer_state": "unconfigured", "dropped": 0, "last_ok_at": None}


def test_writes_are_sanitised_before_the_store_sees_them():
    async def run():
        store = FakeStore()
        sink = cc.CaptureSink(store)
        await _ready(sink)
        assert sink.enqueue(_record(query_text="my pin is 1 2 3 4 5 6", response_text="sure")) == cc.RESULT_QUEUED
        assert sink.enqueue(_record(query_text="meet at 2026", response_text="ok")) == cc.RESULT_QUEUED
        await sink.stop(drain_s=1)
        return store.written

    first, second = asyncio.run(run())
    assert first.text_withheld and first.query_text == "" and first.response_text is None
    assert second.query_text == "meet at [number]"


def test_queue_full_drops_the_newest_and_counts_it():
    async def run():
        gate = asyncio.Event()
        results = []
        store = FakeStore(block=gate)
        sink = cc.CaptureSink(store, queue_max=2, on_result=results.append)
        await _ready(sink)
        outcomes = [sink.enqueue(_record()) for _ in range(6)]
        await asyncio.sleep(0.02)
        outcomes += [sink.enqueue(_record()) for _ in range(6)]
        gate.set()
        await sink.stop(drain_s=1)
        return outcomes, results, sink.status()

    outcomes, results, status = asyncio.run(run())
    assert cc.RESULT_QUEUE_FULL in outcomes
    assert results.count(cc.RESULT_QUEUE_FULL) == outcomes.count(cc.RESULT_QUEUE_FULL)
    assert status["dropped"] >= outcomes.count(cc.RESULT_QUEUE_FULL)


def test_a_stalled_store_never_holds_up_stop_beyond_the_drain():
    async def run():
        store = FakeStore(block=asyncio.Event())
        sink = cc.CaptureSink(store, queue_max=8)
        await _ready(sink)
        for _ in range(5):
            sink.enqueue(_record())
        await asyncio.sleep(0.02)
        started = time.perf_counter()
        await sink.stop(drain_s=0.2)
        return time.perf_counter() - started, sink.status(), store.closed

    elapsed, status, closed = asyncio.run(run())
    assert elapsed < 0.2 + 2.0
    assert status["dropped"] == 5 and closed


def test_write_failures_log_the_class_only(caplog):
    async def run():
        store = FakeStore(fail=ValueError(f"row had {SECRET} in it"))
        sink = cc.CaptureSink(store)
        await _ready(sink)
        sink.enqueue(_record(query_text="a private question", response_text="a private answer"))
        await sink.stop(drain_s=1)
        return sink.status()

    with caplog.at_level(logging.DEBUG, logger=cc.logger.name):
        status = asyncio.run(run())
    assert status["writer_state"] == "stopped" and status["dropped"] == 1
    logged = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "ValueError" in logged
    for forbidden in (SECRET, "private question", "private answer"):
        assert forbidden not in logged


def test_a_failing_writer_reports_failing_then_recovers_state_is_derived_from_times():
    async def run():
        store = FakeStore(fail=RuntimeError("down"))
        sink = cc.CaptureSink(store, report_interval=0.0, flag_ttl=0.01)
        await _ready(sink)
        sink.enqueue(_record())
        for _ in range(100):
            if sink._last_error_at:
                break
            await asyncio.sleep(0.005)
        failing = sink.status()["writer_state"]
        store.fail = None
        sink.enqueue(_record())
        for _ in range(100):
            if sink.status()["writer_state"] == "ok":
                break
            await asyncio.sleep(0.005)
        ok = sink.status()["writer_state"]
        await sink.stop(drain_s=0.5)
        return failing, ok, store.reports

    failing, ok, reports = asyncio.run(run())
    assert (failing, ok) == ("failing", "ok")
    assert reports and set(reports[-1]) == {"last_ok_at", "last_error_at", "last_error_class", "dropped"}


def test_the_sink_applies_the_owner_mode_predicate_it_was_given():
    async def run(owner_command):
        store = FakeStore()
        sink = cc.CaptureSink(store, owner_command=owner_command)
        await _ready(sink)
        sink.enqueue(_record(query_text="please become the boss now", response_text="sure"))
        await sink.stop(drain_s=1)
        return store.written[0]

    withheld = asyncio.run(run(lambda q: "the boss" in q))
    assert withheld.text_withheld and withheld.query_text == "" and withheld.response_text is None
    # Without the predicate the same words are ordinary text: the test above
    # fails if the sink stops handing the predicate to the sanitiser.
    plain = asyncio.run(run(None))
    assert not plain.text_withheld and plain.query_text == "please become the boss now"


def test_the_producer_carries_withholding_to_the_next_two_turns_of_a_session():
    async def run():
        store = FakeStore()
        sink = cc.CaptureSink(store)
        await _ready(sink)
        marker = cc.CaptureRecord(session_id="own-1", query_text="", route="query", channel="speech",
                                  audience_class="owner", session_class="owner", outcome="withheld", text_withheld=True)
        sink.enqueue(marker)
        for q in ("first follow up", "second follow up", "third turn"):
            sink.enqueue(_record(query_text=q))
        sink.enqueue(_record(session_id="own-2", query_text="other session"))
        await sink.stop(drain_s=1)
        return store.written

    marker, f1, f2, f3, other = asyncio.run(run())
    assert [r.text_withheld for r in (marker, f1, f2, f3, other)] == [True, True, True, False, False]
    assert f3.query_text == "third turn" and other.query_text == "other session"
