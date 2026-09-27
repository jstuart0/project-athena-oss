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

sys.path.insert(0, "src")

import orchestrator.smart_home_controller as shc  # noqa: E402


def _controller():
    return shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=mock.MagicMock())


def _set_config(monkeypatch, raw: str):
    monkeypatch.setattr(shc, "get_config", lambda: mock.MagicMock(ha_bed_warmer_entities=raw))


def test_unconfigured_returns_clear_not_configured_message_no_ha_calls(monkeypatch):
    _set_config(monkeypatch, "")
    ha_client = mock.MagicMock()
    ha_client.call_service = mock.AsyncMock()

    result = asyncio.run(
        _controller()._handle_bed_warmer_intent("turn_on", {"side": "both", "level": 3}, ha_client)
    )

    assert "isn't configured" in result.lower()
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
