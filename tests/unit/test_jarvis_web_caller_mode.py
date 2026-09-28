"""ATHENA-69 Pass F -- jarvis-web caller resolution (D18), the owner_only
route gate (D19), guest-safe GET /api/mode (D31), and chat-embed's
guest-only relay (P6.3).

Loads apps/jarvis-web/backend/main.py under a private synthetic module name
(same hazard/pattern as tests/unit/test_jarvis_web_appliances_ha_entities.py:20-35
-- main.py is a name shared by several services in this repo).

 - test_unauthenticated_chat_sends_guest
 - test_unauthenticated_chat_stream_sends_guest
 - test_authenticated_owner_chat_uses_household_mode
 - test_authenticated_operator_chat_uses_household_mode
 - test_scoped_role_bearer_is_guest
 - test_no_authorization_header_makes_no_admin_call
 - test_admin_url_unset_is_guest
 - test_invalid_bearer_is_guest
 - test_auth_me_timeout_is_guest
 - test_auth_me_connect_error_is_guest
 - test_auth_cache_ttl_60s
 - test_browser_supplied_caller_trust_and_source_ignored
 - test_public_mode_household_legacy
 - test_public_mode_invalid_value_is_guest
 - test_public_mode_household_reminder_logs_repeatedly
 - test_mode_override_ignored_for_unauthenticated
 - test_owner_only_routes_refuse_unauthenticated (named member: POST /api/climate/mode/{mode})
 - test_owner_only_routes_allow_owner_and_operator
 - test_ws_routes_close_unauthenticated
 - test_reads_stay_open
 - test_token_never_logged_or_forwarded
 - test_chat_embed_sends_guest
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
# chat / chat_stream caller mode
# ---------------------------------------------------------------------------

class TestChatCallerMode:
    def test_unauthenticated_chat_sends_guest(self, client):
        captured = {}

        class _ChatOrchClient(_RoutedAsyncClient):
            async def post(self, url, json=None, **kw):
                captured["body"] = json
                return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            resp = client.post("/api/chat", json={"message": "hello"})

        assert resp.status_code == 200
        assert captured["body"]["mode"] == "guest"
        assert captured["body"]["caller_trust"] == "web_public"

    def test_unauthenticated_chat_stream_sends_guest(self, client):
        captured = {}

        class _StreamCtx:
            def __init__(self, payload):
                self._payload = payload

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def aiter_text(self):
                yield "data: [DONE]\n\n"

        class _ChatOrchClient(_RoutedAsyncClient):
            def stream(self, method, url, json=None, **kw):
                captured["body"] = json
                return _StreamCtx(json)

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            resp = client.post("/api/chat/stream", json={"message": "hello"})
            # Drain the streaming response body to force generate() to run.
            list(resp.iter_lines())

        assert captured["body"]["mode"] == "guest"
        assert captured["body"]["caller_trust"] == "web_public"

    def test_authenticated_owner_chat_uses_household_mode(self, client):
        _install_role("owner")
        captured = {}

        class _ChatOrchClient(_RoutedAsyncClient):
            async def post(self, url, json=None, **kw):
                captured["body"] = json
                return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            resp = client.post("/api/chat", json={"message": "hello"}, headers=_bearer())

        assert resp.status_code == 200
        assert captured["body"]["mode"] == "owner"  # no guest booked -> get_current_mode()'s auto-owner
        assert captured["body"]["caller_trust"] == "web_authenticated"

    def test_authenticated_operator_chat_uses_household_mode(self, client):
        _install_role("operator")
        captured = {}

        class _ChatOrchClient(_RoutedAsyncClient):
            async def post(self, url, json=None, **kw):
                captured["body"] = json
                return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            resp = client.post("/api/chat", json={"message": "hello"}, headers=_bearer())

        assert resp.status_code == 200
        assert captured["body"]["mode"] == "owner"
        assert captured["body"]["caller_trust"] == "web_authenticated"

    @pytest.mark.parametrize("role", ["viewer", "support", "unknown-role"])
    def test_scoped_role_bearer_is_guest(self, client, role):
        _install_role(role)
        captured = {}

        class _ChatOrchClient(_RoutedAsyncClient):
            async def post(self, url, json=None, **kw):
                captured["body"] = json
                return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            resp = client.post("/api/chat", json={"message": "hello"}, headers=_bearer())

        assert resp.status_code == 200
        assert captured["body"]["mode"] == "guest"
        assert captured["body"]["caller_trust"] == "web_public"

    def test_browser_supplied_caller_trust_and_source_ignored(self, client):
        """ChatMessage has no caller_trust field at all, and message.source
        is analytics-only -- neither can move mode/caller_trust."""
        captured = {}

        class _ChatOrchClient(_RoutedAsyncClient):
            async def post(self, url, json=None, **kw):
                captured["body"] = json
                return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            resp = client.post(
                "/api/chat",
                json={"message": "hello", "source": "voice", "caller_trust": "household"},
            )

        assert resp.status_code == 200
        assert captured["body"]["mode"] == "guest"
        assert captured["body"]["caller_trust"] == "web_public"
        assert captured["body"]["source"] == "voice"  # source itself is forwarded, just not as a trust signal


# ---------------------------------------------------------------------------
# resolve_caller resolution details
# ---------------------------------------------------------------------------

class TestResolveCallerResolution:
    def test_no_authorization_header_makes_no_admin_call(self, client):
        calls = []

        async def _spy(token):
            calls.append(token)
            return _FakeResponse(200, {"role": "owner"})
        caller_auth._set_auth_me_callable_for_tests(_spy)

        resp = client.post("/api/climate/mode/heat")

        assert resp.status_code == 403
        assert calls == []

    def test_admin_url_unset_is_guest(self, client, monkeypatch):
        monkeypatch.setattr(caller_auth, "get_admin_url", lambda: "")
        _install_role("owner")

        resp = client.post("/api/climate/mode/heat", headers=_bearer())

        assert resp.status_code == 403
        assert resp.json()["detail"] == "sign_in_required"

    def test_invalid_bearer_is_guest(self, client):
        async def _reject(token):
            return _FakeResponse(401, {"detail": "invalid token"})
        caller_auth._set_auth_me_callable_for_tests(_reject)

        resp = client.post("/api/climate/mode/heat", headers=_bearer("bad-token"))

        assert resp.status_code == 403
        assert resp.json()["detail"] == "sign_in_required"

    def test_auth_me_timeout_is_guest(self, client):
        import httpx as _httpx

        async def _timeout(token):
            raise _httpx.TimeoutException("timed out")
        caller_auth._set_auth_me_callable_for_tests(_timeout)

        resp = client.post("/api/climate/mode/heat", headers=_bearer())

        assert resp.status_code == 403
        assert resp.json()["detail"] == "sign_in_required"

    def test_auth_me_connect_error_is_guest(self, client):
        import httpx as _httpx

        async def _connect_error(token):
            raise _httpx.ConnectError("connection refused")
        caller_auth._set_auth_me_callable_for_tests(_connect_error)

        resp = client.post("/api/climate/mode/heat", headers=_bearer())

        assert resp.status_code == 403
        assert resp.json()["detail"] == "sign_in_required"

    def test_auth_cache_ttl_60s(self, client):
        calls = []

        async def _counted(token):
            calls.append(token)
            return _FakeResponse(200, {"role": "owner"})
        caller_auth._set_auth_me_callable_for_tests(_counted)

        now = [1000.0]
        with mock.patch("caller_auth.time.monotonic", side_effect=lambda: now[0]):
            client.post("/api/climate/mode/heat", headers=_bearer("same-token"))
            client.post("/api/climate/mode/heat", headers=_bearer("same-token"))
            assert len(calls) == 1  # second call within TTL hit the cache

            now[0] += caller_auth._AUTH_CACHE_TTL_SECONDS + 1
            client.post("/api/climate/mode/heat", headers=_bearer("same-token"))
            assert len(calls) == 2  # expired -> re-validated


# ---------------------------------------------------------------------------
# JARVIS_PUBLIC_MODE
# ---------------------------------------------------------------------------

class TestPublicMode:
    def test_public_mode_household_legacy(self, monkeypatch):
        # is_owner_permitted() reads caller_auth's own module-level
        # JARVIS_PUBLIC_MODE directly -- main.py's imported binding is
        # unrelated and patching it has no effect on the gate.
        monkeypatch.setattr(caller_auth, "JARVIS_PUBLIC_MODE", "household")

        c = TestClient(jarvis_main.app)
        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _RoutedAsyncClient):
            resp = c.post("/api/climate/mode/heat")

        assert resp.status_code == 200

    def test_public_mode_invalid_value_is_guest(self, monkeypatch):
        errors = []
        monkeypatch.setattr(caller_auth.logger, "error", lambda *a, **kw: errors.append((a, kw)))
        monkeypatch.setenv("JARVIS_PUBLIC_MODE", "nonsense")

        parsed = caller_auth._parse_public_mode()

        assert parsed == "guest"
        assert len(errors) == 1

    def test_mode_override_ignored_for_unauthenticated(self, client):
        """The process-global mode_override only applies via
        get_current_mode(), which unauthenticated callers never reach
        under the default guest public mode."""
        jarvis_main.mode_override = "owner"
        try:
            captured = {}

            class _ChatOrchClient(_RoutedAsyncClient):
                async def post(self, url, json=None, **kw):
                    captured["body"] = json
                    return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

            with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
                client.post("/api/chat", json={"message": "hello"})

            assert captured["body"]["mode"] == "guest"
        finally:
            jarvis_main.mode_override = None

    def test_public_mode_household_reminder_logs_repeatedly(self, monkeypatch):
        monkeypatch.setattr(caller_auth, "JARVIS_PUBLIC_MODE", "household")
        warnings = []
        monkeypatch.setattr(caller_auth.logger, "warning", lambda *a, **kw: warnings.append((a, kw)))

        async def _run():
            task = asyncio.create_task(caller_auth._posture_reminder_loop(0.01))
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_run())

        household_warnings = [w for w in warnings if w[0][0] == "jarvis_public_mode_household_active"]
        assert len(household_warnings) >= 2


class TestModeGuestNameSuppression:
    """D31 (tessa's Pass F mutation review, High): GET /api/mode is a read
    (never gated by require_owner_caller), but a web_public caller must
    not learn the current guest's name from it."""

    def test_mode_guest_name_suppressed_for_web_public(self, client):
        class _GuestAsyncClient(_RoutedAsyncClient):
            async def get(self, url, *a, **kw):
                if "current-guest" in url:
                    return _FakeResponse(200, {"has_guest": True, "guest_name": "Alice", "id": "g1"})
                return await super().get(url, *a, **kw)

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _GuestAsyncClient):
            resp = client.get("/api/mode")

        assert resp.status_code == 200
        body = resp.json()
        assert body["has_guest"] is True
        assert body["guest_name"] is None

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
        body = resp.json()
        assert body["has_guest"] is True
        assert body["guest_name"] == "Alice"

    def test_current_guest_call_sends_service_key(self, client):
        """D31: get_current_guest()'s outbound call to admin's
        internal current-guest endpoint must carry X-Service-Key."""
        captured_headers = []

        class _SpyAsyncClient(_RoutedAsyncClient):
            async def get(self, url, headers=None, **kw):
                if "current-guest" in url:
                    captured_headers.append(headers or {})
                return await super().get(url, headers=headers, **kw)

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _SpyAsyncClient):
            resp = client.get("/api/mode")

        assert resp.status_code == 200
        assert len(captured_headers) == 1
        assert captured_headers[0].get("X-Service-Key") == jarvis_main.SERVICE_API_KEY
        assert jarvis_main.SERVICE_API_KEY  # the assertion above is vacuous if this is empty/None


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
        resp = client.request(method, path, json=body)
        assert resp.status_code == 403, f"{route_key}: expected 403, got {resp.status_code}: {resp.text}"
        assert resp.json()["detail"] == "sign_in_required"

    def test_owner_only_routes_refuse_unauthenticated_named_member(self, client):
        """tessa's Pass F mutation review, Medium: 403 + detail alone
        doesn't prove the handler body never ran -- spy on the outbound HA
        call count too, so a mutation that lets the request fall through
        to set_hvac_mode() (which would itself 503 without HA_TOKEN,
        possibly masquerading as an unrelated-looking failure) is caught
        at the source instead."""
        calls = []

        class _SpyAsyncClient(_RoutedAsyncClient):
            async def post(self, url, *a, **kw):
                calls.append(url)
                return await super().post(url, *a, **kw)

        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _SpyAsyncClient):
            resp = client.post("/api/climate/mode/heat")

        assert resp.status_code == 403
        assert resp.json()["detail"] == "sign_in_required"
        assert calls == []

    @pytest.mark.parametrize("route_key", _owner_only_http_routes())
    @pytest.mark.parametrize("role", ["owner", "operator"])
    def test_owner_only_routes_allow_owner_and_operator(self, client, route_key, role):
        _install_role(role)
        method, path, body = _build_path_and_body(route_key)
        resp = client.request(method, path, json=body, headers=_bearer())
        assert resp.status_code == 200, f"{route_key} ({role}): expected 200, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# WebSocket gate
# ---------------------------------------------------------------------------

class TestWebSocketGate:
    @pytest.mark.parametrize("path", ["/ma/ws", "/ma/sendspin"])
    def test_ws_routes_close_unauthenticated(self, client, path):
        with pytest.raises(Exception):
            with client.websocket_connect(path):
                pass  # pragma: no cover -- connection must not reach here


# ---------------------------------------------------------------------------
# Reads stay open
# ---------------------------------------------------------------------------

class TestReadsStayOpen:
    @pytest.mark.parametrize("path", [
        "/api/climate",
        "/api/media",
        "/api/appliances/oven",
        "/api/appliances/fridge",
        "/api/appletv",
        "/api/appletv/apps",
        "/api/sensors/motion",
        "/api/sensors/temperature",
        "/api/sensors/illuminance",
        "/api/sensors/summary",
        "/api/music/config",
        "/livekit/config",
    ])
    def test_reads_stay_open(self, client, path):
        resp = client.get(path)
        assert resp.status_code != 403


# ---------------------------------------------------------------------------
# Token hygiene
# ---------------------------------------------------------------------------

class TestTokenHygiene:
    def test_token_never_logged_or_forwarded(self, client, monkeypatch, caplog):
        _install_role("owner")
        forwarded = {}

        class _ChatOrchClient(_RoutedAsyncClient):
            async def post(self, url, json=None, headers=None, **kw):
                forwarded["headers"] = headers or {}
                return _FakeResponse(200, {"answer": "hi", "session_id": "s1", "metadata": {}})

        logged_events = []
        real_info = jarvis_main.logger.info

        def _capture_info(event, **kw):
            logged_events.append((event, kw))
            return real_info(event, **kw)

        monkeypatch.setattr(jarvis_main.logger, "info", _capture_info)

        secret_token = "super-secret-token-value"
        with mock.patch.object(jarvis_main.httpx, "AsyncClient", _ChatOrchClient):
            client.post("/api/chat", json={"message": "hello"}, headers=_bearer(secret_token))

        assert secret_token not in json.dumps(forwarded)
        assert not any(secret_token in str(kw) for _, kw in logged_events)


# ---------------------------------------------------------------------------
# chat-embed
# ---------------------------------------------------------------------------

class TestChatEmbedSendsGuest:
    def test_chat_embed_sends_guest(self):
        chat_embed_path = _REPO_ROOT / "apps" / "chat-embed" / "main.py"
        text = chat_embed_path.read_text(encoding="utf-8")
        assert '"mode": "guest"' in text
        assert '"mode": "owner"' not in text
