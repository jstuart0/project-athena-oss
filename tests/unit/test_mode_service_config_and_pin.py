"""Unit tests for src/mode_service/main.py's config loading (D26/D37/D38)
and the owner-PIN override path (D16/D25).

The admin backend is faked with httpx.MockTransport -- for `load_config()`
(a fresh httpx.AsyncClient per call) by monkeypatching `mode_service.main.
httpx.AsyncClient`; for `_verify_owner_pin()` (the lazily-created,
module-level client) by installing a MockTransport-backed client directly
onto `mode_service.main._admin_http_client` before the call.
"""
from __future__ import annotations

import asyncio
import sys
import time
import unittest.mock as umock

sys.path.insert(0, "src")

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient

from shared import config as config_module
from shared.guest_policy import guest_baseline
from shared.booking_window import Booking


def _seed_admin_booking(ms, start, end, *, key="evt-1"):
    """ATHENA-127: simulate a fresh, successful admin fetch with one active
    booking, without going through BookingSources.refresh()'s HTTP path --
    these tests only care about determine_mode()'s consumption of the
    snapshot, not the fetch itself (that's test_mode_service_bookings.py)."""
    state = ms.booking_sources._admin
    state.last_good = [
        Booking(id=1, key=key, source="admin", label=f"admin #1", start=start, end=end, is_test=False)
    ]
    state.last_success_at = time.monotonic()
    state.last_attempt_ok = True

_SERVICE_KEY = "test-mode-service-key"
_HEADERS = {"X-Service-Key": _SERVICE_KEY}

# Captured once at import time, before any test monkeypatches httpx.AsyncClient
# -- reusing a previously-patched reference here would nest one test's fake
# transport inside the next's (or recurse), since the module attribute is
# shared process-wide.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(scope="module", autouse=True)
def _restore_structlog_after_module():
    """Importing mode_service.main calls shared.logging_config.configure_logging(),
    which globally replaces structlog's processors list and rebinds the
    "service" contextvar (shared/logging_config.py:104-117) -- a process-wide
    side effect. In production each service is its own process, so this never
    collides; in this shared pytest session it would otherwise leak
    "mode-service" into every later-running test file's log assertions.

    Module-scoped (not function-scoped): restoring after *every* test would
    invalidate mode_service.main.logger's cache_logger_on_first_use snapshot
    mid-file, breaking this file's own structlog.testing.capture_logs()
    tests. Snapshot once before this file's first test, restore once after
    its last -- leaves the rest of the session untouched by this file's
    import of mode_service.main.
    """
    snapshot = structlog.get_config()
    yield
    structlog.configure(**snapshot)


@pytest.fixture(autouse=True)
def _mode_service_env(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", _SERVICE_KEY)
    monkeypatch.setenv("DEV_MODE", "true")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


@pytest.fixture
def ms(_mode_service_env):
    from mode_service import main as ms_main

    ms_main.ADMIN_API_URL = "http://admin.test"
    ms_main.current_config = {}
    ms_main.current_mode = "owner"
    ms_main.active_override = None
    ms_main._config_loaded = True
    ms_main._last_load_ok = True
    ms_main._config_loaded_at = None
    ms_main._service_key_warned = False
    ms_main._admin_http_client = None
    # ATHENA-127: fresh booking-source state per test (the bookings fetch
    # shares _admin_http_client, reset above).
    ms_main.booking_sources = ms_main.BookingSources()
    yield ms_main
    if ms_main._admin_http_client is not None:
        asyncio.run(ms_main._admin_http_client.aclose())
        ms_main._admin_http_client = None


@pytest.fixture
def client(ms):
    return TestClient(ms.app)


def _patch_config_response(monkeypatch, ms, handler):
    """Make every `httpx.AsyncClient(...)` call inside load_config() route
    through a MockTransport driven by `handler`.

    Uses the module-level `_REAL_ASYNC_CLIENT` captured before any test ever
    patches `httpx.AsyncClient` -- `ms.httpx` *is* the shared `httpx` module
    object (not a copy), so re-capturing "the current AsyncClient" inside a
    test that runs after an earlier patch would wrap that earlier fake
    instead of the real class.
    """

    def fake_async_client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(ms.httpx, "AsyncClient", fake_async_client)


def _json_handler(payload, status_code=200):
    def handler(request):
        return httpx.Response(status_code, json=payload)
    return handler


class TestConfigFetchAuth:
    def test_config_fetch_sends_service_key(self, ms, monkeypatch):
        captured = {}

        def handler(request):
            captured.update(dict(request.headers))
            return httpx.Response(200, json={"enabled": False})

        _patch_config_response(monkeypatch, ms, handler)
        asyncio.run(ms.load_config())

        assert captured.get("x-service-key") == _SERVICE_KEY

    def test_config_fetch_without_key_warns_once(self, ms, monkeypatch):
        monkeypatch.setenv("SERVICE_API_KEY", "")
        config_module._clear_cache_for_tests()
        _patch_config_response(monkeypatch, ms, _json_handler({"enabled": False}))

        with structlog.testing.capture_logs() as logs:
            asyncio.run(ms.load_config())
            asyncio.run(ms.load_config())

        warnings = [e for e in logs if e.get("event") == "mode_service_admin_config_unauthenticated"]
        assert len(warnings) == 1


class TestLastGoodConfig:
    def test_refresh_failure_keeps_last_good_config(self, ms, monkeypatch):
        _patch_config_response(monkeypatch, ms, _json_handler({"enabled": True}))
        asyncio.run(ms.load_config())
        assert ms.current_config["enabled"] is True

        _patch_config_response(monkeypatch, ms, _json_handler({}, status_code=500))
        with structlog.testing.capture_logs() as logs:
            asyncio.run(ms.load_config())

        assert ms.current_config["enabled"] is True
        failed = [e for e in logs if e.get("event") == "mode_service.config.load_failed"]
        assert failed and failed[-1]["using"] == "last_good"

    def test_first_load_failure_uses_baseline_defaults(self, ms, monkeypatch):
        ms._config_loaded = False
        ms._last_load_ok = False
        _patch_config_response(monkeypatch, ms, _json_handler({}, status_code=401))

        with structlog.testing.capture_logs() as logs:
            asyncio.run(ms.load_config())

        failed = [e for e in logs if e.get("event") == "mode_service.config.load_failed"]
        assert failed and failed[-1]["using"] == "defaults"

        baseline = guest_baseline()
        assert ms.current_config["guest_allowed_intents"] == baseline["allowed_intents"]
        assert ms.current_config["guest_restricted_entities"] == baseline["restricted_entities"]
        assert ms.current_config["guest_allowed_domains"] == baseline["allowed_domains"]


class TestModeRecomputedPerRead:
    def test_disabling_guest_mode_returns_owner_without_restart(self, ms, client, monkeypatch):
        now = ms.datetime.now(ms.timezone.utc)
        _seed_admin_booking(ms, now - ms.timedelta(hours=1), now + ms.timedelta(hours=1))
        ms.current_config = {"enabled": True, "buffer_before_checkin_hours": 0, "buffer_after_checkout_hours": 0}
        assert client.get("/mode", headers=_HEADERS).json()["mode"] == "guest"

        _patch_config_response(monkeypatch, ms, _json_handler({"enabled": False}))
        asyncio.run(ms.load_config())

        assert client.get("/mode", headers=_HEADERS).json()["mode"] == "owner"

    def test_guest_override_expires_when_disabled(self, ms, client, monkeypatch):
        now = ms.datetime.now(ms.timezone.utc)
        ms.active_override = {
            "mode": "guest",
            "activated_at": now - ms.timedelta(minutes=10),
            "expires_at": now - ms.timedelta(minutes=1),
            "voice_device_id": None,
        }
        ms.current_config = {"enabled": False}

        resp = client.get("/mode", headers=_HEADERS).json()
        assert resp["mode"] == "owner"
        assert resp["override_active"] is False
        assert ms.active_override is None

    def test_admin_blip_during_booking_stays_guest(self, ms, client, monkeypatch):
        now = ms.datetime.now(ms.timezone.utc)
        _seed_admin_booking(ms, now - ms.timedelta(hours=1), now + ms.timedelta(hours=1))
        ms.current_config = {"enabled": True, "buffer_before_checkin_hours": 0, "buffer_after_checkout_hours": 0}
        assert client.get("/mode", headers=_HEADERS).json()["mode"] == "guest"

        _patch_config_response(monkeypatch, ms, _json_handler({}, status_code=500))
        asyncio.run(ms.load_config())

        assert client.get("/mode", headers=_HEADERS).json()["mode"] == "guest"


class TestOwnerOverridePinFlow:
    def test_owner_override_without_configured_pin_refused(self, ms, client):
        ms.current_config = {"owner_pin_configured": False}
        calls = []
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: (calls.append(1), httpx.Response(200, json={"status": "verified"}))[-1])
        )

        resp = client.post("/mode/override", json={"mode": "owner"}, headers=_HEADERS)
        assert resp.status_code == 403
        assert resp.json()["detail"] == "owner_pin_not_configured"
        assert calls == []
        assert ms.active_override is None

    def test_owner_override_missing_pin_is_401_when_configured(self, ms, client):
        ms.current_config = {"owner_pin_configured": True}
        calls = []
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: (calls.append(1), httpx.Response(200, json={"status": "verified"}))[-1])
        )

        resp = client.post("/mode/override", json={"mode": "owner"}, headers=_HEADERS)
        assert resp.status_code == 401
        assert calls == []

    def test_pin_verified_via_admin_not_local_hash(self, ms, client):
        import mode_service.main as ms_module

        assert hasattr(ms_module, "verify_pin") is False
        source = open(ms_module.__file__).read()
        assert "hashlib.sha256" not in source

        ms.current_config = {"owner_pin_configured": True}
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "verified", "locked_until": None}))
        )

        resp = client.post(
            "/mode/override",
            json={"mode": "owner", "voice_pin": "123456", "caller_tier": "household"},
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        assert resp.json()["mode"] == "owner"
        assert ms.active_override is not None

    @pytest.mark.parametrize(
        "verdict_status,expected_code,expected_detail",
        [
            ("invalid", 403, "Invalid PIN"),
            ("malformed", 400, "PIN must be exactly 6 digits"),
            ("not_configured", 403, "owner_pin_not_configured"),
            ("locked", 429, "owner_override_locked"),
        ],
        ids=["invalid", "malformed", "not_configured", "locked-429"],
    )
    def test_admin_verdict_mapping(self, ms, client, verdict_status, expected_code, expected_detail):
        ms.current_config = {"owner_pin_configured": True}
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": verdict_status}))
        )

        resp = client.post(
            "/mode/override",
            json={"mode": "owner", "voice_pin": "654321"},
            headers=_HEADERS,
        )
        assert resp.status_code == expected_code
        assert resp.json()["detail"] == expected_detail

    @pytest.mark.parametrize(
        "handler",
        [
            lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")),
            lambda r: (_ for _ in ()).throw(httpx.TimeoutException("timed out")),
            lambda r: httpx.Response(500, json={"detail": "boom"}),
            lambda r: httpx.Response(200, text="not json"),
        ],
        ids=["connect_error", "timeout", "http_500", "non_json"],
    )
    def test_admin_verify_unreachable_is_not_verified(self, ms, client, handler):
        ms.current_config = {"owner_pin_configured": True}
        ms._admin_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        resp = client.post(
            "/mode/override",
            json={"mode": "owner", "voice_pin": "654321"},
            headers=_HEADERS,
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "owner_pin_verification_unavailable"
        assert ms.active_override is None

    def test_caller_tier_forwarded_and_missing_is_unknown(self, ms, client):
        captured = {}

        def handler(request):
            captured.update(__import__("json").loads(request.content))
            return httpx.Response(200, json={"status": "verified"})

        ms.current_config = {"owner_pin_configured": True}
        ms._admin_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        client.post(
            "/mode/override",
            json={"mode": "owner", "voice_pin": "654321", "caller_tier": "sms"},
            headers=_HEADERS,
        )
        assert captured["tier"] == "sms"

        captured.clear()
        client.post(
            "/mode/override",
            json={"mode": "owner", "voice_pin": "654321"},
            headers=_HEADERS,
        )
        assert captured["tier"] == "unknown"

    def test_guest_override_needs_no_pin(self, ms, client):
        calls = []
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: (calls.append(1), httpx.Response(200, json={"status": "verified"}))[-1])
        )

        resp = client.post("/mode/override", json={"mode": "guest"}, headers=_HEADERS)
        assert resp.status_code == 200
        assert calls == []


class TestOverrideModeInvalidValues:
    """ATHENA-69 Pass H2 (xander delta review, High): ModeOverrideRequest.mode
    was a bare `str` -- POST /mode/override {"mode":"Owner", ...} (or any
    other non-exact-"owner" value) with a valid service key skipped the
    entire PIN-required branch (only literal `== "owner"` was checked) and
    got stored verbatim; get_permissions()'s old unconditional-else branch
    then granted unrestricted (owner) permissions to it with zero PIN
    attempts. A Literal rejects every case below with 422 before
    override_mode's body ever runs."""

    @pytest.mark.parametrize(
        "bad_mode",
        ["Owner", "OWNER", " owner", "owner ", "admin", "", "guest ", "Guest"],
    )
    def test_non_exact_mode_value_rejected_with_422(self, ms, client, bad_mode):
        calls = []
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: (calls.append(1), httpx.Response(200, json={"status": "verified"}))[-1])
        )

        resp = client.post("/mode/override", json={"mode": bad_mode}, headers=_HEADERS)

        assert resp.status_code == 422
        assert calls == []
        assert ms.active_override is None


class TestOverrideTimeoutCapped:
    """ATHENA-69 Pass H2 (xander delta review, High): timeout_minutes was
    unbounded -- POST /mode/override {"mode":"owner","timeout_minutes":999999}
    with a valid PIN granted an effectively-permanent override. Server-side
    cap via AthenaConfig.override_max_timeout_minutes (default 240),
    applied regardless of PIN outcome."""

    def test_requested_timeout_over_cap_is_clamped(self, ms, client):
        ms.current_config = {"owner_pin_configured": True}
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "verified"}))
        )

        resp = client.post(
            "/mode/override",
            json={"mode": "owner", "voice_pin": "123456", "timeout_minutes": 999999},
            headers=_HEADERS,
        )

        assert resp.status_code == 200
        assert "240 minutes" in resp.json()["message"]
        expires_at = ms.active_override["expires_at"]
        activated_at = ms.active_override["activated_at"]
        assert (expires_at - activated_at).total_seconds() <= 240 * 60 + 1

    def test_requested_timeout_under_cap_is_unaffected(self, ms, client):
        ms.current_config = {"owner_pin_configured": True}
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "verified"}))
        )

        resp = client.post(
            "/mode/override",
            json={"mode": "owner", "voice_pin": "123456", "timeout_minutes": 30},
            headers=_HEADERS,
        )

        assert resp.status_code == 200
        assert "30 minutes" in resp.json()["message"]

    def test_guest_override_timeout_also_capped(self, ms, client):
        """The cap applies regardless of PIN outcome -- guest overrides
        need no PIN at all but must still be bounded."""
        resp = client.post(
            "/mode/override",
            json={"mode": "guest", "timeout_minutes": 999999},
            headers=_HEADERS,
        )
        assert resp.status_code == 200
        expires_at = ms.active_override["expires_at"]
        activated_at = ms.active_override["activated_at"]
        assert (expires_at - activated_at).total_seconds() <= 240 * 60 + 1


class TestColdStart:
    """D38 (codex r3): until the first successful admin-config load, /mode
    must never report owner, and /health must show config_source="none" and
    ready=false. Not in the pre-D38 test-contract list by name, but the
    decision is binding for Pass D."""

    def test_cold_start_before_first_config_load_is_degraded(self, ms, client):
        ms._config_loaded = False
        ms._last_load_ok = False
        ms._config_loaded_at = None
        ms.current_config = {}

        mode_resp = client.get("/mode", headers=_HEADERS).json()
        assert mode_resp["mode"] == "degraded"

        perms_resp = client.get("/mode/permissions", headers=_HEADERS).json()
        assert perms_resp["mode"] == "degraded"
        # ATHENA-69 Pass H2 (xander delta review, Info->fix): degraded now
        # matches orchestrator.mode_permission.degraded_permissions() --
        # allowed_intents=[] means UNRESTRICTED (conversation and every
        # other intent still allowed), not "only these intents". Only the
        # entity floor narrows anything during cold start.
        assert perms_resp["allowed_intents"] == []  # unrestricted = conversation allowed
        assert perms_resp["allowed_domains"] == []  # unrestricted beyond the entity floor
        assert r"^lock\." in perms_resp["restricted_entities"]  # physical security denied

        health = client.get("/health").json()
        assert health["config_source"] == "none"
        assert health["ready"] is False
        assert health["pin_authority"] == "admin"

    def test_cold_start_owner_override_grants_nothing(self, ms, client):
        """Zero owner grants during cold start: owner_pin_configured is
        never present on an unloaded config, so the override path refuses
        before ever consulting the admin backend."""
        ms._config_loaded = False
        ms._last_load_ok = False
        ms.current_config = {}
        calls = []
        ms._admin_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda r: (calls.append(1), httpx.Response(200, json={"status": "verified"}))[-1])
        )

        resp = client.post("/mode/override", json={"mode": "owner"}, headers=_HEADERS)
        assert resp.status_code in (401, 403)
        assert calls == []

    def test_after_first_successful_load_mode_is_normal(self, ms, client, monkeypatch):
        ms._config_loaded = False
        ms._last_load_ok = False
        ms.current_config = {}
        assert client.get("/mode", headers=_HEADERS).json()["mode"] == "degraded"

        _patch_config_response(monkeypatch, ms, _json_handler({"enabled": False}))
        asyncio.run(ms.load_config())

        resp = client.get("/mode", headers=_HEADERS).json()
        assert resp["mode"] == "owner"
        health = client.get("/health").json()
        assert health["config_source"] == "admin"
        assert health["ready"] is True


class TestStaleConfigAlert:
    """D37 (bob r2 M4): ERROR log mode_service_config_stale once age >
    _CONFIG_STALE_AFTER_SECONDS, throttled to at most once per
    _CONFIG_STALE_LOG_INTERVAL_SECONDS. Drives `_check_config_staleness()`
    directly (the function config_refresh_loop calls after every
    load_config()) with scaled-down thresholds instead of the loop's real
    60 s sleep, and real (sub-second) sleeps to cross the throttle window
    deterministically.
    """

    def test_stale_alert_fires_once_then_throttled_then_fires_again(self, ms, monkeypatch):
        monkeypatch.setattr(ms, "_CONFIG_STALE_AFTER_SECONDS", 0.01)
        monkeypatch.setattr(ms, "_CONFIG_STALE_LOG_INTERVAL_SECONDS", 0.2)
        ms._config_loaded = True
        ms._config_loaded_at = time.monotonic() - 1.0  # well past the 0.01s threshold
        ms._last_stale_log_at = None

        mock_logger = umock.MagicMock()
        monkeypatch.setattr(ms, "logger", mock_logger)

        def _stale_calls():
            return [c for c in mock_logger.error.call_args_list if c.args[:1] == ("mode_service_config_stale",)]

        ms._check_config_staleness()
        assert len(_stale_calls()) == 1

        # Immediately again: still inside the throttle window -> no new log.
        ms._check_config_staleness()
        assert len(_stale_calls()) == 1

        # After the throttle window elapses, it fires again.
        time.sleep(0.25)
        ms._check_config_staleness()
        assert len(_stale_calls()) == 2

    def test_stale_alert_silent_while_config_fresh(self, ms, monkeypatch):
        monkeypatch.setattr(ms, "_CONFIG_STALE_AFTER_SECONDS", 500)
        ms._config_loaded = True
        ms._config_loaded_at = time.monotonic()  # fresh
        ms._last_stale_log_at = None

        mock_logger = umock.MagicMock()
        monkeypatch.setattr(ms, "logger", mock_logger)

        ms._check_config_staleness()
        stale_calls = [c for c in mock_logger.error.call_args_list if c.args[:1] == ("mode_service_config_stale",)]
        assert stale_calls == []
