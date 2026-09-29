"""Web push-to-talk is for every browser caller, not only those who may
control (user decision, reconcile r1).

Home network (also during a guest stay), guest Wi-Fi and signed-in callers
get the voice capability and can use /api/voice/*; the anonymous public
audience (internet, the embed relay) gets none. What a voice turn may DO
still follows the caller's permissions: it goes through the same chat path.
LiveKit (always-on voice) stays owner-only.
"""
from __future__ import annotations

import subprocess

import pytest

from . import _jarvis_web_harness as h

caller_auth = h.caller_auth
main = h.main
EDGE_SECRET = "edge-attestation-current-7f3a9c2e1b8d4f6a"
RELAY_KEY = "relay-key-for-tests-0123456789abcdef"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    main.mode_override = None
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="{}"))
    yield
    main.mode_override = None
    h.configure()


def _voice_calls(c, headers):
    health = c.get("/api/voice/health", headers=headers).status_code
    synth = c.post("/api/voice/synthesize", json={"text": "hi"}, headers={**headers, **h.CSRF}).status_code
    return health, synth


@pytest.mark.parametrize("who", ["guest_network", "home_during_stay", "authenticated_remote"])
def test_browser_callers_have_voice(monkeypatch, who):
    """Named: guest_network."""
    h.configure({**h.HOME_ENV, "JARVIS_EDGE_ATTESTATION_SECRET": EDGE_SECRET, "JARVIS_HOUSEHOLD_GROUPS": "household"})
    guest = {"has_guest": True, "guest_name": "Alice Renter", "id": 7} if who == "home_during_stay" else None
    h.install_outbound(monkeypatch, guest=guest)
    headers = {
        "guest_network": {"X-Forwarded-For": h.GUEST_WIFI, "X-Jarvis-Edge-Class": "guest", "X-Jarvis-Edge-Attestation": EDGE_SECRET},
        "home_during_stay": {"X-Forwarded-For": h.LAN, "X-Jarvis-Edge-Class": "home", "X-Jarvis-Edge-Attestation": EDGE_SECRET},
        "authenticated_remote": {"X-Forwarded-For": h.INTERNET, "X-Jarvis-Edge-Class": "authenticated",
                                 "X-Jarvis-Edge-Attestation": EDGE_SECRET, "X-authentik-username": "alice",
                                 "X-authentik-groups": "household"},
    }[who]
    c = h.client()
    caps = c.get("/api/welcome", headers=headers).json()["capabilities"]
    assert caps["voice"] is True, who
    if who != "authenticated_remote":
        assert caps["control"] is False, who  # voice is not control
    health, synth = _voice_calls(c, headers)
    assert health == 200, who
    assert synth not in {401, 403}, who


def test_anonymous_and_relay_have_no_voice(monkeypatch):
    h.configure({**h.HOME_ENV, "JARVIS_RELAY_KEY": RELAY_KEY})
    h.install_outbound(monkeypatch)
    anon = h.client(peer=h.INTERNET)
    assert _voice_calls(anon, {}) == (401, 401)
    relay = {"X-Jarvis-Relay-Key": RELAY_KEY, "X-Jarvis-Relay-Client": "203.0.113.50"}
    assert _voice_calls(h.client(peer="10.3.3.3"), relay) == (401, 401)
    relay_caller = caller_auth.Caller(caller_auth.CLASS_RELAY, "guest", source="relay")
    assert main._capabilities(relay_caller)["voice"] is False
    public = caller_auth.Caller(caller_auth.CLASS_PUBLIC, "guest")
    assert main._capabilities(public)["voice"] is False


def test_livekit_room_stays_owner_only(monkeypatch):
    """Always-on voice (LiveKit rooms) is unchanged: a guest-network caller
    gets 403, not a room."""
    h.configure()
    h.install_outbound(monkeypatch)
    resp = h.client().post("/livekit/rooms", headers={**h.via_proxy(h.GUEST_WIFI), **h.CSRF})
    assert resp.status_code == 403


def test_frontend_gates_voice_on_the_voice_capability():
    index = (h.FRONTEND / "index.html").read_text(encoding="utf-8")
    init = index.split("document.addEventListener('DOMContentLoaded', async () => {", 1)[1].split("\n        });", 1)[0]
    voice_gate = init.index("if (capabilities.voice) {")
    control_gate = init.index("if (canControl()) {")
    assert "await checkVoiceHealth();" in init[voice_gate:control_gate]
    control_block = init[control_gate:]
    assert "await initializeLiveKit();" in control_block and "await initMusicPlayer();" in control_block
    assert "await checkVoiceHealth();" not in control_block
