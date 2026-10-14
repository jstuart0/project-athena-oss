"""A command is never answered as a read (T3).

When the model returns ``get_status`` for an IMPERATIVE utterance, or its JSON
cannot be parsed and the words carry no explicit on or off, the answer is the
unprocessable phrase: no Home Assistant read, no write, and the counter
``imperative_unresolved`` ticks. A question still gets its read; an explicit
"on" or "off" still acts on a garbled answer.

Synthetic utterances only.
"""
from __future__ import annotations

import json
import unittest.mock as mock

import pytest

from orchestrator import room_context
from orchestrator.utterance_kind import UtteranceKind, classify_utterance

from ._clause_harness import QueryReadingModel, drive_control, writes

COMMAND = "set the hall lamp to a calmer look"  # an IMPERATIVE with no explicit on or off


class FixedModel(QueryReadingModel):
    def __init__(self, text):
        super().__init__()
        self._text = text

    async def _generate(self, **kwargs):
        await super()._generate(**kwargs)
        return {"response": self._text}


GET_STATUS = json.dumps({"device_type": "light", "room": "hall", "action": "get_status",
                         "target_scope": "group", "parameters": {}})


def _no_reads(client):
    return client.get_state.await_count == 0 and client.get_states.await_count == 0


@pytest.mark.parametrize("reply", [GET_STATUS, "this is not json at all", "{broken", "```json\nnope\n```"],
                         ids=["get_status", "prose", "broken_json", "fenced_nonsense"])
def test_a_command_the_model_cannot_resolve_gets_the_unprocessable_phrase(reply):
    assert classify_utterance(COMMAND).kind == UtteranceKind.IMPERATIVE
    metric = mock.MagicMock()
    with mock.patch("orchestrator.metrics.state_question_routed_total", metric):
        state, model, client = drive_control(COMMAND, model=FixedModel(reply))
    assert model.queries, "the model was asked"
    assert state.answer == room_context.UNPROCESSABLE_TEXT, state.answer
    assert writes(client) == [] and _no_reads(client)
    metric.labels.assert_called_with(device_type="light", path="imperative_unresolved")
    assert state.retrieved_data in (None, {}, [])


def test_a_garbled_answer_with_an_explicit_target_still_acts():
    state, _model, client = drive_control("set the hall lamp on", model=FixedModel("not json"))
    assert writes(client) and writes(client)[0][:2] == ("light", "turn_on"), state.answer


def test_a_question_still_gets_its_read():
    state, model, client = drive_control("is the hall lamp on", model=FixedModel(GET_STATUS))
    assert state.answer and state.answer != room_context.UNPROCESSABLE_TEXT
    assert writes(client) == []


def test_the_extractor_returns_the_marked_result_for_a_command():
    from orchestrator.smart_home_controller import SmartHomeController
    from ._clause_harness import House, run
    controller = SmartHomeController(entity_manager=House(), llm_router=FixedModel(GET_STATUS))
    with mock.patch("orchestrator.smart_home_controller.get_admin_client") as admin:
        admin.return_value.get_component_model = mock.AsyncMock(return_value=None)
        intent = run(controller.extract_intent(COMMAND))
    assert intent == room_context.unprocessable_intent("imperative_unresolved")
    assert room_context.unprocessable_text(intent) == room_context.UNPROCESSABLE_TEXT
