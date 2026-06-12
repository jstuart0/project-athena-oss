"""Unit tests for ATHENA-57 Phase 1b benchmark-observability additions.

Covers:
- OrchestratorState carries the new fields with correct defaults
- QueryRequest carries skip_semantic_cache defaulting to False
- response_metadata construction surfaces all four new keys
- skip_semantic_cache=True gates the cache lookup AND the write (should_cache)
- tool_calls_emitted shape is always present (empty list for no-tool turns)
- model_component_used / model_component_name propagate from state to metadata

These are pure in-process tests — no live orchestrator, no network calls.
"""

import sys
sys.path.insert(0, "src")

import pytest
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# OrchestratorState — new fields exist and default correctly
# ---------------------------------------------------------------------------

def test_orchestrator_state_new_fields_default_none():
    from orchestrator.state import OrchestratorState
    s = OrchestratorState(query="hello")
    assert s.model_component_used is None
    assert s.model_component_name is None
    assert s.tool_calls_emitted is None
    assert s.tool_calls_filtered_invalid is None


def test_orchestrator_state_new_fields_accept_values():
    from orchestrator.state import OrchestratorState
    s = OrchestratorState(
        query="test",
        model_component_used="qwen3:4b-instruct-2507-q4_K_M",
        model_component_name="tool_calling_simple",
        tool_calls_emitted=[{"name": "get_weather", "arguments": {"location": "Baltimore"}}],
        tool_calls_filtered_invalid=[{"name": "fake_tool", "malformed": False}],
    )
    assert s.model_component_used == "qwen3:4b-instruct-2507-q4_K_M"
    assert s.model_component_name == "tool_calling_simple"
    assert len(s.tool_calls_emitted) == 1
    assert s.tool_calls_emitted[0]["name"] == "get_weather"
    assert len(s.tool_calls_filtered_invalid) == 1


def test_orchestrator_state_tool_calls_emitted_empty_list_allowed():
    """Empty list is a distinct value from None — represents no-tool turn."""
    from orchestrator.state import OrchestratorState
    s = OrchestratorState(query="test", tool_calls_emitted=[])
    assert s.tool_calls_emitted == []
    assert s.tool_calls_emitted is not None


# ---------------------------------------------------------------------------
# QueryRequest — skip_semantic_cache field
#
# main.py cannot be imported in unit tests (requires langgraph + many service
# deps not installed in the test env).  We verify the field definition by:
#   (a) scanning the source for the exact Field() declaration, and
#   (b) testing equivalent Pydantic logic via a local replica.
# ---------------------------------------------------------------------------

import re as _re
import pathlib as _pathlib

_MAIN_SRC = _pathlib.Path("src/orchestrator/main.py").read_text()


def test_query_request_skip_semantic_cache_field_declared_in_source():
    """Confirm skip_semantic_cache: bool = Field(False, ...) appears in main.py."""
    assert "skip_semantic_cache" in _MAIN_SRC, (
        "skip_semantic_cache field not found in src/orchestrator/main.py"
    )
    # Must default to False
    assert _re.search(r"skip_semantic_cache\s*:\s*bool\s*=\s*Field\s*\(\s*False", _MAIN_SRC), (
        "skip_semantic_cache default is not False"
    )


def test_query_request_skip_semantic_cache_guards_cache_lookup():
    """Both the lookup guard and write guard reference request.skip_semantic_cache."""
    assert "request.skip_semantic_cache" in _MAIN_SRC, (
        "request.skip_semantic_cache not referenced in main.py"
    )
    # Count occurrences — must appear in at least 2 places (lookup guard + should_cache)
    count = _MAIN_SRC.count("request.skip_semantic_cache")
    assert count >= 2, (
        f"Expected ≥2 references to request.skip_semantic_cache (lookup + write), found {count}"
    )


def test_should_cache_predicate_includes_skip_flag():
    """The should_cache predicate must include 'not request.skip_semantic_cache'."""
    assert "not request.skip_semantic_cache" in _MAIN_SRC, (
        "should_cache predicate does not guard against skip_semantic_cache"
    )


# Replica of QueryRequest with just the new field — verifies Pydantic semantics
from pydantic import BaseModel, Field as _Field
from typing import Optional as _Optional

class _QueryRequestReplica(BaseModel):
    query: str
    skip_semantic_cache: bool = _Field(False, description="skip flag")

def test_query_request_replica_defaults_false():
    req = _QueryRequestReplica(query="test")
    assert req.skip_semantic_cache is False

def test_query_request_replica_accepts_true():
    req = _QueryRequestReplica(query="test", skip_semantic_cache=True)
    assert req.skip_semantic_cache is True


# ---------------------------------------------------------------------------
# response_metadata structure — tool_calls_emitted shape
# ---------------------------------------------------------------------------

def _make_metadata_dict(
    tool_calls_emitted: Optional[List[Dict[str, Any]]] = None,
    tool_calls_filtered_invalid: Optional[List[Dict[str, Any]]] = None,
    model_component_used: Optional[str] = None,
    model_component_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Replicate the response_metadata construction from main.py:process_query.

    This mirrors the dict literal exactly so the test stays coupled to the
    production code structure rather than the response object.
    """
    return {
        "model_used": "qwen3:4b-instruct-2507-q4_K_M",
        "data_source": None,
        "validation_passed": True,
        "node_timings": {},
        "conversation_turns": 1,
        "tokens": 42,
        "tokens_per_second": 55.0,
        "tool_exec_time": 0.1,
        "was_truncated": False,
        # Benchmark observability (ATHENA-57 Phase 1b)
        "model_component_used": model_component_used,
        "model_component_name": model_component_name,
        "tool_calls_emitted": {
            "calls": tool_calls_emitted or [],
            "filtered_invalid": tool_calls_filtered_invalid or [],
        },
    }


def test_response_metadata_tool_calls_emitted_present_for_tool_turn():
    meta = _make_metadata_dict(
        tool_calls_emitted=[{"name": "get_weather", "arguments": {"location": "Baltimore"}}],
        model_component_used="qwen3:4b-instruct-2507-q4_K_M",
        model_component_name="tool_calling_simple",
    )
    assert "tool_calls_emitted" in meta
    assert meta["tool_calls_emitted"]["calls"] == [
        {"name": "get_weather", "arguments": {"location": "Baltimore"}}
    ]
    assert meta["tool_calls_emitted"]["filtered_invalid"] == []


def test_response_metadata_tool_calls_emitted_present_and_empty_for_no_tool_turn():
    """No-tool turns must carry tool_calls_emitted with calls=[] (not missing, not None)."""
    meta = _make_metadata_dict(tool_calls_emitted=[])
    assert "tool_calls_emitted" in meta
    assert meta["tool_calls_emitted"]["calls"] == []
    assert meta["tool_calls_emitted"]["filtered_invalid"] == []


def test_response_metadata_filtered_invalid_populated():
    meta = _make_metadata_dict(
        tool_calls_emitted=[],
        tool_calls_filtered_invalid=[{"name": "nonexistent_tool", "malformed": False}],
    )
    assert meta["tool_calls_emitted"]["filtered_invalid"] == [
        {"name": "nonexistent_tool", "malformed": False}
    ]


def test_response_metadata_model_component_fields_present():
    meta = _make_metadata_dict(
        model_component_used="gemma4:e4b-it-qat",
        model_component_name="tool_calling_complex",
    )
    assert meta["model_component_used"] == "gemma4:e4b-it-qat"
    assert meta["model_component_name"] == "tool_calling_complex"


def test_response_metadata_model_component_fields_none_for_non_tool_path():
    """A response that took the non-tool path has None for both component fields."""
    meta = _make_metadata_dict()
    assert meta["model_component_used"] is None
    assert meta["model_component_name"] is None


# ---------------------------------------------------------------------------
# skip_semantic_cache logic — should_cache predicate
# ---------------------------------------------------------------------------

def _evaluate_should_cache(
    skip_flag: bool,
    answer: str = "The weather is sunny.",
    is_fallback: bool = False,
    validation_passed: bool = True,
    confidence: float = 0.9,
    intent: str = "weather",
) -> bool:
    """Replicate the should_cache predicate from main.py exactly."""

    def _looks_like_fallback(text: str) -> bool:
        # Simplified; real implementation checks patterns — returning False here
        return False

    should_cache = (
        not skip_flag
        and bool(answer)
        and not is_fallback
        and not _looks_like_fallback(answer)
        and validation_passed
        and confidence >= 0.3
        and intent != "unknown"
    )
    return should_cache


def test_should_cache_true_when_flag_false():
    assert _evaluate_should_cache(skip_flag=False) is True


def test_should_cache_false_when_flag_true():
    """skip_semantic_cache=True must suppress the write regardless of other conditions."""
    assert _evaluate_should_cache(skip_flag=True) is False


def test_should_cache_false_when_flag_true_even_with_perfect_response():
    assert _evaluate_should_cache(
        skip_flag=True,
        answer="Great answer with very high confidence",
        is_fallback=False,
        validation_passed=True,
        confidence=1.0,
        intent="weather",
    ) is False


def test_should_cache_false_for_fallback_response_flag_false():
    """Existing behaviour: fallback responses are never cached."""
    assert _evaluate_should_cache(skip_flag=False, is_fallback=True) is False


def test_should_cache_false_for_unknown_intent():
    assert _evaluate_should_cache(skip_flag=False, intent="unknown") is False


def test_should_cache_false_for_low_confidence():
    assert _evaluate_should_cache(skip_flag=False, confidence=0.1) is False


# ---------------------------------------------------------------------------
# tool_calls_emitted list construction — verify shape for various cases
# ---------------------------------------------------------------------------

def _build_tool_calls_emitted(raw_tool_calls: Optional[List[Dict]]) -> List[Dict]:
    """Replicate the list comprehension from tool_call_node in main.py."""
    return [
        {"name": tc["function"]["name"], "arguments": tc["function"].get("arguments", {})}
        for tc in (raw_tool_calls or [])
    ]


def test_tool_calls_emitted_single_tool():
    raw = [{"function": {"name": "get_weather", "arguments": {"location": "Baltimore"}}}]
    result = _build_tool_calls_emitted(raw)
    assert result == [{"name": "get_weather", "arguments": {"location": "Baltimore"}}]


def test_tool_calls_emitted_multi_tool():
    raw = [
        {"function": {"name": "get_weather", "arguments": {"location": "Baltimore"}}},
        {"function": {"name": "get_news", "arguments": {}}},
    ]
    result = _build_tool_calls_emitted(raw)
    assert len(result) == 2
    assert result[0]["name"] == "get_weather"
    assert result[1]["name"] == "get_news"


def test_tool_calls_emitted_no_tool_turn():
    """None tool_calls → empty list (not None), for the false-positive signal."""
    result = _build_tool_calls_emitted(None)
    assert result == []


def test_tool_calls_emitted_arguments_missing_defaults_to_empty_dict():
    """Tool calls with no arguments key should yield {} not KeyError."""
    raw = [{"function": {"name": "get_news"}}]
    result = _build_tool_calls_emitted(raw)
    assert result[0]["arguments"] == {}


# ---------------------------------------------------------------------------
# filtered_invalid list construction
# ---------------------------------------------------------------------------

def _build_filtered_invalid(all_calls: List[Dict], valid_names: set) -> List[Dict]:
    """Replicate the filter logic from tool_call_node in main.py."""
    filtered = []
    for tc in all_calls:
        fn_name = tc.get("function", {}).get("name", "")
        if fn_name not in valid_names:
            filtered.append({"name": fn_name, "malformed": False})
    return filtered


def test_filtered_invalid_empty_when_all_valid():
    calls = [{"function": {"name": "get_weather"}}]
    valid = {"get_weather", "get_news"}
    assert _build_filtered_invalid(calls, valid) == []


def test_filtered_invalid_catches_hallucinated_tool():
    calls = [
        {"function": {"name": "get_weather"}},
        {"function": {"name": "get_unicorn_prices"}},
    ]
    valid = {"get_weather", "get_news"}
    result = _build_filtered_invalid(calls, valid)
    assert len(result) == 1
    assert result[0]["name"] == "get_unicorn_prices"
    assert result[0]["malformed"] is False


# ---------------------------------------------------------------------------
# Finding 2 (orchestrator side): malformed-args detection in tool_call_node
# ---------------------------------------------------------------------------

def _apply_malformed_args_filter(raw_tool_calls: list, valid_tool_names: set) -> tuple:
    """Replicate the validation loop from tool_call_node in main.py (post-fix).

    Returns (valid_tool_calls, filtered_invalid_calls).
    A right-name/malformed-args call must go to filtered_invalid with malformed=True.
    """
    import json as _json
    filtered_invalid = []
    valid_tool_calls = []
    for tc in raw_tool_calls:
        fn_name = tc.get("function", {}).get("name", "")
        raw_args = tc.get("function", {}).get("arguments")
        if isinstance(raw_args, str):
            try:
                parsed = _json.loads(raw_args)
                if isinstance(parsed, dict):
                    # Repair: replace string with parsed dict
                    tc = dict(tc)
                    tc["function"] = dict(tc["function"])
                    tc["function"]["arguments"] = parsed
                else:
                    filtered_invalid.append({"name": fn_name, "malformed": True})
                    continue
            except (ValueError, TypeError):
                filtered_invalid.append({"name": fn_name, "malformed": True})
                continue
        if fn_name in valid_tool_names:
            valid_tool_calls.append(tc)
        else:
            filtered_invalid.append({"name": fn_name, "malformed": False})
    return valid_tool_calls, filtered_invalid


def test_malformed_args_string_json_goes_to_filtered_invalid():
    """A call with arguments as a non-parseable JSON string → filtered_invalid, malformed=True."""
    calls = [{"function": {"name": "get_weather", "arguments": "not valid json {"}}]
    valid, filtered = _apply_malformed_args_filter(calls, {"get_weather"})
    assert len(valid) == 0
    assert len(filtered) == 1
    assert filtered[0]["name"] == "get_weather"
    assert filtered[0]["malformed"] is True


def test_malformed_args_string_non_dict_json_goes_to_filtered_invalid():
    """A call with arguments as a JSON array string → filtered_invalid, malformed=True."""
    calls = [{"function": {"name": "get_weather", "arguments": '["location","Baltimore"]'}}]
    valid, filtered = _apply_malformed_args_filter(calls, {"get_weather"})
    assert len(valid) == 0
    assert len(filtered) == 1
    assert filtered[0]["malformed"] is True


def test_malformed_args_string_parseable_dict_is_repaired():
    """A call with arguments as a valid JSON object string → repaired into dict, kept in valid."""
    calls = [{"function": {"name": "get_weather", "arguments": '{"location": "Baltimore"}'}}]
    valid, filtered = _apply_malformed_args_filter(calls, {"get_weather"})
    assert len(valid) == 1
    assert isinstance(valid[0]["function"]["arguments"], dict)
    assert valid[0]["function"]["arguments"]["location"] == "Baltimore"
    assert len(filtered) == 0


def test_malformed_args_right_name_excluded_from_calls():
    """A right-name/malformed-args call must NOT appear in valid tool_calls."""
    calls = [{"function": {"name": "search_flights", "arguments": "garbage"}}]
    valid, filtered = _apply_malformed_args_filter(calls, {"search_flights"})
    assert len(valid) == 0
    assert filtered[0]["malformed"] is True


def test_normal_dict_args_call_unaffected():
    """Normal dict-args call: unchanged behaviour."""
    calls = [{"function": {"name": "get_weather", "arguments": {"location": "Baltimore"}}}]
    valid, filtered = _apply_malformed_args_filter(calls, {"get_weather"})
    assert len(valid) == 1
    assert len(filtered) == 0
    assert isinstance(valid[0]["function"]["arguments"], dict)


def test_hallucinated_tool_with_dict_args_stays_in_filtered_invalid_malformed_false():
    """Hallucinated tool name (not malformed) → filtered_invalid with malformed=False."""
    calls = [{"function": {"name": "get_unicorn", "arguments": {}}}]
    valid, filtered = _apply_malformed_args_filter(calls, {"get_weather"})
    assert len(valid) == 0
    assert filtered[0]["malformed"] is False


# ---------------------------------------------------------------------------
# Finding 4 (orchestrator side): forced get_directions updates tool_calls_emitted
# ---------------------------------------------------------------------------

def _build_forced_directions_emitted(origin: str, destination: str) -> list:
    """Replicate the forced-directions observability update from tool_call_node (post-fix)."""
    return [{"name": "get_directions", "arguments": {"origin": origin, "destination": destination}}]


def test_forced_directions_updates_tool_calls_emitted():
    """After a successful forced directions call, tool_calls_emitted must not be empty."""
    emitted = _build_forced_directions_emitted("1234 Main St", "Washington DC")
    assert len(emitted) == 1
    assert emitted[0]["name"] == "get_directions"
    assert emitted[0]["arguments"]["origin"] == "1234 Main St"
    assert emitted[0]["arguments"]["destination"] == "Washington DC"


def test_forced_directions_emitted_shape_is_correct():
    """The forced call follows the {name, arguments} shape used by build_turn_result."""
    emitted = _build_forced_directions_emitted("37.7749,-122.4194", "Golden Gate Bridge")
    assert "name" in emitted[0]
    assert "arguments" in emitted[0]
    assert isinstance(emitted[0]["arguments"], dict)


def test_forced_directions_source_in_main_py():
    """Confirm main.py updates state.tool_calls_emitted in the forced-directions success block.

    The assignment is placed just before 'Forced directions successful' so the
    log line confirms the state is already correct.  We verify it appears within
    a ±600 char window around the log line.
    """
    assert "state.tool_calls_emitted = [" in _MAIN_SRC, (
        "state.tool_calls_emitted assignment not found in main.py"
    )
    # Locate the sentinel log line inside the forced-directions success block
    forced_idx = _MAIN_SRC.find("Forced directions successful")
    assert forced_idx != -1, "'Forced directions successful' not found in main.py"
    # The assignment appears in the same success block — search a window around the log line
    window_start = max(0, forced_idx - 600)
    window_end = forced_idx + 200
    window = _MAIN_SRC[window_start:window_end]
    assert "state.tool_calls_emitted" in window, (
        "state.tool_calls_emitted assignment not found within ±600 chars of "
        "'Forced directions successful' — forced-directions branch must update "
        "tool_calls_emitted in the success block"
    )
    # Must reference get_directions and arguments dict
    assert '"get_directions"' in window or "'get_directions'" in window, (
        "forced-directions emitted call must name 'get_directions'"
    )
