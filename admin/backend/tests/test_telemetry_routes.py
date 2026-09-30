"""The Admin API for telemetry: status (read), the admin switch, a manual
send, and identity reset (owner-only, audited), with the env locks."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import AuditLog, SystemSetting
from app.services.telemetry import sender
from tests._telemetry_support import FakeClock, Recorder, settings
from tests.conftest import TestingSessionLocal


@pytest.fixture
def recorder(telemetry_env, monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(sender, "TRANSPORT", rec.transport)
    return rec


def _put(key, value):
    session = TestingSessionLocal()
    try:
        row = session.query(SystemSetting).filter(SystemSetting.key == key).first()
        if row is None:
            session.add(SystemSetting(key=key, value=value, category="telemetry"))
        else:
            row.value = value
        session.commit()
    finally:
        session.close()


def test_unauthenticated_is_401(client, recorder):
    assert client.get("/api/telemetry/status", headers={"X-API-Key": ""}).status_code == 401
    assert client.put("/api/telemetry/settings", json={"enabled": False}, headers={"X-API-Key": ""}).status_code == 401


def test_operator_reads_status_without_manage_or_key(operator_client, recorder):
    asyncio.run(sender.run_cycle(force=True))
    response = operator_client.get("/api/telemetry/status")
    assert response.status_code == 200
    body = response.json()
    assert body["can_manage"] is False
    assert body["enabled"] is True and body["reason"] == "enabled"
    assert body["installation_id"]
    assert "install_key" not in response.text
    assert settings(TestingSessionLocal)["telemetry.install_key"] not in response.text


def test_operator_cannot_change_anything(operator_client, recorder):
    assert operator_client.put("/api/telemetry/settings", json={"enabled": False}).status_code == 403
    assert operator_client.post("/api/telemetry/send").status_code == 403
    assert operator_client.post("/api/telemetry/reset-identity").status_code == 403


def test_owner_toggle_is_audited_and_stops_sending(owner_client, db, recorder):
    response = owner_client.put("/api/telemetry/settings", json={"enabled": False})
    assert response.status_code == 200
    assert response.json()["reason"] == "admin_setting"
    audits = db.query(AuditLog).filter(AuditLog.resource_type == "telemetry").all()
    assert len(audits) == 1
    assert audits[0].new_value == {"enabled": False}
    assert asyncio.run(sender.run_cycle(force=True)) == "disabled"
    assert recorder.requests == []
    assert owner_client.put("/api/telemetry/settings", json={"enabled": True}).json()["enabled"] is True


def test_env_opt_out_locks_the_toggle(owner_client, db, recorder, monkeypatch):
    monkeypatch.setenv("ATHENA_TELEMETRY", "off")
    response = owner_client.put("/api/telemetry/settings", json={"enabled": True})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "env_locked"
    status = owner_client.get("/api/telemetry/status").json()
    assert (status["enabled"], status["reason"], status["env_locked"]) == (False, "env_athena_telemetry", True)
    assert "telemetry.admin_disabled" not in settings(TestingSessionLocal)


def test_send_rules(owner_client, recorder, monkeypatch):
    monkeypatch.setenv("ATHENA_TELEMETRY", "off")
    disabled = owner_client.post("/api/telemetry/send")
    assert disabled.status_code == 409 and disabled.json()["detail"]["error"] == "disabled"
    monkeypatch.setenv("ATHENA_TELEMETRY", "")
    assert owner_client.post("/api/telemetry/send").status_code == 202
    again = owner_client.post("/api/telemetry/send")
    assert again.status_code == 429 and again.json()["detail"]["error"] == "too_soon"


def test_send_allowed_again_after_ten_minutes(owner_client, recorder, monkeypatch):
    clock = FakeClock(datetime.now(timezone.utc))
    monkeypatch.setattr(sender, "CLOCK", clock)
    scheduled = []
    monkeypatch.setattr(sender, "request_send", lambda: scheduled.append(clock.now))
    assert owner_client.post("/api/telemetry/send").status_code == 202
    assert scheduled == [clock.now]
    clock.advance(minutes=9, seconds=59)
    assert owner_client.post("/api/telemetry/send").status_code == 429
    clock.advance(seconds=1)
    assert owner_client.post("/api/telemetry/send").status_code == 202
    assert len(scheduled) == 2


def test_status_on_a_fresh_db_mints_nothing(owner_client, recorder):
    body = owner_client.get("/api/telemetry/status").json()
    assert body["installation_id"] is None and body["can_manage"] is True
    assert "telemetry.installation_id" not in settings(TestingSessionLocal)


def test_reset_identity(owner_client, db, recorder):
    asyncio.run(sender.run_cycle(force=True))
    state = settings(TestingSessionLocal)
    old_id, key = state["telemetry.installation_id"], state["telemetry.install_key"]
    response = owner_client.post("/api/telemetry/reset-identity")
    assert response.status_code == 200
    assert "telemetry.installation_id" not in settings(TestingSessionLocal)
    audits = db.query(AuditLog).filter(AuditLog.resource_type == "telemetry", AuditLog.action == "reset_identity").all()
    assert len(audits) == 1
    assert audits[0].old_value == {"installation_id": old_id}
    assert audits[0].new_value is None
    assert key not in json.dumps(audits[0].old_value) and "install_key" not in json.dumps(audits[0].old_value)


def test_reset_identity_while_sending_is_409(owner_client, recorder):
    _put(sender.LEASE_KEY, json.dumps({"holder": "other/1",
                                       "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=100)).isoformat()}))
    response = owner_client.post("/api/telemetry/reset-identity")
    assert response.status_code == 409 and response.json()["detail"]["error"] == "send_in_progress"


def test_reset_identity_while_disabled_is_409(owner_client, recorder, monkeypatch):
    monkeypatch.setenv("DO_NOT_TRACK", "1")
    response = owner_client.post("/api/telemetry/reset-identity")
    assert response.status_code == 409 and response.json()["detail"]["error"] == "disabled"


def test_status_strips_endpoint_userinfo(owner_client, recorder, monkeypatch):
    monkeypatch.setenv("ATHENA_TELEMETRY_ENDPOINT", "https://user:pw@evil.example/v1/ping")
    response = owner_client.get("/api/telemetry/status")
    body = response.json()
    assert body["reason"] == "endpoint_invalid"
    assert body["endpoint"] == "https://evil.example/v1/ping"
    assert "user:pw" not in response.text and "pw@" not in response.text
