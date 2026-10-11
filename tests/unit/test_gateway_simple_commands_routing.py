"""With ha_simple_command_fastpath and ha_intent_prerouting both ON, a device
command reaches the orchestrator and the gateway makes no call to Home
Assistant. (Before the gateway's direct writer was removed, "lights" posted
homeassistant/turn_on for light.all from here.)"""
from __future__ import annotations

import os
import sys
import unittest.mock as mock
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-simple-routing")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import gateway.main as gw  # noqa: E402

DEVICE_COMMANDS = ["turn on the kitchen lights", "lights", "turn off all lights"]


class HttpRecorder:
    """Records every HTTP call made through the gateway's HA client or through
    any new httpx.AsyncClient."""

    def __init__(self):
        self.calls = []

    def _record(self, method, url):
        self.calls.append((method, str(url)))
        raise AssertionError(f"unexpected HTTP call {method} from the gateway")

    async def get(self, url, **kwargs):
        self._record("GET", url)

    async def post(self, url, **kwargs):
        self._record("POST", url)


@pytest.fixture
def rig(monkeypatch):
    http = HttpRecorder()
    answer = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="From the orchestrator."))])
    orchestrate = mock.AsyncMock(return_value=(answer, "sess-1"))
    sessions = SimpleNamespace(
        get_session_for_device=mock.AsyncMock(return_value=None),
        update_session_for_device=mock.AsyncMock(),
    )

    async def flags(name, default=False):
        return name in ("ha_simple_command_fastpath", "ha_intent_prerouting")

    class RecordingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kwargs):
            http._record("GET", url)

        async def post(self, url, **kwargs):
            http._record("POST", url)

    monkeypatch.setattr(gw, "get_feature_flag", flags)
    monkeypatch.setattr(gw, "classify_intent", mock.AsyncMock(return_value="HOME"))
    monkeypatch.setattr(gw, "handle_simple_intent", mock.AsyncMock(return_value=None))
    monkeypatch.setattr(gw, "route_to_orchestrator", orchestrate)
    monkeypatch.setattr(gw, "device_session_mgr", sessions)
    monkeypatch.setattr(gw, "ha_client", http)
    monkeypatch.setattr(gw.httpx, "AsyncClient", RecordingClient)
    monkeypatch.setenv("HA_URL", "http://ha.invalid:8123")
    monkeypatch.setenv("HA_TOKEN", "token-for-tests")
    return SimpleNamespace(http=http, orchestrate=orchestrate, sessions=sessions, client=TestClient(gw.app))


@pytest.mark.parametrize("text", DEVICE_COMMANDS)
def test_a_device_command_makes_one_orchestrator_call_and_no_ha_call(rig, text):
    resp = rig.client.post("/ha/conversation", json={"text": text, "device_id": "d" * 32, "language": "en"})
    assert resp.status_code == 200, resp.text
    assert rig.orchestrate.await_count == 1, text
    assert rig.http.calls == [], (text, rig.http.calls)
    assert resp.json()["response"]["speech"]["plain"]["speech"] == "From the orchestrator."


def test_the_time_is_still_answered_by_the_gateway_with_no_ha_call(rig):
    resp = rig.client.post("/ha/conversation", json={"text": "what time is it", "device_id": "d" * 32, "language": "en"})
    assert resp.status_code == 200, resp.text
    assert rig.orchestrate.await_count == 0
    assert rig.http.calls == []
    assert resp.json()["response"]["speech"]["plain"]["speech"].startswith("It's ")


def test_the_population_floor():
    assert len(DEVICE_COMMANDS) == 3


VICTIM = "ab" * 16


def test_an_untrusted_caller_with_a_victims_device_id_gets_no_session_no_fingerprint_no_follow_up(rig):
    resp = rig.client.post("/ha/conversation", json={"text": "turn on the lights", "device_id": VICTIM, "language": "en"})
    assert resp.status_code == 200, resp.text
    rig.sessions.get_session_for_device.assert_not_awaited()
    rig.sessions.update_session_for_device.assert_not_awaited()
    kwargs = rig.orchestrate.await_args.kwargs
    assert kwargs["voice_device_id"] is None and kwargs["trusted_voice"] is False


def test_a_trusted_caller_gets_its_session_and_a_follow_up(rig, monkeypatch):
    monkeypatch.setattr(gw, "API_KEY", "gateway-secret-for-tests")
    resp = rig.client.post(
        "/ha/conversation", json={"text": "turn on the lights", "device_id": VICTIM, "language": "en"},
        headers={"Authorization": "Bearer gateway-secret-for-tests"},
    )
    assert resp.status_code == 200, resp.text
    rig.sessions.get_session_for_device.assert_awaited_once_with(VICTIM)
    kwargs = rig.orchestrate.await_args.kwargs
    assert kwargs["voice_device_id"] == VICTIM and kwargs["trusted_voice"] is True
    rig.sessions.update_session_for_device.assert_awaited_once()


def test_the_orchestrator_payload_has_no_follow_up_without_a_believed_device(monkeypatch):
    posted = []

    class Client:
        async def post(self, path, json=None, **kwargs):
            posted.append(json)
            resp = mock.MagicMock()
            resp.raise_for_status = mock.MagicMock()
            resp.json.return_value = {"answer": "ok", "session_id": "s"}
            return resp

    monkeypatch.setattr(gw, "orchestrator_client", Client())
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    request = gw.ChatCompletionRequest(model="m", messages=[gw.ChatMessage(role="user", content="hi")])
    import asyncio

    asyncio.run(gw.route_to_orchestrator(request, supports_followup=True, voice_device_id=None))
    asyncio.run(gw.route_to_orchestrator(request, supports_followup=True, voice_device_id=VICTIM, trusted_voice=True))
    assert "supports_followup" not in posted[0] and "voice_device_id" not in posted[0] and "trusted_voice" not in posted[0]
    assert posted[1]["supports_followup"] is True and posted[1]["voice_device_id"] == VICTIM and posted[1]["trusted_voice"] is True
