"""Motion control: the rooms that have it are discovered from Home Assistant's
states (``input_boolean.<room>_motion_disable``), the room comes from the
decided room (named, else the speaker's), and no house's room names live in
the handler. Plus the occupancy answer naming a room once.

The fictional house of _room_chat_harness (a study and a galley with Home
Assistant areas) plus a nursery that has helpers and no area."""
from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from . import _room_chat_harness as rch
from ._room_chat_harness import Chat

import orchestrator.smart_home_controller as shc  # noqa: E402
import shared.config  # noqa: E402
from orchestrator import ha_entity_manager as hem  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import write_fanout  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    for reset in (_runtime.reset_for_test, shared.config._clear_cache_for_tests, hem._clear_room_light_exclude_cache):
        reset()
    yield
    for reset in (_runtime.reset_for_test, shared.config._clear_cache_for_tests, hem._clear_room_light_exclude_cache):
        reset()


def _state(entity_id, state="on", name=None):
    return {"entity_id": entity_id, "state": state,
            "attributes": {"friendly_name": name or entity_id.split(".")[1].replace("_", " ").title()}}


def guest(*restricted):
    return {"mode": "guest", "allowed_domains": ["input_boolean", "input_number", "light"], "allowed_intents": ["control"],
            "restricted_entities": list(restricted)}


HELPERS = (
    ("input_boolean", "motion_disable"), ("input_boolean", "leave_lights_on"),
    ("input_boolean", "leave_lights_off"), ("input_boolean", "brightness_override"),
    ("input_number", "leave_lights_on_minutes"), ("input_number", "leave_lights_off_minutes"),
    ("input_number", "override_brightness"),
)


def helper_states(slug, names=None):
    wanted = set(names) if names is not None else {n for _, n in HELPERS}
    return {
        f"{d}.{slug}_{n}": _state(f"{d}.{slug}_{n}", "off", name=f"{slug} {n}")
        for d, n in HELPERS if n in wanted
    }


def house(monkeypatch, helpers=("study", "nursery")):
    chat = Chat(monkeypatch)
    for slug in helpers:
        chat.rig.manager._entities_cache.update(helper_states(slug))
    chat.rig.manager._build_indexes()
    return chat


def intent(room=None, action="disable_motion", **params):
    return {"device_type": "motion_control", "room": room, "action": action,
            "target_scope": "group", "parameters": params}


class TestDiscovery:
    def test_a_room_with_helpers_but_no_area_works(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent("nursery")
        out = chat.turn("disable motion in the nursery")
        assert chat.written == ["input_boolean.nursery_motion_disable"], (chat.written, out.answer)
        assert "nursery" in out.answer.lower()

    def test_the_speakers_room_is_used_when_none_is_named(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent(None)
        chat.turn("disable motion", room="study")
        assert chat.written == ["input_boolean.study_motion_disable"]

    def test_my_room_is_the_speakers_room_not_a_fixed_one(self, monkeypatch):
        chat = house(monkeypatch, helpers=("study", "galley"))
        chat.llm.reply = intent("my room")
        chat.turn("disable motion in my room", room="galley")
        assert chat.written == ["input_boolean.galley_motion_disable"]

    def test_a_room_without_helpers_is_told_which_have_them(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent("galley")
        out = chat.turn("disable motion in the galley")
        assert chat.written == []
        assert out.answer.startswith(shc.MOTION_NOT_SET_UP), out.answer
        assert "nursery and study" in out.answer and "galley" not in out.answer.lower()

    def test_no_helpers_anywhere_gets_the_neutral_reply(self, monkeypatch):
        chat = house(monkeypatch, helpers=())
        chat.llm.reply = intent("study")
        out = chat.turn("disable motion in the study")
        assert chat.written == []
        assert out.answer == shc.MOTION_NOT_SET_UP, out.answer

    def test_rooms_are_discovered_from_the_motion_disable_helper_only(self, monkeypatch):
        chat = house(monkeypatch, helpers=())
        chat.rig.manager._entities_cache.update(helper_states("study", names=["leave_lights_on"]))
        chat.rig.manager._build_indexes()
        rooms = asyncio.run(chat.rig.controller._motion_control_rooms())
        assert rooms == {}

    def test_a_room_missing_a_helper_an_action_needs_writes_nothing(self, monkeypatch):
        chat = house(monkeypatch, helpers=())
        chat.rig.manager._entities_cache.update(helper_states("study", names=["motion_disable"]))
        chat.rig.manager._build_indexes()
        chat.llm.reply = intent("study", "leave_lights_on", duration_minutes=60)
        out = chat.turn("leave the lights on in the study")
        assert chat.written == [], chat.written
        assert "isn't fully set up" in out.answer, out.answer

    def test_leave_lights_on_writes_the_duration_then_the_flag(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent("study", "leave_lights_on", duration_minutes=120)
        out = chat.turn("leave the lights on in the study for 2 hours")
        assert chat.written == [
            "input_number.study_leave_lights_on_minutes", "input_boolean.study_leave_lights_on",
        ], (chat.written, out.answer)
        assert "2 hours" in out.answer


class TestReplyNeverNamesAHardcodedRoom:
    def test_the_two_handlers_hold_no_room_name_from_the_vocabulary_list(self):
        from .test_room_vocabulary_drift import ROOM_NOUNS
        tree = ast.parse((ROOT / "src/orchestrator/smart_home_controller.py").read_text(encoding="utf-8"))
        names = {"_handle_motion_control_intent", "_extract_motion_control_intent",
                 "_motion_control_rooms", "_motion_rooms_reply", "_current_room_brightness"}
        nouns = {noun.replace("_", " ") for noun in ROOM_NOUNS}
        found = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                names.discard(node.name)
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        text = sub.value.lower().replace("_", " ")
                        hit = [n for n in nouns if re.search(rf"\b{n}\b", text)]
                        if hit:
                            found.append((node.name, sorted(hit)))
        assert not names, f"pinned functions not found: {names}"
        assert not found, found

    def test_the_pin_detects_a_planted_room_name(self):
        planted = 'def f():\n    return "Motion control is only in the office"\n'
        tree = ast.parse(planted)
        from .test_room_vocabulary_drift import ROOM_NOUNS
        nouns = {noun.replace("_", " ") for noun in ROOM_NOUNS}
        consts = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        assert any(re.search(rf"\b{n}\b", c.lower()) for c in consts for n in nouns)


class TestGateStillBindsTheWrite:
    def test_a_question_is_stopped_at_the_gate_before_any_write(self, monkeypatch):
        chat = house(monkeypatch)
        controller = chat.rig.controller
        raw = chat.rig.raw

        async def drive():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner",
                                        utterance=classify_utterance("is motion off in the study?")):
                answer = await controller._handle_motion_control_intent(
                    "disable_motion", {}, raw, "study", "is motion off in the study?")
                return answer, write_fanout.take_block()

        _, block = asyncio.run(drive())
        assert raw.call_service.await_count == 0
        assert block is not None


class TestMotionStatusNamesARoomOnce:
    def _status(self, rooms):
        controller = shc.SmartHomeController(entity_manager=MagicMock(), llm_router=MagicMock())
        data = [
            {"entity_id": f"binary_sensor.s{i}_motion", "room": room, "state": "on",
             "last_changed": "2026-10-13T10:00:00+00:00", "sensor_type": "pir"}
            for i, room in enumerate(rooms)
        ]
        return asyncio.run(controller._format_motion_status(data))

    def test_two_sensors_in_one_room_name_it_once(self):
        assert self._status(["Nursery", "Nursery"]) == "Motion detected in: Nursery."

    def test_spelling_variants_of_one_room_are_one_room_and_order_is_kept(self):
        assert self._status(["Den", "Tv Room", "TV room", "Den"]) == "Motion detected in: Den, Tv Room."


@pytest.mark.parametrize("phrase,expected", [
    ("my room", True), ("My Room", True), ("here", True), ("this room", True),
    ("nursery", False), ("my_room_two", False), (None, False), ("", False),
])
def test_a_speaker_relative_room_phrase_is_not_a_room_name(phrase, expected):
    from orchestrator import room_context
    assert room_context.is_speaker_relative(phrase) is expected


class TestPermissions:
    def test_a_restricted_room_never_appears_in_the_reply(self, monkeypatch):
        chat = house(monkeypatch)  # study and nursery have helpers
        chat.llm.reply = intent("galley")
        out = chat.turn("disable motion in the galley", perms=guest(r"input_\w+\.nursery_.*"), mode="guest")
        assert chat.written == []
        assert out.answer == f"{shc.MOTION_NOT_SET_UP} It's set up for study.", out.answer

    def test_every_room_restricted_gets_the_bare_neutral_reply(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent("galley")
        out = chat.turn("disable motion in the galley", perms=guest(r"input_\w+\.(study|nursery)_.*"), mode="guest")
        assert out.answer == shc.MOTION_NOT_SET_UP, out.answer

    def test_a_denied_room_is_refused_not_told_its_helpers_are_missing(self, monkeypatch):
        chat = house(monkeypatch, helpers=())
        chat.rig.manager._entities_cache.update(helper_states("study", names=["motion_disable"]))
        chat.rig.manager._build_indexes()
        chat.llm.reply = intent("study", "leave_lights_on", duration_minutes=60)
        out = chat.turn("leave the lights on in the study", perms=guest(r"input_\w+\.study_.*"), mode="guest")
        assert chat.written == []
        assert "set up" not in out.answer and "helpers" not in out.answer and "trouble" not in out.answer, out.answer

    def test_one_denied_helper_in_the_write_set_refuses_the_whole_command(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent("study", "leave_lights_on", duration_minutes=60)
        out = chat.turn("leave the lights on in the study",
                        perms=guest(r"input_number\.study_leave_lights_on_minutes"), mode="guest")
        assert chat.written == [], chat.written
        assert "trouble" not in out.answer, out.answer

    def test_enable_motion_checks_all_four_helpers(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent("study", "enable_motion")
        chat.turn("turn motion back on in the study", perms=guest(r"input_boolean\.study_brightness_override"), mode="guest")
        assert chat.written == []

    def test_a_fully_permitted_guest_command_writes(self, monkeypatch):
        chat = house(monkeypatch)
        chat.llm.reply = intent("study", "leave_lights_on", duration_minutes=60)
        chat.turn("leave the lights on in the study", perms=guest(r"input_\w+\.nursery_.*"), mode="guest")
        assert chat.written == ["input_number.study_leave_lights_on_minutes", "input_boolean.study_leave_lights_on"]


class TestRoomWords:
    def test_here_with_no_speaker_room_asks_which_room(self, monkeypatch):
        chat = Chat(monkeypatch)
        chat.llm.reply = {"device_type": "light", "room": "here", "action": "turn_on",
                          "target_scope": "group", "parameters": {}}
        out = chat.turn("turn on the lights here", room="unknown", trusted_voice=False)
        assert chat.written == [] and "which" in out.answer.lower(), (chat.written, out.answer)

    def test_a_named_room_beats_here(self, monkeypatch):
        def run(query, room_in_reply):
            chat = Chat(monkeypatch)
            chat.llm.reply = {"device_type": "light", "room": room_in_reply, "action": "turn_on",
                              "target_scope": "group", "parameters": {}}
            chat.turn(query, room="study")
            return set(chat.written)
        here = run("turn on the lights here", "here")
        named = run("turn on the lights in the galley", "here")
        assert here and named and here.isdisjoint(named)


@pytest.mark.parametrize("entity_id", [
    "input_boolean.a__b_motion_disable", "input_boolean.x.y_motion_disable",
    "input_boolean._motion_disable", "input_boolean.Study_motion_disable",
    "input_boolean.a b_motion_disable", "input_number.study_motion_disable",
])
def test_hostile_helper_ids_are_ignored_by_discovery(monkeypatch, entity_id):
    chat = house(monkeypatch, helpers=("study",))
    chat.rig.manager._entities_cache[entity_id] = _state(entity_id, "off")
    chat.rig.manager._build_indexes()
    assert asyncio.run(chat.rig.controller._motion_control_rooms()) == {"study": "study"}
