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
    assert '"raw_query"' not in payload and '"query_hash": query_hash' in payload and '"query_len"' in payload
