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
        ("switch", "turn_on", "plug in the coffee maker on"), ("light", "set_color", "make it red"),
        ("light", "increase", "more"), ("light", "decrease", "less"), ("light", "set_level", "50 percent"),
        ("light", "increase", "too dark"), ("light", "set_brightness", "at 40%"),
        ("light", "get_status", "the guest"), ("light", "check_status", "the guest"),
    ])
    def test_these_carry_a_cue(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is False, (device_type, action, query)

    @pytest.mark.parametrize("device_type,action,query", [
        ("light", "turn_on", Q), ("light", "turn_on", "let the dog out"), ("light", "turn_off", "the guest room"), ("fan", "turn_off", "everything please"), ("fan", "turn_off", "everything please"),
        ("climate", "set_temperature", "set the study to 100"),
        ("media_player", "play", "the living room"), ("fan", "turn_on", "the study please"),
        ("switch", "turn_off", "good morning"), ("light", "turn_on", ""),
    ])
    def test_these_do_not(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is True, (device_type, action, query)

    @pytest.mark.parametrize("device_type", ["lock", "scene", "script", "sensor", "oven", "fridge", "freezer", "appliance"])
    def test_doors_scenes_and_read_only_kinds_keep_their_own_rules(self, device_type):
        assert self.lacks(device_type, "unlock", Q) is False

    def test_a_cover_opening_has_the_door_rules_but_a_closing_is_judged(self):
        for action in ("open", "open_cover", "toggle"):
            assert self.lacks("cover", action, Q) is False
        assert self.lacks("cover", "close", Q) is True
        assert self.lacks("cover", "close", "close the blinds") is False

    @pytest.mark.parametrize("device_type", [None, 7, "vacuum", "robot", ""])
    def test_a_kind_with_no_table_is_refused(self, device_type):
        assert self.lacks(device_type, "turn_on", "turn on the lights") is True

    def test_no_query_is_not_judged(self):
        assert self.lacks("light", "turn_on", None) is False

    def test_every_read_only_action_passes(self):
        for action in access_target.READ_ONLY_ACTIONS:
            assert self.lacks("light", action, Q) is False
        assert self.lacks("light", " GET_STATUS ", Q) is False


class TestEveryKindIsJudged:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("device_type,action,query", [
        ("bed_warmer", "warm_bed", "warm the bed"), ("bed_warmer", "warm_bed", "mattress to level 3"),
        ("motion_control", "disable_motion", "disable motion in the study"),
        ("motion_control", "leave_lights_on", "leave the lights on for 2 hours"),
        ("motion_control", "leave_lights_off", "don't turn on the lights, I'm taking a nap"),
        ("motion_control", "leave_lights_on", "stop turning off the lights"),
        ("motion_control", "set_brightness_override", "set motion brightness to 50%"),
    ])
    def test_with_a_cue(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is False

    @pytest.mark.parametrize("device_type,action", [
        ("bed_warmer", "warm_bed"), ("motion_control", "disable_motion"), ("motion_control", "leave_lights_on"),
        ("cover", "close"), ("fan", "turn_on"), ("media_player", "play"),
    ])
    def test_without_one(self, device_type, action):
        assert self.lacks(device_type, action, Q) is True


class TestColourAndBrightnessActionsNeedEvidence:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("action", ["set_color", "set_brightness", "set_level", "increase", "decrease"])
    def test_the_action_alone_is_not_a_cue(self, action):
        assert self.lacks("light", action, "the guest room please") is True
        assert self.lacks("light", action, Q) is True

    @pytest.mark.parametrize("action,query", [
        ("set_color", "make the guest room red"), ("set_color", "sunset vibes"), ("set_brightness", "dim the guest room"),
        ("set_brightness", "50 percent"), ("set_brightness", "set it to 30%"), ("increase", "too dark"),
        ("increase", "brighter please"), ("decrease", "a little less"), ("decrease", "turn it down"),
        ("set_level", "level 3"), ("increase", "warmer"),
    ])
    def test_evidence_in_the_words_is_(self, action, query):
        assert self.lacks("light", action, query) is False

    def test_end_to_end_a_brightness_action_without_evidence_writes_nothing(self, monkeypatch):
        rig = make_rig(monkeypatch)
        answer = run(rig, post_guard(room="guest_room", action="set_brightness"), "the guest room please")
        assert answer == shc.NO_WRITE_CUE_TEXT and rig.written == []

    def test_a_turn_on_carrying_a_brightness_parameter_counts_percent_as_the_cue(self):
        params = {"brightness": 128}
        assert self.lacks("light", "turn_on", "50 percent in the kitchen", params) is False
        assert self.lacks("light", "turn_on", "at 50% in the kitchen", params) is False
        assert self.lacks("light", "turn_on", "the guest room please", params) is True
        assert self.lacks("light", "turn_on", "50 percent in the kitchen", {"brightness": None}) is True


class TestNegation:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("query", [
        "don't turn on the lights", "do not turn off the fan", "never turn the lights on", "please don't switch it on",
        "can't turn on the lights", "without turning on the lights",
    ])
    def test_a_cue_after_a_negator_does_not_count(self, query):
        assert self.lacks("light", "turn_on", query) is True, query

    @pytest.mark.parametrize("query", [
        "turn on the lights, not the kitchen", "turn on the lights and don't touch the fan", "no more lights",
        "turn on the hall lights but not the den",
    ])
    def test_a_cue_before_or_in_another_clause_still_counts(self, query):
        assert self.lacks("light", "turn_on", query) is False, query


class TestClimateWords:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("query", [
        "warm it up", "cool it down", "raise the temp", "72 in the study", "make it 72", "set the study to 21",
        "it's cold in here", "too hot in here", "crank the heat", "turn the AC on", "it's chilly", "make it 22 degrees",
        "it's freezing",
    ])
    def test_these_are_cues(self, query):
        assert self.lacks("climate", "set_temperature", query) is False, query

    @pytest.mark.parametrize("query", [Q, "set the study to 120", "make it 5", "the study please"])
    def test_these_are_not(self, query):
        assert self.lacks("climate", "set_temperature", query) is True, query


class TestControlVerbs:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("device_type,action,query", [
        ("media", "play", "change the channel"), ("media", "play", "watch the game"),
        ("media", "increase", "turn it up"), ("media", "pause", "please pause"), ("switch", "turn_on", "start it"),
        ("fan", "turn_on", "activate it"),
    ])
    def test_a_clause_initial_control_verb_is_a_cue(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is False, query

    @pytest.mark.parametrize("device_type,action,query", [
        ("switch", "turn_on", "start the coffee maker"), ("switch", "turn_on", "run the dishwasher"),
        ("switch", "turn_on", "can you start the dishwasher"), ("switch", "turn_off", "stop the washer"),
    ])
    def test_a_control_verb_acting_on_a_named_thing_needs_the_thing_to_be_a_device_of_the_house(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is True, query
        assert self.lacks(device_type, action, query, named_entity=True) is False, query

    @pytest.mark.parametrize("device_type", ["light", "fan", "switch", "media", "climate"])
    def test_let_the_guest_in_still_refuses(self, device_type):
        assert self.lacks(device_type, "turn_on", Q) is True
        assert self.lacks(device_type, "turn_on", Q, named_entity=True) is True

    def test_a_verb_in_the_middle_of_a_clause_is_not_a_cue(self):
        assert self.lacks("switch", "turn_on", "the guest said to start the car") is True


class TestTheCueMustNameThisKindOfDevice:
    problem = staticmethod(access_target.write_cue_problem)

    @pytest.mark.parametrize("device_type,query", [
        ("fan", "turn on the lights"), ("light", "turn on the fan"), ("fan", "start the coffee maker"),
        ("switch", "turn off the tv"), ("media", "turn on the thermostat"), ("climate", "turn on the lights"),
    ])
    def test_the_three_probes_and_their_kin_are_refused(self, device_type, query):
        assert self.problem(device_type, "turn_on", query) is not None, (device_type, query)

    def test_a_different_kind_is_a_wrong_device(self):
        assert self.problem("fan", "turn_on", "turn on the lights") == access_target.WRONG_DEVICE
        assert self.problem("light", "turn_on", "turn on the fan") == access_target.WRONG_DEVICE

    def test_no_device_named_at_all_passes_on_a_generic_cue(self):
        assert self.problem("fan", "turn_on", "turn it on") is None
        assert self.problem("light", "turn_off", "turn them off") is None
        assert self.problem("switch", "turn_on", "on please") is None

    def test_the_kinds_own_word_passes_beside_another_kind(self):
        assert self.problem("light", "turn_on", "turn on the lights and the fan") is None
        assert self.problem("fan", "turn_on", "turn on the lights and the fan") is None

    def test_a_switch_named_by_its_friendly_name_passes_a_control_verb(self):
        assert self.problem("switch", "turn_on", "start the coffee maker") == access_target.NO_CUE
        assert self.problem("switch", "turn_on", "start the coffee maker", named_entity=True) is None

    def test_naming_a_device_is_not_a_cue_by_itself(self):
        assert self.problem("light", "turn_on", Q, named_entity=True) == access_target.NO_CUE

    def test_a_colour_word_that_is_also_a_climate_word_names_no_other_device(self):
        assert self.problem("light", "set_color", "make it warmer") is None

    def test_end_to_end_the_wrong_device_gets_the_device_phrase_and_writes_nothing(self, monkeypatch):
        rig = make_rig(monkeypatch)
        intent = {"device_type": "fan", "action": "turn_on", "room": "study", "target_scope": "group", "parameters": {}}
        answer = run(rig, intent, "turn on the lights")
        assert answer == shc.NO_DEVICE_TEXT and rig.written == []

    def test_end_to_end_a_switch_named_in_the_house_passes_the_verb(self, monkeypatch):
        rig = make_rig(monkeypatch)
        rig.manager._entities_cache = {**rig.manager._entities_cache, "switch.coffee": {
            "entity_id": "switch.coffee", "state": "off", "attributes": {"friendly_name": "Coffee Maker"}}}
        rig.manager._build_indexes()
        intent = {"device_type": "switch", "action": "turn_on", "room": None, "target_scope": "group", "parameters": {}}
        for query in ("start the coffee maker", "start the espresso machine"):
            answer = run(rig, intent, query)
            if "coffee" in query:
                assert answer not in (shc.NO_WRITE_CUE_TEXT, shc.NO_DEVICE_TEXT), answer
            else:
                assert answer == shc.NO_WRITE_CUE_TEXT


class TestMotionNeedsAMotionPhrase:
    problem = staticmethod(access_target.write_cue_problem)

    def test_turn_off_the_lights_typed_as_motion_control_refuses(self):
        assert self.problem("motion_control", "disable_motion", "turn off the lights") == access_target.NO_CUE
        assert self.problem("motion_control", "leave_lights_off", Q) == access_target.NO_CUE

    @pytest.mark.parametrize("phrase", access_target.MOTION_CONTROL_PATTERNS)
    def test_every_routing_pattern_is_a_motion_cue(self, phrase):
        assert self.problem("motion_control", "disable_motion", f"please {phrase} now") is None

    def test_one_list_routes_and_judges(self):
        import re
        source = (Path(__file__).resolve().parents[2] / "src/orchestrator/smart_home_controller.py").read_text()
        assert "access_target.MOTION_CONTROL_PATTERNS" in source and "'lights stay on'" not in source


class TestBuiltInPhrasesAreCues:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("query", [
        "I can't see", "need some light", "too much light", "not bright enough", "fade the lights", "easy on my eyes",
        "it's too dark", "tone it down", "make it cozy", "lights out", "kill all",
    ])
    @pytest.mark.parametrize("action", ["turn_on", "turn_off", "increase", "decrease", "set_brightness"])
    def test_a_fast_path_phrase_counts_before_negation(self, query, action):
        assert self.lacks("light", action, query) is False, (query, action)

    def test_a_phrase_that_the_fast_path_does_not_know_still_needs_a_cue(self):
        assert self.lacks("light", "turn_on", "I can see fine") is True


class TestNegationIsLocal:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("query", [
        "don't forget to turn off the lights", "why don't you turn on the lights", "won't you turn on the lights",
        "I can't sleep turn off the lights", "can't sleep, turn off the lights", "you don't need to wait turn on the lights",
    ])
    def test_these_do_not_negate_the_command_after_them(self, query):
        assert self.lacks("light", "turn_off", query) is False, query

    @pytest.mark.parametrize("query", [
        "don't turn on the lights", "do not turn off the lights", "I don't want to turn on the lights",
        "never turn on the lights", "please don't switch the lights on",
    ])
    def test_a_negated_command_is_still_refused(self, query):
        assert self.lacks("light", "turn_on", query) is True, query

    def test_negation_ends_at_the_next_control_verb(self):
        assert self.lacks("light", "turn_on", "I don't like it dark turn on the lights") is False

    def test_has_negator_ignores_the_idioms(self):
        assert access_target.has_negator("no, not that one") is True
        assert access_target.has_negator("don't turn it on") is True
        assert access_target.has_negator("don't forget the kitchen") is False
        assert access_target.has_negator("turn them on") is False


class TestColourActions:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("query", [
        "turn the lights lavender", "lights lavender please", "the nursery should glow lavender", "make it glow",
        "go Packers", "Ohio State in the den", "Ravens colors", "make the nursery lavender please",
    ])
    def test_these_carry_evidence_or_a_device_word_or_a_verb(self, query):
        assert self.lacks("light", "set_color", query) is False, query

    @pytest.mark.parametrize("query", [Q, "the nursery please"])
    def test_these_do_not(self, query):
        assert self.lacks("light", "set_color", query) is True, query

    def test_team_words_come_from_the_prompts_own_table(self):
        import re
        table = access_target.TEAM_TABLE
        assert "Packers" in table and "packers" in access_target.TEAM_WORDS and "ohio" in access_target.TEAM_WORDS
        source = (Path(__file__).resolve().parents[2] / "src/orchestrator/smart_home_controller.py").read_text()
        assert "{access_target.TEAM_TABLE}" in source and "Packers(120,50)" not in source
        for name in re.findall(r"([A-Za-z0-9][A-Za-z0-9 .]*?)\(", table):
            toks = [t for t in name.lower().replace(".", "").split() if len(t) > 2 and t not in access_target._TEAM_STOPWORDS]
            assert all(t in access_target.TEAM_WORDS for t in toks), name


class TestDeviceTypeAliases:
    lacks = staticmethod(access_target.lacks_write_cue)

    @pytest.mark.parametrize("device_type,action,query", [
        ("lights", "turn_on", "turn on the lights"), ("thermostat", "set_temperature", "set the study to 70"),
        ("hvac", "set_temperature", "it's cold in here"), ("tv", "turn_on", "turn on the tv"),
        ("Lights", "turn_off", "lights off"), ("blinds", "close", "close the blinds"),
    ])
    def test_aliases_are_normalised(self, device_type, action, query):
        assert self.lacks(device_type, action, query) is False

    @pytest.mark.parametrize("device_type", ["vacuum", "robot", "garage_robot", "", None, 3])
    def test_anything_else_stays_fail_closed(self, device_type):
        assert self.lacks(device_type, "turn_on", "turn on the lights") is True


class TestRefusedTurnsAreNeverStored:
    def test_every_registered_refusal_is_recognised_exactly(self):
        from orchestrator import room_context as rc
        for text in (shc.NO_WRITE_CUE_TEXT, shc.NO_DEVICE_TEXT, shc.VERB_UNCLEAR_TEXT, shc.ONE_DOOR_REFUSAL,
                     shc.SCHEDULED_OPENING_REFUSAL, shc.UNKNOWN_SCENE_TEXT, shc.SEVERAL_ROOMS_UNSUPPORTED, rc.UNPROCESSABLE_TEXT):
            assert rc.is_refusal_text(text) is True and shc.is_refusal_text(text) is True
        assert rc.is_refusal_text("I couldn't find the guest room light") is False
        assert rc.is_refusal_text(shc.NO_WRITE_CUE_TEXT + " ") is False
        assert rc.is_refusal_text(None) is False

    def test_a_refused_turn_stores_nothing_and_a_later_turn_them_on_writes_nothing(self, monkeypatch):
        from ._room_chat_harness import Chat
        chat = Chat(monkeypatch)
        chat.llm.reply = {"device_type": "light", "room": "galley", "action": "turn_on", "target_scope": "group", "parameters": {}}
        out = chat.turn(Q)
        assert out.answer == shc.NO_WRITE_CUE_TEXT and out.error == "no_write_cue"
        assert chat.stores == [] and chat.written == []
        chat.llm.reply = {"device_type": "light", "room": None, "action": "turn_on", "target_scope": "group", "parameters": {}}
        chat.turn("turn them on", context_ref_info={"has_context_ref": True, "ref_types": ["pronoun"], "anaphora_types": ["pronoun"], "is_inquiry": False})
        assert "light.guest_lamp" not in chat.written


class TestFollowUpsCarryTheEarlierCue:
    REF = {"has_context_ref": True, "ref_types": ["pronoun"], "anaphora_types": ["pronoun"], "is_inquiry": False}

    def _chat(self, monkeypatch):
        from ._room_chat_harness import Chat
        chat = Chat(monkeypatch)
        chat.turn("turn on the galley lights")
        assert chat.written, "the first command wrote"
        chat.reset_writes()
        return chat

    def _follow(self, chat, query, room="study", **kw):
        chat.llm.reply = {"device_type": "light", "room": room, "action": "turn_on", "target_scope": "group", "parameters": {}}
        return chat.turn(query, context_ref_info=self.REF, **kw)

    @pytest.mark.parametrize("query,room,expected", [
        ("and the study too", "study", "light.study_lamp_0"),
        ("the study as well", "study", "light.study_lamp_0"),
        ("same in the study", "study", "light.study_lamp_0"),
        ("and the north bathroom too", "north", "light.north_lamp_0"),
    ])
    def test_the_same_caller_continues_the_command(self, monkeypatch, query, room, expected):
        chat = self._chat(monkeypatch)
        out = self._follow(chat, query, room)
        assert expected in chat.written, (query, out.answer, chat.written)

    def test_the_cue_is_stored_with_the_caller_and_the_chain_grows(self, monkeypatch):
        chat = self._chat(monkeypatch)
        stored = chat.stored_parameters()["write_cue"]
        assert stored["fp"] == "fp-a" and stored["device_type"] == "light" and "turn on" in stored["query"]
        self._follow(chat, "and the study too")
        assert "turn on the galley lights" in chat.stored_parameters()["write_cue"]["query"]
        chat.reset_writes()
        self._follow(chat, "and the north bathroom too", "north")
        assert "light.north_lamp_0" in chat.written

    def test_a_different_caller_gets_no_carry(self, monkeypatch):
        chat = self._chat(monkeypatch)
        out = self._follow(chat, "and the study too", fp="fp-b")
        assert chat.written == [] and out.answer == shc.NO_WRITE_CUE_TEXT

    def test_a_different_audience_gets_no_carry(self, monkeypatch):
        chat = self._chat(monkeypatch)
        guest_perms_ = {"mode": "guest", "allowed_intents": ["control"], "allowed_domains": ["light"], "restricted_entities": []}
        out = self._follow(chat, "and the study too", mode="guest", perms=guest_perms_)
        assert chat.written == [] and out.answer == shc.NO_WRITE_CUE_TEXT

    def test_a_turn_with_no_caller_fingerprint_stores_no_cue(self, monkeypatch):
        from ._room_chat_harness import Chat
        chat = Chat(monkeypatch)
        chat.turn("turn on the galley lights", fp=None)
        assert chat.written and "write_cue" not in chat.stored_parameters()

    def test_a_turn_with_a_negator_borrows_no_cue(self, monkeypatch):
        chat = self._chat(monkeypatch)
        out = self._follow(chat, "no, not the study")
        assert chat.written == [] and out.answer == shc.NO_WRITE_CUE_TEXT

    def test_the_carried_words_are_joined_with_a_clause_break(self):
        from orchestrator.nodes import route_control as rc
        state = type("S", (), {"caller_fingerprint": "fp-a", "query": "the study too", "knowledge_audience": None, "mode": "owner", "permissions": {"mode": "owner"}})()
        cue = {"fp": "fp-a", "aud": rc._audience_class(state), "device_type": "light", "action": "turn_on", "query": "turn on the galley lights"}
        got = rc._carried_cue_query({"write_cue": cue}, {"device_type": "light", "action": "turn_on"}, state)
        assert got == "turn on the galley lights. the study too"

    def test_a_different_device_gets_no_carry(self, monkeypatch):
        chat = self._chat(monkeypatch)
        chat.llm.reply = {"device_type": "fan", "room": "study", "action": "turn_on", "target_scope": "group", "parameters": {}}
        out = chat.turn("and the study too", context_ref_info=self.REF)
        assert chat.written == [] and out.answer == shc.NO_WRITE_CUE_TEXT

    def test_a_different_action_gets_no_carry(self, monkeypatch):
        chat = self._chat(monkeypatch)
        chat.llm.reply = {"device_type": "light", "room": "study", "action": "turn_off", "target_scope": "group", "parameters": {}}
        out = chat.turn("and the study too", context_ref_info=self.REF)
        assert chat.written == [] and out.answer == shc.NO_WRITE_CUE_TEXT

    def test_a_model_forged_write_cue_is_stripped(self, monkeypatch):
        from orchestrator import room_context as rc
        got = rc.strip_server_keys({"device_type": "light", "write_cue": {"fp": "fp-a", "query": "turn on"}})
        assert "write_cue" not in got


class TestMotionRoomComesFromTheWords:
    def _chat(self, monkeypatch):
        from ._room_chat_harness import Chat
        from .test_motion_control_rooms import helper_states
        chat = Chat(monkeypatch)
        for slug in ("study", "galley"):
            chat.rig.manager._entities_cache.update(helper_states(slug))
        chat.rig.manager._build_indexes()
        return chat

    def test_a_model_room_that_differs_from_the_words_is_ignored(self, monkeypatch):
        chat = self._chat(monkeypatch)
        chat.llm.reply = {"device_type": "motion_control", "room": "galley", "action": "disable_motion", "parameters": {}}
        chat.turn("disable motion in the study", room="galley")
        assert chat.written == ["input_boolean.study_motion_disable"], chat.written

    def test_with_no_room_in_the_words_the_speakers_room_is_used(self, monkeypatch):
        chat = self._chat(monkeypatch)
        chat.llm.reply = {"device_type": "motion_control", "room": "galley", "action": "disable_motion", "parameters": {}}
        chat.turn("disable motion", room="study")
        assert chat.written == ["input_boolean.study_motion_disable"], chat.written

    def test_the_prompt_does_not_tell_the_model_where_the_speaker_is(self, monkeypatch):
        chat = self._chat(monkeypatch)
        seen = []
        real = chat.llm.generate

        async def spy(**kw):
            seen.append(kw.get("prompt", ""))
            return await real(**kw)

        chat.llm.generate = spy
        chat.llm.reply = {"device_type": "motion_control", "room": None, "action": "disable_motion", "parameters": {}}
        chat.turn("disable motion", room="study")
        assert seen and all("speaking from" not in p and "Device location" not in p for p in seen)

    @pytest.mark.parametrize("action,query,ok", [
        ("disable_motion", "disable motion in the study", True), ("disable_motion", "turn the lights on please", False),
        ("enable_motion", "turn motion back on", True), ("leave_lights_on", "leave the lights on", True),
        ("leave_lights_off", "keep it dark, I'm napping", True), ("set_brightness_override", "turn on the fan", False),
        ("unknown_action", "disable motion", False),
    ])
    def test_a_retried_motion_answer_must_agree_with_the_words(self, action, query, ok):
        got = shc.SmartHomeController._retried_motion_matches_words({"action": action}, query)
        assert got is ok, (action, query)

    def test_only_known_actions_are_logged(self):
        assert "other" not in shc.MOTION_ACTIONS and "disable_motion" in shc.MOTION_ACTIONS


class TestSequenceClausesAreJudgedPerClause:
    def _plan(self, rig, steps, query):
        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
                return await rig.controller.decide_sequence_rooms(steps, query, "hall")

        return asyncio.run(go())

    def _step(self, action="turn_on", **params):
        return {"action": action, "target": {"device_type": "light", "room": "hall"}, "parameters": params,
                "delay_after": 0, "at_time": None}

    def test_a_timing_only_utterance_with_a_model_light_step_is_refused(self, monkeypatch):
        rig = make_rig(monkeypatch)
        assert self._plan(rig, [self._step()], "in 5 minutes please") == shc.NO_WRITE_CUE_TEXT

    def test_a_clause_without_a_cue_refuses_the_plan_even_when_another_has_one(self, monkeypatch):
        rig = make_rig(monkeypatch)
        steps = [self._step(), self._step()]
        assert self._plan(rig, steps, "turn on the hall lights then in 5 minutes please") == shc.NO_WRITE_CUE_TEXT

    def test_ordinary_sequences_still_plan(self, monkeypatch):
        rig = make_rig(monkeypatch)
        steps = [self._step(), self._step("turn_off")]
        assert self._plan(rig, steps, "turn on the hall lights then turn them off in 5 minutes") is None


def test_every_execute_intent_call_outside_the_sequence_executor_passes_the_words():
    import ast
    root = Path(__file__).resolve().parents[2] / "src"
    seen = 0
    for path in root.rglob("*.py"):
        if path.name == "sequence_executor.py":
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue  # a template that is not Python
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "execute_intent":
                seen += 1
                keywords = {k.arg for k in node.keywords}
                assert "original_query" in keywords or len(node.args) >= 3, f"{path}:{node.lineno}"
    assert seen >= 3


class TestRetriedMotionAnswers:
    def _chat(self, monkeypatch, reply):
        from .test_llm_strict_callers import Scripted, light_chat
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", reply, "stop"))
        return light_chat(monkeypatch, llm)

    MOTION_OFF = {"device_type": "motion_control", "room": "office", "action": "disable_motion", "parameters": {}}

    def test_an_action_that_disagrees_with_the_words_is_unverified(self, monkeypatch):
        chat = self._chat(monkeypatch, {**self.MOTION_OFF, "action": "enable_motion"})
        got = asyncio.run(chat.rig.controller._extract_motion_control_intent("disable motion in the office"))
        assert got["unprocessable_reason"] == "unverified"

    def test_an_action_that_agrees_is_returned(self, monkeypatch):
        chat = self._chat(monkeypatch, self.MOTION_OFF)
        got = asyncio.run(chat.rig.controller._extract_motion_control_intent("disable motion in the office"))
        assert got["action"] == "disable_motion"


class TestMotionInMyRoom:
    def _chat(self, monkeypatch):
        from ._room_chat_harness import Chat
        from .test_motion_control_rooms import helper_states
        chat = Chat(monkeypatch)
        for slug in ("study", "galley"):
            chat.rig.manager._entities_cache.update(helper_states(slug))
        chat.rig.manager._build_indexes()
        return chat

    @pytest.mark.parametrize("model_room", [None, "my room", "my_room", "galley"])
    def test_disable_motion_in_my_room_reaches_the_speakers_room(self, monkeypatch, model_room):
        chat = self._chat(monkeypatch)
        chat.llm.reply = {"device_type": "motion_control", "room": model_room, "action": "disable_motion", "parameters": {}}
        chat.turn("disable motion in my room", room="study")
        assert chat.written == ["input_boolean.study_motion_disable"], (model_room, chat.written)


def test_the_carried_cue_is_computed_from_the_final_intent_right_before_execution():
    source = (Path(__file__).resolve().parents[2] / "src/orchestrator/nodes/route_control.py").read_text()
    call = source.index("_carried_cue_query(state.prev_context")
    assert source.index("Ensure we have required fields") < call < source.index("await _execute_and_record(", call)
    assert source.count("_carried_cue_query(") == 2  # the definition and the one call
    assert source.index("forced after the whole merge") < call


class TestOneDeviceTypeEverywhere:
    canonical = staticmethod(access_target.canonical_device_type)

    @pytest.mark.parametrize("alias,expected", [
        ("lights", "light"), ("Lamp", "light"), ("fans", "fan"), ("plug", "switch"), ("outlet", "switch"),
        ("thermostat", "climate"), ("HVAC", "climate"), ("ac", "climate"), ("blinds", "cover"), ("shades", "cover"),
        ("television", "media_player"), ("tv", "media_player"), ("speaker", "media_player"), ("media", "media_player"),
        ("motion", "motion_control"), ("lock", "lock"), ("scene", "scene"),
    ])
    def test_each_alias_has_one_canonical_name(self, alias, expected):
        assert self.canonical(alias) == expected

    @pytest.mark.parametrize("value", ["vacuum", "robot", "", None, 3])
    def test_an_unknown_type_is_returned_unchanged_and_refused(self, value):
        assert self.canonical(value) == value
        assert access_target.lacks_write_cue(value, "turn_on", "turn on the lights") is True

    @pytest.mark.parametrize("alias,action,query,handler", [
        ("television", "turn_on", "turn on the television", "_handle_media_intent"),
        ("tv", "turn_off", "turn off the tv", "_handle_media_intent"),
        ("speakers", "pause", "pause the speakers", "_handle_media_intent"),
        ("thermostat", "set_temperature", "set the study to 70", "_handle_climate_intent"),
        ("hvac", "set_temperature", "make the hvac warmer", "_handle_climate_intent"),
        ("blinds", "close", "close the blinds", "_handle_cover_intent"),
        ("shades", "close", "close the shades", "_handle_cover_intent"),
        ("fans", "turn_on", "turn on the fans", "_handle_fan_intent"),
    ])
    def test_each_alias_reaches_its_own_handler_and_never_the_light_tail(self, monkeypatch, alias, action, query, handler):
        from unittest.mock import AsyncMock
        rig = make_rig(monkeypatch)
        for name in ("_handle_media_intent", "_handle_climate_intent", "_handle_cover_intent", "_handle_fan_intent",
                     "_dispatch_light_or_room_command"):
            monkeypatch.setattr(rig.controller, name, AsyncMock(return_value="handled"))
        intent = {"device_type": alias, "action": action, "room": "study", "target_scope": "group", "parameters": {}}
        assert run(rig, intent, query) == "handled"
        getattr(rig.controller, handler).assert_awaited_once()
        rig.controller._dispatch_light_or_room_command.assert_not_awaited()
        assert intent["device_type"] == access_target.canonical_device_type(alias)

    @pytest.mark.parametrize("alias", ["lamp", "lights"])
    def test_a_light_alias_reaches_the_light_handler(self, monkeypatch, alias):
        rig = make_rig(monkeypatch)
        run(rig, {"device_type": alias, "action": "turn_on", "room": "guest_room", "target_scope": "group", "parameters": {}},
            "turn on the guest lights")
        assert rig.written == ["light.guest_lamp"]

    def test_a_plug_is_a_switch_not_a_light(self, monkeypatch):
        rig = make_rig(monkeypatch)
        answer = run(rig, {"device_type": "plug", "action": "turn_on", "room": "guest_room", "target_scope": "group",
                           "parameters": {}}, "turn on the plug")
        assert rig.written == [], answer

    def test_the_sequence_planner_canonicalises_each_step(self, monkeypatch):
        rig = make_rig(monkeypatch)
        steps = [{"action": "turn_on", "target": {"device_type": "lamp", "room": "guest_room"}, "parameters": {},
                  "delay_after": 0, "at_time": None}]

        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
                return await rig.controller.decide_sequence_rooms(steps, "turn on the guest lamp", None)

        assert asyncio.run(go()) is None
        assert steps[0]["target"]["device_type"] == "light"

    def test_the_write_precheck_sees_the_canonical_type(self, monkeypatch):
        from ._room_chat_harness import Chat
        from orchestrator.nodes import route_control as rc
        chat = Chat(monkeypatch)
        perms = {"mode": "guest", "allowed_intents": ["control"], "allowed_domains": ["light"], "restricted_entities": []}
        reached = []
        real = chat.rig.controller.execute_intent

        async def spy(*a, **kw):
            reached.append(1)
            return await real(*a, **kw)

        chat.rig.controller.execute_intent = spy
        query = "make the study 70 degrees"
        intent = {"device_type": "thermostat", "room": "study", "action": "set_temperature",
                  "target_scope": "group", "parameters": {"temperature": 70}}

        async def go():
            with chat.patched(), mp.ha_permission_scope(perms, mode="guest", trusted_voice=True) as scope:
                state = chat.state(query, mode="guest", perms=perms)
                await rc._execute_and_record(
                    state, scope, intent, original_query=query, uk=classify_utterance(query),
                    foreign_pending=False, store_query=query,
                )
                return state

        state = asyncio.run(go())
        assert state.error == "permission_denied", (state.error, state.answer)
        assert chat.written == [] and reached == [], "refused by the coarse precheck, before the controller"


class TestTheWrongDeviceCheckComesFirst:
    problem = staticmethod(access_target.write_cue_problem)

    @pytest.mark.parametrize("device_type,action,query", [
        ("light", "increase", "make the tv brighter"), ("light", "set_brightness", "make the fan brighter"),
        ("media", "decrease", "turn the lights down"), ("light", "set_color", "make the tv red"),
        ("light", "turn_on", "I can't see the tv"), ("media_player", "turn_on", "too much light on the fan"),
    ])
    def test_another_kinds_device_word_beats_every_other_cue(self, device_type, action, query):
        assert self.problem(device_type, action, query) == access_target.WRONG_DEVICE, (device_type, query)

    @pytest.mark.parametrize("device_type,action,query", [
        ("light", "increase", "make it brighter"), ("light", "decrease", "turn the lights down"),
        ("light", "turn_on", "I can't see"), ("media", "decrease", "turn the tv down"),
        ("light", "turn_on", "turn on everything but play music in the kitchen"),
        ("light", "increase", "make the lights and the tv brighter"),
    ])
    def test_these_still_pass(self, device_type, action, query):
        assert self.problem(device_type, action, query) is None, (device_type, query)

    def test_a_negated_other_kind_does_not_make_it_wrong(self):
        assert self.problem("light", "turn_on", "turn on the lights, don't touch the tv") is None

    def test_end_to_end_the_tv_brighter_for_a_light_writes_nothing(self, monkeypatch):
        rig = make_rig(monkeypatch)
        answer = run(rig, post_guard(room="guest_room", action="set_brightness"), "make the tv brighter")
        assert answer == shc.NO_DEVICE_TEXT and rig.written == []


class TestSequenceBackingChecksTheOtherKindFirst:
    backed = staticmethod(access_target.device_backed)

    def test_a_light_brightness_or_colour_step_whose_clause_names_another_device_is_not_backed(self):
        assert self.backed("light", "make the tv brighter", "x", "set_brightness") is False
        assert self.backed("light", "make the fan red", "x", "set_color") is False

    def test_still_backed(self):
        assert self.backed("light", "make it brighter", "make it brighter", "set_brightness") is True
        assert self.backed("light", "dim the lights", "x", "set_brightness") is True
        assert self.backed("media", "turn the tv down", "x") is True
        assert self.backed("light", "make it warmer", "make it warmer", "set_color") is True

    def test_a_sequence_with_a_tv_brighter_clause_and_a_light_step_is_refused(self, monkeypatch):
        rig = make_rig(monkeypatch)
        steps = [
            {"action": "set_brightness", "target": {"device_type": "light", "room": "guest_room"}, "parameters": {},
             "delay_after": 0, "at_time": None},
            {"action": "turn_off", "target": {"device_type": "light", "room": "guest_room"}, "parameters": {},
             "delay_after": 0, "at_time": None},
        ]

        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", trusted_voice=True):
                return await rig.controller.decide_sequence_rooms(steps, "make the tv brighter then turn off the guest lights", None)

        assert asyncio.run(go()) == shc.NO_DEVICE_TEXT
        assert rig.written == []


class TestEffectCue:
    """``set_effect`` and ``clear_effect`` are backed only by the effect itself in
    the words, never by a light word, an on/off word or a control verb."""

    @staticmethod
    def cue(action, query, effect=None, device_type="light"):
        parameters = {"effect": effect} if effect is not None else {}
        return access_target.write_cue_problem(device_type, action, query, parameters)

    NO = access_target.NO_CUE

    @pytest.mark.parametrize("query,effect", [
        ("set the study prism to candlelight", "candlelight"),
        ("make the living room twinkle", "twinkle"),
        ("put the study on fire 2012", "Fire 2012"),
        ("set the prism to candlelight mode", "candlelight"),
        ("play the candle effect on the prism", "candle"),
        ("turn on the tv simulator effect on the study prism", "TV Simulator"),
    ])
    def test_the_effect_named_in_the_words_is_the_cue(self, query, effect):
        assert self.cue("set_effect", query, effect) is None

    @pytest.mark.parametrize("query,effect", [
        ("play the effect", "Rainbow"), ("turn on the effect", "twinkle"), ("set the effects on the study", "twinkle"),
        ("turn off the effect", "Rainbow"), ("turn off the effect", None), ("play the sound effect", "rain"),
        ("stop the effects", "twinkle"),
    ])
    def test_the_word_effect_alone_never_backs_a_set(self, query, effect):
        """D16: it backs only a clear."""
        assert self.cue("set_effect", query, effect) == self.NO

    @pytest.mark.parametrize("query,effect", [
        ("turn off the candlelight effect", "candlelight"), ("stop the twinkle", "twinkle"),
        ("candlelight mode off", "candlelight"), ("end the candlelight", "candlelight"),
    ])
    def test_a_clear_verb_beside_the_named_effect_is_not_a_set(self, query, effect):
        assert self.cue("set_effect", query, effect) == self.NO

    @pytest.mark.parametrize("query", [
        "set the lights to sparkle", "turn on the study lights", "make the study brighter", "set the lights to blue",
        "turn the lights on please", "the prism",
    ])
    def test_a_model_only_effect_is_no_cue_even_with_a_light_word_or_a_control_verb(self, query):
        assert self.cue("set_effect", query, "twinkle") == self.NO

    @pytest.mark.parametrize("effect", ["off", "none", "Solid", "on", "the", "Light Mode", "Effect"])
    def test_a_clear_alias_or_a_filler_effect_is_never_named(self, effect):
        assert self.cue("set_effect", f"set the prism to {effect}", effect) == self.NO

    def test_a_clear_with_a_filler_effect_is_not_backed_by_the_stop_word(self):
        assert self.cue("clear_effect", "turn off the lights", "off") == self.NO
        assert self.cue("clear_effect", "turn off the lights", "turn off") == self.NO

    def test_the_stop_word_must_be_outside_the_matched_effect(self):
        assert self.cue("clear_effect", "set the prism to stop motion", "Stop Motion") == self.NO
        assert self.cue("clear_effect", "turn off stop motion", "Stop Motion") is None
        assert self.cue("set_effect", "set the prism to stop motion", "Stop Motion") is None

    def test_a_negated_or_narrated_effect_is_no_cue(self):
        assert self.cue("set_effect", "don't set the prism to candlelight", "candlelight") == self.NO
        assert self.cue("set_effect", "they set the lights to candlelight at night", "candlelight") == self.NO
        assert self.cue("set_effect", "if it gets dark set the prism to candlelight", "candlelight") == self.NO

    def test_an_effect_named_only_in_a_negated_clause_next_to_a_clean_one_is_no_cue(self):
        query = "set the prism to twinkle but don't set it to candlelight"
        assert self.cue("set_effect", query, "candlelight") == self.NO
        assert self.cue("set_effect", query, "twinkle") is None

    def test_the_effect_of_the_light_was_nice_is_not_a_request(self):
        assert self.cue("set_effect", "the effect of the light was nice", "candlelight") == self.NO

    def test_a_near_miss_word_is_not_the_effect(self):
        assert self.cue("set_effect", "turn on the fireplace light", "Fire") == self.NO
        assert self.cue("set_effect", "turn on the fire", "Fire") is None

    def test_another_device_named_is_the_wrong_device(self):
        assert self.cue("set_effect", "make the tv twinkle", "twinkle") == access_target.WRONG_DEVICE
        assert self.cue("set_effect", "play music on the prism", "twinkle") == access_target.WRONG_DEVICE
        assert self.cue("set_effect", "play the prism", "prism") == access_target.WRONG_DEVICE

    def test_only_the_matched_words_of_an_effects_name_are_set_aside(self):
        assert self.cue("set_effect", "turn on the tv simulator effect on the study prism", "TV Simulator") is None
        assert self.cue("set_effect", "make the tv twinkle", "tv simulator") == access_target.WRONG_DEVICE
        assert self.cue("set_effect", "set the tv simulator effect on the tv", "TV Simulator") == access_target.WRONG_DEVICE

    def test_an_effect_named_like_a_device_needs_the_word_effect_or_a_light_word(self):
        assert self.cue("set_effect", "turn on the fan", "Fan") == access_target.WRONG_DEVICE
        assert self.cue("set_effect", "set the fan effect on the study prism", "Fan") is None
        assert self.cue("set_effect", "set the study lamp to fan", "Fan") is None

    def test_play_is_a_verb_only_in_the_clause_that_says_effect(self):
        assert self.cue("set_effect", "play the candle effect on the prism", "candle") is None
        assert self.cue("set_effect", "play the candle effect on the prism. then play it", "candle") == access_target.WRONG_DEVICE
        assert self.cue("set_effect", "play it. play the candle effect on the prism", "candle") == access_target.WRONG_DEVICE
        assert self.cue("set_effect", "play the sound effect", "rain") == self.NO

    def test_set_effect_is_not_a_generic_write_cue_action(self):
        assert "set_effect" not in access_target.WRITE_CUE_ACTIONS and "clear_effect" not in access_target.WRITE_CUE_ACTIONS
        assert access_target.EFFECT_ACTIONS == {"set_effect", "clear_effect"}

    @pytest.mark.parametrize("device_type", ["fan", "scene", "script", "lock", "switch", "media_player", "climate", "cover", "routine", None])
    @pytest.mark.parametrize("action", ["set_effect", "clear_effect"])
    def test_another_kind_of_device_is_never_backed_by_an_effect(self, device_type, action):
        assert self.cue(action, "set the thing to candlelight effect off", "candlelight", device_type) == self.NO

    def test_no_words_is_not_judged_as_before(self):
        assert access_target.write_cue_problem("light", "set_effect", None, {"effect": "x"}) is None

    @pytest.mark.parametrize("query,effect", [
        ("turn off the effect", None), ("stop the effects", None), ("turn the effect off", None),
        ("candlelight mode off", "candlelight"), ("stop the twinkle on the prism", "twinkle"),
        ("turn off the candlelight effect", "candlelight"), ("turn off candlelight mode", "candlelight"),
        ("no more effects", None), ("effects off", None),
    ])
    def test_clearing_needs_a_clear_word_with_the_effect_word_or_a_named_effect(self, query, effect):
        assert self.cue("clear_effect", query, effect) is None

    @pytest.mark.parametrize("query,effect", [
        ("turn them off", None), ("turn off the lights", None), ("stop it", None),
        ("turn off the study lights", "candlelight"), ("set the study to candlelight", "candlelight"),
        ("play the sound effect", None), ("set the effects on the study", None),
    ])
    def test_clearing_without_t4_words_is_no_cue(self, query, effect):
        assert self.cue("clear_effect", query, effect) == self.NO

    def test_lacks_write_cue_agrees(self):
        assert access_target.lacks_write_cue("light", "set_effect", "turn on the lights", {"effect": "twinkle"}) is True
        assert access_target.lacks_write_cue("light", "set_effect", "twinkle the lights", {"effect": "twinkle"}) is False
