"""jarvis-web's Bearer path and owner-only gate.

Signed-in owners/operators (a Bearer admin-backend confirms) get the
household's mode and every owner-only route; any other token is
unauthenticated. Superseded members of the pre-home-network version of this
file (unauthenticated callers served as guests, JARVIS_PUBLIC_MODE, reads
open to everyone) are replaced by test_jarvis_web_caller_classes.py.

Loads apps/jarvis-web/backend/main.py under a private synthetic module name
(same hazard/pattern as tests/unit/test_jarvis_web_appliances_ha_entities.py:20-35
-- main.py is a name shared by several services in this repo).

"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JARVIS_BACKEND = _REPO_ROOT / "apps" / "jarvis-web" / "backend"
sys.path.insert(0, str(_JARVIS_BACKEND))

os.environ.setdefault("SERVICE_API_KEY", "test-key-jarvis-caller-mode")

_spec = importlib.util.spec_from_file_location("_test_jarvis_web_caller_mode_main", _JARVIS_BACKEND / "main.py")
jarvis_main = importlib.util.module_from_spec(_spec)
sys.modules["_test_jarvis_web_caller_mode_main"] = jarvis_main
_spec.loader.exec_module(jarvis_main)

import caller_auth  # noqa: E402 -- resolved from _JARVIS_BACKEND via sys.path, same module main.py imported

from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Generic HTTP fakes
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload

    @property
    def text(self):
        return json.dumps(self._payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")


class _RoutedAsyncClient:
    """Generic httpx.AsyncClient replacement good enough to reach a 200
    from every owner_only route's handler body -- routes on URL substrings
    for the two shapes that matter (room-tv config, otherwise a bland
    success payload)."""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, *a, **kw):
        if "room-tv/internal" in url:
            return _FakeResponse(200, [{
                "media_player_entity_id": "media_player.test_entity",
                "display_name": "Test TV",
                "room_name": "living_room",
                "remote_entity_id": "remote.test_entity",
            }])
        if "current-guest" in url:
            return _FakeResponse(200, {"has_guest": False})
        if url.endswith("/api/states"):
            return _FakeResponse(200, [{
                "entity_id": "sensor.test_motion",
                "state": "off",
                "attributes": {"friendly_name": "Test Motion", "device_class": "motion"},
            }])
        return _FakeResponse(200, {"state": "on", "attributes": {"hvac_modes": ["heat", "cool", "off"]}})

    async def post(self, url, *a, **kw):
        return _FakeResponse(200, {
            "success": True,
            "room_name": "test-room",
            "token": "fake-token",
            "livekit_url": "wss://test",
        })

    async def delete(self, url, *a, **kw):
        return _FakeResponse(200, {"success": True})


async def _fake_auth_me_role(role):
    async def _call(token):
        return _FakeResponse(200, {"role": role})
    return _call


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_caller_auth_state(monkeypatch):
    monkeypatch.setattr(caller_auth, "get_admin_url", lambda: "http://admin.local:8080")
    monkeypatch.setattr(jarvis_main.httpx, "AsyncClient", _RoutedAsyncClient)
    # HA_TOKEN and the appliance entity IDs gate every device-control route
    # with their own "not configured" 503 before the gate even matters for
    # business logic -- set them truthy so gate tests exercise the gate,
    # not an unrelated config guard.
    monkeypatch.setattr(jarvis_main, "HA_TOKEN", "fake-ha-token")
    monkeypatch.setattr(jarvis_main, "OVEN_ENTITY", "water_heater.test_oven")
    monkeypatch.setattr(jarvis_main, "FRIDGE_ENTITY", "water_heater.test_fridge")
    monkeypatch.setattr(jarvis_main, "FREEZER_ENTITY", "water_heater.test_freezer")
    caller_auth._configure_for_tests({"SERVICE_API_KEY": os.environ["SERVICE_API_KEY"]})
    caller_auth._reset_for_tests()
    yield
    caller_auth._reset_for_tests()


@pytest.fixture
def client():
    return TestClient(jarvis_main.app)


def _install_role(role):
    async def _call(token):
        return _FakeResponse(200, {"role": role})
    caller_auth._set_auth_me_callable_for_tests(_call)


def _bearer(token="test-token"):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# chat with a Bearer
# ---------------------------------------------------------------------------

CSRF = {"X-Jarvis-Request": "1"}


def _chat_client():
    captured = {}

    class _ChatOrchClient(_RoutedAsyncClient):
        async def post(self, url, json=None, **kw):
            captured["body"] = json
            return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

    return captured, _ChatOrchClient


class TestChatCallerMode:
    def test_unauthenticated_chat_is_401_without_upstream(self, client):
        captured, fake = _chat_client()
        with mock.patch.object(jarvis_main.httpx, "AsyncClient", fake):
            resp = client.post("/api/chat", json={"message": "hello"}, headers=CSRF)
        assert resp.status_code == 401
        assert captured == {}

    @pytest.mark.parametrize("role", ["owner", "operator"])
    def test_authenticated_chat_uses_household_mode(self, client, role):
        _install_role(role)
        captured, fake = _chat_client()
        with mock.patch.object(jarvis_main.httpx, "AsyncClient", fake):
            resp = client.post("/api/chat", json={"message": "hello"}, headers={**_bearer(), **CSRF})
        assert resp.status_code == 200
        assert captured["body"]["mode"] == "owner"  # no guest booked -> auto owner
        assert captured["body"]["caller_trust"] == "web_authenticated"

    @pytest.mark.parametrize("role", ["viewer", "support", "unknown-role"])
    def test_scoped_role_bearer_is_unauthenticated(self, client, role):
        _install_role(role)
        resp = client.post("/api/chat", json={"message": "hello"}, headers={**_bearer(), **CSRF})
        assert resp.status_code == 401

    def test_browser_supplied_caller_trust_and_source_ignored(self, client):
        _install_role("owner")
        captured, fake = _chat_client()
        with mock.patch.object(jarvis_main.httpx, "AsyncClient", fake):
            resp = client.post(
                "/api/chat",
                json={"message": "hello", "source": "voice", "caller_trust": "household", "mode": "guest"},
                headers={**_bearer(), **CSRF},
            )
        assert resp.status_code == 200
        assert captured["body"]["caller_trust"] == "web_authenticated"
        assert captured["body"]["source"] == "voice"  # analytics only


# ---------------------------------------------------------------------------
# Bearer resolution details
# ---------------------------------------------------------------------------

class TestResolveCallerResolution:
    def test_no_authorization_header_makes_no_admin_call(self, client):
        calls = []

        async def _spy(token):
            calls.append(token)
            return _FakeResponse(200, {"role": "owner"})
        caller_auth._set_auth_me_callable_for_tests(_spy)

        resp = client.post("/api/climate/mode/heat", headers=CSRF)

        assert resp.status_code == 401
        assert calls == []

    def test_admin_url_unset_is_unauthenticated(self, client, monkeypatch):
        monkeypatch.setattr(caller_auth, "get_admin_url", lambda: "")
        _install_role("owner")
        resp = client.post("/api/climate/mode/heat", headers={**_bearer(), **CSRF})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "sign_in_required"

    def test_invalid_bearer_is_unauthenticated(self, client):
        async def _reject(token):
            return _FakeResponse(401, {"detail": "invalid token"})
        caller_auth._set_auth_me_callable_for_tests(_reject)
        resp = client.post("/api/climate/mode/heat", headers={**_bearer("bad-token"), **CSRF})
        assert resp.status_code == 401

    @pytest.mark.parametrize("error", ["timeout", "connect"])
    def test_auth_me_failure_is_unauthenticated(self, client, error):
        import httpx as _httpx

        async def _fail(token):
            if error == "timeout":
                raise _httpx.TimeoutException("timed out")
            raise _httpx.ConnectError("connection refused")
        caller_auth._set_auth_me_callable_for_tests(_fail)
        resp = client.post("/api/climate/mode/heat", headers={**_bearer(), **CSRF})
        assert resp.status_code == 401

    def test_auth_cache_ttl_60s(self, client):
        calls = []

        async def _counted(token):
            calls.append(token)
            return _FakeResponse(200, {"role": "owner"})
        caller_auth._set_auth_me_callable_for_tests(_counted)

        now = [1000.0]
        with mock.patch("caller_auth.time.monotonic", side_effect=lambda: now[0]):
            client.post("/api/climate/mode/heat", headers={**_bearer("same-token"), **CSRF})
            client.post("/api/climate/mode/heat", headers={**_bearer("same-token"), **CSRF})
            assert len(calls) == 1  # second call within TTL hit the cache

            now[0] += caller_auth._AUTH_CACHE_TTL_SECONDS + 1
            client.post("/api/climate/mode/heat", headers={**_bearer("same-token"), **CSRF})
            assert len(calls) == 2  # expired -> re-validated


class TestModeGuestName:
    def test_mode_guest_name_present_for_authenticated_owner(self, client):
        _install_role("owner")

        class _GuestAsyncClient(_RoutedAsyncClient):
            async def get(self, url, *a, **kw):
                if "current-guest" in url:
                    return _FakeResponse(200, {"has_guest": True, "guest_name": "Alice", "id": "g1"})
                return await super().get(url, *a, **kw)

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _GuestAsyncClient):
            resp = client.get("/api/mode", headers=_bearer())

        assert resp.status_code == 200
        assert resp.json()["guest_name"] == "Alice"

    def test_current_guest_call_sends_service_key(self, client):
        """get_current_guest()'s outbound call to admin's internal
        current-guest endpoint carries X-Service-Key."""
        _install_role("owner")
        captured_headers = []

        class _SpyAsyncClient(_RoutedAsyncClient):
            async def get(self, url, headers=None, **kw):
                if "current-guest" in url:
                    captured_headers.append(headers or {})
                return await super().get(url, headers=headers, **kw)

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _SpyAsyncClient):
            resp = client.get("/api/mode", headers=_bearer())

        assert resp.status_code == 200
        assert captured_headers and captured_headers[0].get("X-Service-Key") == jarvis_main.SERVICE_API_KEY
        assert jarvis_main.SERVICE_API_KEY


# ---------------------------------------------------------------------------
# owner_only route gate
# ---------------------------------------------------------------------------

_PATH_PARAM_VALUES = {
    "entity_id": "test_entity",
    "mode": "heat",
    "app_name": "Netflix",
    "action": "on",
    "room_name": "test-room",
}

_BODY_BY_ROUTE = {
    "POST /api/mode": {"mode": "owner"},
    "POST /api/climate/temperature": {"temperature": 70},
    "POST /api/media/{entity_id}/volume": {"volume": 0.5},
    "POST /api/media/{entity_id}/source": {"source": "HDMI1"},
    "POST /api/appliances/oven/temperature": {"temperature": 350},
    "POST /api/appliances/oven/mode": {"mode": "Bake"},
    "POST /api/appliances/fridge/temperature": {"temperature": 37},
    "POST /api/appliances/freezer/temperature": {"temperature": 0},
    "POST /api/appletv/{entity_id}/remote": {"command": "select"},
    "POST /livekit/rooms": {"participant_name": "test"},
    "POST /api/music/play": {"player_id": "p1", "uri": "spotify://track/1", "radio_mode": False},
}


def _build_path_and_body(route_key: str):
    method, path_template = route_key.split(" ", 1)
    path = path_template
    for name, value in _PATH_PARAM_VALUES.items():
        path = path.replace("{" + name + "}", value)
    body = _BODY_BY_ROUTE.get(route_key)
    return method, path, body


def _owner_only_http_routes():
    return sorted(
        key for key, kind in jarvis_main.ROUTE_CLASSIFICATION.items()
        if kind == "owner_only" and not key.startswith("WS ")
    )


class TestOwnerOnlyRouteGate:
    @pytest.mark.parametrize("route_key", _owner_only_http_routes())
    def test_owner_only_routes_refuse_unauthenticated(self, client, route_key):
        method, path, body = _build_path_and_body(route_key)
        resp = client.request(method, path, json=body, headers=CSRF)
        assert resp.status_code == 401, f"{route_key}: expected 401, got {resp.status_code}: {resp.text}"
        assert resp.json()["detail"] == "sign_in_required"

    def test_owner_only_routes_refuse_unauthenticated_named_member(self, client):
        """The handler body never runs: no outbound HA call."""
        calls = []

        class _SpyAsyncClient(_RoutedAsyncClient):
            async def post(self, url, *a, **kw):
                calls.append(url)
                return await super().post(url, *a, **kw)

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _SpyAsyncClient):
            resp = client.post("/api/climate/mode/heat", headers=CSRF)

        assert resp.status_code == 401
        assert calls == []

    @pytest.mark.parametrize("route_key", _owner_only_http_routes())
    @pytest.mark.parametrize("role", ["owner", "operator"])
    def test_owner_only_routes_allow_owner_and_operator(self, client, route_key, role):
        _install_role(role)
        method, path, body = _build_path_and_body(route_key)
        resp = client.request(method, path, json=body, headers={**_bearer(), **CSRF})
        assert resp.status_code == 200, f"{route_key} ({role}): expected 200, got {resp.status_code}: {resp.text}"


class TestWebSocketGate:
    @pytest.mark.parametrize("path", ["/ma/ws", "/ma/sendspin"])
    def test_ws_routes_close_unauthenticated(self, client, path):
        with pytest.raises(Exception):
            with client.websocket_connect(path):
                pass  # pragma: no cover -- connection must not reach here


class TestTokenHygiene:
    def test_token_never_logged_or_forwarded(self, client, monkeypatch):
        _install_role("owner")
        forwarded = {}

        class _ChatOrchClient(_RoutedAsyncClient):
            async def post(self, url, json=None, headers=None, **kw):
                forwarded["headers"] = headers or {}
                forwarded["json"] = json
                return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

        logged_events = []
        real_info = caller_auth.logger.info

        def _capture_info(event, **kw):
            logged_events.append((event, kw))
            return real_info(event, **kw)

        monkeypatch.setattr(caller_auth.logger, "info", _capture_info)
        monkeypatch.setattr(jarvis_main.logger, "info", _capture_info)

        secret_token = "super-secret-token-value"
        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            client.post("/api/chat", json={"message": "hello"}, headers={**_bearer(secret_token), **CSRF})

        assert forwarded, "floor: the orchestrator was called"
        assert secret_token not in json.dumps(forwarded)
        assert logged_events and not any(secret_token in str(kw) for _, kw in logged_events)


# ---------------------------------------------------------------------------
# chat-embed
# ---------------------------------------------------------------------------

class TestChatEmbedSendsGuest:
    def test_chat_embed_sends_guest(self):
        chat_embed_path = _REPO_ROOT / "apps" / "chat-embed" / "main.py"
        text = chat_embed_path.read_text(encoding="utf-8")
        assert '"mode": "guest"' in text
        assert '"mode": "owner"' not in text
