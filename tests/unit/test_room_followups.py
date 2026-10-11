"""'Turn them off' acts where the earlier command did, unless the new words
name a room."""
from __future__ import annotations

from ._room_chat_harness import Chat
from .test_room_light_area_tier import _isolated  # noqa: F401

STUDY = {f"light.study_lamp_{i}" for i in range(3)}
GALLEY = {"light.galley_lamp_0", "light.galley_lamp_1"}
NORTH = {"light.north_lamp_0", "light.north_lamp_1"}
REF = {"has_context_ref": True, "ref_types": ["pronoun"], "anaphora_types": ["pronoun"], "is_inquiry": False}


def them_off(chat, room="study", **kw):
    chat.llm.reply = {"device_type": "light", "room": kw.pop("llm_room", None), "action": "turn_off",
                      "target_scope": "group", "parameters": {}}
    return chat.turn("turn them off", room=room, context_ref_info=REF, **kw)


def off_writes(chat):
    return [c.args[1] for c in chat.rig.raw.call_service.await_args_list]


def test_turn_them_off_after_a_named_room_uses_that_room_not_the_speakers(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("turn on the galley lights")
    assert set(chat.written) == GALLEY
    chat.reset_writes()
    them_off(chat)
    assert set(chat.written) == GALLEY and set(off_writes(chat)) == {"turn_off"}


def test_turn_them_off_after_a_room_less_command_uses_the_room_it_was_decided_in(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("the lights please")
    assert chat.stored_parameters()["decided_room"] == "study"
    chat.reset_writes()
    them_off(chat, room="galley")
    assert set(chat.written) == STUDY, "the earlier decision, not the room this turn comes from"
    assert chat.stored_parameters()["decided_room"] == "study"


def test_a_follow_up_that_names_a_room_decides_afresh(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("turn on the galley lights")
    chat.reset_writes()
    chat.llm.reply = {"device_type": "light", "room": "study", "action": "turn_off",
                      "target_scope": "group", "parameters": {}}
    chat.turn("turn them off in the study", room="galley", context_ref_info=REF)
    assert set(chat.written) == STUDY
    assert chat.stored_parameters()["decided_room"] == "study"


def test_after_a_multi_room_command_the_follow_up_acts_on_the_same_rooms(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("turn on the galley and study lights")
    assert set(chat.written) == GALLEY | STUDY
    chat.reset_writes()
    them_off(chat, room="north")
    assert set(chat.written) == GALLEY | STUDY


def test_after_all_the_lights_the_follow_up_is_the_same_explicit_scope_with_no_confirmation(monkeypatch):
    chat = Chat(monkeypatch, big=20, threshold=6, hard_limit=18)
    first = chat.turn("turn on all the lights")
    everything = set(chat.written)
    assert len(everything) > 18 and not first.answer.endswith("?")
    assert chat.stored_parameters()["decided_room"] == "whole_house"
    chat.reset_writes()
    out = them_off(chat)
    assert set(chat.written) == everything and not out.answer.endswith("?"), out.answer


def test_after_a_whole_house_command_naming_a_room_in_the_follow_up_leaves_the_whole_house(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("turn on all the lights except the galley")
    assert chat.stored_parameters()["decided_excluded"] == ["galley"]
    chat.reset_writes()
    chat.llm.reply = {"device_type": "light", "room": "study", "action": "turn_off",
                      "target_scope": "group", "parameters": {}}
    chat.turn("turn them off in the study", context_ref_info=REF)
    assert set(chat.written) == STUDY


def test_after_a_which_one_answer_the_follow_up_acts_on_the_answered_room(monkeypatch):
    chat = Chat(monkeypatch)
    chat.turn("turn on the bathroom lights")
    chat.turn("north")
    assert set(chat.written) == NORTH
    chat.reset_writes()
    them_off(chat)
    assert set(chat.written) == NORTH


def test_with_no_stored_context_the_follow_up_is_the_speakers_room(monkeypatch):
    chat = Chat(monkeypatch)
    chat.llm.reply = {"device_type": "light", "room": None, "action": "turn_off",
                      "target_scope": "group", "parameters": {}}
    chat.turn("turn them off", room="study", prev=None)
    assert set(chat.written) == STUDY
