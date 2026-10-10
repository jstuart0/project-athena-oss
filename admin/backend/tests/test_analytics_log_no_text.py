"""The legacy analytics log and the Admin Jarvis live feed carry no conversation text.

Whatever an older orchestrator sends, admin-backend drops query/answer text before it
stores a conversation_analytics row or broadcasts an event, and the query-logs report
no longer returns it (older rows may still hold text in the database; they are not shown).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.models import ConversationAnalytics
from app.routes import websocket as websocket_routes
from app.utils.conversation_text import TEXT_KEYS, strip_conversation_text

SECRET = "what is the garage door code SECRET-TEXT"
KEY = {"X-Service-Key": "test-service-key-for-hardening-tests"}


def test_strip_drops_every_text_key_at_any_depth_and_keeps_lengths():
    cleaned = strip_conversation_text({
        "intent": "weather", "query": SECRET, "query_preview": SECRET[:50], "response_preview": "an answer",
        "nested": {"answer": "x", "keep": 1, "query_text": "y"}, "confidence": 0.9,
    })
    assert cleaned == {"intent": "weather", "query_len": len(SECRET), "nested": {"keep": 1}, "confidence": 0.9}
    assert "SECRET-TEXT" not in json.dumps(cleaned)


def test_strip_keeps_an_existing_query_len_and_tolerates_non_dicts():
    assert strip_conversation_text({"query": "abc", "query_len": 99}) == {"query_len": 99}
    assert strip_conversation_text(None) is None
    assert {"query", "query_preview", "response_preview", "answer"} <= TEXT_KEYS


class FakeConn:
    def __init__(self):
        self.calls = []

    async def execute(self, sql, *args):
        self.calls.append((sql, args))

    async def close(self):
        pass


@pytest.fixture
def analytics_route(monkeypatch, client):
    from app.routes import internal

    conn = FakeConn()

    async def get_conn():
        return conn

    monkeypatch.setattr(internal, "get_admin_db_connection", get_conn)
    broadcasts = []

    async def capture_broadcast(event):
        broadcasts.append(event)

    monkeypatch.setattr(websocket_routes.ws_manager, "broadcast", capture_broadcast)
    return conn, broadcasts


def test_the_log_route_drops_query_text_from_the_row_and_the_broadcast(client, analytics_route):
    conn, broadcasts = analytics_route
    resp = client.post("/api/internal/analytics/log", headers=KEY, json={
        "session_id": "s1", "event_type": "query_intent",
        "metadata": {"intent": "weather", "query": SECRET, "confidence": 0.8, "query_preview": SECRET[:50]},
    })
    assert resp.status_code == 200, resp.text
    stored = json.loads(conn.calls[0][1][2])
    assert stored == {"intent": "weather", "query_len": len(SECRET), "confidence": 0.8}
    assert "SECRET-TEXT" not in json.dumps(conn.calls, default=str)
    assert len(broadcasts) == 1
    assert broadcasts[0]["data"] == {"intent": "weather", "query_len": len(SECRET), "confidence": 0.8}
    assert "SECRET-TEXT" not in json.dumps(broadcasts)


def test_a_new_orchestrator_payload_passes_through_unchanged(client, analytics_route):
    conn, broadcasts = analytics_route
    payload = {"intent": "control", "query_len": 12, "confidence": 1.0, "user_id": "owner", "room": "porch"}
    assert client.post("/api/internal/analytics/log", headers=KEY,
                       json={"session_id": "s", "event_type": "query_intent", "metadata": payload}).status_code == 200
    assert json.loads(conn.calls[0][1][2]) == payload and broadcasts[0]["data"] == payload


def test_every_live_broadcast_is_stripped_at_the_single_choke_point(monkeypatch):
    import asyncio

    seen = []

    async def capture(event):
        seen.append(event)

    monkeypatch.setattr(websocket_routes.ws_manager, "broadcast", capture)
    asyncio.run(websocket_routes.broadcast_to_admin_jarvis({
        "event_type": "session_start", "session_id": "s", "data": {"query_preview": SECRET[:50], "mode": "owner"}}))
    asyncio.run(websocket_routes.broadcast_to_admin_jarvis({
        "event_type": "response_ready", "session_id": "s", "data": {"response_preview": "the answer", "total_duration_ms": 5}}))
    assert [e["data"] for e in seen] == [{"mode": "owner"}, {"total_duration_ms": 5}]


def test_old_rows_with_text_are_not_returned_by_the_query_logs_report(client, db, owner_client):
    db.add(ConversationAnalytics(
        session_id="old", event_type="query_intent", timestamp=datetime.now(timezone.utc),
        event_metadata={"intent": "weather", "query": SECRET, "has_rag_service": True, "system_mapping": "weather"}))
    db.commit()
    resp = owner_client.get("/api/analytics/query-logs?days=7")
    assert resp.status_code == 200, resp.text
    entry = resp.json()["logs"][0]
    assert entry["query"] == "" and entry["query_len"] == len(SECRET)
    assert "SECRET-TEXT" not in resp.text
