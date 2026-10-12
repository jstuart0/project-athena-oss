"""The voice-pipeline routes over the shared WebSocket door.

A read-only user cannot change the preferred pipeline, the old ``/health`` route
is gone, and every failure reaches the browser as a closed phrase.
"""
import asyncio
import json

import pytest

from app.routes import ha_pipelines
from app.services import ha_access
from tests import ha_stack
from tests.ha_stack import TOKEN, FakeHA

BASE = "/api/ha-pipelines"
PIPELINES = {
    "pipelines": [
        {"id": "p-simple", "name": "Ollama", "conversation_engine": "conversation.ollama_conversation"},
        {"id": "p-full", "name": "Full", "conversation_engine": "conversation.extended_openai_conversation"},
    ],
    "preferred_pipeline": "p-simple",
}


class PipelineHA(FakeHA):
    """Answers the pipeline commands the way Home Assistant does."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.commands = []

    def ws_connect(self, address="A"):
        outer = self
        base = super().ws_connect(address)

        def connect(url, **kwargs):
            connection = base(url, **kwargs)
            original = connection.send

            async def send(raw):
                frame = json.loads(raw)
                if frame.get("type") == "auth":
                    return await original(raw)
                outer.commands.append(frame["type"])
                result = PIPELINES if frame["type"] == "assist_pipeline/pipeline/list" else None
                connection._frames.put_nowait({"type": "result", "id": frame["id"], "success": True, "result": result})

            connection.send = send
            return connection

        return connect


@pytest.fixture
def ha():
    fake = PipelineHA()
    ha_stack.build(fake, ha_stack.constant_source())
    return fake


def test_the_pipelines_are_listed_through_the_session(owner_client, ha):
    response = owner_client.get(f"{BASE}/pipelines")
    assert response.status_code == 200, response.text
    assert ha.commands == ["assist_pipeline/pipeline/list"]


def test_a_read_only_user_cannot_set_the_preferred_pipeline(viewer_client):
    response = viewer_client.post(f"{BASE}/pipelines/set-preferred", json={"pipeline_id": "p-full"})
    assert response.status_code == 403


def test_a_refused_set_preferred_reaches_nothing(viewer_client, ha):
    viewer_client.post(f"{BASE}/pipelines/set-preferred", json={"pipeline_id": "p-full"})
    assert ha.commands == [] and ha.ws_connects == [], "nothing reached Home Assistant"


def test_a_user_with_write_sets_the_preferred_pipeline(operator_client, ha):
    response = operator_client.post(f"{BASE}/pipelines/set-preferred", json={"pipeline_id": "p-full"})
    assert response.status_code == 200, response.text
    assert ha.commands == ["assist_pipeline/pipeline/list", "assist_pipeline/pipeline/set_preferred"]


def test_an_unknown_pipeline_is_a_404_and_sets_nothing(owner_client, ha):
    response = owner_client.post(f"{BASE}/pipelines/set-preferred", json={"pipeline_id": "nope"})
    assert response.status_code == 404
    assert ha.commands == ["assist_pipeline/pipeline/list"]


def test_the_old_health_route_is_gone(owner_client):
    assert owner_client.get(f"{BASE}/health").status_code == 404
    assert not hasattr(ha_pipelines, "check_ha_connection")


def test_asking_for_the_old_health_route_reaches_nothing(owner_client, ha):
    owner_client.get(f"{BASE}/health")
    assert ha.ws_connects == [] and ha.rest_sent == []


def test_a_mode_with_no_matching_pipeline_names_no_home_assistant_text(owner_client, ha):
    response = owner_client.post(f"{BASE}/mode/set", json={"mode": "streaming_rag"})
    assert response.status_code == 404
    assert "Ollama" not in response.text and "conversation." not in response.text


def test_home_assistant_not_set_up_is_a_closed_409(owner_client):
    ha_stack.build(FakeHA(), ha_stack.absent_source())
    response = owner_client.get(f"{BASE}/pipelines")
    assert (response.status_code, response.json()["detail"]) == (409, ha_access.NOT_SET_UP)


def test_a_rejected_token_is_a_closed_502_and_the_second_call_sends_nothing(owner_client):
    fake = FakeHA(valid=())
    ha_stack.build(fake, ha_stack.constant_source())
    first = owner_client.get(f"{BASE}/pipelines")
    second = owner_client.get(f"{BASE}/pipelines")
    assert (first.status_code, first.json()["detail"]) == (502, ha_access.TOKEN_REJECTED)
    assert second.json()["detail"] == ha_access.TOKEN_REJECTED
    assert fake.failed_total == 1, "the stop holds across routes"


def test_an_unexpected_failure_leaks_no_text(owner_client, monkeypatch):
    ha_stack.build(FakeHA(), ha_stack.constant_source())

    class Boom:
        async def __aenter__(self):
            raise RuntimeError(f"secret detail {TOKEN}")

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(ha_access, "ws_session", lambda profile: Boom())
    for path in ("/pipelines", "/pipelines/preferred"):
        response = owner_client.get(BASE + path)
        assert response.status_code == 502 and response.json()["detail"] == ha_access.FAILED
        assert "secret" not in response.text and TOKEN not in response.text
    response = owner_client.post(f"{BASE}/mode/set", json={"mode": "simple"})
    assert response.json()["detail"] == ha_access.FAILED


def test_an_answer_the_route_cannot_read_is_a_closed_502(owner_client):
    ha_stack.build(FakeHA(), ha_stack.constant_source())  # answers every command with a bare list
    for path in ("/pipelines", "/pipelines/preferred"):
        response = owner_client.get(BASE + path)
        assert (response.status_code, response.json()["detail"]) == (502, ha_access.FAILED)
        assert "attribute" not in response.text and "list" not in response.text
    posted = owner_client.post(f"{BASE}/pipelines/set-preferred", json={"pipeline_id": "p-full"})
    assert posted.json()["detail"] == ha_access.FAILED
    mode = owner_client.post(f"{BASE}/mode/set", json={"mode": "simple"})
    assert mode.json()["detail"] == ha_access.FAILED


def test_the_route_module_holds_no_websocket_or_environment_access():
    source = open(ha_pipelines.__file__).read()
    for word in ("websockets", "os.getenv", "HA_TOKEN", "access_token", "/api/websocket"):
        assert word not in source, word
