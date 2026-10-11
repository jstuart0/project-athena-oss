"""Conversation capture routes: settings, status, purge, and owner-only text.

Real production authentication (DEV_MODE's bypass is off); conversation rows
live in a separate SQLite database standing in for the main one (the SQL the
read routes run is portable), and purge's PostgreSQL statements are covered
in tests/integration/test_conversation_purge_postgres.py.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth.oidc import create_access_token
from app.database import get_analytics_db
from app.models import AuditLog, SystemSetting
from app.routes import conversations as conversations_routes
from app.routes.conversations import get_optional_analytics_db
from app.services import conversation_capture as capture
from app.services import settings_lease
from app.utils import rate_limit
from main import app
from shared import conversation_capture as contract
from shared.route_walk import dependency_calls, iter_api_routes

BASE = "/api/conversations"
SECRET_QUERY = "what is the owner's secret garage plan"
SECRET_ANSWER = "the garage plan answer text"

SCHEMA = [
    """CREATE TABLE conversations (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, room TEXT, user_mode TEXT, interface_type TEXT,
        started_at TIMESTAMP, ended_at TIMESTAMP, turn_count INTEGER NOT NULL, total_tokens INTEGER,
        created_at TIMESTAMP, session_class TEXT)""",
    """CREATE TABLE conversation_turns (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, turn_number INTEGER NOT NULL, request_id TEXT,
        query_text TEXT NOT NULL, response_text TEXT, intent TEXT, confidence NUMERIC, model_used TEXT,
        rag_tools_used TEXT, response_time_ms INTEGER, tokens_generated INTEGER, tokens_per_second NUMERIC,
        validation_passed BOOLEAN, error_message TEXT, source TEXT NOT NULL, complexity TEXT, is_fallback BOOLEAN,
        base_knowledge_populated BOOLEAN, node_timings_ms TEXT, created_at TIMESTAMP,
        route TEXT, channel TEXT, audience_class TEXT, outcome TEXT, text_withheld BOOLEAN DEFAULT 0, debug_detail TEXT)""",
    """CREATE TABLE turn_evaluations (
        id TEXT PRIMARY KEY, turn_id TEXT NOT NULL, rating INTEGER NOT NULL, notes TEXT,
        evaluated_by_user_id INTEGER, evaluated_by_username TEXT, created_at TIMESTAMP)""",
]


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    monkeypatch.setattr(capture, "DEV_MODE", False)  # the purge route refuses DEV_MODE; PostgreSQL-only SQL
    capture._refusal_audited_at.clear()


@pytest.fixture
def conv_engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.begin() as conn:
        for ddl in SCHEMA:
            conn.exec_driver_sql(ddl)
    raw = engine.raw_connection()
    raw.driver_connection.create_function("NOW", 0, lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    raw.close()
    return engine


@pytest.fixture
def conv(conv_engine):
    """The conversation database wired in for the read routes."""
    maker = sessionmaker(bind=conv_engine, autoflush=False)

    def override():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_analytics_db] = override
    app.dependency_overrides[get_optional_analytics_db] = override
    yield conv_engine
    app.dependency_overrides.pop(get_analytics_db, None)
    app.dependency_overrides.pop(get_optional_analytics_db, None)


def seed_conversation(engine, *, cid="conv-1", session_id="own-0123456789abcdef", cls="owner", turns=2, debug_detail=None):
    started = datetime(2026, 10, 1, 12, 0, 0)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO conversations VALUES (:id, :sid, 'kitchen', 'owner', 'voice', :t, :t, :n, 10, :t, :cls)"),
            {"id": cid, "sid": session_id, "t": started, "n": turns, "cls": cls})
        for number in range(1, turns + 1):
            conn.execute(text(
                "INSERT INTO conversation_turns (id, conversation_id, turn_number, query_text, response_text, intent, source, "
                "created_at, route, channel, audience_class, outcome, text_withheld, error_message, debug_detail) VALUES "
                "(:id, :cid, :n, :q, :a, 'control', 'debug', :t, 'query', 'speech', 'owner', 'answered', 0, 'err text here', :dd)"),
                {"id": f"{cid}-t{number}", "cid": cid, "n": number, "q": SECRET_QUERY, "a": SECRET_ANSWER, "t": started,
                 "dd": json.dumps(debug_detail or {"validation_reason": "some words", "cache_hit": True})})


def bearer(user):
    token = create_access_token({"user_id": user.id, "username": user.username, "role": user.role})
    return {"Authorization": f"Bearer {token}"}


def audits(db, action=None):
    db.expire_all()
    query = db.query(AuditLog)
    if action:
        query = query.filter(AuditLog.action == action)
    return query.all()


def set_setting(db, key, value, updated_at=None):
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        row = SystemSetting(key=key, value=value, category="privacy")
        db.add(row)
    row.value = value
    if updated_at is not None:
        row.updated_at = updated_at
    db.commit()


def writer_row(db, replica, *, age_s=10, last_ok=None, last_error=None, dropped=0):
    now = datetime.now(timezone.utc)
    key = contract.writer_status_key(replica)
    value = json.dumps({"last_ok_at": last_ok.isoformat() if last_ok else None,
                        "last_error_at": last_error.isoformat() if last_error else None,
                        "last_error_class": "OSError" if last_error else None, "dropped": dropped})
    db.add(SystemSetting(key=key, value=value, category=contract.WRITER_STATUS_CATEGORY,
                         updated_at=now - timedelta(seconds=age_s)))
    db.commit()


# ---------------------------------------------------------------------------
# GET /capture
# ---------------------------------------------------------------------------

def test_capture_status_auth_matrix(client, db, conv, test_user, operator_user, viewer_user, support_user):
    assert client.get(f"{BASE}/capture").status_code == 401
    assert client.get(f"{BASE}/capture", headers=bearer(viewer_user)).status_code == 403
    assert client.get(f"{BASE}/capture", headers=bearer(support_user)).status_code == 403
    for user in (operator_user, test_user):
        resp = client.get(f"{BASE}/capture", headers=bearer(user))
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "off"  # the route is not swallowed by /{conversation_id}


def test_capture_status_carries_no_text_and_counts(client, db, conv, test_user):
    seed_conversation(conv)
    body = client.get(f"{BASE}/capture", headers=bearer(test_user)).json()
    assert body["turn_count"] == 2 and body["oldest_turn_at"] and body["last_turn_at"]
    assert SECRET_QUERY not in json.dumps(body) and SECRET_ANSWER not in json.dumps(body)
    assert body["retention_days"] == body["retention_days"]  # present even when off


def test_capture_status_works_without_conversation_storage(client, db, test_user):
    """DEV_MODE (no conversation database): settings readable, counts null."""
    resp = client.get(f"{BASE}/capture", headers=bearer(test_user))
    assert resp.status_code == 200, resp.text
    assert resp.json()["turn_count"] is None


def test_status_state_matrix(client, db, conv, test_user):
    headers = bearer(test_user)

    def state():
        return client.get(f"{BASE}/capture", headers=headers).json()["state"]

    assert state() == "off"
    set_setting(db, contract.ANALYTICS_KEY, "true")
    assert state() == "on_no_writer"
    writer_row(db, "r1", age_s=1000)
    assert state() == "on_stale"
    now = datetime.now(timezone.utc)
    writer_row(db, "r2", age_s=5, last_ok=now - timedelta(seconds=30))
    assert state() == "on_ok"
    db.query(SystemSetting).filter(SystemSetting.key == contract.writer_status_key("r2")).delete()
    writer_row(db, "r2", age_s=5, last_ok=now - timedelta(seconds=30), last_error=now - timedelta(seconds=5))
    assert state() == "on_failing"
    set_setting(db, contract.ANALYTICS_KEY, "false")
    assert state() == "off"


def test_status_aggregates_replicas_and_ignores_old_rows(client, db, conv, test_user):
    set_setting(db, contract.ANALYTICS_KEY, "true")
    now = datetime.now(timezone.utc)
    writer_row(db, "healthy", age_s=5, last_ok=now, dropped=2)
    writer_row(db, "broken", age_s=5, last_ok=now - timedelta(minutes=1), last_error=now, dropped=3)
    writer_row(db, "gone", age_s=25 * 3600, last_error=now, dropped=100)
    body = client.get(f"{BASE}/capture", headers=bearer(test_user)).json()
    assert body["state"] == "on_failing"  # worst fresh replica wins
    assert body["dropped"] == 5 and body["writer_replicas"] == 2  # summed; the 25 h row is ignored


def test_debug_since_and_next_expiry(client, db, conv, test_user):
    set_setting(db, contract.RETENTION_KEY, "30")
    set_setting(db, contract.DEBUG_KEY, "true")
    seed_conversation(conv)
    body = client.get(f"{BASE}/capture", headers=bearer(test_user)).json()
    assert body["debug_enabled"] is True and body["debug_enabled_since"]
    assert body["next_expiry_at"].startswith("2026-10-31")


@pytest.mark.parametrize("n", [0, 3651, -1])
def test_preview_retention_bounds(client, db, conv, test_user, n):
    assert client.get(f"{BASE}/capture?preview_retention={n}", headers=bearer(test_user)).status_code == 422


def test_preview_retention_is_in_the_payload_for_valid_values(client, db, conv, test_user, monkeypatch):
    monkeypatch.setattr(capture, "preview_delete_turns", lambda analytics_db, days: 7 if days == 30 else 0)
    resp = client.get(f"{BASE}/capture?preview_retention=30", headers=bearer(test_user))
    assert resp.status_code == 200 and resp.json()["preview_delete_turns"] == 7


# ---------------------------------------------------------------------------
# PUT /capture
# ---------------------------------------------------------------------------

def put(client, user, **body):
    return client.put(f"{BASE}/capture", json=body, headers=bearer(user))


def test_put_validates_bounds_and_requires_both_fields(client, db, conv, test_user):
    assert put(client, test_user, debug_enabled=True, retention_days=0).status_code == 422
    assert put(client, test_user, debug_enabled=True, retention_days=3651).status_code == 422
    assert put(client, test_user, debug_enabled=True).status_code == 422
    assert put(client, test_user, retention_days=30).status_code == 422
    assert put(client, test_user, debug_enabled=True, retention_days=30, extra=1).status_code == 422
    assert put(client, test_user, debug_enabled=True, retention_days="abc").status_code == 422


def test_put_saves_both_values_and_audits_without_text(client, db, conv, test_user):
    set_setting(db, contract.RETENTION_KEY, "30")
    resp = put(client, test_user, debug_enabled=True, retention_days=365)
    assert resp.status_code == 200, resp.text
    assert resp.json()["debug_enabled"] is True and resp.json()["retention_days"] == 365
    rows = audits(db, "conversation_capture_settings_updated")
    assert len(rows) == 1
    assert rows[0].new_value == {"debug_enabled": True, "retention_days": 365}
    assert rows[0].old_value == {"debug_enabled": False, "retention_days": 30}


def test_put_requires_write(client, db, conv, viewer_user, support_user):
    assert put(client, viewer_user, debug_enabled=False, retention_days=30).status_code == 403
    assert put(client, support_user, debug_enabled=False, retention_days=30).status_code == 403
    assert client.put(f"{BASE}/capture", json={"debug_enabled": False, "retention_days": 30}).status_code == 401


def test_operator_may_raise_retention_but_not_lower_it(client, db, conv, test_user, operator_user):
    set_setting(db, contract.RETENTION_KEY, "365")
    refused = put(client, operator_user, debug_enabled=False, retention_days=30)
    assert refused.status_code == 403
    assert db.query(SystemSetting).filter(SystemSetting.key == contract.RETENTION_KEY).one().value == "365"
    rows = audits(db, "conversation_capture_settings_refused")
    assert len(rows) == 1 and rows[0].new_value["reason"] == "lowering_retention_requires_delete"
    assert put(client, operator_user, debug_enabled=True, retention_days=400).status_code == 200
    assert put(client, test_user, debug_enabled=True, retention_days=30).status_code == 200


# ---------------------------------------------------------------------------
# POST /purge
# ---------------------------------------------------------------------------

def purge(client, user=None, headers=None, **body):
    hdrs = headers if headers is not None else (bearer(user) if user else {})
    return client.post(f"{BASE}/purge", json=body, headers=hdrs)


@pytest.fixture
def fake_purge(monkeypatch):
    calls = []
    result = {"value": capture.PurgeResult(turns=3, conversations=1)}

    def fake(session_factory, scope, *, progress=None, **kw):
        calls.append(scope)
        outcome = result["value"]
        if isinstance(outcome, Exception):
            raise outcome
        if progress is not None:
            progress.turns, progress.conversations, progress.aborted = outcome.turns, outcome.conversations, outcome.aborted
        return outcome

    monkeypatch.setattr(capture, "purge", fake)
    return calls, result


def test_purge_401_without_credentials_and_with_a_service_key(client, db, conv, test_user, fake_purge):
    assert purge(client, scope="expired").status_code == 401
    resp = purge(client, headers={**bearer(test_user), "X-Service-Key": "anything"}, scope="expired")
    assert resp.status_code == 401
    assert audits(db, "conversation_purge_refused") == []
    assert fake_purge[0] == []


def test_purge_forbidden_for_operators_with_an_audit_row_and_for_scoped_roles(client, db, conv, operator_user, viewer_user, support_user, fake_purge):
    assert purge(client, operator_user, scope="expired").status_code == 403
    rows = audits(db, "conversation_purge_refused")
    assert len(rows) == 1
    assert rows[0].user_id == operator_user.id
    assert rows[0].new_value == {"reason": "insufficient_permission", "scope": "expired"}
    # Scoped roles (viewer, support) are turned away by get_current_user itself,
    # before the guard runs, so no purge-refusal row exists for them.
    for user in (viewer_user, support_user):
        assert purge(client, user, scope="expired").status_code == 403
    assert len(audits(db, "conversation_purge_refused")) == 1
    assert fake_purge[0] == []


def test_purge_all_needs_the_typed_confirmation(client, db, conv, test_user, fake_purge):
    assert purge(client, test_user, scope="all").status_code == 400
    assert purge(client, test_user, scope="all", confirm="yes").status_code == 400
    assert fake_purge[0] == []
    assert [r.new_value["reason"] for r in audits(db, "conversation_purge_refused")] == ["confirmation_missing"] * 2
    ok = purge(client, test_user, scope="all", confirm="purge all conversations")
    assert ok.status_code == 200 and ok.json() == {"turns": 3, "conversations": 1, "aborted": None}
    assert fake_purge[0] == ["all"]
    done = audits(db, "conversation_purged")
    assert len(done) == 1 and done[0].new_value == {"scope": "all", "turns": 3, "conversations": 1, "aborted": None}
    assert len(audits(db, "conversation_purge_started")) == 1  # written before the delete


def test_purge_body_is_closed(client, db, conv, test_user, fake_purge):
    assert purge(client, test_user, scope="everything").status_code == 422
    assert purge(client, test_user, scope="expired", sneaky=True).status_code == 422


def test_purge_lease_busy_is_409_and_audited(client, db, conv, test_user, fake_purge):
    fake_purge[1]["value"] = settings_lease.LeaseBusy("busy")
    resp = purge(client, test_user, scope="expired")
    assert resp.status_code == 409
    assert audits(db, "conversation_purge_refused")[0].new_value["reason"] == "purge_in_progress"


def test_a_lost_lease_is_reported_not_hidden(client, db, conv, test_user, fake_purge):
    fake_purge[1]["value"] = capture.PurgeResult(turns=2, conversations=0, aborted="lease_lost")
    body = purge(client, test_user, scope="expired").json()
    assert body["aborted"] == "lease_lost"


def test_purge_errors_never_echo_the_exception_and_end_with_a_failed_row(client, db, conv, test_user, fake_purge):
    fake_purge[1]["value"] = RuntimeError("password=hunter2 SELECT secret")
    resp = purge(client, test_user, scope="expired")
    assert resp.status_code == 500 and "hunter2" not in resp.text and "RuntimeError" in resp.text
    assert len(audits(db, "conversation_purge_started")) == 1
    failed = audits(db, "conversation_purge_failed")
    assert len(failed) == 1 and failed[0].new_value["scope"] == "expired"
    assert "hunter2" not in json.dumps(failed[0].new_value)


def test_a_cancelled_purge_still_ends_with_an_audit_row_carrying_partial_counts(db, test_user, monkeypatch):
    """A client disconnect cancels the handler; the finally block records what had committed."""
    import asyncio

    async def cancelled(func, factory, scope, *, progress):
        progress.turns, progress.conversations = 4, 1
        raise asyncio.CancelledError()

    monkeypatch.setattr(conversations_routes, "run_in_threadpool", cancelled)
    body = conversations_routes.PurgeRequest(scope="expired")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(conversations_routes.purge_conversations(body, db=db, session_factory=lambda: None, current_user=test_user))
    rows = audits(db, "conversation_purge_interrupted")
    assert len(rows) == 1 and rows[0].new_value == {"scope": "expired", "turns": 4, "conversations": 1, "aborted": None}


def test_purge_is_unavailable_in_dev_mode_and_audits_nothing(client, db, conv, test_user, fake_purge, monkeypatch):
    monkeypatch.setattr(capture, "DEV_MODE", True)
    assert purge(client, test_user, scope="expired").status_code == 503
    assert fake_purge[0] == [] and audits(db, "conversation_purge_started") == []


def test_repeated_refusals_are_audited_once_per_user_per_minute(client, db, conv, operator_user, test_user, fake_purge):
    for _ in range(4):
        assert purge(client, operator_user, scope="expired").status_code == 403
    assert len(audits(db, "conversation_purge_refused")) == 1


def test_the_sixth_purge_in_a_minute_is_429_and_audited(client, db, conv, test_user, fake_purge, monkeypatch):
    from fastapi import HTTPException

    seen = {"n": 0}

    async def limited(request, response, *, times, window_ms, budget, subject=""):
        assert budget == "conversation_purge" and times == 5
        assert subject == str(test_user.id)  # per user, not per ingress address
        seen["n"] += 1
        if seen["n"] > 5:
            raise HTTPException(status_code=429, detail="Too Many Requests")

    monkeypatch.setattr(rate_limit, "LIMITER_ACTIVE", True)
    monkeypatch.setattr(rate_limit, "_enforce", limited)
    codes = [purge(client, test_user, scope="expired").status_code for _ in range(8)]
    assert codes == [200] * 5 + [429] * 3
    rows = audits(db, "conversation_purge_refused")
    assert len(rows) == 1 and rows[0].new_value == {"reason": "rate_limited", "scope": "expired"}  # capped


def test_the_purge_route_carries_the_text_permission_guard():
    walked = [r for r in iter_api_routes(app) if r.path == f"{BASE}/purge" and "POST" in r.methods]
    assert len(walked) == 1
    guard = next(c for c in dependency_calls(walked[0]) if getattr(c, "required_permission", None))
    assert guard.required_permission == "view_conversation_text" and guard.caller_kinds == ("user",)


# ---------------------------------------------------------------------------
# Invalid stored retention
# ---------------------------------------------------------------------------

def test_invalid_stored_retention_purges_nothing_and_says_so(client, db, conv, test_user, monkeypatch):
    set_setting(db, contract.RETENTION_KEY, "abc")
    seed_conversation(conv)
    maker = sessionmaker(bind=db.get_bind(), autoflush=False)
    app.dependency_overrides[capture.get_purge_session_factory] = lambda: maker
    try:
        status = client.get(f"{BASE}/capture", headers=bearer(test_user)).json()
        assert status["retention_state"] == "retention_invalid" and status["retention_days"] is None
        resp = purge(client, test_user, scope="expired")
        assert resp.status_code == 409
        assert audits(db, "conversation_purge_refused")[0].new_value["reason"] == "retention_invalid"
        assert capture.run_scheduled_purge(maker) is None  # one loop tick
    finally:
        app.dependency_overrides.pop(capture.get_purge_session_factory, None)
    with conv.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM conversation_turns")).scalar() == 2


@pytest.mark.parametrize("value", ["0", "3651", "", "30d", " 30"])
def test_every_unreadable_retention_blocks_the_loop(db, value):
    set_setting(db, contract.RETENTION_KEY, value)
    maker = sessionmaker(bind=db.get_bind(), autoflush=False)
    assert capture.run_scheduled_purge(maker) is None


# ---------------------------------------------------------------------------
# D14: conversation text is owner-only and every read is audited
# ---------------------------------------------------------------------------

def test_operator_gets_metadata_only_and_no_audit_row(client, db, conv, operator_user):
    seed_conversation(conv)
    listing = client.get(BASE, headers=bearer(operator_user)).json()
    assert listing["text_visible"] is False
    assert listing["items"][0]["first_query"] is None and listing["items"][0]["turn_count"] == 2
    detail = client.get(f"{BASE}/conv-1", headers=bearer(operator_user)).json()
    assert detail["text_visible"] is False
    turn = detail["turns"][0]
    assert (turn["query_text"], turn["response_text"], turn["error_message"]) == (None, None, None)
    assert turn["debug_detail"] == {"cache_hit": True}
    assert turn["route"] == "query" and turn["channel"] == "speech" and turn["intent"] == "control"
    assert SECRET_QUERY not in json.dumps(listing) + json.dumps(detail)
    assert audits(db, "conversation_text_viewed") == []


def test_owner_gets_text_and_exactly_one_audit_row_without_it(client, db, conv, test_user):
    seed_conversation(conv)
    listing = client.get(f"{BASE}?audience_class=owner", headers=bearer(test_user)).json()
    assert listing["text_visible"] is True and listing["items"][0]["first_query"] == SECRET_QUERY[:120]
    rows = audits(db, "conversation_text_viewed")
    assert len(rows) == 1
    assert rows[0].new_value["endpoint"] == "GET /api/conversations" and rows[0].new_value["returned_rows"] == 1
    assert rows[0].new_value["audience_class"] == "owner"

    detail = client.get(f"{BASE}/conv-1", headers=bearer(test_user)).json()
    assert detail["turns"][0]["query_text"] == SECRET_QUERY and detail["turns"][0]["response_text"] == SECRET_ANSWER
    assert detail["turns"][0]["debug_detail"]["validation_reason"] == "some words"
    rows = audits(db, "conversation_text_viewed")
    assert len(rows) == 2
    for row in rows:
        dumped = json.dumps([row.old_value, row.new_value])
        for forbidden in (SECRET_QUERY, SECRET_ANSWER, "err text here", "some words"):
            assert forbidden not in dumped
    assert rows[1].new_value == {"endpoint": "GET /api/conversations/{conversation_id}", "conversation_id": "conv-1", "turns": 2}


def test_support_and_viewer_cannot_read_conversations(client, db, conv, support_user, viewer_user):
    seed_conversation(conv)
    for user in (support_user, viewer_user):
        assert client.get(BASE, headers=bearer(user)).status_code == 403
        assert client.get(f"{BASE}/conv-1", headers=bearer(user)).status_code == 403


def test_text_bearing_routes_are_all_gated_and_audited():
    """Population: every GET route whose handler returns query/answer text is in the gated set."""
    text_fields = ("query_text", "response_text", "first_query")
    gated = {}
    for walked in iter_api_routes(app):
        if walked.path.startswith(BASE) and "GET" in walked.methods:
            source = inspect.getsource(walked.route.endpoint)
            if any(field in source for field in text_fields):
                gated[walked.path] = ("_text_visible(" in source, "audit_text_read(" in source)
    assert len(gated) >= 2 and f"{BASE}/{{conversation_id}}" in gated and BASE in gated
    assert all(flags == (True, True) for flags in gated.values()), gated
    for model in (conversations_routes.ConversationSummary, conversations_routes.ConversationTurnOut):
        assert {"first_query", "query_text", "response_text"} & set(model.model_fields)


def test_session_ids_are_shown_truncated_to_everyone(client, db, conv, test_user, operator_user):
    seed_conversation(conv, session_id="sms_" + "a" * 40)
    for user in (test_user, operator_user):
        assert client.get(BASE, headers=bearer(user)).json()["items"][0]["session_id"] == "sms_aaaaaaaa"
        assert client.get(f"{BASE}/conv-1", headers=bearer(user)).json()["session_id"] == "sms_aaaaaaaa"


def test_withheld_rows_show_their_flag(client, db, conv, test_user):
    seed_conversation(conv, turns=1)
    with conv.begin() as c:
        c.execute(text("UPDATE conversation_turns SET text_withheld = 1, outcome = 'withheld', query_text = '', response_text = NULL"))
    turn = client.get(f"{BASE}/conv-1", headers=bearer(test_user)).json()["turns"][0]
    assert turn["text_withheld"] is True and turn["outcome"] == "withheld" and turn["query_text"] == ""


# ---------------------------------------------------------------------------
# Paging, filters, query budget
# ---------------------------------------------------------------------------

def test_audience_filter_and_keyset_paging(client, db, conv, test_user):
    for i in range(3):
        seed_conversation(conv, cid=f"c{i}", session_id=f"own-{i}" if i < 2 else f"gst-{i}", cls="owner" if i < 2 else "guest")
        with conv.begin() as c:
            c.execute(text("UPDATE conversations SET started_at = :t WHERE id = :id"), {"t": datetime(2026, 10, 1, 12, i), "id": f"c{i}"})
    headers = bearer(test_user)
    assert [i["id"] for i in client.get(f"{BASE}?audience_class=guest", headers=headers).json()["items"]] == ["c2"]
    first = client.get(f"{BASE}?per_page=2", headers=headers).json()
    assert [i["id"] for i in first["items"]] == ["c2", "c1"] and first["next_before"]
    second = client.get(f"{BASE}?per_page=2&before={first['next_before']}", headers=headers).json()
    assert [i["id"] for i in second["items"]] == ["c0"]
    assert client.get(f"{BASE}?before=garbage", headers=headers).status_code == 422
    assert client.get(f"{BASE}?page=51&per_page=200", headers=headers).status_code == 422


def test_list_and_detail_stay_within_six_queries(client, db, conv, test_user):
    seed_conversation(conv, turns=6)
    counter = {"n": 0}

    def count(*_args, **_kw):
        counter["n"] += 1

    event.listen(conv, "before_cursor_execute", count)
    try:
        for path in (BASE, f"{BASE}/conv-1"):
            counter["n"] = 0
            assert client.get(path, headers=bearer(test_user)).status_code == 200
            assert counter["n"] <= 6, (path, counter["n"])
    finally:
        event.remove(conv, "before_cursor_execute", count)


# ---------------------------------------------------------------------------
# Errors name the class, never the message
# ---------------------------------------------------------------------------

def test_error_details_and_logs_carry_the_class_name_only(client, db, test_user):
    """No conversation tables at all: every handler fails with a database error."""
    broken = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    maker = sessionmaker(bind=broken)

    def override():
        session = maker()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_analytics_db] = override
    try:
        headers = bearer(test_user)
        responses = [
            client.get(f"{BASE}/stats", headers=headers),
            client.get(BASE, headers=headers),
            client.get(f"{BASE}/conv-1", headers=headers),
            client.post(f"{BASE}/turns/t1/evaluate", json={"rating": 3}, headers=headers),
        ]
    finally:
        app.dependency_overrides.pop(get_analytics_db, None)
    for resp in responses:
        assert resp.status_code == 500
        assert "OperationalError" in resp.json()["detail"]
        assert "no such table" not in resp.text and "SELECT" not in resp.text


def test_evaluate_still_works_for_operators(client, db, conv, operator_user):
    seed_conversation(conv, turns=1)
    resp = client.post(f"{BASE}/turns/conv-1-t1/evaluate", json={"rating": 4, "notes": "ok"}, headers=bearer(operator_user))
    assert resp.status_code == 200 and resp.json()["action"] == "created"


def test_non_owners_get_an_allowlisted_debug_block_tools_timings_and_no_evaluation_notes(client, db, conv, test_user, operator_user):
    nested = {
        "cache_hit": True, "prompt_tokens": 12, "validation_reason": "free text",
        "caller_trust_tier": {"deep": "hello there"}, "degraded": ["leaked", {"x": "y"}],
        "unknown_key": 5, "stay_read_only": "yes",
    }
    seed_conversation(conv, turns=1, debug_detail=nested)
    with conv.begin() as c:
        c.execute(text("UPDATE conversation_turns SET rag_tools_used = :t, node_timings_ms = :n"),
                  {"t": json.dumps(["weather", "what is the secret garage plan", {"q": "x"}]),
                   "n": json.dumps({"classify": 12.5, "my secret note": 3, "flag": True, "nested": {"a": 1}})})
        c.execute(text("INSERT INTO turn_evaluations VALUES ('e1', 'conv-1-t1', 3, 'quotes the secret garage plan', 1, 'someone', :t)"),
                  {"t": datetime(2026, 10, 2)})
    op = client.get(f"{BASE}/conv-1", headers=bearer(operator_user)).json()["turns"][0]
    assert op["debug_detail"] == {"cache_hit": True, "prompt_tokens": 12}
    assert op["rag_tools_used"] == ["weather"]
    assert op["node_timings_ms"] == {"classify": 12.5}
    assert op["evaluations"][0]["notes"] is None and op["evaluations"][0]["rating"] == 3
    for fragment in ("free text", "hello there", "leaked", "garage", "secret"):
        assert fragment not in json.dumps(op)
    owner = client.get(f"{BASE}/conv-1", headers=bearer(test_user)).json()["turns"][0]
    assert owner["debug_detail"]["validation_reason"] == "free text"
    assert owner["evaluations"][0]["notes"] == "quotes the secret garage plan"


def test_the_writers_debug_shape_is_what_the_non_owner_allowlist_keeps():
    """Pins the producer: every key the writer can store is in the allowlist and every value is a scalar or a label."""
    record = contract.sanitize_record(contract.CaptureRecord(
        session_id="own-1", query_text="hi", route="query", channel="speech", audience_class="owner", session_class="owner",
        rag_tools_used=["weather", "news"], node_timings_ms={"classify": 1.5},
        debug_detail={k: ("label" if k.endswith(("reason", "tier")) else True) for k in contract.DEBUG_DETAIL_KEYS}))
    assert set(record.debug_detail) <= contract.DEBUG_DETAIL_KEYS
    assert conversations_routes._tool_names(record.rag_tools_used, False) == record.rag_tools_used
    assert conversations_routes._timings(record.node_timings_ms, False) == record.node_timings_ms
