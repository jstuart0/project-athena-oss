"""Unit tests for orchestrator.nodes.synthesize.synthesize_node.

Covers major behavioral branches:

 1.  skip_synthesis fast path — answer already set, no LLM call.
 2.  General-info + direct_response fast path — _direct_general_info_response hits.
 3.  General-info, no direct response — falls through to LLM synthesis.
 4.  Retrieved-data RAG path — context injected, LLM called, answer set.
 5.  Continuation-conversation path — is_continuation flag + history.
 6.  No RAG data, has conversation history — "conversation context" prompt.
 7.  No RAG data, no history — "CRITICAL: You do NOT have access" prompt.
 8.  Owner-mode → owner_name lookup (get_base_knowledge called).
 9.  Base knowledge context injected when get_knowledge_context_for_user returns content.
10.  Memory context injected into system prompt.
11.  Interruption context injected when interrupted_response is set.
12.  Conversation history formatting — messages appear in history_context.
13.  History summary path — summary used instead of raw history.
14.  SMS content detection — offer_sms set when detect_textable_content returns truthy.
15.  SMS detection exception — non-critical, node continues.
16.  store_conversation_context called for WEATHER intent with session_id.
17.  Outer exception handler — synthesis error caught, state.error set, fallback answer.
18.  timing_tracker.track_sync called after node completes.
19.  EVENTS_AVAILABLE path — emit_llm_generating and emit_llm_complete called.
20.  Component config missing model_name key — KeyError propagates to outer handler.

Patching strategy:
- LLM router: patch ``orchestrator.nodes.synthesize.llm_router.generate`` via the
  proxy object.  Because _LLMRouterProxy.__getattr__ delegates to get_llm_router(),
  it is simpler to install a MagicMock directly on the module-level proxy's
  underlying router via ``orchestrator.nodes._runtime.set_llm_router``.
- Collaborators patched at their source modules (lazy-import safe):
  ``orchestrator.helpers.get_component_config``,
  ``orchestrator.helpers._direct_general_info_response``,
  ``orchestrator.helpers.store_conversation_context``,
  ``shared.assistant_profile.build_core_assistant_prompt``,
  ``shared.base_knowledge_utils.get_knowledge_context_for_user``,
  ``shared.admin_config.get_admin_client``,
  ``sms.content_detector.detect_textable_content``.
"""
from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "src")

from orchestrator.nodes import synthesize_node
from orchestrator.nodes import _runtime
from orchestrator.state import OrchestratorState, IntentCategory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_state(
    *,
    query: str = "what is the weather like?",
    intent: IntentCategory | None = IntentCategory.GENERAL_INFO,
    retrieved_data: dict | None = None,
    session_id: str | None = None,
    mode: str = "guest",
    skip_synthesis: bool = False,
    answer: str = "",
    conversation_history: list | None = None,
    history_summary: str = "",
    memory_context: str = "",
    context: dict | None = None,
    context_ref_info: dict | None = None,
    interruption_context: dict | None = None,
    interface_type: str = "voice",
    timing_tracker=None,
) -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.intent = intent
    state.retrieved_data = retrieved_data or {}
    state.session_id = session_id
    state.mode = mode
    state.skip_synthesis = skip_synthesis
    state.answer = answer
    state.conversation_history = conversation_history or []
    state.history_summary = history_summary
    state.memory_context = memory_context
    state.context = context or {}
    state.context_ref_info = context_ref_info or {}
    state.interruption_context = interruption_context or {}
    state.interface_type = interface_type
    state.timing_tracker = timing_tracker
    return state


def _make_llm_router(response: str = "Synthesized answer.") -> MagicMock:
    """Return a mock LLM router whose generate() coroutine returns a response dict."""
    router = MagicMock()
    router.generate = AsyncMock(return_value={"response": response, "eval_count": 10, "tokens": 10})
    return router


def _patch_component_config(model_name: str = "gpt-test"):
    return patch(
        "orchestrator.nodes.synthesize.get_component_config",
        new=AsyncMock(return_value={"model_name": model_name, "system_prompt": ""}),
    )


def _patch_build_prompt(text: str = "SYSTEM_PROMPT\n"):
    return patch(
        "orchestrator.nodes.synthesize.build_core_assistant_prompt",
        new=AsyncMock(return_value=text),
    )


def _patch_knowledge_context(text: str = ""):
    return patch(
        "orchestrator.nodes.synthesize.get_knowledge_context_for_user",
        new=AsyncMock(return_value=text),
    )


def _patch_admin_client():
    client = MagicMock()
    client.get_base_knowledge = AsyncMock(return_value=[])
    return patch("orchestrator.nodes.synthesize.get_admin_client", return_value=client)


def _patch_direct_response(response: str | None = None):
    return patch(
        "orchestrator.nodes.synthesize._direct_general_info_response",
        return_value=response,
    )


def _patch_store_context():
    return patch(
        "orchestrator.nodes.synthesize.store_conversation_context",
        new=AsyncMock(),
    )


def _patch_sms_detection(should_offer=False, items=None, reason="none"):
    mock_item = MagicMock()
    mock_item.content_type = "directions"
    detected = items if items is not None else ([mock_item] if should_offer else [])
    return patch(
        "orchestrator.nodes.synthesize.detect_textable_content",
        return_value=(should_offer, detected, reason),
    )


# ---------------------------------------------------------------------------
# Test 1: skip_synthesis fast path
# ---------------------------------------------------------------------------

def test_skip_synthesis_returns_early():
    """When skip_synthesis=True and answer is set, no LLM call, node returns immediately."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(skip_synthesis=True, answer="Pre-built answer.")
    result = asyncio.run(synthesize_node(state))

    router.generate.assert_not_called()
    assert result.answer == "Pre-built answer."
    assert "synthesize" in result.node_timings


# ---------------------------------------------------------------------------
# Test 2: General-info + direct_response fast path
# ---------------------------------------------------------------------------

def test_direct_general_info_fast_path():
    """When _direct_general_info_response returns a string, answer is set without LLM."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(
        query="hello there",
        intent=IntentCategory.GENERAL_INFO,
        retrieved_data={},
    )
    with _patch_direct_response("Hello! How can I help?"), _patch_admin_client():
        result = asyncio.run(synthesize_node(state))

    router.generate.assert_not_called()
    assert result.answer == "Hello! How can I help?"
    assert result.skip_synthesis is True
    assert result.llm_tokens == 0


# ---------------------------------------------------------------------------
# Test 3: General-info, no direct response — LLM called
# ---------------------------------------------------------------------------

def test_general_info_no_direct_response_calls_llm():
    """When direct response is None, falls through to LLM synthesis."""
    router = _make_llm_router("Here's your general info answer.")
    _runtime.set_llm_router(router)

    state = _make_state(intent=IntentCategory.GENERAL_INFO, retrieved_data={})
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        result = asyncio.run(synthesize_node(state))

    router.generate.assert_awaited_once()
    assert result.answer == "Here's your general info answer."


# ---------------------------------------------------------------------------
# Test 4: Retrieved-data RAG path
# ---------------------------------------------------------------------------

def test_rag_data_present_injects_context_and_calls_llm():
    """When retrieved_data is set, context is serialized and LLM synthesizes from it."""
    router = _make_llm_router("RAG answer.")
    _runtime.set_llm_router(router)

    state = _make_state(
        intent=IntentCategory.WEATHER,
        retrieved_data={"temperature": "72F"},
        session_id="sess-1",
    )
    with (
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        result = asyncio.run(synthesize_node(state))

    router.generate.assert_awaited_once()
    call_kwargs = router.generate.call_args
    # The prompt should contain the retrieved data as JSON
    assert "72F" in call_kwargs[1].get("prompt", "") or "72F" in str(call_kwargs)
    assert result.answer == "RAG answer."


# ---------------------------------------------------------------------------
# Test 5: Continuation-conversation path
# ---------------------------------------------------------------------------

def test_continuation_path_uses_continuation_prompt():
    """is_continuation=True with history triggers the continuation synthesis prompt."""
    router = _make_llm_router("Continuation answer.")
    _runtime.set_llm_router(router)

    state = _make_state(
        query="yes please",
        intent=IntentCategory.GENERAL_INFO,
        retrieved_data={},
        context_ref_info={"is_continuation": True},
        conversation_history=[{"role": "assistant", "content": "Would you like me to proceed?"}],
    )
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        result = asyncio.run(synthesize_node(state))

    router.generate.assert_awaited_once()
    prompt = router.generate.call_args[1].get("prompt", "")
    assert "continuing a conversation" in prompt
    assert result.answer == "Continuation answer."


# ---------------------------------------------------------------------------
# Test 6: No RAG data, has conversation history
# ---------------------------------------------------------------------------

def test_no_rag_with_history_uses_conversation_context_prompt():
    """Without RAG data but with history, uses 'conversation history' prompt."""
    router = _make_llm_router("Context-aware answer.")
    _runtime.set_llm_router(router)

    state = _make_state(
        intent=IntentCategory.CONTROL,
        retrieved_data={},
        conversation_history=[{"role": "user", "content": "my name is Alex"}],
    )
    with (
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        result = asyncio.run(synthesize_node(state))

    prompt = router.generate.call_args[1].get("prompt", "")
    assert "conversation history and your built-in knowledge" in prompt


# ---------------------------------------------------------------------------
# Test 7: No RAG, no history — "CRITICAL: You do NOT have access" prompt
# ---------------------------------------------------------------------------

def test_no_rag_no_history_uses_no_access_prompt():
    """Without RAG or history, uses the 'CRITICAL: you do NOT have access' prompt."""
    router = _make_llm_router("Honest limitation answer.")
    _runtime.set_llm_router(router)

    state = _make_state(
        intent=IntentCategory.CONTROL,
        retrieved_data={},
        conversation_history=[],
        history_summary="",
    )
    with (
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        result = asyncio.run(synthesize_node(state))

    prompt = router.generate.call_args[1].get("prompt", "")
    assert "do NOT have access" in prompt


# ---------------------------------------------------------------------------
# Test 8: Owner mode → base knowledge lookup
# ---------------------------------------------------------------------------

def test_owner_mode_calls_get_base_knowledge():
    """In owner mode, get_base_knowledge is called to resolve owner_name."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    admin_client = MagicMock()
    admin_client.get_base_knowledge = AsyncMock(return_value=[
        {"category": "owner", "key": "owner_name", "value": "Alice"}
    ])

    state = _make_state(mode="owner", retrieved_data={})
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        patch("orchestrator.nodes.synthesize.get_admin_client", return_value=admin_client),
        _patch_knowledge_context(),
        _patch_build_prompt(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        asyncio.run(synthesize_node(state))

    admin_client.get_base_knowledge.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 9: Base knowledge context injected
# ---------------------------------------------------------------------------

def test_base_knowledge_context_sets_flag():
    """When get_knowledge_context_for_user returns text, state.base_knowledge_populated is True."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(retrieved_data={})
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        _patch_build_prompt(),
        patch(
            "orchestrator.nodes.synthesize.get_knowledge_context_for_user",
            new=AsyncMock(return_value="Some base knowledge."),
        ),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        result = asyncio.run(synthesize_node(state))

    assert result.base_knowledge_populated is True


# ---------------------------------------------------------------------------
# Test 10: Memory context injected
# ---------------------------------------------------------------------------

def test_memory_context_appended_to_system_prompt():
    """When state.memory_context is set, it appears in the full prompt."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(
        memory_context="[Memory: user prefers metric units]",
        retrieved_data={},
    )
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        asyncio.run(synthesize_node(state))

    prompt = router.generate.call_args[1].get("prompt", "")
    assert "Memory: user prefers metric units" in prompt


# ---------------------------------------------------------------------------
# Test 11: Interruption context injected
# ---------------------------------------------------------------------------

def test_interruption_context_injected_when_set():
    """When interruption_context has interrupted_response, interruption text appears in prompt."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(
        interruption_context={
            "interrupted_response": "The weather in Boston is...",
            "previous_query": "what's the weather",
            "audio_position_ms": 1500,
        },
        retrieved_data={},
    )
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        asyncio.run(synthesize_node(state))

    prompt = router.generate.call_args[1].get("prompt", "")
    assert "interrupted" in prompt.lower()
    assert "what's the weather" in prompt


# ---------------------------------------------------------------------------
# Test 12: Conversation history formatted into prompt
# ---------------------------------------------------------------------------

def test_conversation_history_formatted_in_prompt():
    """Conversation history messages appear in the full prompt with Role: prefix."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(
        intent=IntentCategory.CONTROL,
        retrieved_data={},
        conversation_history=[
            {"role": "user", "content": "I live in Boston"},
            {"role": "assistant", "content": "Got it!"},
        ],
    )
    with (
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        asyncio.run(synthesize_node(state))

    prompt = router.generate.call_args[1].get("prompt", "")
    assert "I live in Boston" in prompt
    assert "Got it!" in prompt


# ---------------------------------------------------------------------------
# Test 13: History summary path
# ---------------------------------------------------------------------------

def test_history_summary_used_when_set():
    """When history_summary is set, it appears in the prompt (not raw messages)."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(
        intent=IntentCategory.CONTROL,
        retrieved_data={},
        history_summary="User asked about weather twice.",
        conversation_history=[],
    )
    with (
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        asyncio.run(synthesize_node(state))

    prompt = router.generate.call_args[1].get("prompt", "")
    assert "User asked about weather twice." in prompt


# ---------------------------------------------------------------------------
# Test 14: SMS content detection — offer_sms set
# ---------------------------------------------------------------------------

def test_sms_content_detected_sets_offer_sms():
    """When detect_textable_content returns truthy, state.offer_sms is set."""
    router = _make_llm_router("Here are the directions: 123 Main St")
    _runtime.set_llm_router(router)

    mock_item = MagicMock()
    mock_item.content_type = "directions"

    state = _make_state(interface_type="voice", retrieved_data={})
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        patch("orchestrator.nodes.synthesize.detect_textable_content", return_value=(True, [mock_item], "has directions")),
        patch("orchestrator.nodes.synthesize.extract_sms_content", return_value="123 Main St"),
    ):
        result = asyncio.run(synthesize_node(state))

    assert result.offer_sms is True
    assert result.sms_content_type == "directions"


# ---------------------------------------------------------------------------
# Test 15: SMS detection exception — non-critical
# ---------------------------------------------------------------------------

def test_sms_detection_exception_does_not_fail_node():
    """When detect_textable_content raises, node continues and answer is still set."""
    router = _make_llm_router("Good answer.")
    _runtime.set_llm_router(router)

    state = _make_state(interface_type="voice", retrieved_data={})
    with (
        _patch_direct_response(None),
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        patch("orchestrator.nodes.synthesize.detect_textable_content", side_effect=RuntimeError("SMS boom")),
    ):
        result = asyncio.run(synthesize_node(state))

    assert result.answer == "Good answer."
    assert result.error is None


# ---------------------------------------------------------------------------
# Test 16: store_conversation_context called for WEATHER intent
# ---------------------------------------------------------------------------

def test_store_context_called_for_weather_intent():
    """store_conversation_context is called when session_id and answer are set."""
    router = _make_llm_router("Sunny, 75F.")
    _runtime.set_llm_router(router)

    store_mock = AsyncMock()
    state = _make_state(
        intent=IntentCategory.WEATHER,
        retrieved_data={"temp": "75F"},
        session_id="sess-weather",
    )
    with (
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        patch("orchestrator.nodes.synthesize.store_conversation_context", store_mock),
        _patch_sms_detection(),
    ):
        asyncio.run(synthesize_node(state))

    store_mock.assert_awaited_once()
    call_kwargs = store_mock.call_args[1]
    assert call_kwargs["session_id"] == "sess-weather"
    assert call_kwargs["intent"] == IntentCategory.WEATHER.value


# ---------------------------------------------------------------------------
# Test 17: Outer exception handler — synthesis error caught
# ---------------------------------------------------------------------------

def test_outer_exception_sets_error_and_fallback_answer():
    """When get_component_config raises, outer handler catches it and sets state.error."""
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    state = _make_state(retrieved_data={})
    with (
        _patch_direct_response(None),
        patch("orchestrator.nodes.synthesize.get_component_config", new=AsyncMock(side_effect=RuntimeError("DB down"))),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
    ):
        result = asyncio.run(synthesize_node(state))

    assert result.error is not None
    assert "Synthesis failed" in result.error
    assert "I apologize" in result.answer


# ---------------------------------------------------------------------------
# Test 18: timing_tracker.track_sync called
# ---------------------------------------------------------------------------

def test_timing_tracker_track_sync_called():
    """timing_tracker.track_sync is called with 'graph', 'synthesize', float.

    Note: the skip_synthesis and direct-response fast paths return before the
    final track_sync call. This test uses the full LLM synthesis path to reach it.
    """
    router = _make_llm_router()
    _runtime.set_llm_router(router)

    tracker = MagicMock()
    state = _make_state(
        intent=IntentCategory.CONTROL,
        retrieved_data={},
        timing_tracker=tracker,
    )
    with (
        _patch_component_config(),
        _patch_build_prompt(),
        _patch_knowledge_context(),
        _patch_admin_client(),
        _patch_store_context(),
        _patch_sms_detection(),
    ):
        asyncio.run(synthesize_node(state))

    tracker.track_sync.assert_called_once()
    args = tracker.track_sync.call_args[0]
    assert args[0] == "graph"
    assert args[1] == "synthesize"
    assert isinstance(args[2], float)


# ---------------------------------------------------------------------------
# Test 19: EVENTS_AVAILABLE path — emit functions called
# ---------------------------------------------------------------------------

def test_events_emitted_when_available_and_session_id_set():
    """When EVENTS_AVAILABLE and session_id are set, emit_llm_generating and emit_llm_complete are called."""
    import orchestrator.nodes.synthesize as synth_mod

    original = synth_mod.EVENTS_AVAILABLE
    synth_mod.EVENTS_AVAILABLE = True

    router = _make_llm_router()
    _runtime.set_llm_router(router)

    emit_gen = AsyncMock()
    emit_done = AsyncMock()
    state = _make_state(session_id="sess-events", retrieved_data={})
    try:
        with (
            _patch_direct_response(None),
            _patch_component_config(),
            _patch_build_prompt(),
            _patch_knowledge_context(),
            _patch_admin_client(),
            _patch_store_context(),
            _patch_sms_detection(),
            patch.object(synth_mod, "emit_llm_generating", emit_gen),
            patch.object(synth_mod, "emit_llm_complete", emit_done),
        ):
            asyncio.run(synthesize_node(state))

        emit_gen.assert_awaited_once()
        emit_done.assert_awaited_once()
    finally:
        synth_mod.EVENTS_AVAILABLE = original


# ---------------------------------------------------------------------------
# Test 20: node_timings always set
# ---------------------------------------------------------------------------

def test_node_timings_always_set():
    """synthesize key is present in node_timings regardless of branch taken."""
    _runtime.set_llm_router(_make_llm_router())

    state = _make_state(skip_synthesis=True, answer="quick answer")
    result = asyncio.run(synthesize_node(state))
    assert "synthesize" in result.node_timings
    assert result.node_timings["synthesize"] >= 0.0
