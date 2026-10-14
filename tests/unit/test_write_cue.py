"""A device changes only when the words ask for it.

A command that changes a light, fan, switch, media player or thermostat needs
a cue in the user's words: a device word for its kind, an on/off word, or a
colour/brightness action. A room the words happen to mention ("the guest" in
"let the guest in", read as the Guest Room), the speaker's room, or a model's
whole-house guess never makes a write on its own. Doors keep their own rules.
Real controller, guard and scope over a fictional house that has a Guest Room.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import access_target  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402

from . import test_room_light_area_tier as tat  # noqa: E402
from .test_room_light_area_tier import Rig, _isolated, _reg, _state, guest, light  # noqa: E402,F401

Q = "let the guest in please"
MODEL_WHOLE_HOUSE = {"device_type": "light", "action": "turn_on", "room": "whole_house",
                     "excluded_rooms": ["guest"], "target_scope": "group", "parameters": {}}


def _house_with_guest(area_name="Guest Room"):
    real_house = tat.house

    def house(attic_state="unavailable"):
        raw, states = real_house(attic_state=attic_state)
        raw["areas"].append({"area_id": "area_guest", "name": area_name, "aliases": []})
        raw["entities"].append(_reg("light.guest_lamp", area="area_guest"))
        states["light.guest_lamp"] = _state("light.guest_lamp", name="Guest Lamp")
        return raw, states

    return house


def make_rig(monkeypatch, area_name="Guest Room", **kw):
    monkeypatch.setattr(tat, "house", _house_with_guest(area_name))
    return Rig(monkeypatch, **kw)


def run(rig, intent, query, *, mode="owner", device_room=None, ground=False):
    perms = guest(allowed_intents=["control"]) if mode == "guest" else {"mode": "owner"}

    async def go():
        with mp.ha_permission_scope(perms, mode=mode, utterance=classify_utterance(query), trusted_voice=True):
            if ground:
                rig.controller._ground_intent_rooms(intent, query)
            return await rig.controller.execute_intent(intent, rig.raw, query, device_room=device_room)

    return asyncio.run(go())


def post_guard(room=None, action="turn_on", **extra):
    return {"device_type": "light", "action": action, "room": room, "target_scope": "group", "parameters": {}, **extra}


@pytest.mark.parametrize("mode", ["guest", "owner"])
class TestTheGuestInShape:
    def test_no_write_for_let_the_guest_in(self, monkeypatch, mode):
        rig = make_rig(monkeypatch)
        answer = run(rig, post_guard(), Q, mode=mode)
        assert rig.written == [] and answer == shc.NO_WRITE_CUE_TEXT

    def test_no_write_when_the_speaker_is_in_the_guest_room(self, monkeypatch, mode):
        rig = make_rig(monkeypatch)
        run(rig, post_guard(), Q, mode=mode, device_room="guest")
        assert rig.written == []

    def test_a_model_whole_house_with_guest_excluded_gives_no_write(self, monkeypatch, mode):
        rig = make_rig(monkeypatch)
        run(rig, dict(MODEL_WHOLE_HOUSE), Q, mode=mode, ground=True)
        assert rig.written == []

    def test_an_exact_name_room_called_guest_behaves_the_same(self, monkeypatch, mode):
        rig = make_rig(monkeypatch, area_name="Guest")
        run(rig, post_guard(), Q, mode=mode, device_room="guest")
        assert rig.written == []
        run(rig, post_guard(), "turn on the guest lights", mode=mode)
        assert "light.guest_lamp" in rig.written


@pytest.mark.parametrize("mode", ["guest", "owner"])
@pytest.mark.parametrize("query,action", [
    ("turn on the guest room", "turn_on"),
    ("turn on the guest lights", "turn_on"),
    ("guest room lights on", "turn_on"),
    ("dim the guest room", "set_brightness"),
    ("dim the guest room", "turn_on"),
    ("guest room off", "turn_off"),
])
def test_a_command_with_a_cue_still_writes(monkeypatch, mode, query, action):
    rig = make_rig(monkeypatch)
    run(rig, post_guard(action=action), query, mode=mode)
    assert rig.written == ["light.guest_lamp"], (query, rig.written)


def test_reads_are_unchanged(monkeypatch):
    rig = make_rig(monkeypatch)
    for query in ("is the guest room light on", Q):
        answer = run(rig, post_guard(action="get_status", room="guest_room"), query)
        assert answer != shc.NO_WRITE_CUE_TEXT, query
    assert rig.written == []


def test_no_words_means_an_internal_caller_and_is_not_judged(monkeypatch):
    rig = make_rig(monkeypatch)

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
            return await rig.controller.execute_intent(post_guard(room="guest_room"), rig.raw)

    asyncio.run(go())
    assert rig.written == ["light.guest_lamp"]


class TestTheCueTable:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("device_type,action,query", [
        ("light", "turn_off", "kill the lights"), ("light", "turn_off", "kill all"),
        ("light", "turn_off", "lights out"), ("light", "turn_off", "shut it off"),
        ("light", "turn_off", "everything off"), ("light", "turn_off", "all off"),
        ("light", "turn_on", "get the kitchen lit"), ("light", "turn_off", "no more lights"),
        ("light", "turn_on", "brighten up the study"), ("light", "turn_off", "get everything dark apart from the galley"),
        ("light", "turn_on", "it's dark in here"), ("light", "turn_off", "the whole house"), ("light", "turn_off", "everything"), ("light", "turn_on", "dim the study"),
        ("climate", "set_temperature", "set the study to 70"),
        ("climate", "set_temperature", "make it warmer"),
        ("climate", "set_temperature", "set the thermostat to 68"),
        ("media_player", "pause", "pause the tv"), ("media", "pause", "pause"), ("media", "play", "play some music"),
        ("tv", "turn_off", "stop the tv"), ("speaker", "increase", "louder"),
        ("fan", "turn_on", "fan on high"), ("fan", "turn_on", "turn on the fan"),
        ("switch", "turn_on", "plug in the coffee maker on"), ("light", "set_color", "make it cozy"),
        ("light", "increase", "more"), ("light", "decrease", "less"), ("light", "set_level", "five"),
        ("light", "get_status", "the guest"), ("light", "check_status", "the guest"),
    ])
    def test_these_carry_a_cue(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is False, (device_type, action, query)

    @pytest.mark.parametrize("device_type,action,query", [
        ("light", "turn_on", Q), ("light", "turn_on", "let the dog out"), ("light", "turn_off", "the guest room"), ("fan", "turn_off", "everything please"), ("fan", "turn_off", "everything please"),
        ("climate", "set_temperature", "it's cold in here"), ("climate", "set_temperature", "set the study to 100"),
        ("media_player", "play", "the living room"), ("fan", "turn_on", "the study please"),
        ("switch", "turn_off", "good morning"), ("light", "turn_on", ""),
    ])
    def test_these_do_not(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is True, (device_type, action, query)

    @pytest.mark.parametrize("device_type", ["lock", "cover", "scene", "motion_control", "sensor", "oven", "bed_warmer", None, 7])
    def test_doors_and_other_kinds_keep_their_own_rules(self, device_type):
        assert self.lacks(device_type, "unlock", Q) is False

    def test_no_query_is_not_judged(self):
        assert self.lacks("light", "turn_on", None) is False

    def test_every_read_only_action_passes(self):
        for action in access_target.READ_ONLY_ACTIONS:
            assert self.lacks("light", action, Q) is False
        assert self.lacks("light", " GET_STATUS ", Q) is False


@pytest.mark.parametrize("action", ["set_color", "set_brightness", "set_level", "increase", "decrease"])
def test_a_colour_or_brightness_action_is_a_cue_on_its_own(monkeypatch, action):
    rig = make_rig(monkeypatch)
    answer = run(rig, post_guard(room="guest_room", action=action), "the guest room please")
    assert answer != shc.NO_WRITE_CUE_TEXT
