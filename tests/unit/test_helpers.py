"""Unit tests for orchestrator.helpers (Phase 2.1, ATHENA-10).

One happy-path test per helper, proving:
  1. The import path resolves correctly.
  2. The function is callable and produces expected output.

Pattern-1 helpers that need runtime singletons: install fakes via
_runtime.set_X(fake) and call the helper.
Pattern-2 helpers: pass fakes as arguments.
Pattern-1 PURE helpers: call directly with fixed inputs.

Note: async tests use asyncio.run() directly (consistent with test_runtime.py
and test_route_info_node.py — no pytest-asyncio dependency).
"""
import asyncio
import sys
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

# Insert src/ so `orchestrator.*` imports resolve without the package installed.
sys.path.insert(0, "src")

from orchestrator.nodes import _runtime
from orchestrator.helpers import (
    _CONTINUATION_PATTERN,
    _component_system_prompt,
    _direct_general_info_response,
    _fallback_to_web_search,
    _normalized_general_info_query,
    _strip_hallucinated_continuation,
    detect_insufficient_response,
    enhance_query_with_year,
    get_automation_system_mode,
    get_component_config,
    get_feature_config,
    get_model_for_component,
    get_post_synthesis_fallback_config,
    get_rag_service_url,
    get_weather_provider_mode,
    maybe_post_synthesis_fallback,
    store_conversation_context,
    summarize_conversation_history,
)


# ---------------------------------------------------------------------------
# Fixture: clean runtime + component model cache time between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_runtime():
    """Reset runtime before and after every test."""
    _runtime.reset_for_test()
    # Also reset the module-level cache timestamp in helpers so each test
    # starts with an expired cache (forces the first branch to be exercised).
    import orchestrator.helpers as _h
    _h._component_model_cache_time = 0.0
    _h._post_synthesis_fallback_cache.clear()
    yield
    _runtime.reset_for_test()
    _h._component_model_cache_time = 0.0
    _h._post_synthesis_fallback_cache.clear()


# ===========================================================================
# Pattern-1 PURE helpers — no runtime state needed
# ===========================================================================

# --- enhance_query_with_year ---

def test_enhance_query_with_year_adds_year_for_sports():
    result = enhance_query_with_year("NFL standings")
    from datetime import datetime
    assert str(datetime.now().year) in result


def test_enhance_query_with_year_skips_if_year_present():
    result = enhance_query_with_year("NFL standings 2024")
    assert result == "NFL standings 2024"


def test_enhance_query_with_year_plain_query_unchanged():
    result = enhance_query_with_year("what is gravity")
    assert result == "what is gravity"


# --- detect_insufficient_response ---

def test_detect_insufficient_response_returns_none_on_good_response():
    config: Dict[str, Any] = {}
    assert detect_insufficient_response("The answer is 42.", config) is None


def test_detect_insufficient_response_detects_pattern():
    config: Dict[str, Any] = {}
    result = detect_insufficient_response("I couldn't find that information.", config)
    assert result is not None


def test_detect_insufficient_response_short_response():
    config: Dict[str, Any] = {"min_response_length": 50}
    result = detect_insufficient_response("ok", config)
    assert result == "response_too_short"


def test_detect_insufficient_response_empty():
    result = detect_insufficient_response("", {})
    assert result == "empty_response"


# --- _component_system_prompt ---

def test_component_system_prompt_no_thinking_disabled():
    assert _component_system_prompt({"disable_thinking": False}) is None


def test_component_system_prompt_thinking_disabled():
    assert _component_system_prompt({"disable_thinking": True}) == "/no_think"


def test_component_system_prompt_none_config():
    assert _component_system_prompt(None) is None


# --- _normalized_general_info_query ---

def test_normalized_general_info_query_lowercases():
    assert _normalized_general_info_query("Hello World") == "hello world"


def test_normalized_general_info_query_strips_punctuation():
    assert _normalized_general_info_query("What's up?") == "whats up"


def test_normalized_general_info_query_empty():
    assert _normalized_general_info_query("") == ""


def test_normalized_general_info_query_none():
    assert _normalized_general_info_query(None) == ""


# --- _direct_general_info_response ---

def test_direct_general_info_response_hello():
    assert _direct_general_info_response("hello") == "Hello. How can I help?"


def test_direct_general_info_response_thanks():
    assert _direct_general_info_response("thanks") == "You're welcome."


def test_direct_general_info_response_unknown():
    assert _direct_general_info_response("who won the game") is None


# --- _strip_hallucinated_continuation ---

def test_strip_hallucinated_continuation_clean_text():
    text = "The answer is 42."
    assert _strip_hallucinated_continuation(text) == text


def test_strip_hallucinated_continuation_strips_user_turn():
    text = "The answer is 42.\nUser: another question"
    result = _strip_hallucinated_continuation(text)
    assert "User:" not in result
    assert "The answer is 42." in result


def test_strip_hallucinated_continuation_empty():
    assert _strip_hallucinated_continuation("") == ""


def test_strip_hallucinated_continuation_none():
    assert _strip_hallucinated_continuation(None) is None


def test_strip_hallucinated_continuation_strips_repetition():
    # Three paragraphs where second and third repeat the first
    para = "The sky is blue."
    text = f"{para}\n\n{para}\n\n{para}"
    result = _strip_hallucinated_continuation(text)
    # Result should be truncated before the second occurrence
    assert result.count(para) == 1


# ===========================================================================
# Pattern-2 helpers — dependencies injected as arguments (these don't need
# the runtime; the functions take all state via parameters)
# ===========================================================================

# These helpers call the admin API and use caches; we test the fallback path
# (admin API fails → returns default value).

def test_get_automation_system_mode_fallback():
    """When admin API is unreachable, should return 'pattern_matching'."""
    result = asyncio.run(get_automation_system_mode())
    assert result == "pattern_matching"


def test_get_weather_provider_mode_fallback():
    """When admin API is unreachable, should return 'standard'."""
    result = asyncio.run(get_weather_provider_mode())
    assert result == "standard"


def test_get_post_synthesis_fallback_config_fallback():
    """When admin API is unreachable, should return enabled=False dict."""
    result = asyncio.run(get_post_synthesis_fallback_config())
    assert result == {"enabled": False, "config": {}}


def test_get_feature_config_fallback():
    """When admin API is unreachable, should return enabled=False dict."""
    result = asyncio.run(get_feature_config("some_flag"))
    assert result.get("enabled") is False


# ===========================================================================
# Pattern-1 helpers — use _runtime at call time
# ===========================================================================

# --- get_model_for_component ---

def test_get_model_for_component_returns_fallback_when_cache_empty():
    """With empty cache and failed admin client, returns a default model string."""
    result = asyncio.run(get_model_for_component("response_synthesis"))
    # Should be a non-empty string (the FALLBACK_MODELS or _DEFAULT_MODEL value)
    assert isinstance(result, str)
    assert len(result) > 0


def test_get_model_for_component_uses_runtime_cache():
    """Prepopulate runtime cache; helper should return cached model name."""
    cache = _runtime.get_component_model_cache()
    cache["response_synthesis"] = {
        "component_name": "response_synthesis",
        "model_name": "test-model:7b",
        "enabled": True,
    }
    # Also set _component_model_cache_time so cache is NOT expired
    import time
    import orchestrator.helpers as _h
    _h._component_model_cache_time = time.time() + 300  # far future

    result = asyncio.run(get_model_for_component("response_synthesis"))
    assert result == "test-model:7b"


# --- get_component_config ---

def test_get_component_config_returns_dict_with_model_name():
    """With empty cache and no admin API, returns a dict with model_name key."""
    result = asyncio.run(get_component_config("response_synthesis"))
    assert "model_name" in result
    assert "backend_type" in result
    assert "enabled" in result


def test_get_component_config_uses_runtime_cache():
    """Prepopulate runtime cache; helper should return cached config."""
    cache = _runtime.get_component_model_cache()
    cache["fact_check_validation"] = {
        "component_name": "fact_check_validation",
        "model_name": "cached-model:4b",
        "backend_type": "ollama",
        "temperature": 0.5,
        "max_tokens": 512,
        "disable_thinking": True,
        "enabled": True,
    }
    import time
    import orchestrator.helpers as _h
    _h._component_model_cache_time = time.time() + 300

    result = asyncio.run(get_component_config("fact_check_validation"))
    assert result["model_name"] == "cached-model:4b"
    assert result["disable_thinking"] is True


# --- get_rag_service_url ---

def test_get_rag_service_url_known_intent_returns_env_fallback():
    """For 'weather' intent with no admin/registry, should return env fallback URL."""
    result = asyncio.run(get_rag_service_url("weather"))
    # WEATHER_SERVICE_URL defaults to http://localhost:8010
    assert result is not None
    assert "localhost" in result or "8010" in result


def test_get_rag_service_url_unknown_intent_returns_none():
    """For unknown intent with no admin/registry, should return None."""
    result = asyncio.run(get_rag_service_url("unknown_intent_xyz"))
    assert result is None


# --- store_conversation_context ---

def test_store_conversation_context_empty_session_returns_false():
    """Empty session_id should return False immediately."""
    result = asyncio.run(store_conversation_context(
        session_id="",
        intent="weather",
        query="What's the weather?",
        entities={},
        parameters={},
        response="It's sunny.",
    ))
    assert result is False


def test_store_conversation_context_stores_in_memory_context():
    """With no Redis, context lands in the memory_context dict."""
    session_id = "test-session-001"
    memory = _runtime.get_memory_context()
    memory.clear()

    result = asyncio.run(store_conversation_context(
        session_id=session_id,
        intent="weather",
        query="What's the weather?",
        entities={"location": "New York"},
        parameters={},
        response="It's 72°F and sunny.",
    ))
    assert result is True
    assert session_id in memory
    assert memory[session_id]["context"].intent == "weather"


# --- summarize_conversation_history ---

def test_summarize_conversation_history_empty_history():
    """Empty history returns empty string (or previous_summary if provided)."""
    result = asyncio.run(summarize_conversation_history([], "new query"))
    assert result == ""


def test_summarize_conversation_history_empty_with_prev_summary():
    result = asyncio.run(summarize_conversation_history(
        [], "new query", previous_summary="Previous context: user is Alice"
    ))
    assert "Alice" in result


def test_summarize_conversation_history_short_path_no_llm():
    """1-2 messages take the short path — no LLM call needed."""
    history = [{"role": "user", "content": "My name is Bob."}]
    result = asyncio.run(summarize_conversation_history(history, "who am I"))
    assert "Bob" in result
    assert result.startswith("Previous context:")


# --- maybe_post_synthesis_fallback ---

def test_maybe_post_synthesis_fallback_disabled_returns_false():
    """With no admin API, feature config returns enabled=False → False."""
    state = MagicMock()
    state.intent = None
    state.answer = "I couldn't find that information."
    state.query = "test query"
    result = asyncio.run(maybe_post_synthesis_fallback(state))
    assert result is False


# --- _fallback_to_web_search ---

def test_fallback_to_web_search_no_search_engine_exits_gracefully():
    """With no parallel_search_engine installed, function exits without raising."""
    # _runtime.get_parallel_search_engine() returns None (uninitialized)
    state = MagicMock()
    state.entities = {}
    state.query = "test query"
    state.retrieved_data = None
    state.data_source = None

    # Should not raise even without the search engine
    asyncio.run(_fallback_to_web_search(state, "test_rag", "connection refused"))
    # state.retrieved_data set to {} on error path
    assert state.retrieved_data == {} or state.retrieved_data is None
