"""Which turns reach the dynamic automation agent.

A motion-flow control ("disable motion in the garage") and a question are the
controller's; a motion condition ("when there's motion in the hall, ...") is an
automation and is the agent's. The route table runs the real `route_control_node`
in dynamic-agent mode with a fake agent that fails the test if it is called.
"""
from __future__ import annotations

import sys
from unittest import mock
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, "src")
sys.path.insert(0, "src/orchestrator")

from orchestrator.automation_agent import is_motion_trigger_form, should_use_automation_agent
from orchestrator.nodes import _runtime
from orchestrator.nodes.route_control import _is_agent_exempt
from orchestrator.utterance_kind import classify_utterance
from unit.test_write_fanout_confirmation import _Harness, _run, _state54

NOT_THE_AGENT = [
    "disable motion in the garage",
    "turn off motion in the office",
    "pause motion detection in the hall",
    "is there motion in the garage",
    "was there motion detected in the garage",
    "motion in the garage",
    "turn on the kitchen lights",
    "leave the lights on, I'm taking a nap",
    "I wonder if there was motion in the hall",
    "I wonder if there was motion in the hall and the lights came on",
]
THE_AGENT = [
    "when there's motion in the hallway turn on the lamp",
    "if motion is detected in the garage turn on the lights",
    "when there is no motion for ten minutes turn off the lights",
    "whenever there is movement in the kitchen turn on the light",
    "turn off the porch light at 6pm",
]


class _FailingAgent:
    def __init__(self):
        self.calls = []

    async def execute(self, query, context, model=None):
        self.calls.append((query, model))
        return "agent answered"


class _GateHarness(_Harness):
    def __init__(self):
        super().__init__(n_lights=3)
        self.agent = _FailingAgent()

    def _install(self):
        super()._install()
        _runtime.set_automation_agent(self.agent)

    def route(self, query):
        async def _go():
            with self.patched(), \
                    mock.patch("orchestrator.nodes.route_control.get_automation_system_mode",
                               new_callable=AsyncMock, return_value="dynamic_agent"), \
                    mock.patch("orchestrator.nodes.route_control.should_use_automation_agent",
                               new=should_use_automation_agent), \
                    mock.patch("orchestrator.nodes.route_control.get_model_for_component",
                               new_callable=AsyncMock, return_value="agent-model"):
                return await self.arun(_state54(query, context_ref_info={}), patch=False)
        return _run(_go())


@pytest.mark.parametrize("query", NOT_THE_AGENT, ids=NOT_THE_AGENT)
def test_the_gate_leaves_these_to_the_controller(query):
    uk = classify_utterance(query)
    assert should_use_automation_agent(query) is False or _is_agent_exempt(query, uk)


@pytest.mark.parametrize("query", NOT_THE_AGENT, ids=NOT_THE_AGENT)
def test_the_agent_is_never_called_for_them(query):
    harness = _GateHarness()
    harness.route(query)
    assert harness.agent.calls == []


@pytest.mark.parametrize("query", THE_AGENT, ids=THE_AGENT)
def test_a_condition_or_schedule_reaches_the_agent_with_the_component_model(query):
    harness = _GateHarness()
    out = harness.route(query)
    assert [model for _, model in harness.agent.calls] == ["agent-model"]
    assert out.answer == "agent answered"


def test_the_bare_motion_phrases_are_no_longer_triggers():
    assert is_motion_trigger_form("motion in the garage") is False
    assert is_motion_trigger_form("motion detected in the garage") is False
    assert is_motion_trigger_form("when there's motion in the garage") is True
    assert should_use_automation_agent("disable motion in the garage") is False


def test_a_motion_condition_that_contains_a_control_phrase_stays_with_the_agent():
    query = "when there's no motion for ten minutes leave the lights off"
    assert _is_agent_exempt(query, classify_utterance(query)) is False


def test_the_exemption_covers_a_question_and_a_motion_control_directly():
    assert _is_agent_exempt("are the lights on in the garage", classify_utterance("are the lights on in the garage"))
    for query in ("disable motion in the garage", "pause motion detection in the hall", "no motion for me tonight"):
        assert _is_agent_exempt(query, classify_utterance(query)), query
    for query in ("turn off the porch light at 6pm", "when there's motion in the hall turn on the lamp"):
        assert not _is_agent_exempt(query, classify_utterance(query)), query


def test_automation_listing_is_not_blocked_by_the_exemption():
    """The exemption never claims a request to list or manage automations. "list my automations"
    reaches the agent; "what automations do I have" is narration to the clause rules, at base
    as now, and the exemption leaves it exactly as it was."""
    for query in ("list my automations", "show me my routines", "what automations do I have"):
        assert _is_agent_exempt(query, classify_utterance(query)) is False, query
    assert should_use_automation_agent("list my automations") is True
    assert should_use_automation_agent("show me my routines") is True
