"""ATHENA-89 Phase 8 (DC17 item 1, valerie r2) -- src/orchestrator/
smart_home_controller.py's scene-activation-failed fallback (movie mode /
good morning / arriving home) used to turn_on light entity_id "all" (every
light in the house) whenever the named scene/script didn't exist. That's a
house-wide regression compared to the original hardcoded
light.living_room_all / light.office_all group entities it replaced.

Fixed: each fallback now looks up its room in HA_LIGHT_GROUPS
({"room": "light group entity"}); if the room isn't configured, it does
NOT call turn_on at all -- falls through to the existing "scene not found,
no fallback configured" message. Covers both the unconfigured (no
turn_on("all"), ever) and configured (correct room's group only) paths.
"""
from __future__ import annotations

import asyncio
import sys
from unittest import mock

import pytest

sys.path.insert(0, "src")

import orchestrator.smart_home_controller as shc  # noqa: E402


def _controller():
    return shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=mock.MagicMock())


def _set_light_groups(monkeypatch, raw: str):
    monkeypatch.setattr(shc, "_light_groups_cache", None)
    monkeypatch.setattr(shc, "_light_groups_warned", False)
    monkeypatch.setattr(shc, "get_config", lambda: mock.MagicMock(ha_light_groups=raw))


def _failing_ha_client():
    """The scene/script activation call always fails (that's what triggers
    the fallback); any subsequent light/lock call the fallback itself
    makes succeeds, so a configured fallback's own return value is
    reachable."""
    async def _call_service(domain, service, data):
        if domain in ("scene", "script"):
            raise RuntimeError("scene/script does not exist")
        return {"result": "ok"}

    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock(side_effect=_call_service)
    return ha_client


@pytest.mark.parametrize(
    "query,entity_id",
    [
        ("let's watch a movie", "scene.movie_mode"),
        ("good morning", "script.good_morning"),
        ("i'm home", "script.arriving"),
    ],
)
def test_unconfigured_room_never_calls_turn_on_with_all(monkeypatch, query, entity_id):
    _set_light_groups(monkeypatch, "")
    ha_client = _failing_ha_client()

    result = asyncio.run(
        _controller()._handle_scene_intent("activate", {"entity_id": entity_id}, ha_client, original_query=query)
    )

    for call in ha_client.call_service.await_args_list:
        args = call.args
        if len(args) >= 3 and isinstance(args[2], dict):
            assert args[2].get("entity_id") != "all" or args[0] != "light" or args[1] != "turn_on", (
                f"turn_on with entity_id='all' must never fire: {call}"
            )
    assert "may not be configured yet" in result


def test_configured_movie_mode_uses_living_room_group_not_all(monkeypatch):
    _set_light_groups(monkeypatch, '{"living_room": "light.living_room_all", "office": "light.office_all"}')
    ha_client = _failing_ha_client()

    result = asyncio.run(
        _controller()._handle_scene_intent(
            "activate", {"entity_id": "scene.movie_mode"}, ha_client, original_query="let's watch a movie"
        )
    )

    turn_on_calls = [
        c for c in ha_client.call_service.await_args_list
        if c.args[:2] == ("light", "turn_on")
    ]
    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].args[2]["entity_id"] == "light.living_room_all"
    assert "living room" in result.lower()


def test_configured_good_morning_uses_office_group_not_all(monkeypatch):
    _set_light_groups(monkeypatch, '{"living_room": "light.living_room_all", "office": "light.office_all"}')
    ha_client = _failing_ha_client()

    result = asyncio.run(
        _controller()._handle_scene_intent(
            "activate", {"entity_id": "script.good_morning"}, ha_client, original_query="good morning"
        )
    )

    turn_on_calls = [
        c for c in ha_client.call_service.await_args_list
        if c.args[:2] == ("light", "turn_on")
    ]
    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].args[2]["entity_id"] == "light.office_all"
    assert "turned on the lights" in result.lower()


def test_configured_arriving_home_uses_office_group_not_all(monkeypatch):
    _set_light_groups(monkeypatch, '{"living_room": "light.living_room_all", "office": "light.office_all"}')
    ha_client = _failing_ha_client()

    result = asyncio.run(
        _controller()._handle_scene_intent(
            "activate", {"entity_id": "script.arriving"}, ha_client, original_query="i'm home"
        )
    )

    turn_on_calls = [
        c for c in ha_client.call_service.await_args_list
        if c.args[:2] == ("light", "turn_on")
    ]
    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].args[2]["entity_id"] == "light.office_all"
    assert "welcome home" in result.lower()
