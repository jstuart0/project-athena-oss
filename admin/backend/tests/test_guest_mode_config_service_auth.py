"""ATHENA-69 D25/D31/D39: dual-auth (X-Service-Key OR OIDC/API-key) for
GET /api/guest-mode/config, and service-key-only auth for
GET /api/guest-mode/internal/current-guest.

Covers:
 - a valid X-Service-Key gets 200 without any user/permission check.
 - a wrong service key or no credentials at all gets 401.
 - the pre-existing Bearer/API-key path is unaffected (200 for a reader,
   403 for a role without `read`).
 - the response never leaks the PIN hash, only owner_pin_configured.
 - guest_restricted_intents is included.
 - a service-key fetch against an empty DB never auto-creates a row (D39 --
   created_by_id is NOT NULL, so a service-key caller has no user to
   attribute one to).
 - GET /api/guest-mode/internal/current-guest requires X-Service-Key (D31 --
   previously open to any caller on the cluster network).
"""
from __future__ import annotations

from app.auth.oidc import create_access_token
from app.models import GuestModeConfig
from shared.config import _clear_cache_for_tests, get_config

CONFIG_URL = "/api/guest-mode/config"
CURRENT_GUEST_URL = "/api/guest-mode/internal/current-guest"


def test_guest_mode_config_accepts_service_key(client, db):
    _clear_cache_for_tests()
    key = get_config().service_api_key
    assert key, "conftest.py must set SERVICE_API_KEY for this test to be meaningful"

    resp = client.get(CONFIG_URL, headers={"X-Service-Key": key})
    assert resp.status_code == 200


def test_guest_mode_config_rejects_wrong_service_key(client, db):
    resp = client.get(CONFIG_URL, headers={"X-Service-Key": "definitely-wrong"})
    assert resp.status_code == 401


def test_guest_mode_config_no_auth(client, db, monkeypatch):
    # DEV_MODE's bypass (conftest sets DEV_MODE=true globally) would
    # otherwise silently authenticate this as dev-admin regardless of
    # headers -- disable it for this one test to exercise the real
    # "production, unauthenticated" path. get_guest_mode_config calls
    # get_optional_user/get_current_user as plain function calls (not via
    # Depends()), so app.dependency_overrides can't intercept them here.
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)
    resp = client.get(CONFIG_URL)
    assert resp.status_code == 401


def test_guest_mode_config_bearer_path_unchanged(client, db, test_user, viewer_user, monkeypatch):
    monkeypatch.setattr("app.auth.oidc.DEV_MODE", False)

    db.add(GuestModeConfig(
        enabled=False, calendar_source="ical", guest_allowed_intents=[],
        guest_restricted_entities=[], guest_allowed_domains=[],
        created_by_id=test_user.id,
    ))
    db.commit()

    owner_token = create_access_token({"user_id": test_user.id, "username": test_user.username, "role": test_user.role})
    resp = client.get(CONFIG_URL, headers={"Authorization": f"Bearer {owner_token}"})
    assert resp.status_code == 200

    # A viewer's Bearer token is rejected the same way it always was:
    # oidc.py's pre-existing _enforce_scoped_role_route_access 403s any
    # scoped role on an unlisted /api/* path, before this route's own
    # has_permission('read') check even runs.
    viewer_token = create_access_token({"user_id": viewer_user.id, "username": viewer_user.username, "role": viewer_user.role})
    resp = client.get(CONFIG_URL, headers={"Authorization": f"Bearer {viewer_token}"})
    assert resp.status_code == 403


def test_guest_mode_config_response_has_no_pin_material(client, db):
    # DEV_MODE bypass (conftest-global) resolves this no-credential request
    # to an auto-created dev-admin owner via get_optional_user's plain-call
    # path -- exactly like the "no auth" test above but on the success side.
    resp = client.get(CONFIG_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert "owner_pin" not in body
    assert body["owner_pin_configured"] is False

    config = db.query(GuestModeConfig).first()
    config.owner_pin = "pbkdf2_sha256$600000$deadbeef$cafebabe"
    db.commit()

    resp2 = client.get(CONFIG_URL)
    body2 = resp2.json()
    assert "owner_pin" not in body2
    assert body2["owner_pin_configured"] is True
    assert "deadbeef" not in resp2.text
    assert "cafebabe" not in resp2.text


def test_guest_mode_config_includes_restricted_intents(client, db):
    resp = client.get(CONFIG_URL)
    assert resp.status_code == 200
    assert resp.json()["guest_restricted_intents"] == ["tesla"]


def test_service_key_auto_create_has_no_creator(client, db):
    """D39 correction: a service-key fetch against an empty DB returns the
    built-in defaults and creates NOTHING -- created_by_id is NOT NULL, so a
    service-key caller (no user) can never satisfy it."""
    _clear_cache_for_tests()
    key = get_config().service_api_key

    assert db.query(GuestModeConfig).count() == 0
    resp = client.get(CONFIG_URL, headers={"X-Service-Key": key})
    assert resp.status_code == 200
    body = resp.json()
    assert body["owner_pin_configured"] is False
    assert body["config_source"] == "defaults"
    assert body["id"] is None

    assert db.query(GuestModeConfig).count() == 0


def test_current_guest_requires_service_key(client, db):
    resp = client.get(CURRENT_GUEST_URL)
    assert resp.status_code == 401


def test_current_guest_wrong_key_401(client, db):
    resp = client.get(CURRENT_GUEST_URL, headers={"X-Service-Key": "definitely-wrong"})
    assert resp.status_code == 401


def test_current_guest_with_service_key_returns_200(client, db):
    _clear_cache_for_tests()
    key = get_config().service_api_key
    assert key, "conftest.py must set SERVICE_API_KEY for this test to be meaningful"

    resp = client.get(CURRENT_GUEST_URL, headers={"X-Service-Key": key})
    assert resp.status_code == 200
    assert resp.json() == {"has_guest": False}
