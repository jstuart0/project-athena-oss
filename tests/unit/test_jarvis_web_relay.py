"""The embed relay (V6.1, D16, D9).

chat-embed relays an anonymous website visitor's chat to jarvis-web with
X-Jarvis-Relay-Key and X-Jarvis-Relay-Client. On the chat routes the relay
is resolved first: a valid key is always the public audience, whatever
edge, Bearer or network evidence rides along; a wrong key is 401. Limits
are per visitor and global.
"""
from __future__ import annotations

import pytest

from . import _jarvis_web_harness as h

caller_auth = h.caller_auth
RELAY_KEY = "relay-key-for-tests-0123456789abcdef"
EDGE_SECRET = "edge-attestation-current-7f3a9c2e1b8d4f6a"
RELAY_ENV = {**h.HOME_ENV, "JARVIS_RELAY_KEY": RELAY_KEY}


@pytest.fixture(autouse=True)
def _reset():
    h.configure(RELAY_ENV)
    yield
    h.configure()


@pytest.fixture
def out(monkeypatch):
    return h.install_outbound(monkeypatch, guest={"has_guest": True, "guest_name": "Alice Renter", "id": 7})


def _relay(visitor="203.0.113.50", key=RELAY_KEY, **extra):
    headers = {"X-Jarvis-Relay-Key": key, "X-Jarvis-Relay-Client": visitor}
    headers.update(extra)
    return headers


def _chat(headers, path="/api/chat", peer="10.3.3.3", message="hi", session_id=None):
    body = {"message": message}
    if session_id:
        body["session_id"] = session_id
    return h.client(peer=peer).post(path, json=body, headers=headers)


def test_relay_is_always_public(out):
    resp = _chat(_relay(visitor=h.LAN, **{"X-Forwarded-For": h.LAN}), peer=h.PROXY)
    assert resp.status_code == 200
    body = out.orchestrator_bodies()[-1]
    assert body["caller_trust"] == "web_public"
    assert body["mode"] == "guest"
    assert "guest_name" not in body["context"] and "guest_id" not in body["context"]


def test_relay_key_beats_edge_and_bearer(out):
    """Named: a valid relay key plus valid edge-home, edge-auth or owner
    Bearer evidence is still the public audience."""
    h.configure({**RELAY_ENV, "JARVIS_EDGE_ATTESTATION_SECRET": EDGE_SECRET, "JARVIS_HOUSEHOLD_GROUPS": "household"})
    h.install_role("owner")
    for extra in (
        {"X-Forwarded-For": h.LAN, "X-Jarvis-Edge-Class": "home", "X-Jarvis-Edge-Attestation": EDGE_SECRET},
        {"X-Forwarded-For": h.INTERNET, "X-Jarvis-Edge-Class": "authenticated", "X-Jarvis-Edge-Attestation": EDGE_SECRET,
         "X-authentik-username": "alice", "X-authentik-groups": "household"},
        {"Authorization": "Bearer owner-token"},
    ):
        resp = _chat(_relay(**extra), peer=h.PROXY)
        assert resp.status_code == 200
        body = out.orchestrator_bodies()[-1]
        assert body["caller_trust"] == "web_public"
        assert "guest_name" not in body["context"]


def test_wrong_relay_key_is_401_no_fallthrough(out, captured_logs):
    headers = _relay(key="x" * 36, **{"X-Forwarded-For": h.LAN, **h.CSRF})
    resp = _chat(headers, peer=h.PROXY)
    assert resp.status_code == 401 and resp.json() == {"detail": "relay_key_invalid"}
    assert out.orchestrator_bodies() == []
    errors = [e for e in captured_logs if e["event"] == "jarvis_relay_key_invalid"]
    assert errors and "x" * 36 not in repr(errors)


def test_relay_disabled_without_key(out):
    h.configure(h.HOME_ENV)
    assert _chat(_relay()).status_code == 401


@pytest.mark.parametrize("visitor", [None, "", "not-an-ip", "1" * 70, "fe80::1%eth0"])
def test_relay_client_missing_or_bad_is_400(out, visitor):
    headers = {"X-Jarvis-Relay-Key": RELAY_KEY}
    if visitor is not None:
        headers["X-Jarvis-Relay-Client"] = visitor
    resp = _chat(headers)
    assert resp.status_code == 400 and resp.json() == {"detail": "relay_client_required"}


def test_relay_client_without_key_ignored(out):
    headers = {"X-Jarvis-Relay-Client": "203.0.113.50", **h.via_proxy(h.LAN), **h.CSRF}
    resp = _chat(headers, peer=h.PROXY)
    assert resp.status_code == 200
    assert out.orchestrator_bodies()[-1]["caller_trust"] == "web_local"


def test_relay_headers_ignored_off_relay_routes(out):
    c = h.client(peer="10.3.3.3")
    assert c.get("/api/welcome", headers=_relay()).status_code == 401
    assert c.get("/api/sensors/motion", headers=_relay()).status_code == 401
    lan = h.client(peer=h.PROXY).get("/api/welcome", headers={**_relay(), **h.via_proxy(h.LAN)})
    assert lan.status_code == 200 and lan.json()["guest"]["guest_name"] == "Alice Renter"


def test_relay_only_on_chat_routes():
    relay_routes = {k for k, v in h.main.ROUTE_CLASSIFICATION.items() if v == "relay_chat"}
    assert relay_routes == {"POST /api/chat", "POST /api/chat/stream"}


def test_relay_21st_is_429(out):
    for _ in range(20):
        assert _chat(_relay()).status_code == 200
    resp = _chat(_relay())
    assert resp.status_code == 429 and resp.headers["retry-after"] == "60"
    assert len(out.orchestrator_bodies()) == 20


def test_distinct_relay_clients_distinct_buckets(out):
    for _ in range(20):
        _chat(_relay(visitor="203.0.113.50"))
    assert _chat(_relay(visitor="203.0.113.51")).status_code == 200
    assert _chat(_relay(visitor="203.0.113.50")).status_code == 429


def test_relay_ipv6_visitors_share_their_64(out):
    h.configure({**RELAY_ENV, "JARVIS_RELAY_REQUESTS_PER_MINUTE": "1"})
    assert _chat(_relay(visitor="2001:db8:5:6::1")).status_code == 200
    assert _chat(_relay(visitor="2001:db8:5:6::ffff")).status_code == 429


def test_relay_global_ceiling(out):
    h.configure({**RELAY_ENV, "JARVIS_RELAY_GLOBAL_PER_MINUTE": "3"})
    statuses = [_chat(_relay(visitor=f"203.0.113.{i}")).status_code for i in range(1, 5)]
    assert statuses == [200, 200, 200, 429]


def test_relay_with_edge_home_charged_to_relay_limiter(out):
    h.configure({**RELAY_ENV, "JARVIS_EDGE_ATTESTATION_SECRET": EDGE_SECRET, "JARVIS_HOUSEHOLD_GROUPS": "household",
                 "JARVIS_RELAY_REQUESTS_PER_MINUTE": "2"})
    extra = {"X-Forwarded-For": h.LAN, "X-Jarvis-Edge-Class": "home", "X-Jarvis-Edge-Attestation": EDGE_SECRET}
    statuses = [_chat(_relay(**extra), peer=h.PROXY).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


def test_limiter_reset_goes_through_reset_for_tests(out):
    for _ in range(20):
        _chat(_relay())
    assert _chat(_relay()).status_code == 429
    caller_auth._reset_for_tests()
    assert _chat(_relay()).status_code == 200


@pytest.mark.parametrize("field, value", [
    ("SERVICE_API_KEY", RELAY_KEY), ("JARVIS_EDGE_ATTESTATION_SECRET", RELAY_KEY),
])
def test_relay_key_collision_refused(field, value):
    env = {"SERVICE_API_KEY": h.SERVICE_KEY, "JARVIS_RELAY_KEY": RELAY_KEY, field: value}
    with pytest.raises(SystemExit):
        caller_auth.load_settings(env, own_ips=())


@pytest.mark.parametrize("key", ["short-key", "CONFIGURE_ME_RELAY_KEY_PADDING_TO_32_CHARS"])
def test_relay_key_hygiene(key):
    with pytest.raises(SystemExit):
        caller_auth.load_settings({"JARVIS_RELAY_KEY": key}, own_ips=())


# ---------------------------------------------------------------------------
# Public multi-turn continuity (xander P4 item 2)
# ---------------------------------------------------------------------------

def test_relay_session_is_public_and_continuous(out):
    first = _chat(_relay())
    sid = first.json()["session_id"]
    assert sid.startswith("pub-")
    second = _chat(_relay(), session_id=sid)
    assert second.json()["session_id"] == sid
    assert out.orchestrator_bodies()[-1]["session_id"] == sid


def test_relay_never_adopts_a_non_public_session_id(out):
    resp = _chat(_relay(), session_id="0123abcd.feedface")
    assert resp.json()["session_id"].startswith("pub-")
    assert out.orchestrator_bodies()[-1]["session_id"] != "0123abcd.feedface"


def test_relay_session_of_another_visitor_starts_fresh(out):
    """Named (codex High, xander L3): a pub- id minted for one visitor,
    presented by another, is a fresh session; the owner can still resume."""
    sid = _chat(_relay(visitor="203.0.113.50")).json()["session_id"]
    stolen = _chat(_relay(visitor="203.0.113.51"), session_id=sid)
    assert stolen.json()["session_id"] != sid and stolen.json()["session_id"].startswith("pub-")
    assert out.orchestrator_bodies()[-1]["session_id"] != sid
    assert _chat(_relay(visitor="203.0.113.50"), session_id=sid).json()["session_id"] == sid


def test_relay_session_same_ipv6_64_continues(out):
    sid = _chat(_relay(visitor="2001:db8:5:6::1")).json()["session_id"]
    assert _chat(_relay(visitor="2001:db8:5:6::2"), session_id=sid).json()["session_id"] == sid


@pytest.mark.parametrize("presented", ["pub-1", "pub-" + "0" * 32 + "." + "0" * 24, "pub-" + "a" * 200])
def test_relay_unminted_pub_id_starts_fresh(out, presented):
    resp = _chat(_relay(), session_id=presented)
    assert resp.json()["session_id"] != presented
    assert out.orchestrator_bodies()[-1]["session_id"] != presented


def test_relay_stream_announces_its_public_session(out):
    resp = _chat(_relay(), path="/api/chat/stream")
    first = resp.text.split("\n\n", 1)[0]
    assert '"stage": "session"' in first and '"session_id": "pub-' in first
    sid = first.split('"session_id": "', 1)[1].split('"', 1)[0]
    again = _chat(_relay(), path="/api/chat/stream", session_id=sid)
    assert f'"session_id": "{sid}"' in again.text.split("\n\n", 1)[0]


def test_transcribe_401_reads_no_body(out, monkeypatch):
    """bob L5: the refusal happens before the multipart parser runs."""
    from starlette.requests import Request

    def _boom(self, *a, **kw):
        raise AssertionError("the body was read for a refused caller")

    monkeypatch.setattr(Request, "form", _boom)
    resp = h.client(peer=h.INTERNET).post(
        "/api/voice/transcribe", files={"audio": ("a.webm", b"x" * 1024, "audio/webm")}, headers=h.CSRF,
    )
    assert resp.status_code == 401
