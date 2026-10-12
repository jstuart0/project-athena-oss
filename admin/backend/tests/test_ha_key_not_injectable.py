"""The Home Assistant token can't be attached to a tool or put into a workflow.

``shared.ha_credentials.NON_INJECTABLE_KEY_SERVICES`` is the one set; the admin
route that attaches a key to a tool refuses it, and the n8n placeholder
injection leaves it alone.
"""
import asyncio

import pytest

from app.models import ExternalAPIKey, ToolApiKeyRequirement, ToolRegistry
from app.routes import tool_proposals
from app.utils.encryption import encrypt_value
from shared.ha_credentials import HA_KEY_SERVICE, NON_INJECTABLE_KEY_SERVICES, NOT_INJECTABLE_REFUSAL

URL = "/api/tool-calling/tools/{}/api-keys"


@pytest.fixture
def tool(db, test_user):
    row = ToolRegistry(tool_name="weather_lookup", display_name="Weather", description="d", category="rag", function_schema={"name": "weather_lookup"}, enabled=True)
    db.add(row)
    db.add(ExternalAPIKey(service_name=HA_KEY_SERVICE, api_name="Home Assistant", api_key_encrypted=encrypt_value("ha-secret"),
                          endpoint_url="https://ha.example.invalid", enabled=True, created_by_id=test_user.id))
    db.add(ExternalAPIKey(service_name="openweather", api_name="OpenWeather", api_key_encrypted=encrypt_value("ow-secret"),
                          endpoint_url="https://api.openweather.example", enabled=True, created_by_id=test_user.id))
    db.commit()
    return row


def test_the_set_is_the_home_assistant_key():
    assert NON_INJECTABLE_KEY_SERVICES == frozenset({"home-assistant"})


def test_attaching_the_home_assistant_key_to_a_tool_is_a_422_with_a_closed_phrase(owner_client, db, tool):
    response = owner_client.post(URL.format(tool.id), json={"tool_id": tool.id, "api_key_service": HA_KEY_SERVICE, "inject_as": "token", "is_required": True})
    assert response.status_code == 422 and response.json()["detail"] == NOT_INJECTABLE_REFUSAL
    assert db.query(ToolApiKeyRequirement).count() == 0
    assert "ha-secret" not in response.text


def test_the_refusal_comes_before_the_key_lookup_so_it_does_not_reveal_whether_the_key_exists(owner_client, db, tool):
    db.query(ExternalAPIKey).filter_by(service_name=HA_KEY_SERVICE).delete()
    db.commit()
    response = owner_client.post(URL.format(tool.id), json={"tool_id": tool.id, "api_key_service": HA_KEY_SERVICE, "inject_as": "token"})
    assert (response.status_code, response.json()["detail"]) == (422, NOT_INJECTABLE_REFUSAL)


def test_another_service_is_still_attachable(owner_client, db, tool):
    response = owner_client.post(URL.format(tool.id), json={"tool_id": tool.id, "api_key_service": "openweather", "inject_as": "appid"})
    assert response.status_code == 200, response.text
    assert db.query(ToolApiKeyRequirement).count() == 1


def test_the_n8n_placeholder_for_the_home_assistant_key_is_never_replaced(db, tool):
    workflow = {"nodes": [{"params": {"a": "{{ATHENA_API_KEY:home-assistant}}", "b": "{{ATHENA_API_KEY:openweather}}"}}]}
    out = asyncio.run(tool_proposals.inject_api_keys(workflow, db))
    params = out["nodes"][0]["params"]
    assert params["a"] == "{{ATHENA_API_KEY:home-assistant}}" and "ha-secret" not in str(out)
    assert params["b"] == "ow-secret"


def test_the_n8n_injection_honours_whatever_the_shared_set_holds(db, tool, monkeypatch):
    """The placeholder pattern can't even match a hyphenated service name, so the
    skip is checked against a name it can match."""
    monkeypatch.setattr(tool_proposals, "NON_INJECTABLE_KEY_SERVICES", frozenset({"openweather"}))
    workflow = {"nodes": [{"params": {"b": "{{ATHENA_API_KEY:openweather}}"}}]}
    out = asyncio.run(tool_proposals.inject_api_keys(workflow, db))
    assert out["nodes"][0]["params"]["b"] == "{{ATHENA_API_KEY:openweather}}"
