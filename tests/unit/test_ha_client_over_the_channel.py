"""HomeAssistantClient and HAEntityManager hold no credentials or HTTP client:
they speak through the channel, and each call maps to one method and path."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

_SRC = str(Path(__file__).resolve().parents[2] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from orchestrator.ha_entity_manager import HAEntityManager
from shared.ha_client import HomeAssistantClient, HomeAssistantNotConfiguredError
from shared.ha_credentials import HACredentials, StaticResolver
from shared.ha_http import HAChannel

from .ha_auth_fakes import FakeHA, make_stack


def run(coro):
    return asyncio.run(coro)


class Recording(FakeHA):
    def _rest(self, request, address):
        response = super()._rest(request, address)
        if request.url.path == "/api/states" and response.status_code == 200:
            return httpx.Response(200, json=[{"entity_id": "light.a", "state": "on", "attributes": {}}], request=request)
        return response


def client_and_fake(**kw):
    fake = Recording(**kw)
    stack = make_stack(fake=fake, gate_wait=0.1)
    return HomeAssistantClient(stack.channel), fake


def test_each_call_is_one_request_with_the_documented_method_and_path():
    async def go():
        client, fake = client_and_fake()
        await client.get_states()
        await client.get_state("light.living_room")
        await client.call_service("light", "turn_on", {"entity_id": "light.a"})
        assert await client.create_automation("night_light", {"alias": "x"}) is True
        assert await client.delete_automation("night_light") is True
        assert await client.disable_automation("night_light") is True
        return [(m, p) for _, m, p in fake.rest_sent]

    sent = run(go())
    assert sent[:3] == [
        ("GET", "/api/states"), ("GET", "/api/states/light.living_room"), ("POST", "/api/services/light/turn_on"),
    ]
    assert ("POST", "/api/config/automation/config/night_light") in sent
    assert ("DELETE", "/api/config/automation/config/night_light") in sent
    assert ("POST", "/api/services/automation/turn_off") in sent
    assert sent.count(("POST", "/api/services/automation/reload")) == 2


def test_an_id_that_cannot_name_a_resource_is_refused_before_it_is_sent():
    async def go():
        client, fake = client_and_fake()
        with pytest.raises(ValueError):
            await client.get_state("../config")
        with pytest.raises(ValueError):
            await client.call_service("light", "turn_on/../../config", {})
        assert await client.create_automation("../x", {}) is False
        return fake.rest_sent

    assert run(go()) == []


def test_a_refused_request_is_an_error_like_any_401():
    async def go():
        client, fake = client_and_fake(valid=())
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await client.get_states()
        return caught.value.response.status_code, fake.rest_sent

    assert run(go()) == (401, [])


def test_not_configured_says_where_to_set_it_and_sends_nothing():
    async def go():
        stack = make_stack()
        channel = HAChannel(StaticResolver(HACredentials()), stack.latch, caller="orchestrator_rest")
        client = HomeAssistantClient(channel)
        assert client.is_configured is False
        for call in (client.get_states(), client.call_service("light", "turn_on")):
            with pytest.raises(HomeAssistantNotConfiguredError) as caught:
                await call
        return str(caught.value)

    message = run(go())
    assert "External API Keys" in message and "HA_URL" in message and "MODULE_HOME_ASSISTANT" not in message


def test_the_client_closes_its_channel():
    async def go():
        client, _ = client_and_fake()
        await client.get_states()
        await client.close()
        return client._channel._client.is_closed

    assert run(go()) is True


def test_the_client_holds_no_url_token_or_http_client():
    client, _ = client_and_fake()
    for name in ("url", "token", "headers", "client"):
        assert not hasattr(client, name), name


def test_the_entity_manager_refreshes_through_the_client_it_is_given():
    async def go():
        client, fake = client_and_fake()
        manager = HAEntityManager(client)
        entities = await manager.refresh_entities()
        return entities, manager.cached_light_group_names(), fake.rest_sent

    entities, names, sent = run(go())
    assert set(entities) == {"light.a"} and names == []
    assert [(m, p) for _, m, p in sent] == [("GET", "/api/states")]


def test_the_entity_manager_holds_no_credentials():
    manager = HAEntityManager(object())
    for name in ("ha_url", "ha_token", "headers", "client"):
        assert not hasattr(manager, name), name


class ServiceResponder(FakeHA):
    """Answers a service call the way Home Assistant does under
    ``?return_response``, and records the query it came with."""

    def __init__(self, reply, status=200, **kw):
        super().__init__(**kw)
        self.reply, self.status, self.queries = reply, status, []

    def _rest(self, request, address):
        response = super()._rest(request, address)
        if request.method == "POST" and request.url.path.startswith("/api/services/") and response.status_code == 200:
            self.queries.append(request.url.query.decode())
            return httpx.Response(self.status, json=self.reply, request=request)
        return response


def response_client(reply, status=200):
    fake = ServiceResponder(reply, status)
    stack = make_stack(fake=fake, gate_wait=0.1)
    return HomeAssistantClient(stack.channel), fake


def test_return_response_posts_the_flag_and_returns_the_service_response():
    async def go():
        client, fake = response_client({"changed_states": [], "service_response": {"until": "", "stale": False}})
        answer = await client.call_service_response("script", "room_policy_set", {"area": "den"})
        return answer, fake.queries, [(m, p) for _, m, p in fake.rest_sent]

    answer, queries, sent = run(go())
    assert answer == {"until": "", "stale": False}
    assert queries == ["return_response"]
    assert sent == [("POST", "/api/services/script/room_policy_set")]


def test_return_response_without_a_service_response_is_a_shape_error():
    from shared.ha_client import HAResponseShapeError

    for reply in ({"changed_states": []}, {"service_response": "text"}, [], {"service_response": None}):
        async def go(reply=reply):
            client, _ = response_client(reply)
            with pytest.raises(HAResponseShapeError):
                await client.call_service_response("script", "room_policy_set", {})

        run(go())


def test_return_response_error_status_is_an_http_error_with_the_body():
    async def go():
        client, _ = response_client({"message": "no such room"}, status=400)
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await client.call_service_response("script", "room_policy_set", {})
        return caught.value.response.status_code, caught.value.response.json()

    assert run(go()) == (400, {"message": "no such room"})


def test_return_response_is_the_only_query_the_channel_sends():
    async def go():
        client, fake = response_client({"service_response": {}})
        with pytest.raises(ValueError):
            await client._channel.request("POST", "/api/services/script/x?return_response")
        with pytest.raises(ValueError):
            await client._channel.request("POST", "/api/services/script/x?other=1", return_response=True)
        await client._channel.request("POST", "/api/services/script/x", json={}, return_response=True)
        await client._channel.request("POST", "/api/services/script/x", json={})
        return fake.queries

    assert run(go()) == ["return_response", ""]


def test_return_response_names_a_service_like_call_service_does():
    async def go():
        client, fake = response_client({"service_response": {}})
        with pytest.raises(ValueError):
            await client.call_service_response("script", "x/../../config", {})
        return fake.rest_sent

    assert run(go()) == []


def test_return_response_without_credentials_says_where_to_set_them():
    async def go():
        stack = make_stack(fake=ServiceResponder({"service_response": {}}), gate_wait=0.1)
        client = HomeAssistantClient(stack.channel)
        client._channel = type("Unset", (), {"configured": False})()
        with pytest.raises(HomeAssistantNotConfiguredError):
            await client.call_service_response("script", "room_policy_set", {})

    run(go())
