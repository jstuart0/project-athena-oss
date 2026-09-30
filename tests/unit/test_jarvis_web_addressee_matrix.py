"""Who jarvis-web addresses by name, per caller class and house mode.

Only the guest network is addressed as the staying guest; a signed-in
household member gets their own first name (from the edge's name header);
everyone else gets no name. The UI's guest block keeps informing every
guest-read caller. The member's name is never logged.
"""
from __future__ import annotations

import pytest

from . import _jarvis_web_harness as h

main = h.main
caller_auth = h.caller_auth
SECRET = "edge-attestation-current-7f3a9c2e1b8d4f6a"
RELAY_KEY = "relay-key-for-tests-0123456789abcdef"
EDGE_ENV = {**h.HOME_ENV, "JARVIS_EDGE_ATTESTATION_SECRET": SECRET, "JARVIS_HOUSEHOLD_GROUPS": "household"}
RELAY_ENV = {**h.HOME_ENV, "JARVIS_RELAY_KEY": RELAY_KEY}
GUEST = {"has_guest": True, "guest_name": "Gina Guest", "id": 7}
MEMBER_NAME = "Pat Example"
NAMES = ("Gina", "Pat")


def _edge_signed_in(groups="household", name=MEMBER_NAME, **extra):
    headers = {
        "X-Forwarded-For": h.INTERNET,
        "X-Jarvis-Edge-Class": "authenticated",
        "X-Jarvis-Edge-Attestation": SECRET,
        "X-authentik-username": "pat",
        "X-authentik-groups": groups,
    }
    if name is not None:
        headers["X-authentik-name"] = name
    headers.update(extra)
    return headers


# (id, env, headers, bearer role, expected context, expected trust, greeting name or None)
ROWS = [
    ("web_local", h.HOME_ENV, h.via_proxy(h.LAN), None, {}, "web_local", None),
    ("edge_member_named", EDGE_ENV, _edge_signed_in(), None, {"speaker_first_name": "Pat"}, "web_authenticated", "Pat"),
    ("edge_member_unnamed", EDGE_ENV, _edge_signed_in(name=None), None, {}, "web_authenticated", None),
    ("bearer", h.HOME_ENV, {**h.via_proxy(h.INTERNET), "Authorization": "Bearer t"}, "owner", {}, "web_authenticated", None),
    ("not_household_bearer", EDGE_ENV, _edge_signed_in(groups="visitors", Authorization="Bearer t"), "owner",
     {}, "web_authenticated", None),
]


def _configure(env, role):
    h.configure(env)
    if role:
        h.install_role(role)


def _chat(headers):
    resp = h.client().post("/api/chat", json={"message": "hi"}, headers={**headers, **h.CSRF})
    assert resp.status_code == 200, resp.text
    return resp


def _welcome(headers):
    resp = h.client().get("/api/welcome", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _greeting_name(welcome):
    greeting = welcome["greeting"]
    for name in NAMES:
        if f", {name}!" in greeting:
            return name
    return None


@pytest.fixture(autouse=True)
def _reset():
    main.mode_override = None
    yield
    main.mode_override = None
    h.configure()


@pytest.mark.parametrize("house", ["owner", "guest"])
@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_household_classes(monkeypatch, captured_logs, row, house):
    _id, env, headers, role, context, trust, greeting_name = row
    out = h.install_outbound(monkeypatch, guest=GUEST if house == "guest" else None)
    _configure(env, role)

    _chat(headers)
    body = out.orchestrator_bodies()[-1]
    assert body["caller_trust"] == trust
    assert body["context"] == context

    welcome = _welcome(headers)
    assert _greeting_name(welcome) == greeting_name
    assert "Pat" not in welcome["subtitle"]
    if house == "guest":
        assert welcome["guest"]["guest_name"] == "Gina Guest"
    else:
        assert welcome["guest"]["has_guest"] is False

    logged = repr(captured_logs)
    assert "Pat" not in logged
    resolved = [e for e in captured_logs if e.get("event") == "jarvis_caller_resolved"]
    assert resolved and all("has_speaker_name" in e for e in resolved)
    assert any(e["has_speaker_name"] for e in resolved) == (greeting_name == "Pat")


def test_guest_network_is_addressed_as_the_guest(monkeypatch, captured_logs):
    out = h.install_outbound(monkeypatch, guest=GUEST)
    h.configure()
    headers = h.via_proxy(h.GUEST_WIFI)
    _chat(headers)
    body = out.orchestrator_bodies()[-1]
    assert body["caller_trust"] == "web_guest_net"
    assert body["context"] == {"guest_id": 7, "guest_name": "Gina Guest"}
    welcome = _welcome(headers)
    assert _greeting_name(welcome) == "Gina"
    assert welcome["guest"]["guest_name"] == "Gina Guest"


def test_guest_network_in_a_vacant_house_has_no_name(monkeypatch):
    out = h.install_outbound(monkeypatch)
    h.configure()
    headers = h.via_proxy(h.GUEST_WIFI)
    _chat(headers)
    body = out.orchestrator_bodies()[-1]
    assert body["caller_trust"] == "web_guest_net"
    assert body["context"] == {}
    assert _greeting_name(_welcome(headers)) is None


def test_guest_network_never_carries_a_member_name(monkeypatch):
    """A name header on a non-authenticated request is never read."""
    out = h.install_outbound(monkeypatch, guest=GUEST)
    h.configure()
    _chat(h.via_proxy(h.GUEST_WIFI, **{"X-authentik-name": MEMBER_NAME}))
    assert "speaker_first_name" not in out.orchestrator_bodies()[-1]["context"]


def test_relay_gets_no_identity(monkeypatch):
    out = h.install_outbound(monkeypatch, guest=GUEST)
    h.configure(RELAY_ENV)
    headers = {"X-Jarvis-Relay-Key": RELAY_KEY, "X-Jarvis-Relay-Client": "203.0.113.50"}
    resp = h.client(peer="10.3.3.3").post("/api/chat", json={"message": "hi"}, headers=headers)
    assert resp.status_code == 200
    body = out.orchestrator_bodies()[-1]
    assert body["caller_trust"] == "web_public"
    assert body["context"] == {}


def test_mode_state_shows_the_guest_to_household_and_guest_network(monkeypatch):
    h.install_outbound(monkeypatch, guest=GUEST)
    h.configure()
    for source in (h.LAN, h.GUEST_WIFI):
        resp = h.client().get("/api/mode", headers=h.via_proxy(source))
        assert resp.status_code == 200
        assert resp.json()["guest_name"] == "Gina Guest"
