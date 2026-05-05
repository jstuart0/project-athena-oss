"""Unit tests for orchestrator.nodes.send_sms.send_sms_node.

Covers the five behavioral branches:
1. No previous response in conversation_history → "I don't have a previous message" prompt.
2. Missing phone number in context → "Could you tell me your phone number?" prompt.
3. Happy path (phone + previous response) → delegates to handle_text_me_that, sets state.answer.
4. get_sms_service raises → handler logs warning, calls handle_text_me_that with sms_service=None.
5. Outer try raises (handle_text_me_that itself raises) → error answer + state.error set.

Note: send_sms_node is async; tests use asyncio.run() directly to avoid
requiring pytest-asyncio (consistent with test_route_info_node.py pattern).
"""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Insert src/ so `orchestrator.*` imports resolve without the package installed.
sys.path.insert(0, "src")

from orchestrator.nodes import send_sms_node
from orchestrator.state import OrchestratorState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    *,
    conversation_history=None,
    context=None,
) -> OrchestratorState:
    """Build a minimal OrchestratorState for send_sms_node testing."""
    return OrchestratorState(
        query="text me that",
        conversation_history=conversation_history or [],
        context=context or {},
    )


def _with_previous_response(text: str = "Here is the weather: sunny and 72°F.") -> list:
    """Return a conversation_history list with one assistant message."""
    return [{"role": "user", "content": "What's the weather?"}, {"role": "assistant", "content": text}]


# ---------------------------------------------------------------------------
# Test 1: No previous response → early return with prompt
# ---------------------------------------------------------------------------

def test_no_previous_response_sets_prompt():
    """With empty conversation_history, node returns 'I don't have a previous message'."""
    state = _make_state()
    result = asyncio.run(send_sms_node(state))
    assert "I don't have a previous message" in result.answer


def test_no_previous_response_records_timing():
    """node_timings['send_sms'] is set even on the early-return path."""
    state = _make_state()
    result = asyncio.run(send_sms_node(state))
    assert "send_sms" in result.node_timings
    assert result.node_timings["send_sms"] >= 0.0


def test_no_previous_response_returns_same_state():
    state = _make_state()
    result = asyncio.run(send_sms_node(state))
    assert result is state


# ---------------------------------------------------------------------------
# Test 2: Missing phone number → prompt for phone number
# ---------------------------------------------------------------------------

def test_missing_phone_number_sets_prompt():
    """With a previous response but no phone in context, node asks for phone number."""
    state = _make_state(conversation_history=_with_previous_response(), context={})
    result = asyncio.run(send_sms_node(state))
    assert "phone number" in result.answer.lower()


def test_missing_phone_number_records_timing():
    state = _make_state(conversation_history=_with_previous_response(), context={})
    result = asyncio.run(send_sms_node(state))
    assert "send_sms" in result.node_timings
    assert result.node_timings["send_sms"] >= 0.0


def test_missing_phone_number_returns_same_state():
    state = _make_state(conversation_history=_with_previous_response(), context={})
    result = asyncio.run(send_sms_node(state))
    assert result is state


# ---------------------------------------------------------------------------
# Test 3: Happy path — phone present, handle_text_me_that succeeds
# ---------------------------------------------------------------------------

def test_happy_path_sets_answer_from_handler():
    """When everything is available, state.answer comes from handle_text_me_that result."""
    state = _make_state(
        conversation_history=_with_previous_response(),
        context={"phone_number": "4105551234"},
    )

    fake_result = {"success": True, "answer": "Done! Sent to 4105551234."}
    fake_sms = MagicMock()

    with (
        patch("orchestrator.nodes.send_sms.get_sms_service", new=AsyncMock(return_value=fake_sms)),
        patch("sms.text_me_that.handle_text_me_that", new=AsyncMock(return_value=fake_result)),
    ):
        result = asyncio.run(send_sms_node(state))

    assert result.answer == "Done! Sent to 4105551234."


def test_happy_path_records_timing():
    state = _make_state(
        conversation_history=_with_previous_response(),
        context={"phone_number": "4105551234"},
    )

    fake_result = {"success": True, "answer": "Sent!"}
    fake_sms = MagicMock()

    with (
        patch("orchestrator.nodes.send_sms.get_sms_service", new=AsyncMock(return_value=fake_sms)),
        patch("sms.text_me_that.handle_text_me_that", new=AsyncMock(return_value=fake_result)),
    ):
        result = asyncio.run(send_sms_node(state))

    assert "send_sms" in result.node_timings
    assert result.node_timings["send_sms"] >= 0.0


def test_happy_path_guest_phone_key_also_works():
    """context['guest_phone'] is accepted as well as context['phone_number']."""
    state = _make_state(
        conversation_history=_with_previous_response(),
        context={"guest_phone": "4105559999"},
    )

    fake_result = {"success": True, "answer": "Done via guest_phone!"}
    fake_sms = MagicMock()

    with (
        patch("orchestrator.nodes.send_sms.get_sms_service", new=AsyncMock(return_value=fake_sms)),
        patch("sms.text_me_that.handle_text_me_that", new=AsyncMock(return_value=fake_result)),
    ):
        result = asyncio.run(send_sms_node(state))

    assert result.answer == "Done via guest_phone!"


# ---------------------------------------------------------------------------
# Test 4: get_sms_service raises → handler continues with sms_service=None
# ---------------------------------------------------------------------------

def test_sms_service_init_failure_continues_with_none():
    """If get_sms_service raises, the node still calls handle_text_me_that with sms_service=None."""
    state = _make_state(
        conversation_history=_with_previous_response(),
        context={"phone_number": "4105551234"},
    )

    fake_result = {"success": True, "answer": "Sent without SMS service."}
    captured_kwargs = {}

    async def fake_handle(**kwargs):
        captured_kwargs.update(kwargs)
        return fake_result

    with (
        patch("orchestrator.nodes.send_sms.get_sms_service", side_effect=RuntimeError("Twilio down")),
        patch("sms.text_me_that.handle_text_me_that", new=fake_handle),
    ):
        result = asyncio.run(send_sms_node(state))

    assert captured_kwargs.get("sms_service") is None
    assert result.answer == "Sent without SMS service."


# ---------------------------------------------------------------------------
# Test 5: Outer try raises (handle_text_me_that itself raises) → error answer
# ---------------------------------------------------------------------------

def test_handler_raises_sets_error_answer():
    """If handle_text_me_that raises, state.answer is the 'having trouble' string."""
    state = _make_state(
        conversation_history=_with_previous_response(),
        context={"phone_number": "4105551234"},
    )

    fake_sms = MagicMock()

    with (
        patch("orchestrator.nodes.send_sms.get_sms_service", new=AsyncMock(return_value=fake_sms)),
        patch("sms.text_me_that.handle_text_me_that", side_effect=ValueError("network failure")),
    ):
        result = asyncio.run(send_sms_node(state))

    assert "having trouble sending the text" in result.answer
    assert result.error is not None
    assert "network failure" in result.error


def test_handler_raises_still_returns_state():
    """Even on outer exception, the same state instance is returned."""
    state = _make_state(
        conversation_history=_with_previous_response(),
        context={"phone_number": "4105551234"},
    )

    fake_sms = MagicMock()

    with (
        patch("orchestrator.nodes.send_sms.get_sms_service", new=AsyncMock(return_value=fake_sms)),
        patch("sms.text_me_that.handle_text_me_that", side_effect=ValueError("oops")),
    ):
        result = asyncio.run(send_sms_node(state))

    assert result is state
