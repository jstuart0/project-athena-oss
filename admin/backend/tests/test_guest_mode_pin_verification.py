"""ATHENA-69 D25/D30/D35/D40: POST /api/internal/guest-mode/verify-pin.

The admin backend is the sole owner-PIN authority: hash (PBKDF2, D30), the
per-tier lockout counter (D25/D35), and the verdict. This endpoint is
service-key-only via `require_service_key_401` (D40), not the router-level
`verify_service_api_key` (which would 422 rather than 401 on a missing
header).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import structlog

from app.auth.oidc import create_access_token
from app.models import GuestModeConfig, OwnerPinAttempt
from app.utils.passwords import hash_password
from shared.config import _clear_cache_for_tests, get_config

VERIFY_URL = "/api/internal/guest-mode/verify-pin"


def _service_key():
    _clear_cache_for_tests()
    return get_config().service_api_key


def _set_pin(db, pin="123456", created_by_id=None):
    config = db.query(GuestModeConfig).first()
    if not config:
        config = GuestModeConfig(enabled=False, calendar_source="ical", created_by_id=created_by_id)
        db.add(config)
    config.owner_pin = hash_password(pin)
    db.commit()
    db.refresh(config)
    return config


def test_verify_pin_rejects_oidc_only_caller(client, db, test_user):
    _set_pin(db, created_by_id=test_user.id)
    token = create_access_token({"user_id": test_user.id, "username": test_user.username, "role": test_user.role})
    resp = client.post(
        VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 401


def test_verify_pin_response_has_no_hash(client, db, test_user):
    _set_pin(db, created_by_id=test_user.id)
    resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": _service_key()})
    assert resp.status_code == 200
    assert set(resp.json().keys()) == {"status", "locked_until"}


@pytest.mark.parametrize(
    "pin,expected",
    [("123456", "verified"), ("999999", "invalid"), ("12345", "malformed"), ("abcdef", "malformed")],
    ids=["correct", "wrong", "malformed-counts", "malformed-nondigit"],
)
def test_verify_pin_statuses(client, db, test_user, pin, expected):
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    resp = client.post(VERIFY_URL, json={"pin": pin, "tier": "household"}, headers={"X-Service-Key": _service_key()})
    assert resp.json()["status"] == expected


def test_verify_pin_statuses_no_pin_configured(client, db, test_user):
    resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": _service_key()})
    assert resp.json()["status"] == "not_configured"


def test_legacy_sha256_hash_reads_as_not_configured(client, db, test_user):
    """D30: a pre-ATHENA-69 unsalted-SHA256 owner_pin (no "pbkdf2_sha256$"
    prefix) can't be verified against the new hash scheme -- it must read
    as not_configured, never verified or invalid, and the attempt must not
    be counted. Without the explicit format guard, verify_password's own
    "algorithm != pbkdf2_sha256" parse failure would instead return False
    for *any* PIN (including the correct plaintext one), surfacing as
    "invalid" and silently counting toward the lockout threshold.
    """
    import hashlib

    config = db.query(GuestModeConfig).first()
    if not config:
        config = GuestModeConfig(enabled=False, calendar_source="ical", created_by_id=test_user.id)
        db.add(config)
    config.owner_pin = hashlib.sha256("123456".encode()).hexdigest()
    db.commit()

    resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": _service_key()})
    assert resp.json()["status"] == "not_configured"
    assert db.query(OwnerPinAttempt).filter(OwnerPinAttempt.tier == "household").count() == 0


def test_missing_pin_config_does_not_count(client, db, test_user):
    key = _service_key()
    client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})
    assert db.query(OwnerPinAttempt).filter(OwnerPinAttempt.tier == "household").count() == 0


def test_malformed_pin_counts(client, db, test_user):
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    key = _service_key()
    resp = client.post(VERIFY_URL, json={"pin": "abcdef", "tier": "household"}, headers={"X-Service-Key": key})
    assert resp.json()["status"] == "malformed"
    attempt = db.query(OwnerPinAttempt).filter(OwnerPinAttempt.tier == "household").first()
    assert attempt.failed_count == 1


def test_lockout_after_threshold_failures(client, db, test_user):
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    key = _service_key()
    threshold = get_config().mode_override_lockout_threshold

    for _ in range(threshold):
        resp = client.post(VERIFY_URL, json={"pin": "000000", "tier": "household"}, headers={"X-Service-Key": key})
        assert resp.json()["status"] == "invalid"

    resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})
    assert resp.json()["status"] == "locked"

    attempt = db.query(OwnerPinAttempt).filter(OwnerPinAttempt.tier == "household").first()
    attempt.locked_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()

    resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})
    assert resp.json()["status"] == "verified"


def test_hash_not_evaluated_during_lockout(client, db, test_user):
    """bob r2 L4: while locked, the PIN hash comparison never runs at all --
    patch verify_password (not the raw hmac.compare_digest, which the
    unrelated X-Service-Key ingress check also calls on every request and
    would give a false-positive call count)."""
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    key = _service_key()
    threshold = get_config().mode_override_lockout_threshold
    for _ in range(threshold):
        client.post(VERIFY_URL, json={"pin": "000000", "tier": "household"}, headers={"X-Service-Key": key})

    with patch("app.routes.internal.verify_password") as spy:
        resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})

    assert resp.json()["status"] == "locked"
    assert spy.call_count == 0


def test_lockout_not_extended_by_attempts_during_lockout(client, db, test_user):
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    key = _service_key()
    threshold = get_config().mode_override_lockout_threshold
    for _ in range(threshold):
        client.post(VERIFY_URL, json={"pin": "000000", "tier": "household"}, headers={"X-Service-Key": key})

    attempt = db.query(OwnerPinAttempt).filter(OwnerPinAttempt.tier == "household").first()
    locked_until_before = attempt.locked_until

    client.post(VERIFY_URL, json={"pin": "000000", "tier": "household"}, headers={"X-Service-Key": key})
    client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})

    db.refresh(attempt)
    assert attempt.locked_until == locked_until_before


def test_success_resets_tier_counter(client, db, test_user):
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    key = _service_key()
    client.post(VERIFY_URL, json={"pin": "000000", "tier": "household"}, headers={"X-Service-Key": key})
    resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})
    assert resp.json()["status"] == "verified"

    attempt = db.query(OwnerPinAttempt).filter(OwnerPinAttempt.tier == "household").first()
    assert attempt.failed_count == 0
    assert attempt.locked_until is None


def test_lockout_is_per_tier(client, db, test_user):
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    key = _service_key()
    threshold = get_config().mode_override_lockout_threshold
    for _ in range(threshold):
        client.post(VERIFY_URL, json={"pin": "000000", "tier": "sms"}, headers={"X-Service-Key": key})

    locked = client.post(VERIFY_URL, json={"pin": "123456", "tier": "sms"}, headers={"X-Service-Key": key})
    assert locked.json()["status"] == "locked"

    ok = client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})
    assert ok.json()["status"] == "verified"


def test_unknown_tier_value_rejected(client, db, test_user):
    resp = client.post(VERIFY_URL, json={"pin": "123456", "tier": "web_public"}, headers={"X-Service-Key": _service_key()})
    assert resp.status_code == 422


def test_pin_change_clears_lockout(client, db, test_user):
    _set_pin(db, pin="123456", created_by_id=test_user.id)
    key = _service_key()
    threshold = get_config().mode_override_lockout_threshold
    for _ in range(threshold):
        client.post(VERIFY_URL, json={"pin": "000000", "tier": "household"}, headers={"X-Service-Key": key})
    assert db.query(OwnerPinAttempt).count() == 1

    resp = client.patch("/api/guest-mode/config", json={"owner_pin": "654321"})
    assert resp.status_code == 200
    assert db.query(OwnerPinAttempt).count() == 0

    verify = client.post(VERIFY_URL, json={"pin": "654321", "tier": "household"}, headers={"X-Service-Key": key})
    assert verify.json()["status"] == "verified"


def test_verify_pin_never_logs_pin(client, db, test_user):
    config = _set_pin(db, pin="123456", created_by_id=test_user.id)
    stored_hash = config.owner_pin
    key = _service_key()

    with structlog.testing.capture_logs() as logs:
        client.post(VERIFY_URL, json={"pin": "123456", "tier": "household"}, headers={"X-Service-Key": key})
        client.post(VERIFY_URL, json={"pin": "999999", "tier": "household"}, headers={"X-Service-Key": key})

    for entry in logs:
        text = str(entry)
        assert "123456" not in text
        assert "999999" not in text
        assert stored_hash not in text


def test_verify_pin_uses_row_lock_for_update():
    """D35 (bob r2 M2): the lockout check+increment reads the tier's
    OwnerPinAttempt row via with_for_update() so concurrent requests
    (including across admin-backend replicas on Postgres) serialize on the
    row instead of racing the increment.

    A real multi-threaded exercise against this repo's SQLite test fixture
    isn't meaningful evidence either way: SQLite ignores FOR UPDATE
    (single-connection serialization is sufficient there, per the
    endpoint's own docstring) and, worse, FastAPI's TestClient is not safe
    for concurrent use by multiple OS threads -- an earlier version of this
    test did exactly that and intermittently deadlocked the whole session
    on httpx's anyio portal machinery. This is a structural check that the
    row-lock call is present; test_lockout_after_threshold_failures and
    test_lockout_not_extended_by_attempts_during_lockout cover the
    sequential counting/threshold behavior the lock protects.
    """
    import inspect

    from app.routes import internal as internal_module

    source = inspect.getsource(internal_module.verify_owner_pin)
    assert "with_for_update()" in source
