"""A light command's reply never lists lights.

Every action on every path (a room, several rooms, a room group, the whole
house, the whole house with exclusions), the status answers, the
unavailable replies, and the fan-out gate's prompt and rewording: no entity
id, no raw room key, at most one friendly name (only when the command wrote
exactly one light), and for a status answer at most three names, only when
asked "which".
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from . import test_room_light_area_tier as base
from .test_room_light_area_tier import Rig, _reg, _state, house, light

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import write_fanout  # noqa: E402
from shared import ha_registry as hr  # noqa: E402

ENTITY_ID = re.compile(r"\b(?:light|switch|fan|group)\.[a-z0-9_]+")
_isolated = base._isolated

ACTIONS = ("turn_on", "turn_off", "set_color", "set_brightness", "increase", "decrease")
PATHS = ("single_light_room", "room", "multi_room", "room_group", "whole_house", "whole_house_except")
GROUP = {"display_name": "Upper", "name": "upper", "members": [{"room_name": "study"}, {"room_name": "hall"}]}


def intent_for(path, action):
    params = {}
    extra = {}
    if action == "set_color":
        params = {"hs_colors": [[10, 10]]}
        extra["color_description"] = "blue"
    if action == "set_brightness":
        params = {"brightness": 128}
    if action in ("increase", "decrease"):
        params = {"brightness_step": 50}
    room = {"single_light_room": "study", "room": "galley", "multi_room": "multi_room",
            "room_group": "upper", "whole_house": "whole_house", "whole_house_except": "whole_house"}[path]
    intent = light(room, action, parameters=params, **extra)
    if path == "multi_room":
        intent["rooms"] = ["galley", "study"]
    if path == "whole_house_except":
        intent["excluded_rooms"] = ["galley"]
    return intent


QUERY = {
    "turn_on": "turn on the lights", "turn_off": "turn off the lights", "set_color": "make the lights blue",
    "set_brightness": "set the lights to 50 percent", "increase": "make the lights brighter",
    "decrease": "make the lights dimmer",
}


def friendly_names(rig):
    return {
        s["attributes"]["friendly_name"].lower()
        for s in rig.manager._entities_cache.values() if s["attributes"].get("friendly_name")
    }


def assert_clean(reply, rig, *, max_names=1):
    assert not ENTITY_ID.search(reply), reply
    assert "_" not in reply, f"raw key in {reply!r}"
    lowered = reply.lower()
    names = [n for n in friendly_names(rig) if n in lowered]
    assert len(names) <= max_names, (reply, names)


SUPPORTED = {
    "multi_room": {"turn_on", "turn_off"},
    "room_group": {"turn_on", "turn_off", "set_color"},
    "whole_house": {"turn_on", "turn_off", "set_color"},
    "whole_house_except": {"turn_on", "turn_off", "set_color"},
}
CASES = [(path, action) for path in PATHS for action in ACTIONS]


def test_the_matrix_floor():
    assert len(CASES) >= 30


@pytest.mark.parametrize("path,action", CASES, ids=[f"{p}-{a}" for p, a in CASES])
def test_a_command_reply_never_lists_lights(monkeypatch, path, action):
    rig = Rig(monkeypatch, attic_state="on", resolve_group=GROUP if path == "room_group" else None,
              exclude='["(?:^|[._])(?:led_ring|status_led)(?:_|$)", "^light\\\\.galley_relay_1$"]')
    reply = rig.run(intent_for(path, action), QUERY[action])
    if action in SUPPORTED.get(path, ACTIONS):
        assert rig.written, (path, action, reply)
    else:
        assert rig.written == [] and reply == shc.SEVERAL_ROOMS_UNSUPPORTED, (path, action, reply)
    assert_clean(reply, rig)


def test_turn_on_for_a_room_is_exactly_the_documented_sentence(monkeypatch):
    rig = Rig(monkeypatch, exclude='["^light\\\\.galley_relay_1$"]')
    assert rig.run(light("galley"), "turn on the galley lights") == "Turned on the galley lights."


def test_a_single_light_is_named_once(monkeypatch):
    rig = Rig(monkeypatch)
    assert rig.run(light("study"), "turn on the study lights") == "Turned on the Study Lamp."


def test_the_whole_house_sentences(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    assert rig.run(light("whole_house", "turn_off"), "turn off all the lights") == "Turned off all the lights."


def test_brightness_wording(monkeypatch):
    rig = Rig(monkeypatch)
    assert rig.run(light("galley", "increase", parameters={"brightness_step": 50}), "brighter") == "Brightened the galley lights."
    assert rig.run(light("galley", "decrease", parameters={"brightness_step": 50}), "dimmer") == "Dimmed the galley lights."
    assert rig.run(light("galley", "set_brightness", parameters={"brightness": 102}), "40 percent") == \
        "Set the galley lights to 40% brightness."


# -- the unavailable replies ---------------------------------------------------

def test_the_unavailable_replies_do_not_list_lights(monkeypatch):
    rig = Rig(monkeypatch)
    assert_clean(rig.run(light("attic"), "turn on the attic lights"), rig)
    rig.manager._entities_cache["light.north_lamp"]["state"] = "unavailable"
    rig.manager._build_indexes()
    assert_clean(rig.run(light("multi_room", rooms=["north_bathroom", "study"]), "turn on the north bathroom and the study lights"), rig)


# -- the status answers ----------------------------------------------------------

def status(rig, room, query):
    return rig.run({"device_type": "light", "room": room, "action": "get_status"}, query)


def test_status_answers_are_counts_and_name_at_most_three_only_when_asked_which(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    assert status(rig, "galley", "are the galley lights on") == "3 lights are on in the galley."
    asked = status(rig, "galley", "which galley lights are on")
    assert asked.startswith("These lights are on in the galley: ")
    listed = asked.split(": ", 1)[1]
    assert len([n for n in re.split(r", | and ", listed.rstrip(".")) if n]) == 3
    assert_clean(status(rig, None, "are any lights on"), rig, max_names=0)
    assert status(rig, None, "are any lights on").endswith("lights are on in the house.")


def test_a_status_of_a_big_room_names_three_and_counts_the_rest(monkeypatch):
    states = {f"light.big_{i}": _state(f"light.big_{i}", name=f"Big {i}") for i in range(6)}
    rig = Rig(monkeypatch, registry=False, states=states)
    answer = status(rig, "big", "which big lights are on")
    assert answer.startswith("6 lights are on in the big, including ")
    assert len(re.findall(r"Big \d", answer)) == 3


# -- the gate's prompt and rewording speak the room, never its key -------------------

def living_room_rig(monkeypatch, threshold):
    names = {f"light.lr_{c}": _state(f"light.lr_{c}", name=f"Lr {c}") for c in "abcd"}
    raw = {"areas": [{"area_id": "area_lr", "name": "Living Room (2)", "aliases": []}], "devices": [],
           "entities": [_reg(eid, area="area_lr") for eid in names]}
    rig = Rig(monkeypatch, states=names, threshold=threshold)
    rig.registry._snapshot = hr.RegistrySnapshot.from_raw(raw, fetched_at=1.0)
    return rig


def test_the_gate_prompt_speaks_the_spoken_label_not_the_key(monkeypatch):
    rig = living_room_rig(monkeypatch, threshold=2)
    answer = rig.run(light("living_room_2"), "please")
    assert rig.written == []
    assert "living room 2" in answer and "living_room_2" not in answer and not ENTITY_ID.search(answer), answer


def test_the_rewording_speaks_the_spoken_label_not_the_key(monkeypatch):
    rig = living_room_rig(monkeypatch, threshold=2)
    answer = rig.run(light("living_room_2"), "please")
    block_text = answer
    assert "say:" in block_text.lower() or "?" in block_text
    assert "living_room_2" not in block_text


# -- structure -----------------------------------------------------------------------

def test_no_writer_builds_names_from_entity_ids():
    source = (Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "smart_home_controller.py").read_text()
    tree = ast.parse(source)
    methods = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in ("_dispatch_light_or_room_command", "_execute_multi_room_command",
                 "_execute_room_group_command", "_execute_whole_house_command"):
        text = ast.get_source_segment(source, methods[name])
        assert "light_names" not in text, name
        assert "vary_response(LIGHT_" not in text, name
    assert "light_names" not in source and "LIGHT_ON_RESPONSES" not in source
