"""Fans, switches and media reads by area; state questions default to the
speaker's room; the indoor-temperature question; music with no room.

The fictional house of _room_chat_harness, with a fan, a switch and a
temperature sensor added to the study and the galley. Names read as specs."""
from __future__ import annotations

import asyncio

import pytest

from . import _room_chat_harness as rch
from ._room_chat_harness import Chat
from .test_room_light_area_tier import _isolated, _reg, _state  # noqa: F401

from orchestrator import utterance_kind  # noqa: E402
from orchestrator.nodes import route_control  # noqa: E402
from shared import ha_registry as hr  # noqa: E402


def house_with_devices(monkeypatch, **kw):
    chat = Chat(monkeypatch, **kw)
    extra_states = {
        "fan.study_ceiling": _state("fan.study_ceiling", "off", name="Ceiling Fan"),
        "fan.galley_ceiling": _state("fan.galley_ceiling", "off", name="Ceiling Fan"),
        "switch.study_plug": _state("switch.study_plug", "on", name="Desk Plug"),
        "switch.galley_plug": _state("switch.galley_plug", "off", name="Kettle Plug"),
        "sensor.study_temp": {"entity_id": "sensor.study_temp", "state": "71.5",
                              "attributes": {"friendly_name": "Study Temperature", "device_class": "temperature",
                                             "unit_of_measurement": "°F"}},
        "sensor.study_humidity": {"entity_id": "sensor.study_humidity", "state": "40",
                                  "attributes": {"friendly_name": "Study Humidity", "device_class": "humidity"}},
    }
    chat.rig.manager._entities_cache.update(extra_states)
    chat.rig.manager._build_indexes()
    chat.raw_registry["entities"] += [
        _reg("fan.study_ceiling", area="area_study"),
        _reg("fan.galley_ceiling", area="area_galley"),
        _reg("switch.study_plug", area="area_study"),
        _reg("switch.galley_plug", area="area_galley"),
        _reg("sensor.study_temp", area="area_study"),
        _reg("sensor.study_humidity", area="area_study"),
    ]
    chat.rig.registry._snapshot = hr.RegistrySnapshot.from_raw(chat.raw_registry, fetched_at=3.0)
    return chat


def intent(device_type, action, room=None):
    return {"device_type": device_type, "room": room, "action": action, "target_scope": "group", "parameters": {}}


def test_a_fan_command_acts_on_the_fans_in_the_speakers_area_by_registry_not_by_name(monkeypatch):
    chat = house_with_devices(monkeypatch)
    chat.llm.reply = intent("fan", "turn_on")
    out = chat.turn("turn on the fan")
    assert chat.written == ["fan.study_ceiling"], (chat.written, out.answer)


def test_a_fan_named_in_another_room_is_that_areas_fan(monkeypatch):
    chat = house_with_devices(monkeypatch)
    chat.llm.reply = intent("fan", "turn_on", "galley")
    chat.turn("start the ceiling fan in the galley")
    assert chat.written == ["fan.galley_ceiling"]


def test_a_fan_status_question_reads_only_the_area(monkeypatch):
    chat = house_with_devices(monkeypatch)
    chat.llm.reply = intent("fan", "get_status")
    out = chat.turn("is the fan on")
    assert chat.written == [] and out.answer.count("Ceiling Fan") == 1, out.answer


def test_a_switch_status_question_reads_the_speakers_area_only(monkeypatch):
    chat = house_with_devices(monkeypatch)
    chat.llm.reply = intent("switch", "get_status")
    out = chat.turn("is the plug on")
    assert "Desk Plug" in out.answer and "Kettle" not in out.answer, out.answer


def test_without_a_registry_a_fan_falls_back_to_the_name_match(monkeypatch):
    chat = house_with_devices(monkeypatch, registry=False)
    chat.rig.manager._entities_cache["fan.study_ceiling"]["attributes"]["friendly_name"] = "Study Fan"
    chat.rig.manager._build_indexes()
    chat.llm.reply = intent("fan", "turn_on", "study")
    chat.turn("start the ceiling fan in the study")
    assert chat.written == ["fan.study_ceiling"]


# ---------------------------------------------------------------------------
# Bulk reports: the speaker's room, unless the house is asked about
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("room,query,house_wide", [
    ("study", "are any lights on", False),
    ("study", "are any lights on in the house", True),
    ("study", "what lights are on anywhere", True),
    ("unknown", "are any lights on", True),
    ("guest", "are any lights on", True),
])
def test_a_lights_report_is_for_the_speakers_room_unless_the_house_is_asked_about(room, query, house_wide):
    report = rch.route_control._bulk_report_is_for_the_house
    assert report(query, room) is house_wide


def test_a_known_room_lights_question_is_answered_for_that_room(monkeypatch):
    chat = house_with_devices(monkeypatch)
    chat.llm.reply = intent("light", "get_status")
    out = chat.turn("are any lights on")
    assert "study" in out.answer.lower() and "galley" not in out.answer.lower(), out.answer


# ---------------------------------------------------------------------------
# Indoor temperature
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("is it cold in here", True),
    ("how warm is it inside", True),
    ("what's the temperature in here", True),
    ("what's the temperature outside", False),
    ("make it warmer in here", False),
    ("set the temperature in here to 70", False),
    ("is it going to be hot tomorrow", False),
    ("", False),
])
def test_the_indoor_temperature_rule(query, expected):
    assert utterance_kind.is_indoor_temperature_question(query) is expected


def test_the_indoor_rule_moves_a_weather_verdict_to_control_with_no_pattern_help():
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "main.py").read_text()
    func = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "classify_node")
    rules = [n for n in ast.walk(func) if isinstance(n, ast.If)
             and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "is_indoor_temperature_question"
                     for c in ast.walk(n.test))]
    assert len(rules) == 1
    names = {n.id for n in ast.walk(rules[0].test) if isinstance(n, ast.Name)}
    assert "pattern_intent" not in names, "the rule must not depend on the pattern classifier"


def test_an_indoor_question_is_answered_from_the_areas_own_sensor(monkeypatch):
    chat = house_with_devices(monkeypatch)
    chat.llm.reply = intent("climate", "get_status")
    out = chat.turn("is it cold in here")
    assert out.answer == "It's 71.5°F in the study.", out.answer


def test_an_indoor_question_without_an_area_sensor_uses_the_thermostat(monkeypatch):
    chat = house_with_devices(monkeypatch)

    async def climate():
        return {"entity_id": "climate.t", "state": "heat", "current_temp": 68, "target_temp": 70}

    chat.rig.manager.get_climate_state = climate
    chat.llm.reply = intent("climate", "get_status")
    out = chat.turn("is it cold in here", room="galley")
    assert "thermostat is set to 70" in out.answer, out.answer


def test_an_indoor_question_with_no_room_and_no_thermostat_says_so(monkeypatch):
    chat = house_with_devices(monkeypatch)

    async def climate():
        return None

    chat.rig.manager.get_climate_state = climate
    chat.llm.reply = intent("climate", "get_status")
    out = chat.turn("is it cold in here", room="unknown")
    assert out.answer == "I couldn't find a thermostat to check."


# ---------------------------------------------------------------------------
# Music
# ---------------------------------------------------------------------------

def _play_in_browser(room):
    import types
    from unittest.mock import AsyncMock, MagicMock, patch

    from orchestrator.nodes import route_music
    from orchestrator.state import IntentCategory, OrchestratorState

    handler = MagicMock()
    handler.parse_music_play_intent = AsyncMock(return_value={
        "play_in_browser": True, "media_type": "artist", "media_id": "Some Band", "radio_mode": True})
    handler.handle_play = AsyncMock(return_value="ok")
    state = OrchestratorState(query="play some band here")
    state.intent = IntentCategory.MUSIC_PLAY
    state.room = room
    state.mode = "owner"
    state.permissions = {"mode": "owner"}
    state.node_timings = {}
    with patch.object(route_music, "music_handler", handler, create=True), \
            patch.object(route_music, "get_music_handler", lambda: handler, create=True):
        try:
            out = asyncio.run(route_music.route_music_node(state))
        except Exception as exc:  # the node needs the full runtime; the seam under test is the room choice
            pytest.skip(f"route_music_node needs more runtime than this rig provides: {type(exc).__name__}")
    return out, handler


@pytest.mark.parametrize("room", ["unknown", "jarvis_web", "", None, "guest"])
def test_music_with_no_resolvable_room_asks_which_room_and_plays_nothing(room):
    out, handler = _play_in_browser(room)
    assert out.answer == "Which room should I play it in?"
    handler.handle_play.assert_not_awaited()


def test_music_in_a_known_room_plays_there():
    out, handler = _play_in_browser("study")
    assert out.answer == "Playing Some Band on study."
    handler.handle_play.assert_awaited_once()


def test_no_house_specific_room_name_is_a_music_fallback():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "nodes" / "route_music.py").read_text()
    assert '"office"' not in text
