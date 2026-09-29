"""jarvis-web never logs a secret it holds or one it's handed (tessa C4).

Realistic rows: values shaped like real secrets (not placeholders a
redaction might special-case), at startup and on every rejection path that
sees them: an attestation value presented while that slot isn't
configured, a wrong relay key, a wrong service key, and the settings'
own repr.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from . import _jarvis_web_harness as h

caller_auth = h.caller_auth
SECRET = "edge-attestation-current-7f3a9c2e1b8d4f6a"
PREVIOUS = "edge-attestation-previous-5d2e8b1c9a7f3e6d"
RELAY_KEY = "relay-key-4b9e2d7a1c6f3e8b0a5d9c2e7f1b4a6d"
WRONG_RELAY = "relay-key-9f1e3c7b5a2d8e4c6b0a3f9d1e7c5b2a"
WRONG_SERVICE = "svc-key-2c8e6a4f1d9b7e3c5a0f8d2b6e4c1a9f"
EDGE_ENV = {**h.HOME_ENV, "JARVIS_EDGE_ATTESTATION_SECRET": SECRET, "JARVIS_HOUSEHOLD_GROUPS": "household",
            "JARVIS_RELAY_KEY": RELAY_KEY}


@pytest.fixture(autouse=True)
def _reset():
    yield
    h.configure()


def _rendered(captured_logs, caplog):
    return repr(captured_logs) + caplog.text


def test_previous_shaped_value_while_previous_unset_is_not_logged(monkeypatch, captured_logs, caplog):
    """Named: the edge has no previous slot, a request presents a real-looking
    previous value; it's refused and the value appears nowhere."""
    h.configure(EDGE_ENV)
    h.install_outbound(monkeypatch)
    headers = {"X-Forwarded-For": h.LAN, "X-Jarvis-Edge-Class": "home", "X-Jarvis-Edge-Attestation": PREVIOUS}
    assert h.client().get("/api/welcome", headers=headers).status_code == 401
    rendered = _rendered(captured_logs, caplog)
    assert PREVIOUS not in rendered and SECRET not in rendered
    assert any(e["event"] == "jarvis_caller_resolved" for e in captured_logs)


def test_wrong_relay_key_is_not_logged(monkeypatch, captured_logs, caplog):
    h.configure(EDGE_ENV)
    h.install_outbound(monkeypatch)
    resp = h.client(peer="10.3.3.3").post(
        "/api/chat", json={"message": "hi"},
        headers={"X-Jarvis-Relay-Key": WRONG_RELAY, "X-Jarvis-Relay-Client": "203.0.113.50"},
    )
    assert resp.status_code == 401
    rendered = _rendered(captured_logs, caplog)
    assert any(e["event"] == "jarvis_relay_key_invalid" for e in captured_logs)
    assert WRONG_RELAY not in rendered and RELAY_KEY not in rendered


def test_wrong_service_key_is_not_logged(monkeypatch, captured_logs, caplog):
    h.configure(EDGE_ENV)
    h.install_outbound(monkeypatch)
    resp = h.client(peer="10.3.3.3").get("/api/sensors/summary", headers={"X-Service-Key": WRONG_SERVICE})
    assert resp.status_code == 401
    rendered = _rendered(captured_logs, caplog)
    assert WRONG_SERVICE not in rendered and h.SERVICE_KEY not in rendered


def test_settings_repr_holds_no_secret():
    s = caller_auth.load_settings(EDGE_ENV, own_ips=())
    for value in (SECRET, RELAY_KEY, h.SERVICE_KEY):
        assert value not in repr(s)


def _start(env):
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {str(h.BACKEND)!r})
        os.environ.update({env!r})
        import caller_auth
        print("STARTED")
    """)
    base = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(h.REPO_ROOT / "src")}
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, env=base, timeout=60)


@pytest.mark.parametrize("override, starts", [
    ({}, True),
    ({"JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS": PREVIOUS}, True),
    ({"JARVIS_RELAY_KEY": h.SERVICE_KEY + "-padded-to-thirty-two"}, True),
    ({"SERVICE_API_KEY": RELAY_KEY}, False),  # relay key equal to the service key
], ids=["edge", "edge_previous", "relay_distinct", "relay_collision"])
def test_startup_output_holds_no_secret(override, starts):
    env = {**EDGE_ENV, **override}
    proc = _start(env)
    output = proc.stdout + proc.stderr
    assert (proc.returncode == 0 and "STARTED" in output) is starts, output[-2000:]
    for name in ("SERVICE_API_KEY", "JARVIS_EDGE_ATTESTATION_SECRET", "JARVIS_EDGE_ATTESTATION_SECRET_PREVIOUS",
                 "JARVIS_RELAY_KEY"):
        if env.get(name):
            assert env[name] not in output, name
