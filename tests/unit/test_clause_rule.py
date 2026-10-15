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
    # Names and bare plurals as the subject (M2), and a modal question about others (L4).
    "Sarah wants the lights blue",
    "Mike turns the lights off every night",
    "kids keep the lights on all night",
    "Mike puts Netflix on every night",
    "can the kids turn off the lights",
    "I like the lights blue",
    # A device first, then a present-tense verb that is no command (the over-refusal round).
    "lights go off at ten",
    "tv turns on by itself",
    "the kitchen lights keep turning on",
    "kitchen lights turn on by themselves",
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
    # Copula lead-ins with a pronoun subject are no narrative lead (ian High).
    ("we're home, turn on the lights", IMPERATIVE, "turn on the lights"),
    ("we are leaving, lock the door", IMPERATIVE, "lock the door"),
    ("you're right, turn it off", IMPERATIVE, "turn it off"),
    ("I'm back, turn on the lights", IMPERATIVE, "turn on the lights"),
    ("porch light off", IMPERATIVE, "porch light off"),
    ("kitchen lights on and turn them blue", IMPERATIVE, "kitchen lights on and turn them blue"),
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
    device = uk.device_type or "light"
    action = "set_color" if "blue" in uk.command_text else ("lock" if device == "lock" else "turn_on")
    assert write_cue_problem(device, action, uk.command_text, kind_hint=uk.kind) is None


# Requests said without a verb at the front: UNKNOWN, and the words still back a write.
UNKNOWN_REQUESTS = [
    ("lights please", "light", "turn_on"),
    ("blue lights in the den", "light", "set_color"),
    ("lights cool white please", "light", "set_color"),
    ("I'd like the lights blue", "light", "set_color"),
    ("I would like the lights blue", "light", "set_color"),
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
    # "lights" alone is no command, so "and turn them blue" stays in its clause.
    uk = classify_utterance("lights and turn them blue")
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
    h = _agent_calls_after("turn off the office light", *HA_OFF)
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
            mock.patch("orchestrator.access_target.cue_text", side_effect=lambda q, names=(), **kw: q):
        state, _model, client = drive_control("those hall lights look great")
    assert writes(client), state.answer


# --- the review round: window, carried cue, fragments, the agent ------------------------------


def test_the_analysis_window_is_the_extractors_cap():
    from orchestrator import room_context
    from orchestrator.utterance_kind import MAX_CLASSIFIED_CHARS
    assert MAX_CLASSIFIED_CHARS == room_context.SMART_HOME_MAX_QUERY_BYTES


def test_nothing_past_the_old_window_drives_a_write():
    chatter = " ".join(["the weather was grey and the street was quiet for a long while"] * 4) + "."
    text = f"turn on the hall light. {chatter} they like it red, make the porch red"
    assert 300 < len(text) < 1000
    state, model, client = drive_control(text)
    assert writes(client) == [("light", "turn_on", ("light.hall_lamp",))], (writes(client), state.answer)
    assert all("red" not in q for q in model.queries)


def test_a_carried_cue_does_not_back_narration():
    state, client = drive_followup_rows("turn off the kitchen lights", "turn_off", "they turn them off at night")
    assert writes(client) == [], (writes(client), state.answer)


def test_a_carried_cue_still_backs_a_follow_up_command():
    from ._clause_harness import drive_followup
    state, client = drive_followup("turn off the kitchen lights", "turn_off", "do the same in the hall")
    assert state.answer  # the follow-up path ran


def drive_followup_rows(prev_query, prev_action, query):
    from ._clause_harness import drive_followup
    return drive_followup(prev_query, prev_action, query)


def test_the_carried_cue_function_refuses_narration_and_joins_commands():
    from orchestrator.nodes import route_control as rc
    from orchestrator.state import OrchestratorState
    state = OrchestratorState(query="")
    state.mode = "owner"
    state.permissions = {"mode": "owner"}
    state.caller_fingerprint = "fp-1"
    params = {"write_cue": {"fp": "fp-1", "aud": rc._audience_class(state), "device_type": "light",
                            "action": "turn_off", "query": "turn off the kitchen lights"}}
    intent = {"device_type": "light", "action": "turn_off"}
    state.query = "they turn them off at night"
    assert rc._carried_cue_query(params, intent, state) is None
    state.query = "turn them off again"
    assert rc._carried_cue_query(params, intent, state) == "turn off the kitchen lights. turn them off again"
    state.query = "off again"
    assert rc._carried_cue_query(params, intent, state) == "turn off the kitchen lights. off again"


def test_a_fragment_after_a_comma_runs_no_routine():
    state, model, client = drive_control("turn on the hall light, heading out")
    assert writes(client) == [("light", "turn_on", ("light.hall_lamp",))], (writes(client), state.answer)


def test_a_failing_analysis_fails_closed(monkeypatch):
    from orchestrator import utterance_kind as uk
    from orchestrator.utterance_kind import cue_text, has_narration, is_narration

    def boom(*a, **k):
        raise RuntimeError("planted")

    monkeypatch.setattr(uk, "_analyze_cached", boom)
    assert cue_text("turn on the lights") == ""
    assert is_narration("turn on the lights") is True
    assert has_narration("turn on the lights") is True
    assert access_target.media_write_allowed(UNKNOWN, "play jazz") is False
    assert write_cue_problem("light", "turn_on", "turn on the lights") == NO_CUE


@pytest.mark.parametrize("utterance", [
    "turn off the lights, I love it and the kids never sleep",
    "the kids like it dark, turn off the lights at ten",
], ids=str)
def test_the_agents_create_tool_refuses_when_any_clause_is_narration(utterance):
    create = ("create_automation", {"name": "Lights", "trigger": {"type": "time", "time": "22:00"},
                                    "actions": [{"type": "service", "entity_id": "light.office_0",
                                                 "service": "turn_off"}]})
    h = _agent_calls_after(utterance, *create)
    assert h.client.create_automation.await_count == 0 and h.admin_writes() == [], utterance


@pytest.mark.parametrize("domain,service", [
    ("input_boolean", "turn_on"), ("automation", "trigger"), ("alarm_control_panel", "alarm_disarm"),
    ("scene", "turn_on"), ("script", "turn_on"),
], ids=str)
def test_the_agents_service_tool_judges_domains_outside_the_device_kinds(domain, service):
    args = {"domain": domain, "service": service, "entity_id": f"{domain}.thing"}
    h = _agent_calls_after("the kids use the thing all night", "ha_service", args)
    assert h.client.call_service.await_count == 0, (domain, h.client.call_service.await_args_list)


def test_a_command_still_reaches_a_domain_outside_the_device_kinds():
    args = {"domain": "input_boolean", "service": "turn_on", "entity_id": "input_boolean.thing"}
    h = _agent_calls_after("turn on the thing", "ha_service", args)
    assert h.client.call_service.await_count == 1


# --- the over-refusal round: real commands that must work, and the rows that must not ----------

# Exact strings the review named. Each must get past the write cue (never the
# no-request refusal); the ones with a room the rig knows must also write.
MUST_WORK = [
    "kitchen lights set to 50 percent", "bedroom lights turn off", "kitchen turn off the lights",
    "living room lamp turn on", "porch light on and set it to 50%",
    "can we dim the lights", "could we turn it down", "can I get the lights off", "can u turn on the fan",
    "yeah leave the lights on", "thanks turn off the lights", "sure keep it on", "actually turn on the fan",
    "okay so turn on the fan",
]
# The same kinds of command with a room the rig can write: the write must be observed.
MUST_WRITE = [
    ("kitchen lights set to 50 percent", "light.kitchen_lamp"),
    ("kitchen turn off the lights", "light.kitchen_lamp"),
    ("kitchen lights turn on", "light.kitchen_lamp"),
    ("hall lights turn off", "light.hall_lamp"),
    ("porch light on and set it to 50%", "light.porch_lamp"),
    ("can we dim the hall lights", "light.hall_lamp"),
    ("could we turn off the porch light", "light.porch_lamp"),
    ("can I get the hall lights off", "light.hall_lamp"),
    ("can u turn on the kitchen light", "light.kitchen_lamp"),
    ("yeah leave the porch light on", "light.porch_lamp"),
    ("thanks turn off the hall light", "light.hall_lamp"),
    ("sure keep the porch light on", "light.porch_lamp"),
    ("actually turn on the hall light", "light.hall_lamp"),
]


@pytest.mark.parametrize("utterance", MUST_WORK, ids=str)
def test_a_real_command_is_not_narration_and_gets_a_cue(utterance):
    from orchestrator.utterance_kind import has_narration
    uk = classify_utterance(utterance, NAMES)
    assert uk.kind == IMPERATIVE and uk.command_text, utterance
    assert has_narration(utterance, NAMES) is False
    assert write_cue_problem("light", "turn_on", uk.command_text, kind_hint=uk.kind) in (None, access_target.WRONG_DEVICE)


@pytest.mark.parametrize("utterance", MUST_WORK, ids=str)
def test_a_real_command_gets_past_the_write_cue_through_route_control(utterance):
    state, _model, _client = drive_control(utterance, names=NAMES)
    assert state.answer != NO_WRITE_CUE_TEXT, (utterance, state.answer)


@pytest.mark.parametrize("utterance,entity", MUST_WRITE, ids=[u for u, _ in MUST_WRITE])
def test_a_real_command_writes_through_route_control(utterance, entity):
    state, _model, client = drive_control(utterance, names=NAMES)
    assert writes(client) and {e for w in writes(client) for e in w[2]} == {entity}, (
        utterance, writes(client), state.answer
    )


@pytest.mark.parametrize("utterance", MUST_WORK[:4] + MUST_WORK[5:9] + MUST_WORK[10:13], ids=str)
def test_a_real_command_reaches_the_music_and_tv_nodes(utterance):
    for intent in (IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL):
        state, _raw, _h = drive_music(utterance, names=NAMES, intent=intent)
        assert state.answer != NO_WRITE_CUE_TEXT, (utterance, intent)
    state, _raw, _h = drive_tv(utterance, names=NAMES)
    assert state.answer != NO_WRITE_CUE_TEXT, utterance


def test_lights_off_at_ten_is_a_schedule_not_narration():
    from orchestrator.utterance_kind import has_narration, is_narration
    assert not has_narration("lights off at ten") and not is_narration("lights off at ten")
    uk = classify_utterance("lights off at ten")
    assert (uk.kind, uk.command_text) == (UNKNOWN, None)
    assert write_cue_problem("light", "turn_off", "lights off at ten", kind_hint=uk.kind) is None


AUTOMATION_REQUESTS = [
    "have the lights turn on at sunset", "make the porch light come on at sunset", "let the fan run until 10",
    "the porch light should come on at sunset",
]


@pytest.mark.parametrize("utterance", AUTOMATION_REQUESTS, ids=str)
def test_an_automation_request_is_no_narration_and_the_agent_may_set_it_up(utterance):
    from orchestrator.utterance_kind import has_narration, is_narration
    assert not has_narration(utterance) and not is_narration(utterance)
    assert classify_utterance(utterance).kind != UtteranceKind.STATE_QUESTION
    create = ("create_automation", {"name": "Sunset", "trigger": {"type": "time", "time": "18:00"},
                                    "actions": [{"type": "service", "entity_id": "light.office_0",
                                                 "service": "turn_on"}]})
    h = _agent_calls_after(utterance, *create)
    assert h.client.create_automation.await_count == 1, utterance


def test_the_causative_request_still_reaches_the_agent():
    assert should_use_automation_agent("have the lights turn on at sunset") is True


def test_a_scheduled_device_request_writes_nothing_now():
    for utterance in ("the porch light should come on at sunset", "the kids should turn off the lights at ten"):
        state, _model, client = drive_control(utterance)
        assert writes(client) == [], (utterance, writes(client))


def test_the_carried_cue_reads_the_configured_names():
    from orchestrator.nodes import route_control as rc
    from orchestrator.state import OrchestratorState
    state = OrchestratorState(query="")
    state.mode = "owner"
    state.permissions = {"mode": "owner"}
    state.caller_fingerprint = "fp-1"
    params = {"write_cue": {"fp": "fp-1", "aud": rc._audience_class(state), "device_type": "light",
                            "action": "turn_off", "query": "turn off the kitchen lights"}}
    intent = {"device_type": "light", "action": "turn_off"}
    state.query = "okay nebu hall lights turn off"
    assert rc._carried_cue_query(params, intent, state, None, ("nebu",)) is not None
    state.query = "they turn them off at night"
    assert rc._carried_cue_query(params, intent, state, None, ("nebu",)) is None


def test_a_modal_request_from_the_speaker_is_no_narration_even_without_a_control_verb():
    from orchestrator.utterance_kind import has_narration
    for text in ("can we have the lights on", "could I have the porch light off"):
        assert not has_narration(text), text
        assert write_cue_problem("light", "turn_on", text, kind_hint=classify_utterance(text).kind) is None, text


def test_a_room_first_on_off_request_stays_non_imperative_unless_it_is_the_first_of_two_commands():
    # Its fan-out limits are those of any non-imperative request.
    assert classify_utterance("office lights off please").kind == UNKNOWN
    assert classify_utterance("office lights off at ten").kind == UNKNOWN
    assert classify_utterance("porch light on and set it to 50%").kind == IMPERATIVE


def test_a_room_first_command_needs_a_room_the_generic_vocabulary_knows():
    # Known limit: a house's own room name in first position is not recognised (it fails closed).
    assert classify_utterance("kitchen turn off the lights").kind == IMPERATIVE
    assert classify_utterance("kids turn off the lights").kind == UNKNOWN
    assert classify_utterance("galley turn off the lights").kind == UNKNOWN


# --- reconcile round: closed room-first tails, schedule-led commands, the house's own names ------

# Never an immediate write. "Deferred": the words ask for something later or on a condition.
SCHEDULE_LED = [
    "in 5 minutes unlock the front door", "in 5 minutes lock the door", "at 11 turn off the lights",
    "if it gets dark, turn on the porch light",
]
ROOM_FIRST_DEFERRED = [
    "porch light turn on whenever someone walks by", "kitchen lights turn off after the kids leave",
    "upstairs lights turn off at ten",
]
# Room phrases that are a house's own names (R3) and ones that are not.
OWN_NAME_COMMANDS = ["passage light turn on", "master closet light turn off"]
NOT_OWN_NAME_COMMANDS = ["kids lights turn off at nine", "Sarah's lamp turn on", "neighbor's lights turn on at night"]
ROOM_FIRST_NOT_CLOSED = [
    "kitchen lights keep turning on", "kitchen lights turn on by themselves", "kitchen lights turn off all night",
    "kitchen lights turn off if the kids leave",
]


@pytest.mark.parametrize("utterance", SCHEDULE_LED + ROOM_FIRST_DEFERRED + NOT_OWN_NAME_COMMANDS[:1], ids=str)
def test_a_schedule_led_or_conditional_command_is_deferred_never_immediate(utterance):
    uk = classify_utterance(utterance, NAMES)
    assert (uk.kind, uk.command_text, uk.deferred) == (UNKNOWN, None, True), utterance
    for device, action in HOSTILE_WRITES:
        assert write_cue_problem(device, action, utterance, kind_hint=uk.kind, assistant_names=NAMES) == NO_CUE


@pytest.mark.parametrize("utterance", SCHEDULE_LED + ROOM_FIRST_DEFERRED + NOT_OWN_NAME_COMMANDS + ROOM_FIRST_NOT_CLOSED,
                         ids=str)
def test_no_route_writes_now_for_a_deferred_or_not_closed_row(utterance):
    state, _model, client = drive_control(utterance, names=NAMES)
    assert writes(client) == [], (utterance, writes(client), state.answer)
    for intent in (IntentCategory.MUSIC_PLAY, IntentCategory.MUSIC_CONTROL):
        _s, raw, _h = drive_music(utterance, names=NAMES, intent=intent)
        assert raw.call_service.await_count == 0, (utterance, intent)
    _s, raw, _h = drive_tv(utterance, names=NAMES)
    assert raw.call_service.await_count == 0, utterance


@pytest.mark.parametrize("utterance,domain,service", [
    ("in 5 minutes unlock the front door", "lock", "unlock"), ("in 5 minutes lock the door", "lock", "lock"),
    ("in 5 minutes close the garage door", "cover", "close_cover"),
], ids=str)
def test_a_door_never_moves_now_for_a_schedule_led_clause(utterance, domain, service):
    # Through the agent's immediate tool, and through the controller with a lock the model picked.
    h = _agent_calls_after(utterance, "ha_service", {"domain": domain, "service": service,
                                                      "entity_id": f"{domain}.front_door"})
    assert h.client.call_service.await_count == 0
    from orchestrator import mode_permission as mp
    from orchestrator.smart_home_controller import SmartHomeController
    from unittest.mock import AsyncMock, MagicMock
    from ._clause_harness import House, run
    controller = SmartHomeController(entity_manager=House(), llm_router=MagicMock())
    raw = MagicMock()
    raw.call_service = AsyncMock(return_value={})
    raw.get_state = AsyncMock(return_value={"state": "locked"})
    intent = {"device_type": domain, "action": "unlock" if service == "unlock" else "lock",
              "room": "front_door", "target_scope": "group", "parameters": {}}

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(utterance)):
            return await controller.execute_intent(intent, raw, original_query=utterance)

    assert run(go()) == NO_WRITE_CUE_TEXT and raw.call_service.await_count == 0


@pytest.mark.parametrize("utterance", SCHEDULE_LED, ids=str)
def test_a_schedule_led_command_reaches_the_automation_agent(utterance):
    assert should_use_automation_agent(utterance) is True
    create = ("create_automation", {"name": "Later", "trigger": {"type": "time", "time": "23:00"},
                                    "actions": [{"type": "service", "entity_id": "light.office_0",
                                                 "service": "turn_off"}]})
    h = _agent_calls_after(utterance, *create)
    assert h.client.create_automation.await_count == 1, utterance


@pytest.mark.parametrize("utterance", ["in 5 minutes turn off the lights", "then at 11 turn off all the lights"], ids=str)
def test_a_timed_step_of_a_sequence_still_has_its_cue(utterance):
    # The sequence loop judges each step's own clause; a time-led one is the step's cue.
    assert write_cue_problem("light", "turn_off", utterance, allow_scheduled=True) is None
    assert write_cue_problem("light", "turn_off", utterance) == NO_CUE


@pytest.mark.parametrize("utterance", OWN_NAME_COMMANDS, ids=str)
def test_a_house_own_room_name_in_first_position_is_a_command(utterance):
    uk = classify_utterance(utterance)
    assert (uk.kind, uk.command_text) == (IMPERATIVE, utterance)
    state, _model, client = drive_control(utterance)
    assert state.answer != NO_WRITE_CUE_TEXT, (utterance, state.answer)


@pytest.mark.parametrize("utterance", NOT_OWN_NAME_COMMANDS + ROOM_FIRST_NOT_CLOSED, ids=str)
def test_other_leading_phrases_are_not_room_first_commands(utterance):
    assert classify_utterance(utterance).kind != IMPERATIVE, utterance


@pytest.mark.parametrize("utterance", [
    "kitchen lights set to 50 percent", "kitchen lights set to blue", "kitchen lights dim to 30%",
    "kitchen lights dim", "kitchen lights turn off please", "kitchen turn off the lights",
    "bedroom lights turn up", "porch light on and set it to 50%",
], ids=str)
def test_the_closed_room_first_tails_are_commands(utterance):
    assert classify_utterance(utterance).kind == IMPERATIVE, utterance


def test_an_unavailable_room_vocabulary_is_retried_not_cached(monkeypatch):
    import sys
    from orchestrator import utterance_kind as uk
    monkeypatch.setattr(uk, "_room_words_cache", None)
    monkeypatch.setattr(uk, "_room_words_failure_logged", False)
    real = sys.modules.get("orchestrator.room_context")
    monkeypatch.setitem(sys.modules, "orchestrator.room_context", None)  # an import error
    assert uk._room_words() == frozenset()
    assert uk._room_words_cache is None and uk._room_words_failure_logged is True
    if real is not None:
        monkeypatch.setitem(sys.modules, "orchestrator.room_context", real)
    else:
        monkeypatch.delitem(sys.modules, "orchestrator.room_context")
    assert "kitchen" in uk._room_words()


def test_the_room_first_tail_is_closed_and_a_condition_anywhere_blocks_it():
    from orchestrator.utterance_kind import _room_first_command as rf
    assert rf("kitchen lights turn off please".split()) and rf("kitchen lights turn off now".split())
    # An open tail is no command, with no schedule word to refuse it.
    assert not rf("kitchen lights turn off tomorrow".split())
    assert not rf("kitchen lights turn off the kids".split())
    # A closed tail with a schedule or condition word in the clause is none either.
    assert rf("kitchen lights turn off and set it to 50".split())
    assert not rf("kitchen lights turn off and set it to 50 after ten".split())
    assert not rf("kitchen lights turn off and set it to 50 if dark".split())


# A place or a filler after in/at/by is no time (review: "in the kitchen turn on the lights").
PLACE_LED = [
    ("in the kitchen turn on the lights", "kitchen"), ("at the bar turn on the lights", None),
    ("by the way turn off the lights", None),
]
TIME_LED = ["in 5 minutes lock the door", "at 11 turn off the lights", "in ten minutes turn off the fan"]


@pytest.mark.parametrize("utterance,room", PLACE_LED, ids=[p[0] for p in PLACE_LED])
def test_a_place_or_filler_lead_is_a_command_not_a_schedule(utterance, room):
    uk = classify_utterance(utterance)
    assert (uk.kind, uk.deferred, uk.room) == (IMPERATIVE, False, room), utterance
    assert uk.command_text == utterance


@pytest.mark.parametrize("utterance", TIME_LED, ids=str)
def test_a_time_lead_stays_deferred(utterance):
    uk = classify_utterance(utterance)
    assert (uk.kind, uk.command_text, uk.deferred) == (UNKNOWN, None, True), utterance
    state, _model, client = drive_control(utterance)
    assert writes(client) == [], (utterance, writes(client))


@pytest.mark.parametrize("utterance,entity", [
    ("in the kitchen turn on the lights", "light.kitchen_lamp"),
    ("at the bar turn on the hall lights", "light.hall_lamp"),
    ("by the way turn off the porch light", "light.porch_lamp"),
], ids=str)
def test_a_place_or_filler_lead_writes(utterance, entity):
    state, _model, client = drive_control(utterance)
    assert {e for w in writes(client) for e in w[2]} == {entity}, (utterance, writes(client), state.answer)


# --- reconcile r1: a time or condition after the command, and narrated openings ------------

TRAILING_DEFERRED = [
    "unlock the front door in 5 minutes", "unlock the front door when I get home", "open the garage door at 5pm",
    "open the garage tonight", "lock the door in an hour", "lock the door once I arrive",
    "open the garage door before I get home", "lock the front door after 10pm", "unlock the front door at 5",
    "unlock the front door, when I get home", "open the garage door in half an hour",
]
# A light's timer is the sequence executor's and its condition the agent's, as before: the door rule leaves them alone.
LIGHT_TIMERS = ["turn off the lights in 10 minutes", "turn off the lights at 10pm", "turn off the lights tonight"]
LIGHT_CONDITIONS = ["turn off the lights when I leave", "turn on the porch light at sunset"]
NARRATED_OPENINGS = [
    "she said unlock the front door", "the neighbor said open the garage door", "maybe unlock the front door",
    "in the movie they unlock the front door", "the burglar tried to unlock the front door",
    "Unlock the front door, said nobody", "he asked to open the garage door",
]
DOOR_ENTITIES = {
    "lock.front": {"state": "locked", "attributes": {"friendly_name": "Front Door"}},
    "cover.garage": {"state": "closed", "attributes": {"friendly_name": "Garage Door", "device_class": "garage"}},
}


def _door_controller_calls(utterance, domain, action):
    """Drive the controller with the lock or cover the model picked; the calls
    that reached Home Assistant, and the answer."""
    from orchestrator import mode_permission as mp
    from orchestrator.smart_home_controller import SmartHomeController
    from unittest.mock import AsyncMock, MagicMock
    from ._clause_harness import run

    class Doors:
        entities = dict(DOOR_ENTITIES)

        async def get_entities(self, force_refresh=False, max_age=None):
            return dict(self.entities)

        async def get_entity(self, entity_id, max_age=None):
            return self.entities.get(entity_id)

        def invalidate(self, ids):
            pass

    controller = SmartHomeController(entity_manager=Doors(), llm_router=MagicMock())
    raw = MagicMock()
    raw.call_service = AsyncMock(return_value={})
    raw.get_state = AsyncMock(return_value={"state": "locked"})
    intent = {"device_type": domain, "action": action, "room": "front door" if domain == "lock" else "garage",
              "target_scope": "group", "parameters": {}}

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(utterance),
                                    trusted_voice=True):
            return await controller.execute_intent(intent, raw, original_query=utterance)

    answer = run(go())
    return answer, raw.call_service.await_args_list


@pytest.mark.parametrize("utterance", TRAILING_DEFERRED, ids=str)
def test_a_trailing_time_or_condition_defers_the_command(utterance):
    uk = classify_utterance(utterance)
    assert (uk.kind, uk.command_text, uk.deferred) == (UNKNOWN, None, True), utterance
    state, _model, client = drive_control(utterance)
    assert writes(client) == [], (utterance, writes(client))


@pytest.mark.parametrize("utterance", LIGHT_TIMERS + LIGHT_CONDITIONS, ids=str)
def test_a_lights_trailing_time_keeps_its_route(utterance):
    from orchestrator.smart_home_controller import SmartHomeController
    from unittest.mock import MagicMock
    uk = classify_utterance(utterance)
    assert (uk.kind, uk.deferred) == (IMPERATIVE, False)
    controller = SmartHomeController(entity_manager=MagicMock(), llm_router=MagicMock())
    scheduled = controller.detect_sequence_intent(utterance)
    assert scheduled if utterance in LIGHT_TIMERS else should_use_automation_agent(utterance), utterance


@pytest.mark.parametrize("utterance", [u for u in TRAILING_DEFERRED if "door" in u or "garage" in u], ids=str)
def test_a_door_never_moves_now_for_a_trailing_time(utterance):
    domain, action = ("cover", "open") if "garage" in utterance else ("lock", "lock" if "lock the" in utterance and "unlock" not in utterance else "unlock")
    answer, calls = _door_controller_calls(utterance, domain, action)
    assert answer == NO_WRITE_CUE_TEXT and calls == [], (utterance, answer)


@pytest.mark.parametrize("utterance", TRAILING_DEFERRED, ids=str)
def test_a_trailing_time_or_condition_reaches_the_automation_agent(utterance):
    assert should_use_automation_agent(utterance) is True


@pytest.mark.parametrize("utterance,domain,service", [
    ("unlock the front door when I get home", "lock", "unlock"), ("lock the door in an hour", "lock", "lock"),
    ("open the garage door at 5pm", "cover", "open_cover"),
], ids=str)
def test_the_agents_immediate_tool_refuses_a_door_on_a_deferred_turn(utterance, domain, service):
    h = _agent_calls_after(utterance, "ha_service", {"domain": domain, "service": service,
                                                      "entity_id": f"{domain}.front_door"})
    assert h.client.call_service.await_count == 0, utterance


def test_a_trailing_time_is_still_set_up_by_the_agent_for_lights_and_doors():
    create = ("create_automation", {"name": "Later", "trigger": {"type": "time", "time": "17:00"},
                                    "actions": [{"type": "service", "entity_id": "light.office_0",
                                                 "service": "turn_off"}]})
    for utterance in ("turn off the lights in 10 minutes", "turn off the lights when I leave"):
        h = _agent_calls_after(utterance, *create)
        assert h.client.create_automation.await_count == 1, utterance


@pytest.mark.parametrize("utterance", NARRATED_OPENINGS, ids=str)
def test_a_narrated_opening_writes_nothing_through_the_controller(utterance):
    domain, action = ("cover", "open") if "garage" in utterance else ("lock", "unlock")
    answer, calls = _door_controller_calls(utterance, domain, action)
    assert answer == NO_WRITE_CUE_TEXT and calls == [], (utterance, answer)


@pytest.mark.parametrize("utterance", NARRATED_OPENINGS, ids=str)
def test_a_narrated_opening_writes_nothing_through_the_agent(utterance):
    domain, service = ("cover", "open_cover") if "garage" in utterance else ("lock", "unlock")
    h = _agent_calls_after(utterance, "ha_service", {"domain": domain, "service": service,
                                                      "entity_id": f"{domain}.front_door"})
    assert h.client.call_service.await_count == 0, utterance


@pytest.mark.parametrize("utterance,domain,action", [
    ("unlock the front door", "lock", "unlock"), ("please unlock the front door", "lock", "unlock"),
    ("open the garage door", "cover", "open"),
], ids=str)
def test_an_ordinary_door_opening_still_writes(utterance, domain, action):
    answer, calls = _door_controller_calls(utterance, domain, action)
    assert len(calls) == 1, (utterance, answer)


@pytest.mark.parametrize("utterance", [
    "she said unlock the front door", "in the movie they unlock the front door", "Unlock the front door, said nobody",
], ids=str)
def test_narrated_and_tagged_openings_are_no_command(utterance):
    uk = classify_utterance(utterance)
    assert uk.kind != IMPERATIVE and uk.command_text is None, utterance
