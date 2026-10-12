"""AdminConfigClient.get_api_keys_for_tool never hands out the Home Assistant token.

A row created before the admin route refused the attachment still can't leak:
the key is skipped without being fetched.
"""
import asyncio

from shared.admin_config import AdminConfigClient


def client_with(requirements, keys):
    client = AdminConfigClient(admin_url="http://admin.example.invalid", api_key="k")
    fetched = []

    async def get_requirements(tool_name):
        return requirements

    async def get_key(service):
        fetched.append(service)
        return {"api_key": keys[service]} if service in keys else None

    client.get_tool_api_key_requirements = get_requirements
    client.get_external_api_key = get_key
    return client, fetched


def test_the_home_assistant_key_is_skipped_and_never_fetched():
    client, fetched = client_with(
        [{"api_key_service": "home-assistant", "inject_as": "token"}, {"api_key_service": "openweather", "inject_as": "appid"}],
        {"home-assistant": "ha-secret", "openweather": "ow-secret"},
    )
    out = asyncio.run(client.get_api_keys_for_tool("weather"))
    assert out == {"appid": "ow-secret"}
    assert fetched == ["openweather"], "the Home Assistant key was not even requested"


def test_a_requirement_with_the_default_parameter_name_is_skipped_too():
    client, fetched = client_with([{"api_key_service": "home-assistant"}], {"home-assistant": "ha-secret"})
    assert asyncio.run(client.get_api_keys_for_tool("weather")) == {} and fetched == []


def test_other_services_still_inject_with_the_default_name():
    client, _ = client_with([{"api_key_service": "brave-search"}], {"brave-search": "b"})
    assert asyncio.run(client.get_api_keys_for_tool("weather")) == {"brave_search_api_key": "b"}
