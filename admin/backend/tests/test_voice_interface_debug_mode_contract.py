"""VoiceInterface.debug_mode is deprecated: always false, never stored from input.

The column stays in the table (an older pod still maps it), the ORM no longer
does, and the service-facing shape keeps the key as a constant because
shared/admin_config.py caches that payload whole. Removing any of this later
is a deliberate contract change that fails here first.
"""
from app.models import VoiceInterface


def _body(**extra):
    return {"interface_name": "wp_test", "display_name": "WP", **extra}


def test_orm_no_longer_maps_the_column():
    assert "debug_mode" not in VoiceInterface.__table__.columns
    assert VoiceInterface(interface_name="x").to_dict()["debug_mode"] is False


def test_create_ignores_debug_mode_input(owner_client, db):
    resp = owner_client.post("/api/voice-interfaces", json=_body(debug_mode=True))
    assert resp.status_code == 200, resp.text
    assert resp.json()["debug_mode"] is False


def test_update_ignores_debug_mode_input(owner_client, db):
    assert owner_client.post("/api/voice-interfaces", json=_body()).status_code == 200
    resp = owner_client.put("/api/voice-interfaces/wp_test", json={"debug_mode": True, "display_name": "Renamed"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Renamed"
    assert body["debug_mode"] is False


def test_internal_config_keeps_the_constant_keys(owner_client, db):
    assert owner_client.post("/api/voice-interfaces", json=_body(debug_mode=True)).status_code == 200
    resp = owner_client.get("/api/voice-interfaces/internal/config/wp_test")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["interface"]["debug_mode"] is False
    assert payload["behavior"]["debug_mode"] is False
