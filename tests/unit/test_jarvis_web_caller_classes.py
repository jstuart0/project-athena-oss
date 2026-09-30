"""jarvis-web caller classes and fail-closed gating (V4.1).

Without configuration every browser is refused (401 sign_in_required). The
home network (D8) is served without sign-in; the guest network (A1) always
as a guest; signed-in owners/operators and the service key as documented.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from . import _jarvis_web_harness as h

main = h.main
caller_auth = h.caller_auth
PUBLIC_ROUTES = {"GET /api/health", "GET /"}
DOCS_ROUTES = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]
GUEST_READS = [
    "GET /api/welcome", "GET /api/mode", "GET /api/climate", "GET /api/appletv", "GET /api/appletv/apps",
    "GET /api/music/config", "GET /api/music/stream/{uri:path}", "POST /api/music/search", "GET /livekit/config",
]
HOUSEHOLD_ONLY_READS = [
    "GET /api/sensors/motion", "GET /api/sensors/temperature", "GET /api/sensors/illuminance",
    "GET /api/sensors/summary", "GET /api/media", "GET /api/appliances/oven", "GET /api/appliances/fridge",
]


@pytest.fixture(autouse=True)
def _reset():
    main.mode_override = None
    yield
    main.mode_override = None
    h.configure()


@pytest.fixture
def out(monkeypatch):
    return h.install_outbound(monkeypatch)


def _http_routes(kind=None):
    from_census = main.ROUTE_CLASSIFICATION
    return sorted(
        k for k, v in from_census.items()
        if not k.startswith(("WS ", "MOUNT ")) and k != "GET /" and (kind is None or v == kind)
    )


def _home_headers(source=h.LAN, **extra):
    return {**h.via_proxy(source), **h.CSRF, **extra}


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------

def test_unconfigured_browser_is_401_everywhere(out):
    """Every non-public route answers 401 with no configuration. Floor =
    census minus public; named member GET /api/welcome."""
    h.configure({})
    routes = [k for k in _http_routes() if main.ROUTE_CLASSIFICATION[k] != "public"]
    assert "GET /api/welcome" in routes
    assert len(routes) >= 50
    c = h.client()
    for key in routes:
        resp = h.call(c, key, headers={**h.via_proxy(h.LAN), **h.CSRF})
        assert resp.status_code == 401, key
        assert resp.json() == {"detail": "sign_in_required"}, key


def test_401_has_www_authenticate_and_no_upstream(out, monkeypatch):
    """Named: a refused caller costs nothing upstream: no orchestrator,
    Home Assistant, admin or /api/auth/me call."""
    h.configure()
    auth_calls = []

    async def _spy(token):
        auth_calls.append(token)
        return h.FakeResponse(200, {"role": "owner"})

    caller_auth._set_auth_me_callable_for_tests(_spy)
    c = h.client(peer=h.INTERNET)
    for key in ["GET /api/welcome", "POST /api/chat", "GET /api/sensors/motion", "POST /api/climate/mode/{mode}"]:
        resp = h.call(c, key, headers=h.CSRF)
        assert resp.status_code == 401
        assert resp.headers["www-authenticate"] == "Jarvis"
    assert out.calls == []
    assert auth_calls == []


def test_public_surface_is_health_and_the_sign_in_page(out):
    """Named (D28): the sole anonymous surface besides the relay is
    /api/health (status only) and the 401 sign-in page; docs, static
    assets, the app list and voice health all refuse an anonymous caller."""
    h.configure()
    public = {k for k, v in main.ROUTE_CLASSIFICATION.items() if v == "public"}
    assert public == PUBLIC_ROUTES
    c = h.client(peer=h.INTERNET)
    health = c.get("/api/health")
    assert health.status_code == 200 and set(health.json()) == {"status"}
    for path in DOCS_ROUTES:
        assert c.get(path).status_code == 404, path
    for path in ("/static/jarvis-fetch.js", "/static/index.html", "/api/appletv/apps", "/api/voice/health"):
        resp = c.get(path, headers=h.CSRF)
        assert resp.status_code == 401 and resp.json() == {"detail": "sign_in_required"}, path


def test_static_assets_for_browsers_revalidated(out):
    """B8: pages and scripts carry Cache-Control: no-cache, so an open tab
    never runs a script older than the server."""
    h.configure()
    c = h.client()
    for path in ("/", "/static/jarvis-fetch.js"):
        resp = c.get(path, headers=h.via_proxy(h.LAN))
        assert resp.status_code == 200, path
        assert resp.headers["cache-control"] == "no-cache", path
    assert c.get("/static/jarvis-fetch.js", headers=h.via_proxy(h.GUEST_WIFI)).status_code == 200


def test_voice_health_for_browsers_only(out, monkeypatch):
    """Web voice is for every browser caller (user decision): the guest
    network reads voice health; anonymous callers get 401."""
    import subprocess as _subprocess

    monkeypatch.setattr(_subprocess, "run", lambda *a, **k: _subprocess.CompletedProcess(a, 0, stdout="{}"))
    h.configure()
    c = h.client()
    assert c.get("/api/voice/health", headers=h.via_proxy(h.LAN)).status_code == 200
    assert c.get("/api/voice/health", headers=h.via_proxy(h.GUEST_WIFI)).status_code == 200
    assert h.client(peer=h.INTERNET).get("/api/voice/health").status_code == 401


def test_docs_only_with_the_dev_flag(monkeypatch):
    monkeypatch.setenv("JARVIS_ENABLE_DOCS", "true")
    module = h.load_main("_jw_docs_enabled_main")
    try:
        public = {k for k, v in module.ROUTE_CLASSIFICATION.items() if v == "public"}
        assert {"GET /docs", "GET /redoc", "GET /openapi.json"} <= public
        assert h.client(peer=h.INTERNET, app=module.app).get("/openapi.json").status_code == 200
    finally:
        sys.modules.pop("_jw_docs_enabled_main", None)


def test_no_browser_access_configured_logged(captured_logs):
    h.configure({})
    assert any(e["event"] == "jarvis_no_browser_access_configured" and e["log_level"] == "error" for e in captured_logs)


def test_ws_unauthenticated_closes_1008(out):
    h.configure()
    c = h.client(peer=h.INTERNET)
    for path in ("/ma/ws", "/ma/sendspin"):
        with pytest.raises(Exception) as excinfo:
            with c.websocket_connect(path, headers={"Origin": f"http://{h.HOST}"}):
                pass
        assert getattr(excinfo.value, "code", 1008) == 1008


def test_root_page_401_html_has_no_household_data(monkeypatch):
    h.configure({**h.HOME_ENV, "JARVIS_LOGIN_URL": "https://login.example/start"})
    h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "Alice Renter", "id": 7})
    resp = h.client(peer=h.INTERNET).get("/")
    assert resp.status_code == 401
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "Jarvis is only available on the home network." in body
    assert '<html lang="en">' in body and "<h1>" in body
    assert "prefers-color-scheme" in body
    assert "<script" not in body
    assert "Alice" not in body
    assert 'href="https://login.example/start"' in body
    h.configure()
    assert "Sign in</a>" not in h.client(peer=h.INTERNET).get("/").text


# ---------------------------------------------------------------------------
# Home network (app mode, D8)
# ---------------------------------------------------------------------------

def test_home_network_is_household(monkeypatch):
    h.configure()
    out = h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "Alice Renter", "id": 7})
    c = h.client()
    welcome = c.get("/api/welcome", headers=h.via_proxy(h.LAN))
    assert welcome.status_code == 200
    assert welcome.json()["guest"]["guest_name"] == "Alice Renter"
    for key in HOUSEHOLD_ONLY_READS:
        assert h.call(c, key, headers=h.via_proxy(h.LAN)).status_code == 200, key
    resp = c.post("/api/chat", json={"message": "hi"}, headers=_home_headers())
    assert resp.status_code == 200
    body = out.orchestrator_bodies()[-1]
    assert body["caller_trust"] == "web_local"
    # The home LAN is the household during a stay: the UI shows the guest,
    # but the model never addresses a home-LAN caller as the guest.
    assert "guest_name" not in body["context"]


def test_pod_cidr_forged_xff_not_local(out):
    """Named: a caller-supplied left-hand hop never counts; the proxy's
    appended hop (a pod address) isn't home."""
    h.configure()
    resp = h.client().get("/api/welcome", headers=h.via_proxy("192.0.2.5, 10.244.4.1"))
    assert resp.status_code == 401


def test_untrusted_peer_xff_ignored(out):
    h.configure()
    assert h.client(peer=h.INTERNET).get("/api/welcome", headers=h.via_proxy(h.LAN)).status_code == 401


def test_tailnet_single_host_entry_is_local(out):
    h.configure({**h.HOME_ENV, "JARVIS_LOCAL_NETWORKS": "192.0.2.0/24,10.244.4.1/32"})
    assert h.client().get("/api/welcome", headers=h.via_proxy("10.244.4.1")).status_code == 200


def test_local_disabled_when_trusted_empty(out, captured_logs):
    h.configure({**h.HOME_ENV, "TRUSTED_PROXY_CIDRS": ""})
    assert h.client(peer=h.LAN).get("/api/welcome").status_code == 401
    assert any(e["event"] == "jarvis_local_without_trusted_proxy" for e in captured_logs)


def test_local_without_allowed_hosts_disabled(out, captured_logs):
    h.configure({**h.HOME_ENV, "JARVIS_ALLOWED_HOSTS": ""})
    assert h.client().get("/api/welcome", headers=h.via_proxy(h.LAN)).status_code == 401
    assert any(e["event"] == "jarvis_local_without_allowed_hosts" for e in captured_logs)


def test_local_entry_containing_pod_ip_dropped(out):
    s = h.configure(own_ips=(caller_auth.throttle.parse_ip("192.0.2.77"),))
    assert s.local_networks == ()
    assert h.client().get("/api/welcome", headers=h.via_proxy(h.LAN)).status_code == 401


def test_excluded_node_ip_keeps_lan_cidr(out):
    """Named (A4): the pod's own IP is excluded, so the LAN entry is kept;
    the excluded address itself is never home."""
    own = caller_auth.throttle.parse_ip("192.0.2.14")
    s = h.configure({**h.HOME_ENV, "JARVIS_LOCAL_EXCLUDE": "192.0.2.14/32"}, own_ips=(own,))
    assert [str(n) for n in s.local_networks] == ["192.0.2.0/24"]
    c = h.client()
    assert c.get("/api/welcome", headers=h.via_proxy("192.0.2.50")).status_code == 200
    assert c.get("/api/welcome", headers=h.via_proxy("192.0.2.14")).status_code == 401


def test_local_exclude_subtracts(out):
    h.configure({**h.HOME_ENV, "JARVIS_LOCAL_EXCLUDE": "192.0.2.10/32"})
    assert h.client().get("/api/welcome", headers=h.via_proxy("192.0.2.10")).status_code == 401


def test_broad_local_inside_trusted_dropped(out):
    s = h.configure({**h.HOME_ENV, "JARVIS_LOCAL_NETWORKS": "192.0.2.0/24,10.244.0.0/16,10.0.0.0/8"})
    assert [str(n) for n in s.local_networks] == ["192.0.2.0/24"]
    assert h.client().get("/api/welcome", headers=h.via_proxy("10.244.3.3")).status_code == 401


@pytest.mark.parametrize("header", ["Cf-Connecting-Ip", "Cf-Ray", "cf-ray"])
def test_cf_never_classifies_local(out, header):
    h.configure()
    headers = h.via_proxy(h.LAN, **{header: "abc"})
    assert h.client().get("/api/welcome", headers=headers).status_code == 401


def test_cf_empty_value_counts_as_absent(out):
    h.configure()
    headers = h.via_proxy(h.LAN, **{"Cf-Ray": "", "Cf-Connecting-Ip": "  "})
    assert h.client().get("/api/welcome", headers=headers).status_code == 200


@pytest.mark.parametrize("host, expected", [
    ("JARVIS.example:8443", 200),
    ("jarvis.example.", 200),
    ("evil.example", 401),
])
def test_host_allowlist_rows(out, host, expected):
    h.configure()
    assert h.client(host=host).get("/api/welcome", headers=h.via_proxy(h.LAN)).status_code == expected


def test_rebinding_host_not_local(out):
    """Named (A8 M3): only Host counts, never X-Forwarded-Host."""
    h.configure()
    headers = h.via_proxy(h.LAN, **{"X-Forwarded-Host": h.HOST})
    assert h.client(host="evil.example").get("/api/welcome", headers=headers).status_code == 401


def test_missing_host_not_local():
    h.configure()
    cls, _ = caller_auth.classify_network(h.PROXY, {"x-forwarded-for": h.LAN}, caller_auth.SETTINGS)
    assert cls is None


def test_service_caller_ignores_host_allowlist(out):
    h.configure()
    c = h.client(peer="10.2.2.2", host="athena-jarvis-web.athena-prod.svc")
    assert c.get("/api/sensors/motion", headers={"X-Service-Key": h.SERVICE_KEY}).status_code == 200


# ---------------------------------------------------------------------------
# Direct-client mode (L1', A3)
# ---------------------------------------------------------------------------

def _route_file(tmp_path, gateway_hex="0100000A"):
    route = tmp_path / "route"
    route.write_text(
        "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
        f"eth0\t00000000\t{gateway_hex}\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    return str(route), str(tmp_path / "missing6")


DIRECT_ENV = {"SERVICE_API_KEY": h.SERVICE_KEY, "JARVIS_DIRECT_CLIENTS": "true",
              "JARVIS_LOCAL_NETWORKS": "192.0.2.0/24", "JARVIS_ALLOWED_HOSTS": h.HOST}


def test_direct_client_mode_peer_is_candidate(out, tmp_path):
    route, route6 = _route_file(tmp_path)
    h.configure(DIRECT_ENV, route_file=route, route6_file=route6)
    assert h.client(peer=h.LAN).get("/api/welcome").status_code == 200
    assert h.client(peer=h.INTERNET).get("/api/welcome").status_code == 401


def test_direct_mode_ignores_xff(out, tmp_path):
    """Named: in direct mode the TCP peer is the candidate; XFF is never read."""
    route, route6 = _route_file(tmp_path)
    h.configure(DIRECT_ENV, route_file=route, route6_file=route6)
    assert h.client(peer="198.51.100.7").get("/api/welcome", headers=h.via_proxy("192.0.2.50")).status_code == 401


def test_direct_mode_gateway_peer_never_local(out, tmp_path):
    """Named (A3): the default gateway sits inside LOCAL (excluded, so the
    entry survives) yet a request from it is never home."""
    route, route6 = _route_file(tmp_path, gateway_hex="010200C0")  # 192.0.2.1
    s = h.configure({**DIRECT_ENV, "JARVIS_LOCAL_EXCLUDE": "192.0.2.1/32"}, route_file=route, route6_file=route6)
    assert [str(g) for g in s.gateways] == ["192.0.2.1"]
    assert h.client(peer="192.0.2.1").get("/api/welcome").status_code == 401
    assert h.client(peer="192.0.2.60").get("/api/welcome").status_code == 200


def test_direct_mode_entry_containing_gateway_dropped(out, tmp_path, captured_logs):
    route, route6 = _route_file(tmp_path, gateway_hex="010200C0")
    s = h.configure(DIRECT_ENV, route_file=route, route6_file=route6)
    assert s.local_networks == ()
    assert any(e["event"] == "jarvis_local_network_contains_gateway" for e in captured_logs)


def test_direct_mode_unreadable_route_table_disables_local(out, tmp_path):
    s = h.configure(DIRECT_ENV, route_file=str(tmp_path / "nope"), route6_file=str(tmp_path / "nope6"))
    assert not s.local_enabled


def test_direct_mode_with_trusted_proxies_refused(out, tmp_path):
    route, route6 = _route_file(tmp_path)
    s = h.configure({**DIRECT_ENV, "TRUSTED_PROXY_CIDRS": "10.0.0.0/8"}, route_file=route, route6_file=route6)
    assert not s.local_enabled


def test_direct_mode_without_allowed_hosts_disabled(tmp_path):
    route, route6 = _route_file(tmp_path)
    s = h.configure({**DIRECT_ENV, "JARVIS_ALLOWED_HOSTS": ""}, route_file=route, route6_file=route6)
    assert not s.local_enabled


def test_direct_mode_in_k8s_without_ack_exits():
    with pytest.raises(SystemExit):
        caller_auth.load_settings({**DIRECT_ENV, "KUBERNETES_SERVICE_HOST": "10.96.0.1"}, own_ips=())


def test_direct_mode_in_k8s_with_ack_starts(tmp_path):
    route, route6 = _route_file(tmp_path)
    s = caller_auth.load_settings(
        {**DIRECT_ENV, "KUBERNETES_SERVICE_HOST": "10.96.0.1", "JARVIS_DIRECT_CLIENTS_ACK_SOURCE_PRESERVED": "true"},
        own_ips=(), route_file=route, route6_file=route6,
    )
    assert s.local_enabled


# ---------------------------------------------------------------------------
# Owner-only routes during a stay (D22)
# ---------------------------------------------------------------------------

def test_home_owner_route_during_stay_is_403_guest_stay_active(monkeypatch):
    h.configure({**h.HOME_ENV, "JARVIS_SIGNIN_URL": "https://jarvis-signin.example"})
    h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "G", "id": 1})
    c = h.client()
    resp = c.post("/api/climate/mode/heat", headers=_home_headers())
    assert resp.status_code == 403
    assert resp.json() == {"detail": "guest_stay_active"}
    caps = c.get("/api/welcome", headers=h.via_proxy(h.LAN)).json()["capabilities"]
    assert caps["control"] is False and caps["control_reason"] == "guest_stay"
    assert caps["signin_url"] == "https://jarvis-signin.example"


def test_home_owner_route_when_vacant_allowed(monkeypatch):
    h.configure()
    h.install_outbound(monkeypatch)
    resp = h.client().post("/api/climate/mode/heat", headers=_home_headers())
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Guest network (A1)
# ---------------------------------------------------------------------------

def test_guest_network_vacant_is_guest_not_owner(monkeypatch):
    """Named: the house is vacant (the resolver would say owner); a
    guest-network caller is still guest upstream and refused control."""
    h.configure()
    out = h.install_outbound(monkeypatch)
    c = h.client()
    c.post("/api/chat", json={"message": "hi"}, headers=_home_headers(h.GUEST_WIFI))
    assert out.orchestrator_bodies()[-1]["mode"] == "guest"
    assert out.orchestrator_bodies()[-1]["caller_trust"] == "web_guest_net"
    resp = c.post("/api/climate/mode/heat", headers=_home_headers(h.GUEST_WIFI))
    assert resp.status_code == 403 and resp.json() == {"detail": "guest_stay_active"}
    caps = c.get("/api/welcome", headers=h.via_proxy(h.GUEST_WIFI)).json()["capabilities"]
    assert caps["control"] is False and caps["control_reason"] == "guest_network"
    assert "signin_url" not in caps


def test_guest_network_forced_owner_still_guest(monkeypatch):
    h.configure()
    out = h.install_outbound(monkeypatch)
    main.mode_override = "owner"
    c = h.client()
    c.post("/api/chat", json={"message": "hi"}, headers=_home_headers(h.GUEST_WIFI))
    assert out.orchestrator_bodies()[-1]["mode"] == "guest"
    assert c.post("/api/climate/mode/heat", headers=_home_headers(h.GUEST_WIFI)).status_code == 403


def test_guest_network_reads_limited(monkeypatch):
    """Floor 9 guest reads answer 200; household-only reads answer 403
    guest_network (named member GET /api/sensors/motion)."""
    h.configure()
    h.install_outbound(monkeypatch)
    c = h.client()
    assert len(GUEST_READS) >= 9
    for key in GUEST_READS:
        assert h.call(c, key, headers=_home_headers(h.GUEST_WIFI)).status_code == 200, key
    for key in HOUSEHOLD_ONLY_READS:
        resp = h.call(c, key, headers=h.via_proxy(h.GUEST_WIFI))
        assert resp.status_code == 403 and resp.json() == {"detail": "guest_network"}, key


def test_guest_wins_overlap(out, captured_logs):
    s = h.configure({**h.HOME_ENV, "JARVIS_GUEST_NETWORKS": "192.0.2.128/25"})
    assert any(e["event"] == "jarvis_guest_network_overlaps_local" for e in captured_logs)
    caller_class, _ = caller_auth.classify_network(h.PROXY, {"x-forwarded-for": "192.0.2.200", "host": h.HOST}, s)
    assert caller_class == caller_auth.CLASS_GUEST_NET


# ---------------------------------------------------------------------------
# Service key, Bearer
# ---------------------------------------------------------------------------

def test_service_key_household_read_only(out):
    h.configure()
    c = h.client(peer="10.2.2.2")
    headers = {"X-Service-Key": h.SERVICE_KEY}
    assert c.get("/api/sensors/motion", headers=headers).status_code == 200
    assert c.post("/api/climate/mode/heat", headers={**headers, **h.CSRF}).status_code == 401
    assert c.get("/api/welcome", headers=headers).status_code == 401


def test_service_key_on_chat_is_not_service(out):
    h.configure()
    resp = h.client(peer="10.2.2.2").post(
        "/api/chat", json={"message": "hi"}, headers={"X-Service-Key": h.SERVICE_KEY, **h.CSRF}
    )
    assert resp.status_code == 401


def test_placeholder_service_key_refused(out):
    h.configure({**h.HOME_ENV, "SERVICE_API_KEY": "dev-service-key-change-in-production"})
    c = h.client(peer="10.2.2.2")
    assert c.get("/api/sensors/motion", headers={"X-Service-Key": "dev-service-key-change-in-production"}).status_code == 401


def test_owner_bearer_reads_everything(out):
    h.configure()
    h.install_role("owner")
    c = h.client(peer=h.INTERNET)
    headers = {"Authorization": "Bearer t"}
    assert c.get("/api/sensors/motion", headers=headers).status_code == 200
    assert c.post("/api/climate/mode/heat", headers={**headers, **h.CSRF}).status_code == 200


def test_auth_cache_bounded(out, monkeypatch):
    h.configure()
    h.install_role("owner")
    monkeypatch.setattr(caller_auth, "_AUTH_CACHE_MAX_ENTRIES", 3)
    monkeypatch.setattr(caller_auth, "_auth_attempts", caller_auth.throttle.SlidingWindowLimiter(per_minute=1000))
    c = h.client(peer=h.INTERNET)
    for i in range(6):
        c.get("/api/sensors/motion", headers={"Authorization": f"Bearer token-{i}"})
    assert len(caller_auth._auth_cache) == 3


def test_uncached_bearer_attempts_throttled(out):
    h.configure()
    calls = []

    async def _count(token):
        calls.append(token)
        return h.FakeResponse(401, {})

    caller_auth._set_auth_me_callable_for_tests(_count)
    c = h.client(peer=h.INTERNET)
    for i in range(12):
        c.get("/api/sensors/motion", headers={"Authorization": f"Bearer guess-{i}"})
    assert len(calls) == 10


# ---------------------------------------------------------------------------
# Chat sessions are bound to the browser (xander P4 item 1)
# ---------------------------------------------------------------------------

def test_presented_foreign_session_id_is_replaced(out):
    h.configure()
    alice = h.client()
    first = alice.post("/api/chat", json={"message": "hi"}, headers=_home_headers())
    sid = first.json()["session_id"]
    again = alice.post("/api/chat", json={"message": "again", "session_id": sid}, headers=_home_headers())
    assert again.json()["session_id"] == sid
    bob = h.client()
    stolen = bob.post("/api/chat", json={"message": "what did alice say", "session_id": sid}, headers=_home_headers())
    assert stolen.json()["session_id"] != sid
    assert out.orchestrator_bodies()[-1]["session_id"] != sid


def test_stream_emits_bound_session_first(out):
    h.configure()
    c = h.client()
    resp = c.post("/api/chat/stream", json={"message": "hi"}, headers=_home_headers())
    first = resp.text.split("\n\n", 1)[0]
    assert '"stage": "session"' in first
    sid = first.split('"session_id": "', 1)[1].split('"', 1)[0]
    assert out.orchestrator_bodies()[-1]["session_id"] == sid


# ---------------------------------------------------------------------------
# JARVIS_PUBLIC_MODE is removed (D23, A8 M1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, exits, event", [
    ("household", True, "jarvis_public_mode_removed"),
    ("guest", False, "jarvis_public_mode_deprecated"),
    ("false", False, "jarvis_public_mode_deprecated"),
    ("", False, None),
])
def test_public_mode_env(value, exits, event):
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(h.BACKEND)!r})
        os.environ["JARVIS_PUBLIC_MODE"] = {value!r}
        import caller_auth
        print("STARTED")
    """)
    env = {"PATH": "/usr/bin:/bin", "SERVICE_API_KEY": h.SERVICE_KEY, "PYTHONPATH": str(h.REPO_ROOT / "src")}
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=env, timeout=60)
    output = proc.stdout + proc.stderr
    if exits:
        assert proc.returncode != 0 and "STARTED" not in output
    else:
        assert proc.returncode == 0 and "STARTED" in output
    for candidate in ("jarvis_public_mode_removed", "jarvis_public_mode_deprecated"):
        assert (candidate in output) == (candidate == event), candidate


def test_public_mode_not_set_anywhere():
    """A8 M1 population: no deployment artifact or doc still sets or
    documents JARVIS_PUBLIC_MODE, except the removal note."""
    roots = [h.REPO_ROOT / "manifests", h.REPO_ROOT / "docs"]
    files = [h.REPO_ROOT / ".env.example", *sorted((h.REPO_ROOT / "apps").rglob(".env.example"))]
    for root in roots:
        files.extend(p for p in root.rglob("*") if p.is_file() and p.suffix in {".md", ".yaml", ".yml", ".example", ".txt"})
    # tessa C5: the apps' own manifests and the repo-root docs too
    apps_yaml = sorted(p for p in (h.REPO_ROOT / "apps").rglob("*")
                       if p.suffix in {".yaml", ".yml"} and "node_modules" not in p.parts)
    root_md = sorted(h.REPO_ROOT.glob("*.md"))
    assert h.REPO_ROOT / "apps" / "jarvis-web" / "k8s" / "deployment.yaml" in apps_yaml
    assert h.REPO_ROOT / "README.md" in root_md and h.REPO_ROOT / "CLAUDE.md" in root_md
    files.extend(apps_yaml + root_md)
    assert len(files) >= 10
    hits = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if "JARVIS_PUBLIC_MODE" in line and "removed" not in line:
                hits.append(f"{path.relative_to(h.REPO_ROOT)}:{number}")
    assert hits == []


# ---------------------------------------------------------------------------
# tessa C6 (Lows)
# ---------------------------------------------------------------------------

def test_chat_key_cookie_attributes_and_session_mac(out):
    """The browser-binding cookie is HttpOnly and SameSite=strict, Secure
    whenever the page is https; the minted id's mac is 24 hex."""
    import re

    h.configure()
    plain = h.client().post("/api/chat", json={"message": "hi"}, headers=_home_headers())
    cookie = plain.headers["set-cookie"]
    assert cookie.startswith("jarvis_chat_key=")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Secure" not in cookie
    assert re.fullmatch(r"[0-9a-f]{32}\.[0-9a-f]{24}", plain.json()["session_id"])
    secure = h.client().post("/api/chat", json={"message": "hi"}, headers=_home_headers(**{"X-Forwarded-Proto": "https"}))
    assert "Secure" in secure.headers["set-cookie"]


def test_raw_uppercase_host_still_home():
    """tessa DH-e: Host compares case-insensitively even when the header
    arrives raw (not normalised by a client library)."""
    from starlette.datastructures import Headers

    s = h.configure()
    raw = Headers(raw=[(b"host", h.HOST.upper().encode()), (b"x-forwarded-for", h.LAN.encode())])
    cls, _ = caller_auth.classify_network(h.PROXY, raw, s)
    assert cls == caller_auth.CLASS_LOCAL


def test_guest_network_welcome_capabilities(monkeypatch):
    """tessa GN-c: the guest network sees view-only capabilities, no
    household reads, and no sign-in link (that's for a guest stay only)."""
    h.configure({**h.HOME_ENV, "JARVIS_SIGNIN_URL": "https://signin.example/", "JARVIS_LOGOUT_URL": "https://x/logout"})
    h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "Alice Renter", "id": 7})
    caps = h.client().get("/api/welcome", headers=h.via_proxy(h.GUEST_WIFI)).json()["capabilities"]
    assert caps == {"household_read": False, "control": False, "voice": True, "control_reason": "guest_network",
                    "signed_in": False}
