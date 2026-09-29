"""CORS and CSRF for jarvis-web (V4.4, D21).

Home-network and signed-in browsers are trusted by where they are or what
they carry, so a cross-site page must not be able to make them act. No CORS
by default; every mutating browser request needs X-Jarvis-Request: 1, which
a cross-origin page can't send without a preflight jarvis-web never grants.
"""
from __future__ import annotations

import re

import pytest

from . import _jarvis_web_harness as h

main = h.main
caller_auth = h.caller_auth
FRONTEND_FILES = sorted(list(h.FRONTEND.glob("*.html")) + list(h.FRONTEND.glob("*.js")))
BASE_FETCH_COUNT = 29  # fetch( sites in index.html, livekit-client.js and music-player.js at base


@pytest.fixture(autouse=True)
def _reset():
    yield
    h.configure()


@pytest.fixture
def out(monkeypatch):
    h.configure()
    return h.install_outbound(monkeypatch)


def _home(**extra):
    return {**h.via_proxy(h.LAN), **extra}


def test_missing_header_is_403_reload_required(out):
    """Named: a home caller's mutating request without the header."""
    resp = h.client().post("/api/climate/mode/heat", headers=_home())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "reload_required"}
    assert not [c for c in out.calls if c[0] == "POST"]


def test_mutating_with_header_succeeds(out):
    assert h.client().post("/api/climate/mode/heat", headers=_home(**h.CSRF)).status_code == 200


def test_wrong_header_value_refused(out):
    assert h.client().post("/api/climate/mode/heat", headers=_home(**{"X-Jarvis-Request": "yes"})).status_code == 403


AUTH_BEFORE_CSRF = {
    "relay_chat": "POST /api/chat",
    "browser": "DELETE /api/session/current",
    "guest_read": "POST /api/music/search",
    "owner_only": "POST /api/climate/mode/{mode}",
}


@pytest.mark.parametrize("route_class", sorted(AUTH_BEFORE_CSRF))
def test_unauthenticated_post_is_401_before_reload_required(out, route_class):
    """tessa C2 (named: guest_read): on every gated class a caller with
    neither credentials nor the header is told to sign in (401), never to
    reload (403): authentication is decided before the CSRF header."""
    key = AUTH_BEFORE_CSRF[route_class]
    assert main.ROUTE_CLASSIFICATION[key] == route_class
    resp = h.call(h.client(peer=h.INTERNET), key)
    assert resp.status_code == 401 and resp.json() == {"detail": "sign_in_required"}, key
    assert out.calls == []


def test_every_mutating_gated_route_requires_the_header(out):
    """Cross-check over the census: every non-GET route outside `public`
    refuses a home caller without the header."""
    keys = [k for k, v in main.ROUTE_CLASSIFICATION.items()
            if not k.startswith(("GET ", "WS ", "MOUNT ")) and v != "public"]
    assert len(keys) >= 30
    c = h.client()
    for key in keys:
        resp = h.call(c, key, headers=_home())
        assert resp.status_code == 403 and resp.json()["detail"] in {"reload_required", "guest_stay_active"}, key


def test_service_post_exempt(out):
    """The service caller is exempt (it's never a browser); it only reads,
    so the exemption is pinned on the dependency itself."""
    request = type("R", (), {"method": "POST", "headers": {}, "url": type("U", (), {"path": "/x"})()})()
    caller = caller_auth.Caller(caller_auth.CLASS_SERVICE, "guest")
    caller_auth._require_csrf_header(request, caller)


def test_no_cors_headers_by_default(out):
    resp = h.client().options(
        "/api/chat",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "x-jarvis-request"},
    )
    assert "access-control-allow-origin" not in resp.headers
    assert "access-control-allow-headers" not in resp.headers


def _app_with_cors(origins):
    h.configure({**h.HOME_ENV, "JARVIS_CORS_ORIGINS": origins})
    return h.load_main("_jw_cors_main").app


def test_listed_origin_preflight_allowed(monkeypatch):
    app = _app_with_cors("https://embed.example")
    resp = h.client(app=app).options(
        "/api/chat",
        headers={"Origin": "https://embed.example", "Access-Control-Request-Method": "POST",
                 "Access-Control-Request-Headers": "x-jarvis-request,content-type"},
    )
    assert resp.headers["access-control-allow-origin"] == "https://embed.example"
    assert "x-jarvis-request" in resp.headers["access-control-allow-headers"].lower()
    # tessa CS-j: a listed origin is a same-household origin and sends credentials
    assert resp.headers["access-control-allow-credentials"] == "true"
    denied = h.client(app=app).options(
        "/api/chat", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert denied.headers.get("access-control-allow-origin") != "https://evil.example"


@pytest.mark.parametrize("value", ["*", "null", "*,https://embed.example"])
def test_cors_star_or_null_refused(value, captured_logs):
    s = h.configure({**h.HOME_ENV, "JARVIS_CORS_ORIGINS": value})
    assert "*" not in s.cors_origins and "null" not in s.cors_origins
    assert any(e["event"] == "jarvis_cors_origin_refused" for e in captured_logs)


def _ws_request(origin, host=h.HOST):
    headers = {"host": host}
    if origin is not None:
        headers["origin"] = origin
    return type("WS", (), {"headers": headers})()


def test_ws_origin_rule(out):
    """xander L2 (named): the Origin must be in JARVIS_ALLOWED_HOSTS; a
    matching Host header is not enough, since the client sets it."""
    assert caller_auth.ws_origin_allowed(_ws_request(f"https://{h.HOST}"))
    assert caller_auth.ws_origin_allowed(_ws_request(f"https://{h.HOST.upper()}:443"))
    assert not caller_auth.ws_origin_allowed(_ws_request("http://other.host", host="other.host"))
    assert not caller_auth.ws_origin_allowed(_ws_request("https://evil.example"))
    assert not caller_auth.ws_origin_allowed(_ws_request(None))


@pytest.mark.parametrize("path", ["/ma/ws", "/ma/sendspin"])
def test_ws_foreign_or_missing_origin_closed_for_home_caller(out, path):
    c = h.client()
    for headers in ({"X-Forwarded-For": h.LAN, "Origin": "https://evil.example"}, {"X-Forwarded-For": h.LAN}):
        with pytest.raises(Exception):
            with c.websocket_connect(path, headers=headers):
                pass


def test_frontend_fetches_use_jarvisFetch():
    """Every fetch( in the frontend goes through jarvisFetch; only
    jarvis-fetch.js itself calls the platform fetch. Floor = the base
    count; named member loadModeState."""
    raw = re.compile(r"(?<![A-Za-z_.])fetch\(")
    wrapped = 0
    for path in FRONTEND_FILES:
        text = path.read_text(encoding="utf-8")
        if path.name == "jarvis-fetch.js":
            continue
        assert not raw.search(text), path.name
        wrapped += len(re.findall(r"jarvisFetch\(", text))
    assert wrapped >= BASE_FETCH_COUNT
    index = (h.FRONTEND / "index.html").read_text(encoding="utf-8")
    body = index.split("async function loadModeState()", 1)[1].split("async function", 1)[0]
    assert "jarvisFetch(" in body
    assert index.index('src="/static/jarvis-fetch.js"') < index.index('src="/static/livekit-client.js"')


def test_jarvis_fetch_sets_header_and_manual_redirect():
    source = (h.FRONTEND / "jarvis-fetch.js").read_text(encoding="utf-8")
    assert "headers.set('X-Jarvis-Request', '1')" in source
    assert "options.redirect = 'manual'" in source
    assert "reload_required" in source and "Jarvis was updated. Reload the page." in source
