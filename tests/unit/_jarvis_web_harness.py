"""Shared harness for the jarvis-web caller-class tests.

Loads apps/jarvis-web/backend/main.py under a private module name (several
services share the filename main.py) and exposes helpers to reconfigure
caller_auth's settings per test, drive requests from a chosen TCP peer and
Host, and fake every outbound httpx call.

Nothing here references a symbol the caller-class work adds, so importing
it collects on an unrepaired tree; tests use new symbols in their bodies.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Dict, Optional

from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "apps" / "jarvis-web" / "backend"
FRONTEND = REPO_ROOT / "apps" / "jarvis-web" / "frontend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
SERVICE_KEY = "test-key-jarvis-web-harness-0123456789"
os.environ.setdefault("SERVICE_API_KEY", SERVICE_KEY)
SERVICE_KEY = os.environ["SERVICE_API_KEY"]

import caller_auth  # noqa: E402


def load_main(name: str = "_jw_harness_main"):
    """Exec a fresh copy of jarvis-web's main.py (it reads caller_auth's
    settings at import, e.g. for CORS)."""
    spec = importlib.util.spec_from_file_location(name, BACKEND / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


main = load_main()

HOST = "jarvis.example"
PROXY = "10.1.1.1"
LAN = "192.0.2.10"
GUEST_WIFI = "198.51.100.20"
INTERNET = "203.0.113.9"

HOME_ENV = {
    "SERVICE_API_KEY": SERVICE_KEY,
    "TRUSTED_PROXY_CIDRS": "10.0.0.0/8",
    "JARVIS_LOCAL_NETWORKS": "192.0.2.0/24",
    "JARVIS_GUEST_NETWORKS": "198.51.100.0/24",
    "JARVIS_ALLOWED_HOSTS": HOST,
}


def configure(env: Optional[Dict[str, str]] = None, **kwargs):
    """Replace caller_auth's settings (and reset caches/limiters)."""
    caller_auth._reset_for_tests()
    settings_env = dict(HOME_ENV if env is None else env)
    settings_env.setdefault("SERVICE_API_KEY", SERVICE_KEY)
    if not hasattr(caller_auth, "_configure_for_tests"):
        return None  # an unrepaired tree: let the behaviour itself be judged
    return caller_auth._configure_for_tests(settings_env, **kwargs)


def client(peer: str = PROXY, host: str = HOST, app=None) -> TestClient:
    return TestClient(app or main.app, base_url=f"http://{host}", client=(peer, 50000))


def via_proxy(source: str, **extra) -> Dict[str, str]:
    """Headers as the trusted proxy would send them for `source`."""
    headers = {"X-Forwarded-For": source}
    headers.update(extra)
    return headers


CSRF = {"X-Jarvis-Request": "1"}


class FakeResponse:
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


class Outbound:
    """Records every outbound httpx call jarvis-web makes."""

    def __init__(self, guest: Optional[dict] = None):
        self.calls = []
        self.guest = guest

    def factory(self):
        outer = self

        class _Client:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, *a, **kw):
                outer.calls.append(("GET", url, kw))
                if "current-guest" in url:
                    return FakeResponse(200, outer.guest or {"has_guest": False})
                if "room-tv/internal" in url:
                    return FakeResponse(200, [])
                if url.endswith("/api/states"):
                    return FakeResponse(200, [{
                        "entity_id": "sensor.kitchen_motion", "state": "on",
                        "attributes": {"friendly_name": "Kitchen Motion", "device_class": "motion"},
                    }])
                return FakeResponse(200, {"state": "on", "attributes": {"hvac_modes": ["heat", "off"]}})

            async def post(self, url, *a, json=None, **kw):
                outer.calls.append(("POST", url, {"json": json, **kw}))
                if url.endswith("/query"):
                    return FakeResponse(200, {"answer": "ok", "session_id": (json or {}).get("session_id"), "metadata": {}})
                return FakeResponse(200, {"success": True, "token": "t", "room_name": "r", "livekit_url": "wss://x"})

            async def delete(self, url, *a, **kw):
                outer.calls.append(("DELETE", url, kw))
                return FakeResponse(200, {"success": True})

            def stream(self, method, url, json=None, **kw):
                outer.calls.append(("STREAM", url, {"json": json, **kw}))

                class _Ctx:
                    headers = {"Content-Type": "audio/mpeg"}

                    async def aiter_bytes(self_inner, chunk_size=8192):
                        yield b"audio"

                    async def __aenter__(self_inner):
                        return self_inner

                    async def __aexit__(self_inner, *a):
                        return False

                    async def aiter_text(self_inner):
                        yield 'data: {"stage": "answer_chunk", "content": "hi"}\n\n'
                        yield 'data: {"stage": "complete"}\n\n'

                return _Ctx()

        return _Client

    def orchestrator_bodies(self):
        return [kw.get("json") for method, url, kw in self.calls if url.endswith("/query") or url.endswith("/query/stream")]


def install_outbound(monkeypatch, guest: Optional[dict] = None) -> Outbound:
    out = Outbound(guest)
    monkeypatch.setattr(main.httpx, "AsyncClient", out.factory())
    monkeypatch.setattr(caller_auth, "get_admin_url", lambda: "http://admin.local:8080")
    monkeypatch.setattr(main, "HA_TOKEN", "fake-ha-token")
    monkeypatch.setattr(main, "OVEN_ENTITY", "water_heater.test_oven")
    monkeypatch.setattr(main, "FRIDGE_ENTITY", "water_heater.test_fridge")
    monkeypatch.setattr(main, "FREEZER_ENTITY", "water_heater.test_freezer")
    return out


def install_role(role: str):
    async def _call(token):
        return FakeResponse(200, {"role": role})
    caller_auth._set_auth_me_callable_for_tests(_call)


PATH_VALUES = {"entity_id": "media_player.tv", "mode": "heat", "app_name": "Netflix", "action": "on",
               "room_name": "room1", "session_id": "s1", "uri:path": "spotify%3A%2F%2Ftrack%2F1"}

BODIES = {
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
    "POST /api/music/search": {"query": "jazz"},
    "POST /api/chat": {"message": "hello"},
    "POST /api/chat/stream": {"message": "hello"},
    "POST /api/voice/synthesize": {"text": "hi"},
}

QUERY = {"GET /api/geocode/reverse": {"lat": 1, "lon": 2}, "GET /api/geocode/search": {"q": "x"}}


def request_for(route_key: str):
    method, template = route_key.split(" ", 1)
    path = template
    for name, value in PATH_VALUES.items():
        path = path.replace("{" + name + "}", value)
    return method, path, BODIES.get(route_key), QUERY.get(route_key)


def call(test_client, route_key: str, headers=None):
    method, path, body, params = request_for(route_key)
    return test_client.request(method, path, json=body, params=params, headers=headers or {})
