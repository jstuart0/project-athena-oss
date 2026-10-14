"""The extractor sees the command, not the narration around it (T3).

``route_control`` hands ``cmd`` (the command clauses, plus the room an earlier
clause named) to all four ``extract_intent`` calls and to ``execute_intent`` as
the original query. The real extractor builds its prompt from it, its
``_ground_intent_rooms`` and fast path read it, "split" is not "lit", and a
brightness percent is read by ``parse_brightness`` ("up to 80%").

Synthetic utterances only.
"""
from __future__ import annotations

import unittest.mock as mock
from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.nodes import _runtime, route_control_node
from orchestrator.smart_home_controller import SmartHomeController, parse_brightness

from ._clause_harness import QueryReadingModel, House, drive_control, run, writes
from .test_route_control import _make_smart_controller, _make_state

NARRATION = "the blue led ring and the kids"


def _node(state, sc):
    _runtime.reset_for_test()
    _runtime.set_smart_controller(sc)
    _runtime.set_automation_agent(None)
    _runtime.set_entity_manager(None)
    with (
        patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock,
              return_value={"enabled": False}),
        patch("orchestrator.nodes.route_control.get_automation_system_mode", new_callable=AsyncMock,
              return_value="pattern"),
        patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
        patch("orchestrator.nodes.route_control.store_conversation_context", new_callable=AsyncMock),
        patch("orchestrator.nodes.route_control._configured_assistant_names", new_callable=AsyncMock,
              return_value=()),
    ):
        return run(route_control_node(state))


class TestAllFourExtractionSites:
    """A mock controller records what each call site was handed."""

    def test_normal_extraction(self):
        sc = _make_smart_controller()
        state = _make_state(query=f"{NARRATION}, okay, turn on the hall light")
        _node(state, sc)
        sc.extract_intent.assert_awaited_once_with("turn on the hall light", device_room="living room",
                                                   utterance=mock.ANY)
        assert sc.execute_intent.await_args.kwargs["original_query"] == "turn on the hall light"

    def test_the_context_continuation_site(self):
        sc = _make_smart_controller()
        prev = {"response": "Hall off.", "entities": {"room": "hall", "device_type": "light"},
                "parameters": {"device_type": "light", "action": "turn_off"}, "query": "turn off the hall light"}
        state = _make_state(query=f"{NARRATION}, okay, turn them back on", prev_context=prev,
                            context_ref_info={"has_context_ref": True})
        _node(state, sc)
        assert sc.extract_intent.await_args.args[0] == "turn them back on"
        assert sc.extract_intent.await_args.kwargs["prev_query"] == "turn off the hall light"

    def test_the_different_colors_site(self):
        sc = _make_smart_controller()
        prev = {"response": "Hall blue.", "entities": {"room": "hall", "device_type": "light"},
                "parameters": {"device_type": "light", "action": "set_color"}, "query": "make the hall blue"}
        state = _make_state(query=f"{NARRATION}, okay, turn them another color", prev_context=prev,
                            context_ref_info={"has_context_ref": True, "ref_types": ["modifier"]})
        _node(state, sc)
        queries = [c.args[0] for c in sc.extract_intent.await_args_list]
        assert queries == ["turn them another color", "turn them another color different colors"], queries

    def test_the_state_question_site(self):
        sc = _make_smart_controller(extract_intent=AsyncMock(return_value={"device_type": "climate", "room": None}))
        state = _make_state(query="is the heater on")
        _node(state, sc)
        assert sc.extract_intent.await_args.args[0] == "is the heater on"

    def test_a_query_with_no_command_goes_on_whole(self):
        sc = _make_smart_controller()
        state = _make_state(query="kitchen lights on please")
        _node(state, sc)
        assert sc.extract_intent.await_args.args[0] == "kitchen lights on please"

    def test_the_earlier_room_is_handed_on_with_the_command(self):
        sc = _make_smart_controller()
        state = _make_state(query="In the kitchen. Turn on the lights.")
        _node(state, sc)
        assert sc.extract_intent.await_args.args[0] == "Turn on the lights in the kitchen"
        assert sc.execute_intent.await_args.kwargs["original_query"] == "Turn on the lights in the kitchen"


class TestTheRealExtractorsPrompt:
    def test_narration_words_are_absent_from_the_prompt(self):
        model = QueryReadingModel()
        state, model, client = drive_control(
            "the blue led ring is pretty. the kids love it. okay, set the hall lamp to a calmer look", model=model,
        )
        assert model.queries, "the model was never asked"
        for query in model.queries:
            assert "blue" not in query.lower() and "kids" not in query.lower(), query
        assert all("blue led ring" not in p for p in model.prompts)
        assert [w[1] for w in writes(client)] == ["turn_on"], (writes(client), state.answer)

    def test_the_extractor_is_asked_for_the_command_only_in_a_run_together_sentence(self):
        model = QueryReadingModel()
        drive_control("the blue led ring okay set the hall lamp to a calmer look", model=model)
        assert model.queries == ["set the hall lamp to a calmer look"]


def _controller(model):
    return SmartHomeController(entity_manager=House(), llm_router=model)


def _extract(model, query, **kwargs):
    controller = _controller(model)
    with patch("orchestrator.smart_home_controller.get_admin_client") as admin:
        admin.return_value.get_component_model = AsyncMock(return_value=None)
        return run(controller.extract_intent(query, **kwargs))


class TestLitIsAWord:
    def test_split_is_not_lit(self):
        model = QueryReadingModel()
        intent = _extract(model, "split the hall lamp group")
        assert model.queries, "'split' must not take the turn-on fast path"
        assert intent["action"] == "turn_on"  # the stub's answer, not the fast path's

    def test_lit_is_still_turn_on(self):
        model = QueryReadingModel()
        intent = _extract(model, "get the kitchen lit")
        assert not model.queries and intent["action"] == "turn_on"

    def test_lit_beside_punctuation(self):
        model = QueryReadingModel()
        intent = _extract(model, "get the kitchen lit!")
        assert not model.queries and intent["action"] == "turn_on"


@pytest.mark.parametrize("query,percent", [
    ("turn the lights up to 80%", 80), ("Turn the lights up to 80%", 80), ("down to 0 percent", 0),
    ("lights to fifty percent", 50), ("dim to half", 50), ("set it to 30%", 30), ("up to 80%", 80),
    ("lights at twenty five percent", 25), ("lights at twenty-five percent", 25),
    ("dim the living room 2 lights to 50 percent", 50), ("dim the lights to 40", 40), ("set lights to 100", 100),
    ("fifty five percent lights", 55), ("turn on the lights at 6", None), ("set the thermostat to 72", None),
    ("lights to 150", None), ("dim to 7 pm", None), ("", None), (None, None),
], ids=str)
def test_parse_brightness(query, percent):
    assert parse_brightness(query) == percent


@pytest.mark.parametrize("query,percent", [
    ("turn the lights up to 80%", 80), ("lights to fifty percent", 50), ("dim the lights to 40", 40),
])
def test_brightness_goes_through_the_fast_path(query, percent):
    model = QueryReadingModel()
    intent = _extract(model, query)
    assert not model.queries, "the model must not be asked"
    assert intent["action"] == "set_brightness"
    assert intent["parameters"]["brightness"] == int(percent / 100 * 255)
