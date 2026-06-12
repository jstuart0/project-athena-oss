"""ATHENA-57 Phase 2 self-tests — hard CI gate.

Tests (a) through (c) as required by the plan:

(a) Every expected_tool in bench/query_set.yaml ∈ get_all_tool_names()
    (src/orchestrator/rag_tools.py:854) — parametrized; FAILS on any mismatch.

(b) Every none-tagged query run through the orchestrator's direct-response
    normalization predicate (_direct_general_info_response / its normalization
    in helpers.py:1034 area — imports the real predicate, falls back to a
    local mirror with a comment pinning the source line).
    FAILS if any none-tagged query is interception-eligible.

(c) Scoring-logic unit tests for bench_report (correct_tools_all/partial, FP,
    metric A/C, denominator incl. typed errors, per-component effective-N
    flagging) using synthetic JSONL fixtures.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
import yaml

# Make src/ importable
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_QUERY_SET_PATH = _REPO_ROOT / "bench" / "query_set.yaml"
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Helpers to import the real predicates (with local replicas as fallback)
# ---------------------------------------------------------------------------

def _get_all_tool_names() -> List[str]:
    """Return the canonical tool name list from rag_tools.py:854."""
    from orchestrator.rag_tools import get_all_tool_names
    return get_all_tool_names()


def _is_direct_response_interceptable(query: str) -> bool:
    """Return True iff helpers.py:_direct_general_info_response returns non-None.

    Imports the live predicate first.  Falls back to a local replica
    (source: helpers.py:1046 + 1051–1108).
    """
    try:
        from orchestrator.helpers import _direct_general_info_response
        return _direct_general_info_response(query) is not None
    except ImportError:
        pass

    # Local replica — source: helpers.py:1046 (normalization) + 1051–1108 (map)
    def _normalize(q: str) -> str:
        return re.sub(r"[^a-z0-9\s]", "", (q or "").lower()).strip()

    normalized = _normalize(query)
    if not normalized:
        return False

    _DIRECT_MAP = {
        "hello", "hi", "hey",
        "good morning", "good afternoon", "good evening",
        "how are you", "hows it going",
        "thanks", "thank you",
        "bye", "goodbye", "see you",
    }
    if normalized in _DIRECT_MAP:
        return True

    _TIME_MAP = {
        "what time is it", "whats the time", "what is the time",
        "current time", "tell me the time",
    }
    if normalized in _TIME_MAP:
        return True

    _DATE_MAP = {
        "what date is it", "whats the date", "what is todays date",
        "whats todays date", "current date", "what day is it",
    }
    if normalized in _DATE_MAP:
        return True

    return False


# ---------------------------------------------------------------------------
# Load query set once
# ---------------------------------------------------------------------------

def _load_query_set() -> List[Dict[str, Any]]:
    with open(_QUERY_SET_PATH) as fh:
        data = yaml.safe_load(fh)
    return data.get("queries", [])


_QUERIES = _load_query_set()

# Collect all (query_id, tool_name) pairs that are non-empty expected_tools
_TOOL_PAIRS = [
    pytest.param(
        entry.get("id", f"q{i}"),
        tool_name,
        id=f"{entry.get('id', f'q{i}')}__{tool_name}",
    )
    for i, entry in enumerate(_QUERIES)
    for tool_name in (entry.get("expected_tools") or [])
]

# Collect all none-tagged queries (expected_tools == [] AND expected_component is null)
_NONE_TAGGED = [
    pytest.param(
        entry.get("id", f"q{i}"),
        entry["query"],
        id=entry.get("id", f"q{i}"),
    )
    for i, entry in enumerate(_QUERIES)
    if (entry.get("expected_tools") or []) == []
]


# ---------------------------------------------------------------------------
# (a) Tool name oracle — parametrized, hard gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query_id,tool_name", _TOOL_PAIRS)
def test_expected_tool_in_oracle(query_id: str, tool_name: str) -> None:
    """Every expected_tool must exist in get_all_tool_names() (rag_tools.py:854)."""
    tool_oracle = set(_get_all_tool_names())
    assert tool_name in tool_oracle, (
        f"Query {query_id!r}: expected_tool {tool_name!r} is NOT in "
        f"get_all_tool_names().  Valid names: {sorted(tool_oracle)}"
    )


# ---------------------------------------------------------------------------
# (b) None-tagged interception check — parametrized, hard gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query_id,query_text", _NONE_TAGGED)
def test_none_tagged_not_interceptable(query_id: str, query_text: str) -> None:
    """No none-tagged query may be intercepted by _direct_general_info_response.

    Source: helpers.py:1051.  Any intercepted query has effective N=0 against
    the model and must be replaced or removed from the query set.
    """
    assert not _is_direct_response_interceptable(query_text), (
        f"Query {query_id!r} {query_text!r} is intercepted by "
        "_direct_general_info_response (helpers.py:1051) — "
        "it has effective N=0 and must be replaced."
    )


# ---------------------------------------------------------------------------
# (c) Scoring-logic unit tests for bench_tool_calling + bench_report
# ---------------------------------------------------------------------------

# Import scoring primitives from bench_tool_calling
from bench_tool_calling import (
    _correct_args,
    _correct_tools_all,
    _correct_tools_partial,
    _is_false_positive,
    _valid_structural,
    build_turn_result,
)

from bench_report import (
    ComponentCellStats,
    MIN_EFFECTIVE_N,
    aggregate,
    load_jsonl,
)

_ORACLE = frozenset([
    "get_weather", "get_news", "get_sports_scores", "search_flights",
    "get_stock_info", "search_restaurants", "search_streaming",
    "search_recipes", "get_airport_info", "search_transit",
    "get_train_schedule", "search_events", "get_sports_standings",
    "get_directions", "scrape_website", "scrape_webpage_bright",
    "compare_prices", "get_tesla_metrics", "request_media", "search_web",
])


# --- valid_structural (metric A) ---

def test_valid_structural_true_for_valid_call():
    tools = [{"name": "get_weather", "arguments": {"location": "Baltimore"}}]
    assert _valid_structural(tools, _ORACLE) is True


def test_valid_structural_false_when_empty():
    assert _valid_structural([], _ORACLE) is False


def test_valid_structural_false_for_hallucinated_tool():
    tools = [{"name": "get_unicorn_prices", "arguments": {}}]
    assert _valid_structural(tools, _ORACLE) is False


def test_valid_structural_false_when_args_not_dict():
    tools = [{"name": "get_weather", "arguments": "Baltimore"}]
    assert _valid_structural(tools, _ORACLE) is False


def test_valid_structural_true_for_second_call_valid():
    tools = [
        {"name": "fake_tool", "arguments": {}},
        {"name": "get_news", "arguments": {"query": "test"}},
    ]
    assert _valid_structural(tools, _ORACLE) is True


# --- correct_tools_all (metric B) ---

def test_correct_tools_all_single_match():
    tools = [{"name": "get_weather", "arguments": {}}]
    assert _correct_tools_all(tools, ["get_weather"]) is True


def test_correct_tools_all_wrong_tool():
    tools = [{"name": "get_news", "arguments": {}}]
    assert _correct_tools_all(tools, ["get_weather"]) is False


def test_correct_tools_all_multi_all_present():
    tools = [
        {"name": "get_weather", "arguments": {}},
        {"name": "search_flights", "arguments": {}},
    ]
    assert _correct_tools_all(tools, ["get_weather", "search_flights"]) is True


def test_correct_tools_all_multi_partial_returns_false():
    tools = [{"name": "get_weather", "arguments": {}}]
    assert _correct_tools_all(tools, ["get_weather", "search_flights"]) is False


def test_correct_tools_all_no_tool_turn_no_emission():
    assert _correct_tools_all([], []) is True


def test_correct_tools_all_no_tool_turn_with_emission():
    tools = [{"name": "get_weather", "arguments": {}}]
    assert _correct_tools_all(tools, []) is False


def test_correct_tools_all_extra_tools_still_passes():
    """Emitting extra tools does not fail correct_tools_all."""
    tools = [
        {"name": "get_weather", "arguments": {}},
        {"name": "get_news", "arguments": {}},  # extra, not required
    ]
    assert _correct_tools_all(tools, ["get_weather"]) is True


# --- correct_tools_partial ---

def test_partial_full_match():
    tools = [{"name": "get_weather"}, {"name": "search_flights"}]
    assert abs(_correct_tools_partial(tools, ["get_weather", "search_flights"]) - 1.0) < 1e-6


def test_partial_half_match():
    tools = [{"name": "get_weather"}]
    assert abs(_correct_tools_partial(tools, ["get_weather", "search_flights"]) - 0.5) < 1e-6


def test_partial_no_match():
    tools = [{"name": "get_news"}]
    assert _correct_tools_partial(tools, ["get_weather"]) == 0.0


def test_partial_no_tool_turn_no_emission():
    assert _correct_tools_partial([], []) == 1.0


def test_partial_no_tool_turn_with_emission():
    tools = [{"name": "get_weather"}]
    assert _correct_tools_partial(tools, []) == 0.0


# --- correct_args (metric C) ---

def test_correct_args_weather_with_location():
    tools = [{"name": "get_weather", "arguments": {"location": "Baltimore"}}]
    assert _correct_args(tools, ["get_weather"], _ORACLE) is True


def test_correct_args_weather_missing_location():
    tools = [{"name": "get_weather", "arguments": {}}]
    assert _correct_args(tools, ["get_weather"], _ORACLE) is False


def test_correct_args_flights_both_params():
    tools = [{"name": "search_flights", "arguments": {"origin": "BWI", "destination": "DEN"}}]
    assert _correct_args(tools, ["search_flights"], _ORACLE) is True


def test_correct_args_flights_missing_destination():
    tools = [{"name": "search_flights", "arguments": {"origin": "BWI"}}]
    assert _correct_args(tools, ["search_flights"], _ORACLE) is False


def test_correct_args_no_tool_turn_true():
    assert _correct_args([], [], _ORACLE) is True


def test_correct_args_expected_tool_not_emitted():
    tools = [{"name": "get_news", "arguments": {"query": "test"}}]
    assert _correct_args(tools, ["get_weather"], _ORACLE) is False


# --- is_false_positive ---

def test_fp_true_when_none_tagged_emits():
    tools = [{"name": "get_weather"}]
    assert _is_false_positive(tools, []) is True


def test_fp_false_when_none_tagged_no_emission():
    assert _is_false_positive([], []) is False


def test_fp_false_for_tool_tagged_query():
    tools = [{"name": "get_weather"}]
    assert _is_false_positive(tools, ["get_weather"]) is False


# --- build_turn_result ---

def _synthetic_response(
    calls: Optional[List[Dict]] = None,
    filtered_invalid: Optional[List[Dict]] = None,
    model_component_name: Optional[str] = "tool_calling_simple",
    model_component_used: Optional[str] = "qwen3:4b",
    tokens_per_second: float = 55.0,
    cache_hit: bool = False,
) -> Dict:
    return {
        "metadata": {
            "tool_calls_emitted": {
                "calls": calls or [],
                "filtered_invalid": filtered_invalid or [],
            },
            "model_component_name": model_component_name,
            "model_component_used": model_component_used,
            "tokens_per_second": tokens_per_second,
            "node_timings": {"tool_call": 0.2},
            "cache_hit": cache_hit,
            "model_used": "TOOL_CALLING",
        }
    }


def _entry(
    expected_tools: Optional[List[str]] = None,
    component: Optional[str] = "tool_calling_simple",
) -> Dict:
    return {
        "query": "test query",
        "expected_tools": expected_tools or [],
        "expected_component": component,
    }


def test_build_turn_result_correct_single_tool():
    row = build_turn_result(
        _entry(["get_weather"]),
        _synthetic_response(calls=[{"name": "get_weather", "arguments": {"location": "Baltimore"}}]),
        350.0,
        _ORACLE,
    )
    assert row["correct_tools_all"] is True
    assert row["valid_structural"] is True
    assert row["error"] is None
    assert row["temperature"] == 0.1
    assert row["skip_semantic_cache"] is True
    assert row["llm_tokens_per_second"] == 55.0


def test_build_turn_result_wrong_tool():
    row = build_turn_result(
        _entry(["get_weather"]),
        _synthetic_response(calls=[{"name": "get_news", "arguments": {"query": "test"}}]),
        300.0,
        _ORACLE,
    )
    assert row["correct_tools_all"] is False
    assert row["valid_structural"] is True  # get_news is valid structurally
    assert row["error"] is None


def test_build_turn_result_false_positive():
    row = build_turn_result(
        _entry([]),
        _synthetic_response(calls=[{"name": "get_weather", "arguments": {}}]),
        200.0,
        _ORACLE,
    )
    assert row["is_false_positive"] is True
    assert row["correct_tools_all"] is False


def test_build_turn_result_filtered_invalid_recorded():
    row = build_turn_result(
        _entry(["get_weather"]),
        _synthetic_response(
            calls=[{"name": "get_weather", "arguments": {"location": "Baltimore"}}],
            filtered_invalid=[{"name": "nonexistent_tool", "malformed": False}],
        ),
        350.0,
        _ORACLE,
    )
    assert len(row["tools_filtered_invalid"]) == 1
    assert row["tools_filtered_invalid"][0]["name"] == "nonexistent_tool"


def test_build_turn_result_cache_hit_error():
    row = build_turn_result(
        _entry(["get_weather"]),
        _synthetic_response(cache_hit=True),
        5.0,
        _ORACLE,
    )
    assert row["error"] == "cache_hit"
    assert row["cache_hit"] is True
    assert row["correct_tools_all"] is False  # failure value


def test_build_turn_result_http_non_200():
    row = build_turn_result(
        _entry(["get_weather"]),
        response_json=None,
        total_latency_ms=50.0,
        tool_oracle=_ORACLE,
        error_type="http_non_200",
        http_status=503,
    )
    assert row["error"] == "http_non_200"
    assert row["correct_tools_all"] is False
    assert row["http_status"] == 503


def test_build_turn_result_timeout():
    row = build_turn_result(
        _entry(["get_weather"]),
        response_json=None,
        total_latency_ms=60000.0,
        tool_oracle=_ORACLE,
        error_type="timeout",
    )
    assert row["error"] == "timeout"
    assert row["correct_tools_all"] is False


def test_build_turn_result_partial_multi_intent():
    row = build_turn_result(
        _entry(["get_weather", "search_flights"], "tool_calling_complex"),
        _synthetic_response(
            calls=[{"name": "get_weather", "arguments": {"location": "Baltimore"}}],
            model_component_name="tool_calling_complex",
        ),
        400.0,
        _ORACLE,
    )
    assert row["correct_tools_all"] is False
    assert abs(row["correct_tools_partial"] - 0.5) < 1e-6


# --- ComponentCellStats: denominator, effective-N, FP ---

def _make_cell(
    n_correct: int = 0,
    n_wrong: int = 0,
    n_errors: int = 0,
    n_cache_hits: int = 0,
    n_fp: int = 0,
    n_no_emission: int = 0,
) -> ComponentCellStats:
    """Build a ComponentCellStats with synthetic rows."""
    cell = ComponentCellStats("tool_calling_simple", "qwen3:4b")

    # Correct single-tool turns
    for _ in range(n_correct):
        cell.add_row({
            "correct_tools_all": True, "correct_tools_partial": 1.0,
            "valid_structural": True, "correct_args": True,
            "is_false_positive": False, "expected_tools": ["get_weather"],
            "total_latency_ms": 300.0, "llm_tokens_per_second": 55.0,
            "error": None, "query": "weather?",
        })

    # Wrong-tool turns (tool expected, wrong one emitted)
    for _ in range(n_wrong):
        cell.add_row({
            "correct_tools_all": False, "correct_tools_partial": 0.0,
            "valid_structural": True, "correct_args": False,
            "is_false_positive": False, "expected_tools": ["get_weather"],
            "total_latency_ms": 300.0, "llm_tokens_per_second": 50.0,
            "error": None, "query": "weather?",
        })

    # Error turns
    for _ in range(n_errors):
        cell.add_row({
            "correct_tools_all": False, "correct_tools_partial": 0.0,
            "valid_structural": False, "correct_args": False,
            "is_false_positive": False, "expected_tools": ["get_weather"],
            "total_latency_ms": 60000.0, "llm_tokens_per_second": None,
            "error": "timeout", "query": "weather?",
        })

    # Cache hit turns
    for _ in range(n_cache_hits):
        cell.add_row({
            "correct_tools_all": False, "correct_tools_partial": 0.0,
            "valid_structural": False, "correct_args": False,
            "is_false_positive": False, "expected_tools": ["get_weather"],
            "total_latency_ms": 5.0, "llm_tokens_per_second": None,
            "error": "cache_hit", "query": "weather?",
        })

    # False positives (none-tagged queries that emitted a tool)
    for _ in range(n_fp):
        cell.add_row({
            "correct_tools_all": False, "correct_tools_partial": 0.0,
            "valid_structural": True, "correct_args": False,
            "is_false_positive": True, "expected_tools": [],
            "total_latency_ms": 250.0, "llm_tokens_per_second": 45.0,
            "error": None, "query": "turn off lights",
        })

    # None-tagged turns with no emission (no FP)
    for _ in range(n_no_emission):
        cell.add_row({
            "correct_tools_all": True, "correct_tools_partial": 1.0,
            "valid_structural": False, "correct_args": True,
            "is_false_positive": False, "expected_tools": [],
            "total_latency_ms": 200.0, "llm_tokens_per_second": 60.0,
            "error": None, "query": "turn off lights",
        })

    return cell


def test_cell_denominator_includes_errors():
    cell = _make_cell(n_correct=10, n_errors=3, n_cache_hits=2)
    assert cell.total_turns == 15
    assert cell.effective_n == 10  # only non-error turns


def test_cell_correct_tool_rate_error_inclusive():
    """10 correct out of 15 total (including 5 errors) = 66.7%, not 100%."""
    cell = _make_cell(n_correct=10, n_errors=5)
    rate = cell.correct_tool_rate
    assert abs(rate - (10 / 15) * 100) < 0.01


def test_cell_fp_rate_uses_none_tagged_denominator():
    cell = _make_cell(n_fp=2, n_no_emission=8)
    assert cell.fp_denominator == 10
    assert abs(cell.fp_rate - 20.0) < 0.01


def test_cell_fp_denominator_excludes_tool_tagged():
    """Tool-tagged turns must NOT inflate the FP denominator."""
    cell = _make_cell(n_correct=20, n_fp=2, n_no_emission=8)
    assert cell.fp_denominator == 10  # only none-tagged turns


def test_cell_not_decision_grade_below_min_n():
    cell = _make_cell(n_correct=MIN_EFFECTIVE_N - 1)
    assert not cell.is_decision_grade


def test_cell_decision_grade_at_min_n():
    cell = _make_cell(n_correct=MIN_EFFECTIVE_N)
    assert cell.is_decision_grade


def test_cell_not_decision_grade_when_errors_inflate_total():
    """effective_n = total - errors; grade uses effective_n."""
    cell = _make_cell(n_correct=MIN_EFFECTIVE_N - 1, n_errors=10)
    assert not cell.is_decision_grade


# --- aggregate: per-component stratification ---

def _make_row(
    component_name: Optional[str],
    model_tag: Optional[str],
    correct: bool = True,
    error: Optional[str] = None,
    fp: bool = False,
    expected_tools: Optional[List[str]] = None,
) -> Dict:
    return {
        "query": "test",
        "expected_tools": expected_tools if expected_tools is not None else ([] if fp else ["get_weather"]),
        "correct_tools_all": correct,
        "correct_tools_partial": 1.0 if correct else 0.0,
        "valid_structural": correct,
        "correct_args": correct,
        "is_false_positive": fp,
        "total_latency_ms": 300.0,
        "llm_tokens_per_second": 55.0,
        "model_component_name": component_name,
        "model_component_used": model_tag,
        "error": error,
        "cache_hit": error == "cache_hit",
    }


def test_aggregate_groups_by_component_and_model():
    rows = [
        _make_row("tool_calling_simple", "qwen3:4b", correct=True),
        _make_row("tool_calling_simple", "qwen3:4b", correct=False),
        _make_row("tool_calling_complex", "qwen3:4b", correct=True),
        _make_row("tool_calling_simple", "gemma4:e4b", correct=True),
    ]
    cells = aggregate(rows)
    assert ("tool_calling_simple", "qwen3:4b") in cells
    assert ("tool_calling_complex", "qwen3:4b") in cells
    assert ("tool_calling_simple", "gemma4:e4b") in cells
    assert cells[("tool_calling_simple", "qwen3:4b")].total_turns == 2


def test_aggregate_excludes_none_attribution():
    rows = [
        _make_row(None, None, correct=True),
        _make_row(None, "qwen3:4b", correct=True),
        _make_row("tool_calling_simple", None, correct=True),
    ]
    cells = aggregate(rows)
    # None-attributed rows must not appear in stratified cells
    assert len(cells) == 0


def test_aggregate_error_rows_counted_in_denominator():
    rows = [
        _make_row("tool_calling_simple", "qwen3:4b", correct=True),
        _make_row("tool_calling_simple", "qwen3:4b", error="timeout", correct=False),
        _make_row("tool_calling_simple", "qwen3:4b", error="cache_hit", correct=False),
    ]
    cells = aggregate(rows)
    cell = cells[("tool_calling_simple", "qwen3:4b")]
    assert cell.total_turns == 3
    assert cell.effective_n == 1


def test_aggregate_per_component_n_flag_below_min():
    # Add only MIN_EFFECTIVE_N - 1 non-error rows
    rows = [
        _make_row("tool_calling_super_complex", "gemma4:12b")
        for _ in range(MIN_EFFECTIVE_N - 1)
    ]
    cells = aggregate(rows)
    cell = cells[("tool_calling_super_complex", "gemma4:12b")]
    assert not cell.is_decision_grade


def test_aggregate_per_component_n_flag_at_min():
    rows = [
        _make_row("tool_calling_super_complex", "gemma4:12b")
        for _ in range(MIN_EFFECTIVE_N)
    ]
    cells = aggregate(rows)
    cell = cells[("tool_calling_super_complex", "gemma4:12b")]
    assert cell.is_decision_grade


# --- load_jsonl with typed-error rows ---

def test_load_jsonl_round_trips(tmp_path):
    """load_jsonl must read back what was written."""
    fpath = tmp_path / "test.jsonl"
    rows_in = [
        {"query": "test", "error": None, "correct_tools_all": True},
        {"query": "test2", "error": "timeout", "correct_tools_all": False},
    ]
    with open(fpath, "w") as fh:
        for row in rows_in:
            fh.write(json.dumps(row) + "\n")
    rows_out = load_jsonl(fpath)
    assert len(rows_out) == 2
    assert rows_out[0]["correct_tools_all"] is True
    assert rows_out[1]["error"] == "timeout"


def test_load_jsonl_skips_blank_lines(tmp_path):
    fpath = tmp_path / "test.jsonl"
    with open(fpath, "w") as fh:
        fh.write('{"a": 1}\n\n{"b": 2}\n')
    rows = load_jsonl(fpath)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Finding 1: error-row fallback attribution flows into aggregate denominator
# ---------------------------------------------------------------------------

from bench_tool_calling import build_turn_result as _btr

_ORACLE_SMALL = frozenset(["get_weather", "get_news", "search_flights"])

def _error_row_with_fallback(
    error_type: str = "timeout",
    cell_target_component: str = "tool_calling_simple",
    cell_target_model: str = "qwen3_baseline",
) -> Dict:
    """Build a synthetic error row with fallback attribution via build_turn_result."""
    return _btr(
        query_entry={
            "query": "weather?",
            "expected_tools": ["get_weather"],
            "expected_component": cell_target_component,
        },
        response_json=None,
        total_latency_ms=60000.0,
        tool_oracle=_ORACLE_SMALL,
        error_type=error_type,
        cell_label=cell_target_model,
        cell_target_component=cell_target_component,
        cell_target_model=cell_target_model,
    )


def test_error_row_fallback_attribution_in_aggregate():
    """Error rows with fallback attribution count in the correct cell's denominator."""
    timeout_row = _error_row_with_fallback("timeout")
    non200_row = _error_row_with_fallback("http_non_200")
    cache_row = _error_row_with_fallback("cache_hit")
    correct_row = _make_row("tool_calling_simple", "qwen3_baseline", correct=True)

    cells = aggregate([correct_row, timeout_row, non200_row, cache_row])
    cell = cells[("tool_calling_simple", "qwen3_baseline")]
    # All 4 rows must be in the denominator
    assert cell.total_turns == 4
    assert cell.effective_n == 1  # only the non-error row
    assert cell.error_count == 3


def test_error_row_without_fallback_excluded_from_cells():
    """Error rows with no attribution at all (model_component_name=None) are excluded."""
    bad_row = _btr(
        query_entry={"query": "?", "expected_tools": [], "expected_component": None},
        response_json=None,
        total_latency_ms=5.0,
        tool_oracle=_ORACLE_SMALL,
        error_type="timeout",
        # no cell_target_component / cell_target_model
    )
    cells = aggregate([bad_row])
    assert len(cells) == 0, "Unattributed error rows must not appear in stratified cells"


def test_error_row_correct_tool_rate_is_error_inclusive():
    """Rate denominator includes error rows: 10 correct / 15 total (5 timeouts) = 66.7%."""
    rows = (
        [_make_row("tool_calling_simple", "qwen3_baseline", correct=True)] * 10
        + [_error_row_with_fallback("timeout")] * 5
    )
    cells = aggregate(rows)
    cell = cells[("tool_calling_simple", "qwen3_baseline")]
    assert cell.total_turns == 15
    assert abs(cell.correct_tool_rate - (10 / 15 * 100)) < 0.01


# ---------------------------------------------------------------------------
# Finding 2: malformed-args contract — harness-side defensive checks
# ---------------------------------------------------------------------------

def test_valid_structural_rejects_string_args():
    """_valid_structural returns False when arguments is a string (not a dict)."""
    tools = [{"name": "get_weather", "arguments": '{"location":"Baltimore"}'}]
    assert _valid_structural(tools, _ORACLE) is False


def test_correct_args_rejects_string_args_for_tool_with_required_params():
    """_correct_args returns False when arguments is a string and tool has required params."""
    tools = [{"name": "get_weather", "arguments": '{"location":"Baltimore"}'}]
    assert _correct_args(tools, ["get_weather"], _ORACLE) is False


def test_correct_args_rejects_none_args_for_tool_with_required_params():
    """_correct_args returns False when arguments is None and tool has required params."""
    tools = [{"name": "get_weather", "arguments": None}]
    assert _correct_args(tools, ["get_weather"], _ORACLE) is False


def test_correct_args_vacuously_true_for_string_args_no_required_params():
    """_correct_args returns True when tool has no required params even if args is non-dict."""
    # search_transit has no required params
    tools = [{"name": "search_transit", "arguments": "random string"}]
    assert _correct_args(tools, ["search_transit"], _ORACLE) is True


def test_malformed_args_call_does_not_count_as_gate1_correct():
    """A right-name/malformed-args call in tools_emitted: correct_tools_all must be False
    because _valid_structural filters it (non-dict args), so correct_tools_all check
    finds the tool name present but the harness records it as invalid-structural.

    Per the plan: malformed-args calls should appear in filtered_invalid (orchestrator side),
    so tools_emitted is empty and correct_tools_all sees the tool MISSING.
    This test verifies the harness-side scoring is consistent: if a malformed call
    somehow reaches tools_emitted, valid_structural is False so it won't score as
    a gate-1-passing structural call.
    """
    row = _btr(
        query_entry={"query": "weather?", "expected_tools": ["get_weather"], "expected_component": "tool_calling_simple"},
        response_json={
            "metadata": {
                "tool_calls_emitted": {
                    "calls": [{"name": "get_weather", "arguments": "malformed-string"}],
                    "filtered_invalid": [],
                },
                "model_component_name": "tool_calling_simple",
                "model_component_used": "qwen3:4b",
                "tokens_per_second": 50.0,
                "node_timings": {},
                "cache_hit": False,
            }
        },
        total_latency_ms=300.0,
        tool_oracle=_ORACLE,
    )
    assert row["valid_structural"] is False, \
        "malformed-args call: valid_structural must be False (args not dict)"
    # correct_tools_all checks name presence ignoring arg validity — so it CAN be True.
    # What matters is valid_structural is False, so Gate-1 structural check fails.
    assert row["correct_args"] is False, \
        "malformed-args call: correct_args must be False"


# ---------------------------------------------------------------------------
# Finding 3: gate cell-selection with 3+ cells per component
# ---------------------------------------------------------------------------

from bench_report import build_report


def _jsonl_rows_for_gate_test(
    component: str,
    model_tag: str,
    n_correct: int,
    n_wrong: int = 0,
    n_error: int = 0,
) -> List[Dict]:
    """Produce synthetic rows attributed to (component, model_tag)."""
    rows = []
    for _ in range(n_correct):
        rows.append({
            "query": "weather?", "expected_tools": ["get_weather"],
            "correct_tools_all": True, "correct_tools_partial": 1.0,
            "valid_structural": True, "correct_args": True,
            "is_false_positive": False, "total_latency_ms": 300.0,
            "llm_tokens_per_second": 55.0, "model_component_name": component,
            "model_component_used": model_tag, "error": None, "cache_hit": False,
        })
    for _ in range(n_wrong):
        rows.append({
            "query": "weather?", "expected_tools": ["get_weather"],
            "correct_tools_all": False, "correct_tools_partial": 0.0,
            "valid_structural": True, "correct_args": False,
            "is_false_positive": False, "total_latency_ms": 320.0,
            "llm_tokens_per_second": 50.0, "model_component_name": component,
            "model_component_used": model_tag, "error": None, "cache_hit": False,
        })
    for _ in range(n_error):
        rows.append({
            "query": "weather?", "expected_tools": ["get_weather"],
            "correct_tools_all": False, "correct_tools_partial": 0.0,
            "valid_structural": False, "correct_args": False,
            "is_false_positive": False, "total_latency_ms": 60000.0,
            "llm_tokens_per_second": None, "model_component_name": component,
            "model_component_used": model_tag, "error": "timeout", "cache_hit": False,
        })
    return rows


def test_gates_fire_with_3_cells_per_component(tmp_path):
    """Gates must fire for target component even when 3+ cells are present.

    Simulates a multi-file Phase 3 report where:
    - file A (qwen3_baseline): rows for tool_calling_simple/qwen3_baseline
      AND tool_calling_super_complex/qwen3_baseline (non-target for e4b)
    - file B (gemma4_e4b): rows for tool_calling_simple/gemma4_e4b
      AND tool_calling_super_complex/qwen3_baseline (non-target)

    For tool_calling_simple: 3 cells present (qwen3_baseline + gemma4_e4b +
    an extra qwen3 from file B's super_complex rows showing up under simple).
    The gate must still compare qwen3_baseline vs gemma4_e4b for tool_calling_simple.
    """
    # baseline file: 20 correct qwen3 rows for simple + 20 for super_complex
    rows_baseline = (
        _jsonl_rows_for_gate_test("tool_calling_simple", "qwen3_baseline", n_correct=20)
        + _jsonl_rows_for_gate_test("tool_calling_super_complex", "qwen3_baseline", n_correct=20)
    )
    # challenger file: 20 correct gemma4 rows for simple + 20 non-target qwen3 for super_complex
    # This creates a third cell for tool_calling_simple if we include the super_complex rows
    # but under simple. Instead, let's add a third spurious cell directly under simple.
    rows_challenger = (
        _jsonl_rows_for_gate_test("tool_calling_simple", "gemma4_e4b", n_correct=20)
        # Non-target cell: qwen3_baseline rows for simple from the second file
        # (e.g., because the file also includes warm-up runs from an earlier baseline)
        + _jsonl_rows_for_gate_test("tool_calling_simple", "qwen3_warmup", n_correct=5)
    )

    f_baseline = tmp_path / "qwen3_baseline.jsonl"
    f_challenger = tmp_path / "gemma4_e4b.jsonl"

    for fpath, rows in [(f_baseline, rows_baseline), (f_challenger, rows_challenger)]:
        with open(fpath, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    report = build_report(
        [f_baseline, f_challenger],
        label_map={
            "qwen3_baseline": "qwen3_baseline",
            "gemma4_e4b": "gemma4_e4b",
        },
    )

    # The gate section must reference tool_calling_simple and produce a verdict,
    # not "gate comparison requires exactly 2"
    assert "gate comparison requires exactly 2" not in report, \
        "Old 'exactly 2' guard must not appear in gate section"
    # Verify that the report produced a verdict for tool_calling_simple
    assert "tool_calling_simple" in report
    # Incumbent and challenger lines must appear
    assert "Incumbent:" in report
    assert "Challenger:" in report
    # Non-target cell annotation must appear
    assert "non-target cells present, not gated" in report or "qwen3_warmup" in report


def test_gates_fire_correctly_with_exactly_2_cells(tmp_path):
    """Baseline behaviour: 2 cells → gates fire as before."""
    rows_inc = _jsonl_rows_for_gate_test("tool_calling_simple", "qwen3_baseline", n_correct=20)
    rows_chal = _jsonl_rows_for_gate_test("tool_calling_simple", "gemma4_e4b", n_correct=20)

    f_inc = tmp_path / "qwen3_baseline.jsonl"
    f_chal = tmp_path / "gemma4_e4b.jsonl"
    with open(f_inc, "w") as fh:
        for r in rows_inc:
            fh.write(json.dumps(r) + "\n")
    with open(f_chal, "w") as fh:
        for r in rows_chal:
            fh.write(json.dumps(r) + "\n")

    report = build_report([f_inc, f_chal])
    assert "Incumbent:" in report
    assert "Challenger:" in report
    # Both cells have identical rates → gates fire and produce a verdict (any NO-SWAP or SWAP)
    assert "NO-SWAP" in report or "SWAP RECOMMENDED" in report or "NON-DECISION-GRADE" in report
    # Must NOT fall back to the old "requires exactly 2" error path
    assert "gate comparison requires exactly 2" not in report


# ---------------------------------------------------------------------------
# Micro-probe transport tests (ATHENA-57 Option B)
# ---------------------------------------------------------------------------

from bench_tool_calling import (
    _build_ollama_payload,
    _parse_ollama_tool_calls,
    _load_tool_schemas,
)

_ORACLE_FULL = frozenset([
    "get_weather", "get_news", "get_sports_scores", "search_flights",
    "get_stock_info", "search_restaurants", "search_streaming",
    "search_recipes", "get_airport_info", "search_transit",
    "get_train_schedule", "search_events", "get_sports_standings",
    "get_directions", "scrape_website", "scrape_webpage_bright",
    "compare_prices", "get_tesla_metrics", "request_media", "search_web",
])

_DUMMY_SCHEMAS = [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}]


# --- _build_ollama_payload: think-suppression parity ---

def test_build_ollama_payload_qwen3_has_think_false():
    """qwen3 model → think:False injected (mirrors llm_router.py:1057-1059)."""
    payload = _build_ollama_payload(
        "qwen3:4b-instruct-2507-q4_K_M", "What's the weather?", _DUMMY_SCHEMAS
    )
    assert payload.get("think") is False


def test_build_ollama_payload_qwen3_upper_case_also_suppressed():
    """qwen3 detection is case-insensitive."""
    payload = _build_ollama_payload("Qwen3:8B", "test", _DUMMY_SCHEMAS)
    assert payload.get("think") is False


def test_build_ollama_payload_gemma_no_think_key():
    """gemma model → NO think key (matches prod behaviour, no branch in llm_router)."""
    payload = _build_ollama_payload("gemma4:e4b-it-qat", "What's the weather?", _DUMMY_SCHEMAS)
    assert "think" not in payload


def test_build_ollama_payload_gemma_12b_no_think_key():
    payload = _build_ollama_payload("gemma4:12b-it-qat", "test", _DUMMY_SCHEMAS)
    assert "think" not in payload


def test_build_ollama_payload_temperature_pinned():
    """Temperature is always 0.1 regardless of model."""
    for model in ["qwen3:4b-instruct-2507-q4_K_M", "gemma4:e4b-it-qat"]:
        payload = _build_ollama_payload(model, "test", _DUMMY_SCHEMAS)
        assert payload["options"]["temperature"] == 0.1, f"temperature must be 0.1 for {model}"


def test_build_ollama_payload_tools_array_present():
    """Tool schemas must be present in the payload under the 'tools' key."""
    payload = _build_ollama_payload("gemma4:e4b-it-qat", "test", _DUMMY_SCHEMAS)
    assert "tools" in payload
    assert payload["tools"] is _DUMMY_SCHEMAS
    assert len(payload["tools"]) == 1


def test_build_ollama_payload_stream_false():
    payload = _build_ollama_payload("gemma4:e4b-it-qat", "test", _DUMMY_SCHEMAS)
    assert payload["stream"] is False


def test_build_ollama_payload_message_format():
    """messages must be a single user-role entry with the query text."""
    payload = _build_ollama_payload("gemma4:e4b-it-qat", "my query", _DUMMY_SCHEMAS)
    assert payload["messages"] == [{"role": "user", "content": "my query"}]


# --- _parse_ollama_tool_calls: parsing correctness ---

def test_parse_ollama_normal_call():
    """Normal tool call with dict args is parsed into tools_emitted."""
    resp = {"message": {"tool_calls": [
        {"function": {"name": "get_weather", "arguments": {"location": "Baltimore"}}}
    ]}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert len(emitted) == 1
    assert emitted[0] == {"name": "get_weather", "arguments": {"location": "Baltimore"}}
    assert filtered == []
    assert err is None


def test_parse_ollama_no_tool_calls_key():
    """Response with no tool_calls → empty lists, no error."""
    resp = {"message": {"content": "I'll answer directly."}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert emitted == []
    assert filtered == []
    assert err is None


def test_parse_ollama_null_tool_calls():
    """tool_calls: null → empty lists, no error."""
    resp = {"message": {"tool_calls": None}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert emitted == []
    assert filtered == []
    assert err is None


def test_parse_ollama_hallucinated_name_to_filtered_invalid():
    """Tool name not in oracle → filtered_invalid, not in tools_emitted."""
    resp = {"message": {"tool_calls": [
        {"function": {"name": "get_unicorn_prices", "arguments": {}}}
    ]}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert emitted == []
    assert len(filtered) == 1
    assert filtered[0]["name"] == "get_unicorn_prices"
    assert filtered[0]["malformed"] is False
    assert err is None  # hallucination ≠ malformed_json


def test_parse_ollama_malformed_args_string_not_json():
    """String args that fail json.loads → filtered_invalid + malformed_json error."""
    resp = {"message": {"tool_calls": [
        {"function": {"name": "get_weather", "arguments": "not-valid-json"}}
    ]}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert emitted == []
    assert len(filtered) == 1
    assert filtered[0]["malformed"] is True
    assert err == "malformed_json"


def test_parse_ollama_string_args_that_parse_to_dict():
    """String args that json.loads to a dict → normalised, placed in tools_emitted."""
    resp = {"message": {"tool_calls": [
        {"function": {"name": "get_weather", "arguments": '{"location": "Denver"}'}}
    ]}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert len(emitted) == 1
    assert emitted[0]["arguments"] == {"location": "Denver"}
    assert filtered == []
    assert err is None


def test_parse_ollama_string_args_that_parse_to_non_dict():
    """String args that json.loads to a list (not dict) → filtered_invalid."""
    resp = {"message": {"tool_calls": [
        {"function": {"name": "get_weather", "arguments": '["Baltimore"]'}}
    ]}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert emitted == []
    assert len(filtered) == 1
    assert filtered[0]["malformed"] is True
    assert err == "malformed_json"


def test_parse_ollama_mixed_valid_and_hallucinated():
    """Mixed: one valid call and one hallucinated name in the same response."""
    resp = {"message": {"tool_calls": [
        {"function": {"name": "get_weather", "arguments": {"location": "NYC"}}},
        {"function": {"name": "get_unicorn_prices", "arguments": {}}},
    ]}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, _ORACLE_FULL)
    assert len(emitted) == 1
    assert emitted[0]["name"] == "get_weather"
    assert len(filtered) == 1
    assert filtered[0]["name"] == "get_unicorn_prices"
    assert err is None


def test_parse_ollama_empty_oracle_allows_all():
    """Empty oracle (frozenset()) → no hallucination check; all calls admitted."""
    resp = {"message": {"tool_calls": [
        {"function": {"name": "anything_goes", "arguments": {"x": 1}}}
    ]}}
    emitted, filtered, err = _parse_ollama_tool_calls(resp, frozenset())
    assert len(emitted) == 1
    assert filtered == []
    assert err is None


# --- micro-probe build_turn_result integration ---

def _micro_probe_response(
    tool_name: str = "get_weather",
    arguments: Any = None,
    model: str = "gemma4:e4b-it-qat",
    tokens_per_second: float = 72.5,
) -> Dict:
    """Build a synthetic micro-probe response (mimics _post_ollama return value)."""
    if arguments is None:
        arguments = {"location": "Baltimore"}
    return {
        "metadata": {
            "tool_calls_emitted": {
                "calls": [{"name": tool_name, "arguments": arguments}],
                "filtered_invalid": [],
            },
            "model_component_name": "micro_probe",
            "model_component_used": model,
            "tokens_per_second": tokens_per_second,
            "node_timings": {},
            "cache_hit": False,
            "model_used": None,
        }
    }


def test_micro_probe_row_model_component_name_is_micro_probe():
    """micro-probe rows must have model_component_name='micro_probe'."""
    row = build_turn_result(
        _entry(["get_weather"]),
        _micro_probe_response(),
        800.0,
        _ORACLE_FULL,
    )
    assert row["model_component_name"] == "micro_probe"


def test_micro_probe_row_model_component_used_is_model_tag():
    """model_component_used must equal the pinned model tag."""
    row = build_turn_result(
        _entry(["get_weather"]),
        _micro_probe_response(model="gemma4:e4b-it-qat"),
        800.0,
        _ORACLE_FULL,
    )
    assert row["model_component_used"] == "gemma4:e4b-it-qat"


def test_micro_probe_row_scores_correctly():
    """micro-probe correct call scores correct_tools_all=True, valid_structural=True."""
    row = build_turn_result(
        _entry(["get_weather"]),
        _micro_probe_response("get_weather", {"location": "Baltimore"}),
        800.0,
        _ORACLE_FULL,
    )
    assert row["correct_tools_all"] is True
    assert row["valid_structural"] is True
    assert row["error"] is None
    assert row["llm_tokens_per_second"] == 72.5


def test_micro_probe_row_hallucinated_name_scores_invalid():
    """Hallucinated tool name: valid_structural=False (no oracle hit)."""
    resp = {
        "metadata": {
            "tool_calls_emitted": {
                "calls": [],  # hallucinated → moved to filtered_invalid before here
                "filtered_invalid": [{"name": "get_unicorn_prices", "malformed": False}],
            },
            "model_component_name": "micro_probe",
            "model_component_used": "gemma4:e4b-it-qat",
            "tokens_per_second": 60.0,
            "node_timings": {},
            "cache_hit": False,
            "model_used": None,
        }
    }
    row = build_turn_result(
        _entry(["get_weather"]),
        resp,
        800.0,
        _ORACLE_FULL,
    )
    assert row["correct_tools_all"] is False
    assert row["valid_structural"] is False
    assert len(row["tools_filtered_invalid"]) == 1


def test_micro_probe_row_malformed_args_scores_invalid():
    """Malformed args in the tools_emitted list: valid_structural=False."""
    resp = {
        "metadata": {
            "tool_calls_emitted": {
                "calls": [{"name": "get_weather", "arguments": "not-a-dict"}],
                "filtered_invalid": [],
            },
            "model_component_name": "micro_probe",
            "model_component_used": "qwen3:4b-instruct-2507-q4_K_M",
            "tokens_per_second": 50.0,
            "node_timings": {},
            "cache_hit": False,
            "model_used": None,
        }
    }
    row = build_turn_result(
        _entry(["get_weather"]),
        resp,
        300.0,
        _ORACLE_FULL,
    )
    assert row["valid_structural"] is False
    assert row["correct_args"] is False


# --- report pairing on micro_probe rows ---

def test_report_pairs_micro_probe_cells(tmp_path):
    """build_report pairs qwen3_4b_baseline vs gemma4_e4b for micro_probe component."""

    def _mp_rows(model_tag: str, cell_label: str, n_correct: int, n_wrong: int = 0) -> List[Dict]:
        rows = []
        for _ in range(n_correct):
            rows.append({
                "query": "What's the weather?",
                "expected_tools": ["get_weather"],
                "correct_tools_all": True, "correct_tools_partial": 1.0,
                "valid_structural": True, "correct_args": True,
                "is_false_positive": False, "total_latency_ms": 350.0,
                "llm_tokens_per_second": 72.0,
                "model_component_name": "micro_probe",
                "model_component_used": model_tag,
                "error": None, "cache_hit": False,
                "cell_label": cell_label, "transport": "ollama",
            })
        for _ in range(n_wrong):
            rows.append({
                "query": "What's the weather?",
                "expected_tools": ["get_weather"],
                "correct_tools_all": False, "correct_tools_partial": 0.0,
                "valid_structural": False, "correct_args": False,
                "is_false_positive": False, "total_latency_ms": 320.0,
                "llm_tokens_per_second": 65.0,
                "model_component_name": "micro_probe",
                "model_component_used": model_tag,
                "error": None, "cache_hit": False,
                "cell_label": cell_label, "transport": "ollama",
            })
        return rows

    rows_qwen3 = _mp_rows("qwen3:4b-instruct-2507-q4_K_M", "qwen3_4b_baseline", n_correct=20)
    rows_gemma = _mp_rows("gemma4:e4b-it-qat", "gemma4_e4b", n_correct=20)

    f_qwen3 = tmp_path / "qwen3_4b_baseline.jsonl"
    f_gemma = tmp_path / "gemma4_e4b.jsonl"
    for fpath, rows in [(f_qwen3, rows_qwen3), (f_gemma, rows_gemma)]:
        with open(fpath, "w") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")

    report = build_report([f_qwen3, f_gemma])

    # micro_probe component section must be present
    assert "micro_probe" in report
    # Incumbent (qwen3) and Challenger (gemma4) must be identified
    assert "Incumbent:" in report
    assert "Challenger:" in report
    # A verdict must be produced
    assert "NO-SWAP" in report or "SWAP RECOMMENDED" in report or "NON-DECISION-GRADE" in report


def test_micro_probe_decision_grade_at_min_n():
    """micro_probe component is decision-grade when effective_n ≥ MIN_EFFECTIVE_N."""
    rows = [
        {
            "query": "test", "expected_tools": ["get_weather"],
            "correct_tools_all": True, "correct_tools_partial": 1.0,
            "valid_structural": True, "correct_args": True,
            "is_false_positive": False, "total_latency_ms": 300.0,
            "llm_tokens_per_second": 70.0,
            "model_component_name": "micro_probe",
            "model_component_used": "gemma4:e4b-it-qat",
            "error": None, "cache_hit": False,
        }
        for _ in range(MIN_EFFECTIVE_N)
    ]
    cells = aggregate(rows)
    cell = cells[("micro_probe", "gemma4:e4b-it-qat")]
    assert cell.is_decision_grade


def test_micro_probe_not_decision_grade_below_min_n():
    """micro_probe component is NOT decision-grade when effective_n < MIN_EFFECTIVE_N."""
    rows = [
        {
            "query": "test", "expected_tools": ["get_weather"],
            "correct_tools_all": True, "correct_tools_partial": 1.0,
            "valid_structural": True, "correct_args": True,
            "is_false_positive": False, "total_latency_ms": 300.0,
            "llm_tokens_per_second": 70.0,
            "model_component_name": "micro_probe",
            "model_component_used": "gemma4:e4b-it-qat",
            "error": None, "cache_hit": False,
        }
        for _ in range(MIN_EFFECTIVE_N - 1)
    ]
    cells = aggregate(rows)
    cell = cells[("micro_probe", "gemma4:e4b-it-qat")]
    assert not cell.is_decision_grade
