"""T3: the write cue is read from the command clauses only.

A mechanical table over ``classify_utterance`` + ``write_cue_problem`` for the
narration, condition, mixed and request rows; then the same refused rows driven
through ``route_control``, ``route_music``, ``route_tv`` and the automation
agent, each with a recording Home Assistant client at the channel seam, which
must see zero ``call_service``. Positive controls drive the same routes and see
exactly the write the command asked for.

Synthetic utterances and a fictional house only.
"""
from __future__ import annotations

import unittest.mock as mock

import pytest

from orchestrator import access_target
from orchestrator.access_target import NO_CUE, write_cue_problem
from orchestrator.automation_agent import should_use_automation_agent
from orchestrator.smart_home_controller import NO_WRITE_CUE_TEXT
from orchestrator.state import IntentCategory
from orchestrator.utterance_kind import UtteranceKind, classify_utterance

from ._clause_harness import QueryReadingModel, drive_control, drive_music, drive_tv, writes
from .test_write_fanout_confirmation import HA_OFF, _AgentHarness, _tool_call

NAMES = ("nebu",)
IMPERATIVE = UtteranceKind.IMPERATIVE
UNKNOWN = UtteranceKind.UNKNOWN

# What a hostile model could ask for, whatever the words said.
HOSTILE_WRITES = (
    ("light", "set_color"), ("light", "turn_off"), ("light", "turn_on"),
    ("climate", "set_temperature"), ("media_player", "turn_on"),
)

# --- rows: every one is narration, and none is a request ---------------------------

NARRATION = [
    "they turn the lights off at night",
    "we leave the porch light on at night",
    "the kids keep the lights on all night",
    "the guests usually set the heat to 72",
    "the light switch on the wall is loose",
    "I usually turn on the lights at six",
    "if I turn off the lights it flickers",
    "what happens when you turn off the lights",
    "he'll turn off the lights",
    "my son said turn off the lights",
    "the TV says turn it up",
    "the kids keep saying dim them",
    "those blue lights look great",
    "my partner wants them blue",
    "I want to know if the lights are on",
    "don't make it blue",
    "she said turn off the lights",
    "my son and I said turn off the lights",
    '"turn off the lights," she said',
    "he yelled turn it up",
    "she was like turn it off",
    'the sign reads "turn off the lights"',
    "every night we come home, turn on the lights, and make dinner",
    "hey kids turn off the lights",
    "okay honey turn off the lights",
    "someone left the lights on",
    # One guard each, with no other rule to refuse them: reported speech after
    # the command, and a quotation.
    "turn off the lights, my son said",
    "dim them, the TV says",
    '"turn off the lights"',
]
# A condition: not immediate. No controller write; the agent route is as at base.
CONDITIONAL = ["if it gets dark, turn on the porch light"]
# A scene phrase inside a sentence that is no command runs nothing.
SCENE_MENTIONS = ["I said good night to the kids", "the show ended with goodbye"]

REFUSED = NARRATION + CONDITIONAL + SCENE_MENTIONS


@pytest.mark.parametrize("utterance", NARRATION + CONDITIONAL, ids=str)
def test_a_narration_row_is_unknown_with_no_command_and_no_cue(utterance):
    uk = classify_utterance(utterance, NAMES)
    problems = {
        (device, action): write_cue_problem(device, action, utterance, kind_hint=uk.kind, assistant_names=NAMES)
        for device, action in HOSTILE_WRITES
    }
    assert (uk.kind, uk.command_text) == (UNKNOWN, None), utterance
    assert set(problems.values()) == {NO_CUE}, problems


def test_the_narration_table_has_its_floor():
    assert len(NARRATION) >= 22 and len(set(NARRATION)) == len(NARRATION)


# --- mixed and positive rows ---------------------------------------------------------

# (utterance, kind, command_text): the classifier's whole verdict, exactly.
COMMANDS = [
    ("don't turn off the hall light, turn on the porch light", IMPERATIVE, "turn on the porch light"),
    ("the blue led ring, okay, turn on the hall light", IMPERATIVE, "turn on the hall light"),
    ("the blue led ring okay turn on the hall light", IMPERATIVE, "turn on the hall light"),
    ("the blue led ring okay nebu turn on the hall light", IMPERATIVE, "turn on the hall light"),
    ("kitchen lights on", IMPERATIVE, "kitchen lights on"),
    ("I said turn off the lights", IMPERATIVE, "I said turn off the lights"),
    ("so turn off the lights", IMPERATIVE, "turn off the lights"),
    ("but leave the porch light on", IMPERATIVE, "leave the porch light on"),
    ("before bed turn off the lights", IMPERATIVE, "before bed turn off the lights"),
    ("after dinner dim the lights", IMPERATIVE, "after dinner dim the lights"),
    ("it's dark, turn on the lights", IMPERATIVE, "turn on the lights"),
    ("make them a pretty blue", IMPERATIVE, "make them a pretty blue"),
    ('I said "turn off the lights"', IMPERATIVE, "I said turn off the lights"),
    ("turn the lights on, I love it", IMPERATIVE, "turn the lights on"),
    ("can you turn off the lights", IMPERATIVE, "can you turn off the lights"),
    ("turn on the light and make it blue", IMPERATIVE, "turn on the light and make it blue"),
    ("turn off the kitchen but leave the porch light on", IMPERATIVE,
     "turn off the kitchen but leave the porch light on"),
    ("turn off the lights and the fan", IMPERATIVE, "turn off the lights and the fan"),
    ("turn on the kitchen and hall lights", IMPERATIVE, "turn on the kitchen and hall lights"),
]


@pytest.mark.parametrize("utterance,kind,command_text", COMMANDS, ids=[c[0] for c in COMMANDS])
def test_a_command_row_keeps_exactly_its_command_clauses(utterance, kind, command_text):
    uk = classify_utterance(utterance, NAMES)
    assert (uk.kind, uk.command_text) == (kind, command_text)
    # Hostile writes are judged on the command's words: an on/off or colour
    # the command does not carry never gets a cue from the narration.
    action = "set_color" if "blue" in uk.command_text else "turn_on"
    assert write_cue_problem("light", action, uk.command_text, kind_hint=uk.kind) is None


# Requests said without a verb at the front: UNKNOWN, and the words still back a write.
UNKNOWN_REQUESTS = [
    ("lights please", "light", "turn_on"),
    ("blue lights in the den", "light", "set_color"),
    ("lights cool white please", "light", "set_color"),
    # First-person requests (UNKNOWN + the T-S exemption).
    ("I want the lights blue", "light", "set_color"),
    ("I would like the kitchen lights on", "light", "turn_on"),
    ("I'd like the kitchen lights on", "light", "turn_on"),
    ("I need the lights brighter", "light", "increase"),
    ("I want to turn off the lights", "light", "turn_off"),
]


@pytest.mark.parametrize("utterance,device,action", UNKNOWN_REQUESTS, ids=[r[0] for r in UNKNOWN_REQUESTS])
def test_a_request_without_a_leading_verb_is_unknown_and_still_backed(utterance, device, action):
    uk = classify_utterance(utterance, NAMES)
    assert (uk.kind, uk.command_text) == (UNKNOWN, None)
    assert write_cue_problem(device, action, utterance, kind_hint=uk.kind, assistant_names=NAMES) is None


# The environment forms keep exactly the result they had before the clause rule.
ENVIRONMENT_FORMS = [
    ("it's too dark in here", "light", "turn_on"),
    ("it is too bright in here", "light", "decrease"),
    ("I'm cold", "climate", "set_temperature"),
    ("it's cold in here", "hvac", "set_temperature"),
    ("I can't see", "light", "turn_on"),
]


@pytest.mark.parametrize("utterance,device,action", ENVIRONMENT_FORMS, ids=[r[0] for r in ENVIRONMENT_FORMS])
def test_an_environment_statement_is_no_narration(utterance, device, action):
    uk = classify_utterance(utterance, NAMES)
    assert uk.command_text is None
    # The recorded result of the same call before this change: no problem.
    assert write_cue_problem(device, action, utterance, kind_hint=uk.kind) is None


@pytest.mark.parametrize("query", [
    "kitchen lights on", "lights please", "blue lights in the den", "I want the lights blue",
    "I would like the kitchen lights on", "I need the lights brighter", "I said turn off the lights",
])
def test_positive_controls_pass_the_cue(query):
    uk = classify_utterance(query, NAMES)
    action = "set_color" if "blue" in query else "turn_on"
    assert write_cue_problem("light", action, uk.command_text or query, kind_hint=uk.kind) is None


@pytest.mark.parametrize("query", ["up to 80%", "down to 0 percent", "lights to fifty percent"])
def test_brightness_positive_controls_pass_the_cue(query):
    assert write_cue_problem("light", "set_brightness", query) is None


def test_only_the_command_clauses_carry_the_cue_even_when_the_whole_utterance_is_handed_in():
    # "make" leads a command but is no cue word, and the earlier clause (not narration,
    # so not filtered) holds a colour: the colour must not back a colour write.
    text = "the blue led ring okay make dinner"
    uk = classify_utterance(text, NAMES)
    assert (uk.kind, uk.command_text) == (IMPERATIVE, "make dinner")
    assert write_cue_problem("light", "set_color", text, kind_hint=uk.kind) == NO_CUE
    assert write_cue_problem("light", "set_color", text.replace("make dinner", "turn the lights blue")) is None


def test_a_conjunction_splits_only_after_a_command():
    # "lights on" is no command, so "and turn them blue" stays in its clause.
    uk = classify_utterance("kitchen lights on and turn them blue")
    assert (uk.kind, uk.command_text) == (UNKNOWN, None)
    uk = classify_utterance("turn on the kitchen lights and turn them blue")
    assert (uk.kind, uk.command_text) == (IMPERATIVE, "turn on the kitchen lights and turn them blue")


def test_the_statement_marker_has_a_positive_for_each_exemption():
    # Each T-S exemption keeps a request a request; the same words as narration are refused.
    for request, narration in (
        ("I want the lights blue", "my partner wants them blue"),
        ("I need to turn on the lights", "I want to know if the lights are on"),
    ):
        assert write_cue_problem("light", "set_color", request) is None
        assert write_cue_problem("light", "set_color", narration) == NO_CUE
    assert access_target.statement_clause(["they", "turn", "the", "lights", "off"]) is True
    assert access_target.statement_clause(["turn", "the", "lights", "off"]) is False


# --- zero call_service through every route, for every refused row ----------------------


@pytest.mark.parametrize("utterance", REFUSED, ids=str)
def test_route_control_writes_nothing_for_a_refused_row(utterance):
    model = QueryReadingModel(scene_for={"good night": "script.good_night", "goodbye": "script.leaving"})
    state, _model, client = drive_control(utterance, names=NAMES, model=model)
    assert writes(client) == [], (utterance, writes(client))
    assert state.answer, utterance


MEDIA_REFUSED = NARRATION + CONDITIONAL + SCENE_MENTIONS[:1]


@pytest.mark.parametrize("utterance", MEDIA_REFUSED, ids=str)
def test_route_music_writes_nothing_for_a_refused_row(utterance):
    for intent in (IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL):
        state, raw, _handler = drive_music(utterance, names=NAMES, intent=intent)
        assert raw.call_service.await_count == 0, (utterance, intent)
        assert state.answer == NO_WRITE_CUE_TEXT, (utterance, intent, state.answer)


@pytest.mark.parametrize("utterance", MEDIA_REFUSED, ids=str)
def test_route_tv_writes_nothing_for_a_refused_row(utterance):
    for action, power in (("power", "on"), ("launch", None), ("navigate", None), ("playback", None)):
        state, raw, _handler = drive_tv(utterance, names=NAMES, action=action, power_action=power)
        assert raw.call_service.await_count == 0, (utterance, action)
        assert state.answer == NO_WRITE_CUE_TEXT, (utterance, action, state.answer)


def _agent_calls_after(utterance, tool, arguments, *, kill_switch=False):
    harness = _AgentHarness(_tool_call(tool, arguments), kill_switch=kill_switch)
    harness.run_agent(utterance)
    return harness


@pytest.mark.parametrize("utterance", NARRATION + SCENE_MENTIONS, ids=str)
def test_the_agent_writes_nothing_for_narration(utterance):
    narration_only = classify_utterance(utterance).command_text is None
    assert narration_only
    h = _agent_calls_after(utterance, *HA_OFF)
    assert h.client.call_service.await_count == 0, (utterance, h.client.call_service.await_args_list)


@pytest.mark.parametrize("utterance", [u for u in NARRATION if not u.startswith("don't")], ids=str)
def test_the_agents_other_write_tools_refuse_narration(utterance):
    create = ("create_automation", {"name": "Lights", "trigger": {"type": "time", "time": "18:00"},
                                    "actions": [{"type": "service", "entity_id": "light.office_0",
                                                 "service": "turn_off"}]})
    h = _agent_calls_after(utterance, *create)
    assert h.client.create_automation.await_count == 0 and h.admin_writes() == [], utterance
    h = _agent_calls_after(utterance, "delete_automation", {"automation_id": "voice_1"})
    assert h.admin_writes() == [], utterance
    h = _agent_calls_after(utterance, "notify", {"message": "hi", "target": "all", "room": "office"})
    assert h.client.call_service.await_count == 0, utterance


def test_a_conditional_row_is_not_narration_for_the_agent():
    from orchestrator.utterance_kind import is_narration
    assert is_narration(CONDITIONAL[0]) is False
    assert is_narration("whenever someone comes in, turn on the lights") is False
    assert is_narration("when it gets dark turn on the porch light") is False
    assert is_narration("if I turn off the lights it flickers") is True


def test_a_conditional_row_is_not_immediate_for_the_agent_either():
    # The agent's immediate service tool refuses it; an automation may still be made.
    utterance = CONDITIONAL[0]
    h = _agent_calls_after(utterance, *HA_OFF)
    assert h.client.call_service.await_count == 0
    create = ("create_automation", {"name": "Porch", "trigger": {"type": "time", "time": "18:00"},
                                    "actions": [{"type": "service", "entity_id": "light.office_0",
                                                 "service": "turn_on"}]})
    h = _agent_calls_after(utterance, *create)
    assert h.client.create_automation.await_count == 1


@pytest.mark.parametrize("utterance", NARRATION, ids=str)
def test_the_agent_is_not_chosen_for_narration(utterance):
    assert should_use_automation_agent(utterance) is False


@pytest.mark.parametrize("utterance", [
    "turn off the porch light at 10 pm",
    "when motion is detected in the hall turn on the lights",
], ids=str)
def test_the_agent_is_still_chosen_for_an_automation(utterance):
    assert should_use_automation_agent(utterance) is True


def test_a_command_still_writes_through_the_agent():
    h = _agent_calls_after("turn off the office light at 6pm", *HA_OFF)
    assert [c.args[:2] for c in h.client.call_service.await_args_list] == [("light", "turn_off")]


# --- positive controls: exactly the write the command asked for ------------------------


def test_narration_before_okay_turns_on_only_the_hall_light():
    for query, names in (
        ("the blue led ring, okay, turn on the hall light", ()),
        ("the blue led ring okay turn on the hall light", ()),
        ("the blue led ring okay nebu turn on the hall light", NAMES),
    ):
        state, model, client = drive_control(query, names=names)
        assert writes(client) == [("light", "turn_on", ("light.hall_lamp",))], (query, writes(client), state.answer)
        assert all("blue" not in q.lower() for q in model.queries), model.queries


def test_a_mixed_row_writes_only_the_porch():
    state, _model, client = drive_control("don't turn off the hall light, turn on the porch light")
    assert writes(client) == [("light", "turn_on", ("light.porch_lamp",))], writes(client)


@pytest.mark.parametrize("query,expected", [
    ("kitchen lights on", [("light", "turn_on", ("light.kitchen_lamp",))]),
    ("I said turn off the hall light", [("light", "turn_off", ("light.hall_lamp",))]),
    ("so turn off the hall light", [("light", "turn_off", ("light.hall_lamp",))]),
    ("but leave the porch light on", [("light", "turn_on", ("light.porch_lamp",))]),
    ("it's dark, turn on the hall light", [("light", "turn_on", ("light.hall_lamp",))]),
    ("turn the hall light on, I love it", [("light", "turn_on", ("light.hall_lamp",))]),
    ("can you turn off the hall light", [("light", "turn_off", ("light.hall_lamp",))]),
], ids=str)
def test_a_command_writes_through_route_control(query, expected):
    state, _model, client = drive_control(query)
    assert writes(client) == expected, (query, writes(client), state.answer)


def test_a_request_through_route_control_writes_with_its_own_words():
    state, _model, client = drive_control("I want the hall lights blue")
    assert [w[:2] for w in writes(client)] == [("light", "turn_on")] or writes(client), state.answer
    assert writes(client), state.answer


def test_the_harness_sees_a_write_when_the_gate_is_open():
    # Positive control for the zero-write tests above: with the clause rule
    # bypassed (every text is its own command) the same rig records the write.
    with mock.patch("orchestrator.nodes.route_control.command_query", side_effect=lambda uk, q: q), \
            mock.patch("orchestrator.access_target.cue_text", side_effect=lambda q, names=(): q):
        state, _model, client = drive_control("those hall lights look great")
    assert writes(client), state.answer
