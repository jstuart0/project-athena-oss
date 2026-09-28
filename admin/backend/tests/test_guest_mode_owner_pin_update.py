"""mozart jackson, urgent fix blocking ATHENA-69 rollout: the admin frontend
had no input to set the owner PIN. admin/frontend/guest-mode.js's new
setOwnerPin() sends a PIN-only PATCH body: {"owner_pin": "123456"}, relying
on GuestModeConfigUpdate's existing partial-update semantics (every field
Optional, only `is not None` fields are applied). This covers that exact
request shape end to end: response reports owner_pin_configured, the stored
value is a salted PBKDF2 hash (never the raw PIN), and no other config field
is disturbed.
"""
from __future__ import annotations

from app.auth.oidc import create_access_token
from app.models import GuestModeConfig

CONFIG_URL = "/api/guest-mode/config"


def _owner_headers(test_user):
    token = create_access_token({"user_id": test_user.id, "username": test_user.username, "role": test_user.role})
    return {"Authorization": f"Bearer {token}"}


def test_patch_config_with_owner_pin_only_stores_hashed_pin(client, db, test_user):
    headers = _owner_headers(test_user)

    # Mirrors the real page load: GET auto-creates the config row before the
    # owner ever reaches the Set PIN button (see get_guest_mode_config).
    get_resp = client.get(CONFIG_URL, headers=headers)
    assert get_resp.status_code == 200
    assert get_resp.json()["owner_pin_configured"] is False

    patch_resp = client.patch(CONFIG_URL, json={"owner_pin": "123456"}, headers=headers)
    assert patch_resp.status_code == 200

    body = patch_resp.json()
    assert body["owner_pin_configured"] is True
    assert body["owner_pin_needs_reset"] is False
    assert "owner_pin" not in body  # the hash itself is never in the response

    config = db.query(GuestModeConfig).first()
    assert config.owner_pin.startswith("pbkdf2_sha256$")
    assert config.owner_pin != "123456"


def test_patch_config_with_owner_pin_only_does_not_disturb_other_fields(client, db, test_user):
    headers = _owner_headers(test_user)
    db.add(GuestModeConfig(
        enabled=True, calendar_source="ical", calendar_url="https://example.com/cal.ics",
        buffer_before_checkin_hours=5, guest_allowed_intents=["weather"],
        guest_restricted_entities=[], guest_allowed_domains=[],
        created_by_id=test_user.id,
    ))
    db.commit()

    resp = client.patch(CONFIG_URL, json={"owner_pin": "654321"}, headers=headers)
    assert resp.status_code == 200

    config = db.query(GuestModeConfig).first()
    assert config.enabled is True
    assert config.calendar_url == "https://example.com/cal.ics"
    assert config.buffer_before_checkin_hours == 5
    assert config.guest_allowed_intents == ["weather"]
    assert config.owner_pin.startswith("pbkdf2_sha256$")


def test_patch_config_owner_pin_only_body_is_valid_request_shape(client, db, test_user):
    """The request body the frontend actually sends -- {"owner_pin": "..."}
    with no other keys -- must not be rejected as an incomplete object.
    GuestModeConfigUpdate has every field Optional, so pydantic accepts this
    shape; this pins that contract directly against the wire format."""
    headers = _owner_headers(test_user)
    client.get(CONFIG_URL, headers=headers)  # auto-create, matches page load

    resp = client.patch(CONFIG_URL, json={"owner_pin": "111222"}, headers=headers)
    assert resp.status_code == 200, resp.text
