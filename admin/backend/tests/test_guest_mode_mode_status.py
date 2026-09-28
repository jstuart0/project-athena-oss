"""ATHENA-127 Phase 4 -- GET /api/guest-mode/mode-status: the admin UI's
proxy to the mode service's actual mode decision (D7)."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from shared.config import _clear_cache_for_tests

MODE_STATUS_URL = "/api/guest-mode/mode-status"

# Captured before any test patches app.routes.guest_mode.httpx.AsyncClient --
# guest_mode.httpx IS the shared httpx module object (not a copy), so a fake
# built from a reference read after an earlier patch would recurse into
# itself.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def _reset_config_cache():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


class TestAuth:
    def test_invalid_credentials_is_401(self, client):
        # DEV_MODE bypasses auth entirely when no credentials are supplied
        # at all (conftest sets DEV_MODE=true) -- get_current_user's own
        # docstring: an X-API-Key header, even a bad one, is validated
        # normally regardless of DEV_MODE, so this is the harness's actual
        # "not authenticated" path for a route gated by plain
        # Depends(get_current_user) (see test_guest_mode_config_service_auth.py
        # for the equivalent pattern on this router's other routes).
        resp = client.get(MODE_STATUS_URL, headers={"X-API-Key": "not-a-real-key"})
        assert resp.status_code == 401


class TestReachable:
    def test_header_is_sent_and_fields_pass_through(self, owner_client, monkeypatch):
        monkeypatch.setenv("MODE_SERVICE_URL", "http://mode-service.test")
        _clear_cache_for_tests()

        captured = {}
        body = {
            "mode": "guest",
            "reason": "Active booking: admin #1 (until 2026-07-05T15:00:00+00:00)",
            "override_active": False,
            "events_count": 1,
            "bookings_source": "admin",
            "bookings_status": "fresh",
            "bookings_age_seconds": 12.5,
            "property_timezone": "America/New_York",
            "property_timezone_valid": True,
        }

        def handler(request):
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json=body)

        async def fake_check_ssrf_safe(url):
            captured["url"] = url
            return True, ""

        def fake_async_client(*args, **kwargs):
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

        with patch("app.routes.guest_mode.check_ssrf_safe", new=fake_check_ssrf_safe), \
             patch("app.routes.guest_mode.httpx.AsyncClient", new=fake_async_client):
            resp = owner_client.get(MODE_STATUS_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is True
        assert data["mode"] == "guest"
        assert data["bookings_source"] == "admin"
        assert data["bookings_status"] == "fresh"
        assert data["property_timezone"] == "America/New_York"
        assert data["property_timezone_valid"] is True
        assert captured["url"] == "http://mode-service.test/mode"
        from shared.config import get_config
        assert get_config().service_api_key
        assert captured["headers"].get("x-service-key") == get_config().service_api_key


class TestSsrfBlocked:
    def test_ssrf_blocked_makes_no_request(self, owner_client, monkeypatch):
        monkeypatch.setenv("MODE_SERVICE_URL", "http://169.254.169.254")
        _clear_cache_for_tests()

        called = {"n": 0}

        async def fake_check_ssrf_safe(url):
            return False, "blocked: link-local"

        async def fake_get(*a, **kw):
            called["n"] += 1
            return httpx.Response(200, json={})

        with patch("app.routes.guest_mode.check_ssrf_safe", new=fake_check_ssrf_safe), \
             patch("app.routes.guest_mode.httpx.AsyncClient") as mock_client_cls:
            mock_client_cls.return_value.__aenter__.return_value.get = fake_get
            resp = owner_client.get(MODE_STATUS_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is False
        assert data["error"] == "ssrf_blocked"
        assert called["n"] == 0


class TestUrlUnset:
    def test_unset_url_is_unreachable(self, owner_client, monkeypatch):
        monkeypatch.setenv("MODE_SERVICE_URL", "")
        _clear_cache_for_tests()

        resp = owner_client.get(MODE_STATUS_URL)
        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is False
        assert data["error"] == "mode_service_url_unset"


class TestTimeout:
    def test_timeout_is_unreachable(self, owner_client, monkeypatch):
        monkeypatch.setenv("MODE_SERVICE_URL", "http://mode-service.test")
        _clear_cache_for_tests()

        async def fake_check_ssrf_safe(url):
            return True, ""

        def handler(request):
            raise httpx.TimeoutException("timed out")

        def fake_async_client(*args, **kwargs):
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

        with patch("app.routes.guest_mode.check_ssrf_safe", new=fake_check_ssrf_safe), \
             patch("app.routes.guest_mode.httpx.AsyncClient", new=fake_async_client):
            resp = owner_client.get(MODE_STATUS_URL)

        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is False


def _proxy_with_handler(owner_client, monkeypatch, handler):
    monkeypatch.setenv("MODE_SERVICE_URL", "http://mode-service.test")
    _clear_cache_for_tests()

    async def fake_check_ssrf_safe(url):
        return True, ""

    def fake_async_client(*args, **kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler))

    with patch("app.routes.guest_mode.check_ssrf_safe", new=fake_check_ssrf_safe), \
         patch("app.routes.guest_mode.httpx.AsyncClient", new=fake_async_client):
        return owner_client.get(MODE_STATUS_URL)


class TestProxyHardening:
    def test_non_object_json_is_unreachable_not_500(self, owner_client, monkeypatch):
        resp = _proxy_with_handler(owner_client, monkeypatch, lambda r: httpx.Response(200, json=[1, 2]))
        assert resp.status_code == 200
        data = resp.json()
        assert data["reachable"] is False
        assert data["error"] == "malformed_response"

    def test_error_detail_never_echoes_exception_text(self, owner_client, monkeypatch):
        def handler(request):
            raise httpx.ConnectError("connect failed to http://mode-service.test/?token=SECRET123")

        resp = _proxy_with_handler(owner_client, monkeypatch, handler)
        data = resp.json()
        assert data == {"reachable": False, "error": "ConnectError"}
        assert "SECRET123" not in resp.text

    @pytest.mark.parametrize("raw,expected", [(3, 3), ("7", 7), ("<img src=x onerror=alert(1)>", None), (None, None), (2.9, 2)])
    def test_events_count_is_coerced_to_int(self, owner_client, monkeypatch, raw, expected):
        body = {"mode": "owner", "reason": "No active bookings", "events_count": raw}
        resp = _proxy_with_handler(owner_client, monkeypatch, lambda r: httpx.Response(200, json=body))
        data = resp.json()
        assert data["reachable"] is True
        assert data["events_count"] == expected
