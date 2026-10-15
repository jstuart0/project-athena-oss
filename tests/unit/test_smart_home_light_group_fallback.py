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

# ATHENA-69: smart_home_controller.py's ha_client-accepting methods now
# lazily import orchestrator.mode_permission (-> orchestrator.metrics ->
# prometheus_client) at call time. Stub before import, matching the
# convention used across tests/unit/test_route_control.py and friends.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import mode_permission  # noqa: E402
from shared import household  # noqa: E402


def _controller():
    manager = mock.MagicMock()
    manager.get_entities = mock.AsyncMock(return_value={})
    return shc.SmartHomeController(entity_manager=manager, llm_router=mock.MagicMock())


def _run_as_owner(coro):
    """ATHENA-69: these tests call _handle_scene_intent directly, bypassing
    execute_intent's scope-opening -- without an open owner scope, the
    guard treats the call as unscoped and falls back to a degraded
    baseline, which denies scene/script writes by the D4 floor regardless
    of what this test is actually exercising (the light-group fallback
    logic, not permissions). Open an owner scope for the duration."""
    with mode_permission.ha_permission_scope({"mode": "owner"}, mode="owner"):
        return asyncio.run(coro)


def _set_light_groups(monkeypatch, raw: str):
    monkeypatch.setattr(household, "_config", lambda: mock.MagicMock(ha_light_groups=raw))
    household.reset_room_settings_for_test()


def test_invalid_json_yields_empty_dict_and_logs_warning_not_raise(monkeypatch, caplog):
    """HA_LIGHT_GROUPS='{not json' configures nothing and never raises into the
    controller. The resolver (``shared.household``) logs the error class once
    per raw value, never the text."""
    _set_light_groups(monkeypatch, "{not valid json")

    with caplog.at_level("WARNING", logger="shared.household"):
        assert shc._get_light_groups() == {}
        assert shc._get_light_groups() == {}

    events = [r.getMessage() for r in caplog.records if "ha_light_groups_invalid_json" in r.getMessage()]
    assert events == ["ha_light_groups_invalid_json error_class=JSONDecodeError"]


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

    result = _run_as_owner(
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

    result = _run_as_owner(
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

    result = _run_as_owner(
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


def test_configured_arriving_home_uses_living_room_group_not_all(monkeypatch):
    """P9: the pre-P7 hardcoded target for this fallback was
    light.living_room_all (not light.office_all -- that's good_morning's
    room)."""
    _set_light_groups(monkeypatch, '{"living_room": "light.living_room_all", "office": "light.office_all"}')
    ha_client = _failing_ha_client()

    result = _run_as_owner(
        _controller()._handle_scene_intent(
            "activate", {"entity_id": "script.arriving"}, ha_client, original_query="i'm home"
        )
    )

    turn_on_calls = [
        c for c in ha_client.call_service.await_args_list
        if c.args[:2] == ("light", "turn_on")
    ]
    assert len(turn_on_calls) == 1
    assert turn_on_calls[0].args[2]["entity_id"] == "light.living_room_all"
    assert "welcome home" in result.lower()


@pytest.mark.parametrize("raw", [
    '{"Living Room": "light.living_room_all", "OFFICE": "light.office_all"}',
    '{"living room": "light.living_room_all", " Office ": "light.office_all"}',
])
@pytest.mark.parametrize("entity,query,expected", [
    ("scene.movie_mode", "let's watch a movie", "light.living_room_all"),
    ("script.good_morning", "good morning", "light.office_all"),
    ("script.arriving", "i'm home", "light.living_room_all"),
])
def test_scene_fallback_finds_spaced_and_cased_keys(monkeypatch, raw, entity, query, expected):
    _set_light_groups(monkeypatch, raw)
    ha_client = _failing_ha_client()

    _run_as_owner(
        _controller()._handle_scene_intent("activate", {"entity_id": entity}, ha_client, original_query=query)
    )

    turn_on_calls = [
        c for c in ha_client.call_service.await_args_list
        if c.args[:2] == ("light", "turn_on")
    ]
    assert [c.args[2]["entity_id"] for c in turn_on_calls] == [expected]
