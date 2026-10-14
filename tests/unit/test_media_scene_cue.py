"""Scenes, routines, music and TV are written only for a request (T3).

A statement that mentions a scene, a song or the TV is not a command to run
it. Every existing ``SCENE_PHRASES`` key still runs its routine when it is the
whole utterance (D15), with an optional okay/hey and assistant name, "please",
or a trailing name; inside a longer sentence it needs a command frame ("run
goodbye", "start bedtime mode"). Music and TV writes need an IMPERATIVE
utterance or an UNKNOWN one with a request clause; a question is never refused
here. Home Assistant writes are counted at the guarded channel.

Synthetic utterances only.
"""
from __future__ import annotations

import pytest

from orchestrator.nodes import _runtime  # noqa: F401  (the harness resets it)
from orchestrator.smart_home_controller import (
    NO_WRITE_CUE_TEXT,
    SCENE_PHRASES,
    scene_request_allowed,
)
from orchestrator.state import IntentCategory
from orchestrator.utterance_kind import classify_utterance

from ._clause_harness import drive_control, drive_music, drive_tv, run, writes

NAMES = ("nebu",)
SCENES = {"good night": "script.good_night", "goodnight": "script.good_night", "goodbye": "script.leaving",
          "bedtime": "script.good_night", "i'm home": "script.arriving", "movie": "scene.movie_mode"}


def _scene(query):
    state, _model, client = drive_control(query, names=NAMES, scenes=SCENES)
    return state, writes(client)


def _routine(entity):
    return [(entity.split(".")[0], "turn_on", (entity,))]


# --- D15: every existing key works as the whole utterance --------------------------------


@pytest.mark.parametrize("key", sorted(SCENE_PHRASES), ids=str)
def test_every_scene_phrase_runs_its_routine_as_the_whole_utterance(key):
    state, calls = _scene(key)
    assert calls == _routine(SCENE_PHRASES[key]), (key, calls, state.answer)


@pytest.mark.parametrize("utterance,entity", [
    ("okay, goodnight", "script.good_night"),
    ("hey athena goodbye", "script.leaving"),
    ("goodbye, nebu", "script.leaving"),
    ("goodbye nebu", "script.leaving"),
    ("please goodnight", "script.good_night"),
    ("I'm home", "script.arriving"),
    ("Bedtime.", "script.good_night"),
    ("run goodbye", "script.leaving"),
    ("start bedtime mode", "script.good_night"),
    ("please run goodnight", "script.good_night"),
    ("let's watch a movie", "scene.movie_mode"),
], ids=str)
def test_the_routine_runs_with_its_vocatives_and_in_a_command_frame(utterance, entity):
    state, calls = _scene(utterance)
    assert calls == _routine(entity), (utterance, calls, state.answer)


@pytest.mark.parametrize("utterance", [
    "I said good night to the kids",
    "the show ended with goodbye",
    "the kids say goodnight every night",
    "they always say bedtime at eight",
    "if the kids say goodbye, tell them I'm home",
    "my partner said goodnight",
], ids=str)
def test_a_phrase_inside_a_sentence_that_is_no_command_runs_nothing(utterance):
    state, calls = _scene(utterance)
    assert calls == [], (utterance, calls)
    assert state.answer == NO_WRITE_CUE_TEXT


def test_the_scene_gate_reads_the_classification():
    names = NAMES
    for text, allowed in (
        ("goodnight", True), ("okay jarvis, goodnight", True), ("run goodnight", True),
        ("the kids say goodnight", False), ("I said good night to the kids", False),
        # A question is judged by the read-only scope, not here.
        ("is good night mode on", True),
    ):
        assert scene_request_allowed(classify_utterance(text, names)) is allowed, text
    assert scene_request_allowed(None) is True  # nothing to judge


def test_a_confirmed_replay_of_a_scene_is_not_judged_by_the_confirming_word():
    # The replaying turn says "yes"; the scene was asked for by the earlier turn.
    from orchestrator import mode_permission as mp, write_fanout
    from unittest.mock import AsyncMock, MagicMock
    from orchestrator.smart_home_controller import SmartHomeController
    from ._clause_harness import House

    controller = SmartHomeController(entity_manager=House(), llm_router=MagicMock())
    raw = MagicMock()
    raw.call_service = AsyncMock(return_value={})
    intent = {"device_type": "scene", "action": "activate", "target_scope": "group",
              "parameters": {"entity_id": "script.good_night"}}

    async def go(confirmed):
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("yes")):
            if confirmed:
                with write_fanout.confirmed(["script.good_night"]):
                    return await controller.execute_intent(intent, raw, original_query="goodnight")
            return await controller.execute_intent(intent, raw, original_query="goodnight")

    assert run(go(True)) != NO_WRITE_CUE_TEXT and raw.call_service.await_count == 1
    raw.call_service.reset_mock()
    assert run(go(False)) == NO_WRITE_CUE_TEXT and raw.call_service.await_count == 0


# --- sequences: a routine step needs a command or its own phrase --------------------------


def _scene_step(entity):
    return {"action": "activate", "target": {"device_type": "scene", "entity_id": entity},
            "parameters": {"entity_id": entity}, "delay_after": 0, "at_time": None}


def _light_step(room="hall"):
    return {"action": "turn_off", "target": {"device_type": "light", "room": room}, "parameters": {},
            "delay_after": 0, "at_time": None}


def _plan(monkeypatch, steps, query):
    from orchestrator import mode_permission as mp
    from .test_room_light_area_tier import Rig

    rig = Rig(monkeypatch)

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            return await rig.controller.decide_sequence_rooms(steps, query, None)

    return run(go()), rig


def test_a_sequence_routine_step_runs_for_a_command(monkeypatch):
    text, _ = _plan(monkeypatch, [_light_step(), _scene_step("script.good_night")],
                    "turn off the hall light and then run goodnight")
    assert text is None


def test_a_sequence_routine_step_is_refused_for_a_mention(monkeypatch):
    text, rig = _plan(monkeypatch, [_light_step(), _scene_step("script.good_night")],
                      "turn off the hall light and then the kids say goodnight")
    assert text is not None
    assert rig.raw.call_service.await_count == 0


def test_a_sequence_with_no_query_does_not_raise(monkeypatch):
    # sequence_executor calls execute_intent without words: the cue is not judged
    # again (each step was judged per clause when it was planned).
    from orchestrator import mode_permission as mp
    from orchestrator.sequence_executor import SequenceExecutor
    from .test_room_light_area_tier import Rig

    rig = Rig(monkeypatch)
    steps = [{"action": "turn_on", "target": {"device_type": "light", "room": "hall"}, "parameters": {},
              "delay_after": 0, "at_time": None}]

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            assert await rig.controller.decide_sequence_rooms(steps, "turn on the hall lights", None) is None
            return await SequenceExecutor(rig.controller, rig.raw).execute_sequence(
                steps, session_id=None, background=False)

    run(go())
    assert rig.raw.call_service.await_count >= 1


# --- music ---------------------------------------------------------------------------------


@pytest.mark.parametrize("utterance", [
    "play some jazz in the den", "please play some jazz", "I want to listen to jazz", "I'd like some music",
    "can you play something", "pause the music", "okay nebu, play jazz", "play jazz, the kids are asleep",
], ids=str)
def test_a_music_request_plays(utterance):
    state, raw, handler = drive_music(utterance, names=NAMES)
    assert raw.call_service.await_count == 1, (utterance, state.answer)


def test_an_unclassified_music_phrase_still_plays():
    # Recorded residual: with no statement marker there is nothing to refuse.
    state, raw, _ = drive_music("jazz in the den", names=NAMES)
    assert raw.call_service.await_count == 1


@pytest.mark.parametrize("utterance", [
    "the kids play music all day", "my partner wants jazz in the den", "they always put on jazz",
    "I usually play jazz at six", "don't play any jazz",
], ids=str)
def test_music_narration_plays_nothing(utterance):
    for intent in (IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL):
        state, raw, handler = drive_music(utterance, names=NAMES, intent=intent)
        assert raw.call_service.await_count == 0, (utterance, intent)
        assert state.answer == NO_WRITE_CUE_TEXT and state.error == "no_write_cue"


def test_a_music_read_is_never_refused():
    state, raw, _ = drive_music("the kids ask what is playing", names=NAMES,
                                intent=IntentCategory.MUSIC_CONTROL, control_action="now_playing")
    assert state.answer == "Nothing is playing." and raw.call_service.await_count == 0


def test_the_music_parse_sees_the_command_only():
    state, raw, handler = drive_music("the kids love jazz. okay, play some jazz in the den", names=NAMES)
    assert handler.parsed == ["play some jazz in the den"]
    assert raw.call_service.await_count == 1


# --- TV ------------------------------------------------------------------------------------


@pytest.mark.parametrize("action,power", [("power", "on"), ("launch", None), ("navigate", None),
                                          ("playback", None)], ids=str)
@pytest.mark.parametrize("utterance", [
    "turn on the den tv", "open the video app on the tv", "I want to watch the video app", "tv on please",
], ids=str)
def test_a_tv_request_dispatches(utterance, action, power):
    state, raw, handler = drive_tv(utterance, names=NAMES, action=action, power_action=power)
    assert raw.call_service.await_count == 1, (utterance, action, state.answer)


@pytest.mark.parametrize("utterance", [
    "they put the video app on every night", "the kids turn the tv on at six", "the tv is on all night",
    "my son said turn the tv on", "if it rains, turn on the tv",
], ids=str)
def test_tv_narration_dispatches_nothing(utterance):
    for action, power in (("power", "on"), ("launch", None), ("navigate", None), ("playback", None)):
        state, raw, handler = drive_tv(utterance, names=NAMES, action=action, power_action=power)
        assert raw.call_service.await_count == 0, (utterance, action)
        assert state.answer == NO_WRITE_CUE_TEXT and state.error == "no_write_cue"


def test_a_tv_question_is_answered_from_state_not_refused():
    state, raw, handler = drive_tv("is the tv on", names=NAMES, action="power", power_action="on")
    assert state.answer == "The TV is off." and raw.call_service.await_count == 0


def test_the_tv_parse_sees_the_command_only():
    state, raw, handler = drive_tv("the kids love the video app. okay, turn on the den tv", names=NAMES)
    assert handler.parsed == ["turn on the den tv"]
