"""Unit tests for orchestrator.nodes.finalize.finalize_node.

Covers major behavioral branches:

 1.  Multi-intent — more intents pending: early return, state reset for next intent.
 2.  Multi-intent — all intents done: answers combined, continues to finalize logic.
 3.  Post-synthesis fallback triggered (maybe_post_synthesis_fallback returns True).
 4.  Post-synthesis fallback raises exception: warning logged, continues.
 5.  Validation failed — hallucination reason: hallucination-specific answer set.
 6.  Validation failed — state.error set: error-specific answer set.
 7.  Validation failed — generic reason: generic fallback answer set.
 8.  Empty answer safety net: is_fallback=True, generic answer provided.
 9.  Hallucination continuation stripped (_strip_hallucinated_continuation called).
10.  Cache write succeeds (cache_client.set called with correct key/TTL).
11.  Cache write fails: warning logged, state still returned.
12.  node_timings["finalize"] set on every return path (including early multi-intent).
13.  timing_tracker.track_sync called when timing_tracker is present.
14.  Single-intent happy path: answer unmodified, node_timings set.

Patching strategy:
- ``orchestrator.nodes.finalize.maybe_post_synthesis_fallback`` — AsyncMock.
- ``orchestrator.nodes.finalize._strip_hallucinated_continuation`` — MagicMock.
- cache_client: install via ``orchestrator.nodes._runtime.set_cache_client``.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch, call

# Stub heavy deps not installed in the unit-test environment — must happen
# before any orchestrator import.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator.nodes import finalize_node
from orchestrator.nodes import _runtime
from orchestrator.state import OrchestratorState, IntentCategory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    *,
    query: str = "What is the weather?",
    answer: str = "It is sunny today.",
    intent: IntentCategory | None = IntentCategory.WEATHER,
    validation_passed: bool = True,
    validation_reason: str | None = None,
    is_multi_intent: bool = False,
    intent_parts: list | None = None,
    intent_results: list | None = None,
    current_intent_index: int = 0,
    error: str | None = None,
    is_fallback: bool = False,
    request_id: str = "req-1",
    session_id: str = "sess-1",
    mode: str = "guest",
    room: str = "living_room",
    timing_tracker=None,
    data_source: str | None = "weather_api",
) -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.answer = answer
    state.intent = intent
    state.validation_passed = validation_passed
    state.validation_reason = validation_reason
    state.is_multi_intent = is_multi_intent
    state.intent_parts = intent_parts if intent_parts is not None else []
    state.intent_results = intent_results if intent_results is not None else []
    state.current_intent_index = current_intent_index
    state.error = error
    state.is_fallback = is_fallback
    state.request_id = request_id
    state.session_id = session_id
    state.mode = mode
    state.room = room
    state.timing_tracker = timing_tracker
    state.data_source = data_source
    return state


def _make_cache_client(*, raise_on_set: Exception | None = None) -> MagicMock:
    client = MagicMock()
    if raise_on_set:
        client.set = AsyncMock(side_effect=raise_on_set)
    else:
        client.set = AsyncMock(return_value=None)
    return client


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Test 1: Multi-intent — more intents pending → early return
# ---------------------------------------------------------------------------

def test_multi_intent_more_intents_resets_state_for_next():
    """When current_intent_index < len(intent_parts) - 1, returns early."""
    state = _make_state(
        is_multi_intent=True,
        intent_parts=["What is the weather?", "What time is it?"],
        intent_results=[],
        current_intent_index=0,
        answer="It is sunny.",
    )
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)):
        result = _run(finalize_node(state))

    # Index advanced
    assert result.current_intent_index == 1
    # Query set to next part
    assert result.query == "What time is it?"
    # State reset for next intent
    assert result.intent is None
    assert result.answer is None
    assert result.validation_passed is True
    assert result.validation_reason is None
    # node_timings set on early return
    assert "finalize" in result.node_timings
    # Cache NOT called (early return before cache block)
    cache.set.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: Multi-intent — all intents done → combines answers
# ---------------------------------------------------------------------------

def test_multi_intent_all_done_combines_answers():
    """When last intent processed, answers are joined and execution continues."""
    state = _make_state(
        is_multi_intent=True,
        intent_parts=["part 1", "part 2"],
        intent_results=[{"query": "part 1", "intent": "weather", "answer": "Sunny.", "data_source": None}],
        current_intent_index=1,
        answer="It is 3 PM.",
    )
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    # Combined: first answer + blank separator + second answer
    assert "Sunny." in result.answer
    assert "It is 3 PM." in result.answer
    assert "finalize" in result.node_timings


# ---------------------------------------------------------------------------
# Test 3: Post-synthesis fallback triggered
# ---------------------------------------------------------------------------

def test_post_synthesis_fallback_triggered():
    """maybe_post_synthesis_fallback returning True is logged; state still returned."""
    state = _make_state(answer="Some answer.", validation_passed=True)
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    fallback_mock = AsyncMock(return_value=True)
    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=fallback_mock), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    fallback_mock.assert_awaited_once()
    assert "finalize" in result.node_timings


# ---------------------------------------------------------------------------
# Test 4: Post-synthesis fallback raises exception → continues gracefully
# ---------------------------------------------------------------------------

def test_post_synthesis_fallback_exception_is_swallowed():
    """Exception from maybe_post_synthesis_fallback is caught; node completes."""
    state = _make_state(answer="Some answer.", validation_passed=True)
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    fallback_mock = AsyncMock(side_effect=RuntimeError("network error"))
    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=fallback_mock), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    # Node completes normally despite fallback exception
    assert result.answer == "Some answer."
    assert "finalize" in result.node_timings


# ---------------------------------------------------------------------------
# Test 5: Validation failed — hallucination reason
# ---------------------------------------------------------------------------

def test_validation_failed_hallucination_reason():
    """Hallucination keyword in validation_reason triggers hallucination-specific answer."""
    state = _make_state(
        answer="Wrong data.",
        validation_passed=False,
        validation_reason="Hallucination detected: phone number not in data",
    )
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    assert "I don't have current information" in result.answer
    assert state.query.lower() in result.answer


# ---------------------------------------------------------------------------
# Test 6: Validation failed — state.error set
# ---------------------------------------------------------------------------

def test_validation_failed_with_error():
    """When validation fails and state.error is set, error-specific message used."""
    state = _make_state(
        answer="Failed.",
        validation_passed=False,
        validation_reason="Something went wrong",
        error="upstream timeout",
    )
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    assert "I encountered an issue processing your request" in result.answer


# ---------------------------------------------------------------------------
# Test 7: Validation failed — generic reason (no hallucination, no error)
# ---------------------------------------------------------------------------

def test_validation_failed_generic_reason():
    """Generic validation failure yields the 'not confident' fallback."""
    state = _make_state(
        answer="Some response.",
        validation_passed=False,
        validation_reason="Too vague",
        error=None,
    )
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    assert "not confident" in result.answer


# ---------------------------------------------------------------------------
# Test 8: Empty answer safety net → is_fallback=True
# ---------------------------------------------------------------------------

def test_empty_answer_sets_fallback():
    """When answer is None/empty after all processing, is_fallback is set True."""
    state = _make_state(answer=None, validation_passed=True)
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    assert result.is_fallback is True
    assert result.answer  # Non-empty fallback string provided
    assert "rephrase" in result.answer.lower()


# ---------------------------------------------------------------------------
# Test 9: Hallucination continuation stripped
# ---------------------------------------------------------------------------

def test_strip_hallucinated_continuation_called():
    """_strip_hallucinated_continuation is called on state.answer."""
    state = _make_state(answer="Good answer.\nUser: follow-up", validation_passed=True)
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    strip_mock = MagicMock(return_value="Good answer.")
    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", strip_mock):
        result = _run(finalize_node(state))

    strip_mock.assert_called_once()
    assert result.answer == "Good answer."


# ---------------------------------------------------------------------------
# Test 10: Cache write succeeds
# ---------------------------------------------------------------------------

def test_cache_write_called_with_correct_args():
    """cache_client.set is called with conversation key and 1-hour TTL."""
    state = _make_state(answer="Fine.", validation_passed=True, request_id="req-abc")
    cache = _make_cache_client()
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        _run(finalize_node(state))

    cache.set.assert_awaited_once()
    call_args = cache.set.call_args
    assert call_args[0][0] == "conversation:req-abc"
    assert call_args[1]["ttl"] == 3600


# ---------------------------------------------------------------------------
# Test 11: Cache write fails → warning logged, state returned normally
# ---------------------------------------------------------------------------

def test_cache_write_failure_is_swallowed():
    """Exception from cache_client.set is caught; node still returns state."""
    state = _make_state(answer="Fine.", validation_passed=True)
    cache = _make_cache_client(raise_on_set=RuntimeError("Redis unavailable"))
    _runtime.set_cache_client(cache)

    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    # State is returned despite cache failure
    assert result.answer == "Fine."
    assert "finalize" in result.node_timings


# ---------------------------------------------------------------------------
# Test 12: node_timings["finalize"] set on every return path
# ---------------------------------------------------------------------------

def test_node_timings_set_on_early_multi_intent_return():
    """Early return from multi-intent path still sets node_timings["finalize"]."""
    state = _make_state(
        is_multi_intent=True,
        intent_parts=["q1", "q2", "q3"],
        intent_results=[],
        current_intent_index=0,
        answer="First answer.",
    )
    _runtime.set_cache_client(_make_cache_client())

    result = _run(finalize_node(state))
    assert "finalize" in result.node_timings
    assert result.node_timings["finalize"] >= 0.0


# ---------------------------------------------------------------------------
# Test 13: timing_tracker.track_sync called when present
# ---------------------------------------------------------------------------

def test_timing_tracker_called():
    """timing_tracker.track_sync("graph", "finalize", duration) is called."""
    tracker = MagicMock()
    state = _make_state(answer="Good.", validation_passed=True, timing_tracker=tracker)
    _runtime.set_cache_client(_make_cache_client())

    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    tracker.track_sync.assert_called_once_with("graph", "finalize", result.node_timings["finalize"])


# ---------------------------------------------------------------------------
# Test 14: Single-intent happy path end-to-end
# ---------------------------------------------------------------------------

def test_single_intent_happy_path():
    """Non-multi-intent state with valid answer completes normally."""
    state = _make_state(
        answer="It will be sunny with a high of 72°F.",
        validation_passed=True,
        is_multi_intent=False,
    )
    _runtime.set_cache_client(_make_cache_client())

    with patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", new=AsyncMock(return_value=False)), \
         patch("orchestrator.nodes.finalize._strip_hallucinated_continuation", side_effect=lambda x: x):
        result = _run(finalize_node(state))

    assert result.answer == "It will be sunny with a high of 72°F."
    assert result.is_fallback is False
    assert "finalize" in result.node_timings
