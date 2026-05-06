"""Unit tests for orchestrator.nodes.validate.validate_node.

Covers major behavioral branches:

 1.  Answer too short — validation_passed=False, reason="Response too short".
 2.  Answer too long — validation_passed=False, reason="Response too long".
 3.  Low-risk chitchat bypass — validation_passed=True, no LLM call.
 4.  Built-in time/date bypass — validation_passed=True, no LLM call.
 5.  No specific facts, no supporting data — passes without LLM call.
 6.  Specific facts with supporting data — passes without LLM call.
 7.  Specific facts, no supporting data, base_knowledge_populated=True — passes.
 8.  Specific facts, no supporting data — LLM fact-check called, hallucination detected.
 9.  Specific facts, no supporting data — LLM fact-check called, response clean.
10.  LLM returns unparseable response — validation_passed=False, reason="Could not verify".
11.  LLM returns invalid JSON — json.JSONDecodeError path, validation_passed=False.
12.  LLM router raises exception — exception handler fires, validation_passed=False.
13.  Metrics: validation_counter.labels(...).inc() called on too_short path.
14.  Metrics: hallucination_counter incremented when date pattern found with no data.
15.  node_timings["validate"] set on every return path.
16.  timing_tracker.track_sync called when timing_tracker is set.

Patching strategy:
- ``orchestrator.nodes.validate.get_validation_guardrails`` — AsyncMock returning guardrails dict.
- ``orchestrator.nodes.validate.get_component_config`` — AsyncMock returning config dict.
- LLM router: install via ``orchestrator.nodes._runtime.set_llm_router``.
- Metrics counters/histograms: patched at ``orchestrator.nodes.validate.*``.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch, call

# Stub heavy deps not installed in the unit-test environment — must happen
# before any orchestrator import (matches pattern in test_health_probes.py).
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator.nodes import validate_node
from orchestrator.nodes import _runtime
from orchestrator.state import OrchestratorState, IntentCategory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_GUARDRAILS = {"min_response_chars": 10, "max_response_chars": 5000}


def _make_state(
    *,
    query: str = "What is the weather like?",
    answer: str = "The weather is sunny today with a high of 75 degrees.",
    intent: IntentCategory | None = IntentCategory.WEATHER,
    retrieved_data: dict | None = None,
    base_knowledge_populated: bool = False,
    mode: str = "guest",
    room: str = "living_room",
    session_id: str | None = "sess-1",
    request_id: str | None = "req-1",
    timing_tracker=None,
) -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.answer = answer
    state.intent = intent
    state.retrieved_data = retrieved_data if retrieved_data is not None else {}
    state.base_knowledge_populated = base_knowledge_populated
    state.mode = mode
    state.room = room
    state.session_id = session_id
    state.request_id = request_id
    state.timing_tracker = timing_tracker
    return state


def _patch_guardrails(guardrails: dict | None = None):
    return patch(
        "orchestrator.nodes.validate.get_validation_guardrails",
        new=AsyncMock(return_value=guardrails or _DEFAULT_GUARDRAILS),
    )


def _patch_component_config(model_name: str = "test-model"):
    return patch(
        "orchestrator.nodes.validate.get_component_config",
        new=AsyncMock(return_value={"model_name": model_name, "system_prompt": ""}),
    )


def _make_llm_router(response_json: str | None = None) -> MagicMock:
    """Return a mock LLM router whose generate() returns a fact-check response dict."""
    router = MagicMock()
    payload = response_json or json.dumps({"contains_hallucinations": False, "reason": "clean", "specific_claims": []})
    router.generate = AsyncMock(return_value={"response": payload, "eval_count": 5})
    return router


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test 1: Answer too short
# ---------------------------------------------------------------------------

def test_answer_too_short_fails_validation():
    state = _make_state(answer="Hi")  # < 10 chars
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert result.validation_passed is False
    assert result.validation_reason == "Response too short"
    assert "validate" in result.node_timings


# ---------------------------------------------------------------------------
# Test 2: Answer too long
# ---------------------------------------------------------------------------

def test_answer_too_long_fails_validation():
    state = _make_state(answer="x" * 6000)  # > 5000 chars
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert result.validation_passed is False
    assert result.validation_reason == "Response too long"
    assert "validate" in result.node_timings


# ---------------------------------------------------------------------------
# Test 3: Low-risk chitchat bypass
# ---------------------------------------------------------------------------

def test_low_risk_chitchat_bypasses_validation():
    state = _make_state(
        query="hello there",
        answer="Hi! How can I help you today?",
        intent=IntentCategory.GENERAL_INFO,
    )
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert result.validation_passed is True
    assert result.validation_reason is None
    assert "validate" in result.node_timings


# ---------------------------------------------------------------------------
# Test 4: Built-in time/date bypass
# ---------------------------------------------------------------------------

def test_builtin_time_query_bypasses_validation():
    state = _make_state(
        query="what time is it",
        answer="It is 3:45 PM right now.",
        intent=IntentCategory.GENERAL_INFO,
    )
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert result.validation_passed is True
    assert "validate" in result.node_timings


# ---------------------------------------------------------------------------
# Test 5: No specific facts, no supporting data — passes without LLM
# ---------------------------------------------------------------------------

def test_no_specific_facts_passes_without_llm():
    state = _make_state(
        answer="The weather is nice today.",
        retrieved_data={},
        base_knowledge_populated=False,
    )
    router = _make_llm_router()
    _runtime.set_llm_router(router)
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert result.validation_passed is True
    router.generate.assert_not_called()


# ---------------------------------------------------------------------------
# Test 6: Specific facts with supporting data — passes without LLM call
# ---------------------------------------------------------------------------

def test_specific_facts_with_supporting_data_passes():
    # January 15th, 2024 is a specific date — triggers pattern detection
    state = _make_state(
        answer="Your appointment is on January 15th, 2024.",
        retrieved_data={"appointment": "January 15th, 2024"},
    )
    router = _make_llm_router()
    _runtime.set_llm_router(router)
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert result.validation_passed is True
    router.generate.assert_not_called()


# ---------------------------------------------------------------------------
# Test 7: Specific facts, base_knowledge_populated=True — passes
# ---------------------------------------------------------------------------

def test_specific_facts_base_knowledge_populated_passes():
    state = _make_state(
        answer="It will be $25.00 for that service.",
        retrieved_data={},
        base_knowledge_populated=True,
    )
    router = _make_llm_router()
    _runtime.set_llm_router(router)
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert result.validation_passed is True
    router.generate.assert_not_called()


# ---------------------------------------------------------------------------
# Test 8: Specific facts, no supporting data — LLM detects hallucination
# ---------------------------------------------------------------------------

def test_hallucination_detected_by_llm():
    state = _make_state(
        answer="Call us at 555-123-4567 for help.",
        retrieved_data={},
        base_knowledge_populated=False,
    )
    hallucination_payload = json.dumps({
        "contains_hallucinations": True,
        "reason": "phone number not in data",
        "specific_claims": ["555-123-4567"],
    })
    router = _make_llm_router(response_json=hallucination_payload)
    _runtime.set_llm_router(router)
    with _patch_guardrails(), _patch_component_config():
        result = _run(validate_node(state))
    assert result.validation_passed is False
    assert "Hallucination detected" in result.validation_reason
    assert "555-123-4567" in result.validation_details


# ---------------------------------------------------------------------------
# Test 9: Specific facts, no supporting data — LLM says clean
# ---------------------------------------------------------------------------

def test_llm_fact_check_passes_clean_response():
    state = _make_state(
        answer="The event costs $50.00.",
        retrieved_data={},
        base_knowledge_populated=False,
    )
    clean_payload = json.dumps({
        "contains_hallucinations": False,
        "reason": "all claims verified",
        "specific_claims": [],
    })
    router = _make_llm_router(response_json=clean_payload)
    _runtime.set_llm_router(router)
    with _patch_guardrails(), _patch_component_config():
        result = _run(validate_node(state))
    assert result.validation_passed is True


# ---------------------------------------------------------------------------
# Test 10: LLM returns response with no JSON — parse error path
# ---------------------------------------------------------------------------

def test_llm_returns_no_json_fails_validation():
    state = _make_state(
        answer="Price is $99.99.",
        retrieved_data={},
        base_knowledge_populated=False,
    )
    router = MagicMock()
    router.generate = AsyncMock(return_value={"response": "Sorry, I cannot determine that.", "eval_count": 0})
    _runtime.set_llm_router(router)
    with _patch_guardrails(), _patch_component_config():
        result = _run(validate_node(state))
    assert result.validation_passed is False
    assert result.validation_reason == "Could not verify response accuracy"


# ---------------------------------------------------------------------------
# Test 11: LLM returns invalid JSON — json.JSONDecodeError path
# ---------------------------------------------------------------------------

def test_llm_returns_invalid_json_fails_validation():
    state = _make_state(
        answer="The appointment is 3/15/2024.",
        retrieved_data={},
        base_knowledge_populated=False,
    )
    router = MagicMock()
    # The response contains braces but not valid JSON
    router.generate = AsyncMock(return_value={"response": "{bad json here}", "eval_count": 0})
    _runtime.set_llm_router(router)
    with _patch_guardrails(), _patch_component_config():
        result = _run(validate_node(state))
    assert result.validation_passed is False
    assert result.validation_reason == "Could not verify response accuracy"


# ---------------------------------------------------------------------------
# Test 12: LLM router raises exception — outer exception handler
# ---------------------------------------------------------------------------

def test_llm_router_exception_fails_validation():
    state = _make_state(
        answer="Call 800-555-0100 for support.",
        retrieved_data={},
        base_knowledge_populated=False,
    )
    router = MagicMock()
    router.generate = AsyncMock(side_effect=RuntimeError("connection refused"))
    _runtime.set_llm_router(router)
    with _patch_guardrails(), _patch_component_config():
        result = _run(validate_node(state))
    assert result.validation_passed is False
    assert "Validation error" in result.validation_reason


# ---------------------------------------------------------------------------
# Test 13: Metrics — validation_counter.labels(...).inc() on too_short path
# ---------------------------------------------------------------------------

def test_metrics_counter_incremented_on_too_short():
    state = _make_state(answer="Hi")
    mock_counter = MagicMock()
    mock_layer_duration = MagicMock()
    with _patch_guardrails(), \
         patch("orchestrator.nodes.validate.validation_counter", mock_counter), \
         patch("orchestrator.nodes.validate.validation_layer_duration", mock_layer_duration):
        _run(validate_node(state))
    mock_counter.labels(passed="false", reason="too_short").inc.assert_called_once()


# ---------------------------------------------------------------------------
# Test 14: Metrics — hallucination_counter incremented when phone pattern found
# ---------------------------------------------------------------------------

def test_hallucination_counter_incremented_for_phone_pattern():
    state = _make_state(
        answer="Call 555-867-5309 for info.",
        retrieved_data={},
        base_knowledge_populated=False,
    )
    mock_hallucination = MagicMock()
    router = _make_llm_router()  # Returns clean response
    _runtime.set_llm_router(router)
    with _patch_guardrails(), _patch_component_config(), \
         patch("orchestrator.nodes.validate.hallucination_counter", mock_hallucination):
        _run(validate_node(state))
    mock_hallucination.labels(layer="pattern_detection", type="phone_unsupported").inc.assert_called()


# ---------------------------------------------------------------------------
# Test 15: node_timings["validate"] is set on every return path
# ---------------------------------------------------------------------------

def test_node_timings_set_on_chitchat_bypass():
    state = _make_state(
        query="hey there",
        answer="Hey! What can I do for you?",
        intent=IntentCategory.GENERAL_INFO,
    )
    with _patch_guardrails():
        result = _run(validate_node(state))
    assert "validate" in result.node_timings
    assert result.node_timings["validate"] >= 0.0


# ---------------------------------------------------------------------------
# Test 16: timing_tracker.track_sync called when timing_tracker is present
# ---------------------------------------------------------------------------

def test_timing_tracker_track_sync_called():
    tracker = MagicMock()
    state = _make_state(
        answer="The weather is nice today.",
        timing_tracker=tracker,
    )
    with _patch_guardrails():
        _run(validate_node(state))
    tracker.track_sync.assert_called_once_with("graph", "validate", state.node_timings["validate"])
