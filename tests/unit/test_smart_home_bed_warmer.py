"""ATHENA-89 Phase 7 (DC14 item 1) -- src/orchestrator/smart_home_controller.py's
bed-warmer (Sunbeam dual-zone mattress pad via Tuya) entity IDs are now
configured via HA_BED_WARMER_ENTITIES instead of hardcoded. Covers the
unconfigured/invalid-config "not configured" result and the configured
happy path using entity IDs pulled from config, not hardcoded house
literals.
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


def _controller():
    return shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=mock.MagicMock())


def _set_config(monkeypatch, raw: str):
    """The environment fallback, through the real config and resolver."""
    import shared.config

    monkeypatch.setenv("HA_BED_WARMER_ENTITIES", raw)
    shared.config._clear_cache_for_tests()


def _install_admin(**entities):
    from shared import household

    household.install_for_test(sources={"bed_warmer": entities})


def test_unconfigured_returns_clear_not_configured_message_no_ha_calls(monkeypatch):
    _set_config(monkeypatch, "")
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock()

    result = asyncio.run(
        _controller()._handle_bed_warmer_intent("turn_on", {"side": "both", "level": 3}, ha_client)
    )

    assert "isn't configured" in result.lower()
    assert "Devices page" in result
    assert "HA_BED_WARMER_ENTITIES" in result
    ha_client.call_service.assert_not_awaited()


def test_invalid_json_returns_not_configured_and_logs_error(monkeypatch):
    _set_config(monkeypatch, "{not valid json")
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock()

    result = asyncio.run(
        _controller()._handle_bed_warmer_intent("turn_off", {}, ha_client)
    )

    assert "isn't configured" in result.lower()
    ha_client.call_service.assert_not_awaited()


def test_partial_config_missing_a_required_key_returns_not_configured(monkeypatch):
    _set_config(monkeypatch, '{"level_left": "select.a", "level_right": "select.b", "power_main": "switch.c"}')
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock()

    result = asyncio.run(
        _controller()._handle_bed_warmer_intent("turn_off", {}, ha_client)
    )

    assert "isn't configured" in result.lower()
    ha_client.call_service.assert_not_awaited()


def test_configured_turn_off_uses_configured_power_main_entity(monkeypatch):
    _set_config(monkeypatch, (
        '{"level_left": "select.left", "level_right": "select.right", '
        '"power_main": "switch.custom_power_main", "power_side_a": "switch.a", '
        '"power_side_b": "switch.b"}'
    ))
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock(return_value={})

    result = asyncio.run(
        _controller()._handle_bed_warmer_intent("turn_off", {}, ha_client)
    )

    ha_client.call_service.assert_awaited_once_with(
        "switch", "turn_off", {"entity_id": "switch.custom_power_main"}
    )
    assert "off" in result.lower()


def test_configured_warm_bed_both_sides_uses_configured_level_entities(monkeypatch):
    _set_config(monkeypatch, (
        '{"level_left": "select.custom_left", "level_right": "select.custom_right", '
        '"power_main": "switch.main", "power_side_a": "switch.a", "power_side_b": "switch.b"}'
    ))
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock(return_value={})

    result = asyncio.run(
        _controller()._handle_bed_warmer_intent("warm_bed", {"side": "both", "level": 4}, ha_client)
    )

    calls = ha_client.call_service.await_args_list
    entity_ids_called = {c.args[2]["entity_id"] for c in calls}
    assert "select.custom_left" in entity_ids_called
    assert "select.custom_right" in entity_ids_called
    assert "level 4" in result.lower()


def test_admin_devices_page_value_is_used_over_the_environment(monkeypatch):
    _set_config(monkeypatch, (
        '{"level_left": "select.env_left", "level_right": "select.env_right", '
        '"power_main": "switch.env_main", "power_side_a": "switch.a", "power_side_b": "switch.b"}'
    ))
    _install_admin(level_left="select.adm_left", level_right="select.adm_right",
                   power_main="switch.adm_main", power_side_a="switch.adm_a", power_side_b="switch.adm_b")
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock(return_value={})

    asyncio.run(_controller()._handle_bed_warmer_intent("turn_off", {}, ha_client))

    ha_client.call_service.assert_awaited_once_with("switch", "turn_off", {"entity_id": "switch.adm_main"})


def test_a_partial_admin_value_is_not_mixed_with_the_environment(monkeypatch):
    _set_config(monkeypatch, "")
    _install_admin(power_main="switch.adm_main")
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock()

    result = asyncio.run(_controller()._handle_bed_warmer_intent("turn_off", {}, ha_client))

    assert "isn't configured" in result.lower()
    ha_client.call_service.assert_not_awaited()
