"""Sample queries are what people said: only a holder of view_conversation_text receives them.

Everyone else (operators, service callers) gets ``sample_queries: null`` and ``text_visible: false``,
the shape the Conversations routes use. Every response that carries them writes one audit row,
and intent metrics no longer store the raw query.
"""
from __future__ import annotations

import json

import pytest

from app.models import AuditLog, EmergingIntent, IntentMetric
from app.auth.oidc import create_access_token

SECRET = "tell me about the secret garage plan SECRET-WORDS"
KEY = {"X-Service-Key": "test-service-key-for-hardening-tests"}


def bearer(user):
    return {"Authorization": "Bearer " + create_access_token({"user_id": user.id, "username": user.username, "role": user.role})}


@pytest.fixture(autouse=True)
def _production_auth(monkeypatch):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)


@pytest.fixture
def intent(db):
    row = EmergingIntent(canonical_name="garage_plan", display_name="Garage Plan", sample_queries=[SECRET], status="discovered")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def audits(db):
    db.expire_all()
    return db.query(AuditLog).filter(AuditLog.action == "conversation_text_viewed").all()


def test_operators_get_the_withheld_shape_and_no_audit_row(client, db, operator_user, intent):
    listing = client.get("/api/emerging-intents", headers=bearer(operator_user))
    assert listing.status_code == 200
    row = listing.json()[0]
    assert row["sample_queries"] is None and row["text_visible"] is False and row["canonical_name"] == "garage_plan"
    one = client.get(f"/api/emerging-intents/{intent.id}", headers=bearer(operator_user))
    assert one.json()["sample_queries"] is None and "SECRET-WORDS" not in listing.text + one.text
    assert audits(db) == []


def test_owners_get_the_text_and_one_audit_row_per_response_without_it(client, db, test_user, intent):
    listing = client.get("/api/emerging-intents", headers=bearer(test_user))
    assert listing.json()[0]["sample_queries"] == [SECRET] and listing.json()[0]["text_visible"] is True
    client.get(f"/api/emerging-intents/{intent.id}", headers=bearer(test_user))
    rows = audits(db)
    assert len(rows) == 2
    assert {r.new_value["endpoint"] for r in rows} == {"GET /api/emerging-intents", "GET /api/emerging-intents/{intent_id}"}
    assert "SECRET-WORDS" not in json.dumps([r.new_value for r in rows])


def test_the_internal_routes_never_hand_a_service_caller_the_text(client, db, intent):
    internal = client.get("/api/internal/emerging-intents", headers=KEY)
    assert internal.status_code == 200
    assert internal.json()[0]["sample_queries"] is None and internal.json()[0]["text_visible"] is False
    created = client.post("/api/internal/emerging-intents", headers=KEY,
                          json={"canonical_name": "garage_plan", "sample_queries": ["another private question"]})
    assert created.status_code == 200 and created.json()["sample_queries"] is None
    assert "SECRET-WORDS" not in internal.text + created.text and audits(db) == []


def test_an_owner_reading_the_internal_list_gets_text_and_an_audit_row(client, db, test_user, intent):
    resp = client.get("/api/internal/emerging-intents", headers=bearer(test_user))
    assert resp.json()[0]["sample_queries"] == [SECRET]
    assert len(audits(db)) == 1


def test_intent_metrics_do_not_store_the_raw_query(client, db):
    resp = client.post("/api/internal/intent-metrics", headers=KEY, json={
        "intent": "control", "confidence": 0.9, "raw_query": SECRET, "query_hash": "abc", "query_len": len(SECRET)})
    assert resp.status_code == 200
    db.expire_all()
    metric = db.query(IntentMetric).one()
    assert metric.raw_query is None and metric.query_hash == "abc"
    assert "raw_query" not in metric.to_dict() and "SECRET-WORDS" not in json.dumps(metric.to_dict())


def test_the_orchestrator_no_longer_sends_the_raw_query():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[3] / "src" / "orchestrator" / "intent_discovery.py").read_text()
    body = source[source.index("async def record_intent_metric"):]
    payload = body[body.index("json={"):body.index("}\n            )")]
    assert '"raw_query"' not in payload and '"query_hash": hashed' in payload and '"query_len"' in payload


# ---------------------------------------------------------------------------
# Samples at rest, sorting, and the analytics metadata allowlist
# ---------------------------------------------------------------------------

def test_the_server_redacts_digits_in_every_incoming_sample(client, db):
    created = client.post("/api/internal/emerging-intents", headers=KEY, json={
        "canonical_name": "call_me", "sample_queries": ["call me on 5551234567", "", "plain words"]})
    assert created.status_code == 200
    again = client.post("/api/internal/emerging-intents", headers=KEY, json={
        "canonical_name": "call_me", "sample_queries": ["my code is 998877"]})
    assert again.status_code == 200
    row = db.query(EmergingIntent).filter(EmergingIntent.canonical_name == "call_me").one()
    db.refresh(row)
    assert row.sample_queries == ["call me on [number]", "plain words", "my code is [number]"]
    inc = client.post(f"/api/internal/emerging-intents/{row.id}/increment", headers=KEY, json={"sample_query": "room 20261 please"})
    assert inc.status_code == 200
    db.refresh(row)
    assert "room [number] please" in row.sample_queries
    assert "5551234567" not in json.dumps(row.sample_queries)


@pytest.mark.parametrize("field", ["occurrence_count", "first_seen", "last_seen", "created_at", "canonical_name", "status"])
def test_sort_by_accepts_only_the_listed_fields(client, db, test_user, intent, field):
    assert client.get(f"/api/emerging-intents?sort_by={field}", headers=bearer(test_user)).status_code == 200


@pytest.mark.parametrize("field", ["embedding", "sample_queries", "reviewed_by", "id; DROP TABLE x", "metadata", ""])
def test_sort_by_rejects_anything_else(client, db, test_user, intent, field):
    assert client.get(f"/api/emerging-intents?sort_by={field}", headers=bearer(test_user)).status_code == 422


def test_analytics_metadata_is_an_allowlist_on_write_and_on_read(client, db, monkeypatch):
    from datetime import datetime, timezone

    from app.models import ConversationAnalytics
    from app.routes import internal, websocket as websocket_routes
    from app.utils.conversation_text import sanitize_analytics_metadata

    class Conn:
        calls = []

        async def execute(self, sql, *args):
            Conn.calls.append(args)

        async def close(self):
            pass

    async def get_conn():
        return Conn()

    async def nobroadcast(event):
        pass

    monkeypatch.setattr(internal, "get_admin_db_connection", get_conn)
    monkeypatch.setattr(websocket_routes.ws_manager, "broadcast", nobroadcast)
    resp = client.post("/api/internal/analytics/log", headers=KEY, json={"session_id": "s", "event_type": "followup_detected", "metadata": {
        "intent": "weather", "confidence": 0.8, "user_id": "owner", "free_text": SECRET, "note": SECRET,
        "history": [SECRET], "query": SECRET, "latency_ms": 12, "error": SECRET}})
    assert resp.status_code == 200
    stored = json.loads(Conn.calls[0][2])
    assert stored == {"intent": "weather", "confidence": 0.8, "user_id": "owner", "query_len": len(SECRET), "latency_ms": 12,
                      "error": True, "error_len": len(SECRET)}
    assert "SECRET-WORDS" not in json.dumps(Conn.calls, default=str)

    row = ConversationAnalytics(session_id="old", event_type="query_intent", timestamp=datetime.now(timezone.utc),
                                event_metadata={"intent": "weather", "query": SECRET, "extra": SECRET})
    assert row.to_dict()["metadata"] == {"intent": "weather", "query_len": len(SECRET)}
    assert sanitize_analytics_metadata(json.dumps({"intent": "x", "query": SECRET})) == {"intent": "x", "query_len": len(SECRET)}
    assert sanitize_analytics_metadata(None) is None and sanitize_analytics_metadata("not json") == {}


def test_the_analytics_events_route_returns_no_old_text(client, db, owner_client):
    from datetime import datetime, timezone

    from app.models import ConversationAnalytics

    db.add(ConversationAnalytics(session_id="o1", event_type="query_intent", timestamp=datetime.now(timezone.utc),
                                 event_metadata={"intent": "weather", "query": SECRET}))
    db.commit()
    resp = owner_client.get("/api/conversation/analytics")
    assert resp.status_code == 200 and "SECRET-WORDS" not in resp.text


def test_the_analytics_log_docstring_does_not_claim_it_needs_no_authentication():
    from app.routes import internal

    assert "No authentication required" not in internal.log_analytics_event.__doc__
    assert "X-Service-Key" in internal.log_analytics_event.__doc__


def test_labels_are_redacted_and_capped_when_an_intent_is_created(client, db):
    resp = client.post("/api/internal/emerging-intents", headers=KEY, json={
        "canonical_name": "label_test", "display_name": "Plan for 20261 " + "N" * 300,
        "description": "she said call 5551234567 " + "d" * 700})
    assert resp.status_code == 200
    row = db.query(EmergingIntent).filter(EmergingIntent.canonical_name == "label_test").one()
    assert row.display_name.startswith("Plan for [number]") and len(row.display_name) == 100
    assert "5551234567" not in row.description and len(row.description) == 500


def test_a_missing_display_name_falls_back_to_the_canonical_name_and_an_empty_description_stays_empty(client, db):
    client.post("/api/internal/emerging-intents", headers=KEY, json={"canonical_name": "plain_name", "description": ""})
    row = db.query(EmergingIntent).filter(EmergingIntent.canonical_name == "plain_name").one()
    assert row.display_name == "Plain Name" and row.description is None
