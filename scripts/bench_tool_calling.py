"""ATHENA-57 Phase 2 — Tool-calling benchmark harness.

Drives the orchestrator's POST /query endpoint over the fixed query set
(bench/query_set.yaml) and records per-turn results as JSONL under
bench/results/.

Usage
-----
    python scripts/bench_tool_calling.py \\
        --host http://localhost:8001 \\
        --cell qwen3_baseline \\
        --n 20 \\
        --query-set bench/query_set.yaml

    python scripts/bench_tool_calling.py --self-test

Environment contract (binding per plan)
----------------------------------------
- Every POST /query includes temperature=0.1 and skip_semantic_cache=true.
- Tool calls are read from metadata.tool_calls_emitted.calls (Phase 1b field).
- Stratification key is metadata.model_component_name.
- Model attribution is metadata.model_component_used.
- tok/s is sourced from metadata.tokens_per_second → JSONL column llm_tokens_per_second.
- Any row where metadata.cache_hit is truthy is written with error="cache_hit",
  counted in the denominator, and flagged loudly.  It is NOT silently excluded.

JSONL schema per turn
---------------------
See plan section "JSONL per-turn schema" for the authoritative definition.
Fields: query, expected_tools, expected_component, tools_emitted,
tools_filtered_invalid, valid_structural, correct_tools_all,
correct_tools_partial, correct_args, is_false_positive, total_latency_ms,
node_timings, model_component_name, model_component_used, cache_hit,
llm_tokens_per_second, temperature, skip_semantic_cache, error, model_used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_QUERY_SET = _REPO_ROOT / "bench" / "query_set.yaml"
_RESULTS_DIR = _REPO_ROOT / "bench" / "results"

# ---------------------------------------------------------------------------
# Tool oracle — resolves at import time from the real TOOL_DEFINITIONS so
# the harness stays coupled to the production tool list.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(_REPO_ROOT / "src"))

def _load_tool_oracle() -> frozenset:
    """Return frozenset of all canonical tool names from TOOL_DEFINITIONS."""
    try:
        from orchestrator.rag_tools import get_all_tool_names
        return frozenset(get_all_tool_names())
    except ImportError:
        # Raised during --self-test when the orchestrator env isn't installed.
        # The self-test's own sys.path manipulation handles this separately.
        return frozenset()


# ---------------------------------------------------------------------------
# Normalization predicate — mirrors helpers.py:1046 + 1051 exactly.
# Used for the no-tool interception check; import the live predicate when
# possible so the harness stays coupled to the production logic.
# ---------------------------------------------------------------------------

def _normalized_general_info_query(query: str) -> str:
    """Mirror of helpers.py:_normalized_general_info_query (source: helpers.py:1046).

    Lowercase with all non-alphanumeric-space characters stripped.
    If the real function is importable it is used instead (see below).
    """
    return re.sub(r"[^a-z0-9\s]", "", (query or "").lower()).strip()


def _is_direct_response_interceptable(query: str) -> bool:
    """Return True if the query would be intercepted by _direct_general_info_response.

    Mirrors helpers.py:1051–1108.  Imports the live predicate when available
    so the check stays coupled to production; falls back to a local replica
    with a comment pinning the source line.
    """
    try:
        # Import the live predicate (helpers.py:1051)
        from orchestrator.helpers import _direct_general_info_response
        return _direct_general_info_response(query) is not None
    except ImportError:
        pass

    # Local replica — source: helpers.py:1046 + 1051–1108
    normalized = _normalized_general_info_query(query)
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
# Query set loader
# ---------------------------------------------------------------------------

def load_query_set(path: Path) -> List[Dict[str, Any]]:
    """Load and validate bench/query_set.yaml.

    Returns a list of query dicts with normalised 'expected_tools' (list)
    and 'expected_component' (str or None).
    """
    with open(path) as fh:
        data = yaml.safe_load(fh)

    queries = data.get("queries", [])
    if not queries:
        raise ValueError(f"No queries found in {path}")

    result = []
    for entry in queries:
        if "query" not in entry:
            raise ValueError(f"Query entry missing 'query' key: {entry!r}")
        # Normalise: expected_tools must be a list; null → []
        tools = entry.get("expected_tools") or []
        if isinstance(tools, str):
            tools = [tools]
        entry = dict(entry)
        entry["expected_tools"] = [str(t) for t in tools]
        entry["expected_component"] = entry.get("expected_component")
        result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _valid_structural(tools_emitted: List[Dict], tool_oracle: frozenset) -> bool:
    """Metric A: ≥1 emitted call names a real tool and args parse as a dict."""
    for tc in tools_emitted:
        if tc.get("name") in tool_oracle and isinstance(tc.get("arguments"), dict):
            return True
    return False


def _correct_tools_all(
    tools_emitted: List[Dict], expected_tools: List[str]
) -> bool:
    """Metric B headline: every expected tool is present in the emitted set."""
    if not expected_tools:
        # no-tool / HA turn: correct_tools_all is True iff nothing was emitted
        return len(tools_emitted) == 0
    emitted_names = {tc.get("name") for tc in tools_emitted}
    return all(t in emitted_names for t in expected_tools)


def _correct_tools_partial(
    tools_emitted: List[Dict], expected_tools: List[str]
) -> float:
    """Partial score: |expected ∩ emitted| / |expected|.

    Returns 1.0 for no-tool turns with zero emissions, 0.0 for no-tool
    turns that over-called.
    """
    if not expected_tools:
        return 1.0 if len(tools_emitted) == 0 else 0.0
    emitted_names = {tc.get("name") for tc in tools_emitted}
    hits = sum(1 for t in expected_tools if t in emitted_names)
    return hits / len(expected_tools)


def _correct_args(
    tools_emitted: List[Dict],
    expected_tools: List[str],
    tool_oracle: frozenset,
) -> bool:
    """Metric C (report-only, non-gating): required params are present and non-empty.

    Checks ONLY required params per tool schema.  Does NOT validate semantic
    correctness of free-text values.  Returns True vacuously when expected_tools
    is empty (no-tool turns).
    """
    if not expected_tools:
        return True

    # Required-param map derived from TOOL_DEFINITIONS schema.required arrays.
    _REQUIRED_PARAMS: Dict[str, List[str]] = {
        "get_weather": ["location"],
        "get_sports_scores": ["team"],
        "get_sports_standings": ["league"],
        "get_airport_info": ["query"],
        "search_flights": ["origin", "destination"],
        "search_events": [],
        "search_streaming": ["query"],
        "get_news": ["query"],
        "get_stock_info": ["symbol"],
        "search_web": ["query"],
        "search_restaurants": ["location"],
        "search_recipes": ["query"],
        "search_transit": [],
        "get_directions": ["origin", "destination"],
        "get_train_schedule": ["destination"],
        "scrape_website": ["url"],
        "scrape_webpage_bright": ["url"],
        "compare_prices": ["query"],
        "get_tesla_metrics": ["query"],
        "request_media": ["query"],
    }

    emitted_by_name: Dict[str, Dict] = {
        tc["name"]: tc.get("arguments", {}) for tc in tools_emitted if "name" in tc
    }
    for tool_name in expected_tools:
        if tool_name not in emitted_by_name:
            return False
        args = emitted_by_name[tool_name]
        required = _REQUIRED_PARAMS.get(tool_name, [])
        for param in required:
            val = args.get(param)
            if val is None or val == "" or val == []:
                return False
    return True


def _is_false_positive(tools_emitted: List[Dict], expected_tools: List[str]) -> bool:
    """True iff this is a none-tagged turn that emitted any tool call."""
    return len(expected_tools) == 0 and len(tools_emitted) > 0


# ---------------------------------------------------------------------------
# Per-turn result builder
# ---------------------------------------------------------------------------

def build_turn_result(
    query_entry: Dict[str, Any],
    response_json: Optional[Dict],
    total_latency_ms: float,
    tool_oracle: frozenset,
    error_type: Optional[str] = None,
    http_status: Optional[int] = None,
) -> Dict[str, Any]:
    """Build a single JSONL row from a raw /query response.

    For error rows the scoring fields are set to failure values and the
    error field is populated.  Error rows count in all rate denominators.
    """
    expected_tools: List[str] = query_entry.get("expected_tools", [])
    expected_component: Optional[str] = query_entry.get("expected_component")
    query_text: str = query_entry["query"]

    # Base row with defaults (failure values)
    row: Dict[str, Any] = {
        "query": query_text,
        "expected_tools": expected_tools,
        "expected_component": expected_component,
        "tools_emitted": [],
        "tools_filtered_invalid": [],
        "valid_structural": False,
        "correct_tools_all": False,
        "correct_tools_partial": 0.0,
        "correct_args": False,
        "is_false_positive": False,
        "total_latency_ms": round(total_latency_ms, 2),
        "node_timings": {},
        "model_component_name": None,
        "model_component_used": None,
        "cache_hit": False,
        "llm_tokens_per_second": None,
        "temperature": 0.1,
        "skip_semantic_cache": True,
        "model_used": None,
        "error": error_type,
    }

    if error_type is not None:
        # Error/timeout row: keep failure scoring values, record http_status
        if http_status is not None:
            row["http_status"] = http_status
        return row

    # --- Parse the successful response ---
    metadata: Dict[str, Any] = response_json.get("metadata", {})

    # Belt-and-suspenders: cache_hit MUST be false.  If it's truthy, record
    # as a typed error row — counted in the denominator, flagged loudly.
    cache_hit = bool(metadata.get("cache_hit", False))
    if cache_hit:
        row["cache_hit"] = True
        row["error"] = "cache_hit"
        row["model_used"] = metadata.get("model_used")
        row["model_component_name"] = metadata.get("model_component_name")
        row["model_component_used"] = metadata.get("model_component_used")
        print(
            f"  [CACHE_HIT WIRING FAILURE] query={query_text!r} "
            "— skip_semantic_cache flag not honoured by server",
            file=sys.stderr,
        )
        return row

    # tool_calls_emitted: always present as {"calls": [...], "filtered_invalid": [...]}
    tce = metadata.get("tool_calls_emitted", {})
    if not isinstance(tce, dict):
        tce = {}
    tools_emitted: List[Dict] = tce.get("calls", []) or []
    tools_filtered_invalid: List[Dict] = tce.get("filtered_invalid", []) or []

    # Stratification / attribution fields
    model_component_name: Optional[str] = metadata.get("model_component_name")
    model_component_used: Optional[str] = metadata.get("model_component_used")
    model_used: Optional[str] = metadata.get("model_used")
    node_timings: Dict = metadata.get("node_timings") or {}
    tokens_per_second: Optional[float] = metadata.get("tokens_per_second")

    # Scoring
    v_structural = _valid_structural(tools_emitted, tool_oracle)
    c_all = _correct_tools_all(tools_emitted, expected_tools)
    c_partial = _correct_tools_partial(tools_emitted, expected_tools)
    c_args = _correct_args(tools_emitted, expected_tools, tool_oracle)
    fp = _is_false_positive(tools_emitted, expected_tools)

    row.update({
        "tools_emitted": tools_emitted,
        "tools_filtered_invalid": tools_filtered_invalid,
        "valid_structural": v_structural,
        "correct_tools_all": c_all,
        "correct_tools_partial": round(c_partial, 4),
        "correct_args": c_args,
        "is_false_positive": fp,
        "node_timings": node_timings,
        "model_component_name": model_component_name,
        "model_component_used": model_component_used,
        "cache_hit": cache_hit,
        "llm_tokens_per_second": tokens_per_second,
        "model_used": model_used,
        "error": None,
    })
    return row


# ---------------------------------------------------------------------------
# HTTP runner (async)
# ---------------------------------------------------------------------------

async def _post_query(
    client,  # httpx.AsyncClient
    host: str,
    query: str,
    timeout_s: float = 60.0,
) -> tuple[Optional[Dict], float, Optional[str], Optional[int]]:
    """POST /query and return (response_json, latency_ms, error_type, http_status).

    error_type is one of: None, "http_non_200", "timeout", "malformed_json".
    """
    url = f"{host.rstrip('/')}/query"
    payload = {
        "query": query,
        "mode": "owner",
        "room": "benchmark",
        "temperature": 0.1,
        "skip_semantic_cache": True,
    }
    t0 = time.monotonic()
    try:
        resp = await client.post(url, json=payload, timeout=timeout_s)
        latency_ms = (time.monotonic() - t0) * 1000.0
        if resp.status_code != 200:
            return None, latency_ms, "http_non_200", resp.status_code
        try:
            data = resp.json()
        except Exception:
            return None, latency_ms, "malformed_json", resp.status_code
        return data, latency_ms, None, resp.status_code
    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000.0
        exc_name = type(exc).__name__.lower()
        if "timeout" in exc_name or "read" in exc_name:
            return None, latency_ms, "timeout", None
        return None, latency_ms, "timeout", None


async def _run_async(
    host: str,
    cell: str,
    query_set: List[Dict[str, Any]],
    n: int,
    results_dir: Path,
    tool_oracle: frozenset,
    timeout_s: float = 60.0,
) -> Path:
    """Run the benchmark and write results JSONL.  Returns the output path."""
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required: pip install httpx")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / f"{timestamp}_{cell}.jsonl"
    results_dir.mkdir(parents=True, exist_ok=True)

    total_queries = len(query_set)
    total_turns = total_queries * n
    print(
        f"[bench] cell={cell}  queries={total_queries}  n={n}  "
        f"total_turns={total_turns}  host={host}"
    )
    print(f"[bench] output → {out_path}")

    rows_written = 0
    cache_hit_count = 0
    error_count = 0

    async with httpx.AsyncClient() as client:
        with open(out_path, "w") as fh:
            for qi, entry in enumerate(query_set, 1):
                q = entry["query"]
                print(
                    f"  [{qi}/{total_queries}] {entry.get('id', '?')}  "
                    f"n={n}  q={q[:60]!r}"
                )
                for run_i in range(1, n + 1):
                    resp_json, latency_ms, err_type, http_status = await _post_query(
                        client, host, q, timeout_s=timeout_s
                    )
                    row = build_turn_result(
                        query_entry=entry,
                        response_json=resp_json,
                        total_latency_ms=latency_ms,
                        tool_oracle=tool_oracle,
                        error_type=err_type,
                        http_status=http_status,
                    )
                    # Annotate run index
                    row["run_index"] = run_i
                    row["query_id"] = entry.get("id", f"q{qi}")

                    fh.write(json.dumps(row, separators=(",", ":")) + "\n")
                    rows_written += 1

                    if row["error"] == "cache_hit":
                        cache_hit_count += 1
                    elif row["error"] is not None:
                        error_count += 1

    print(
        f"[bench] done  rows={rows_written}  errors={error_count}  "
        f"cache_hits_flagged={cache_hit_count}"
    )
    if cache_hit_count > 0:
        print(
            f"[bench] WARNING: {cache_hit_count} cache_hit rows detected — "
            "skip_semantic_cache wiring failure on the server.",
            file=sys.stderr,
        )
    return out_path


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run(
    host: str,
    cell: str,
    query_set_path: Path = _DEFAULT_QUERY_SET,
    n: int = 20,
    results_dir: Path = _RESULTS_DIR,
    timeout_s: float = 60.0,
) -> Path:
    """Run the benchmark synchronously.  Returns the path to the JSONL file."""
    import asyncio
    tool_oracle = _load_tool_oracle()
    qs = load_query_set(query_set_path)
    return asyncio.run(
        _run_async(host, cell, qs, n, results_dir, tool_oracle, timeout_s)
    )


# ---------------------------------------------------------------------------
# Self-test (no host required)
# ---------------------------------------------------------------------------

def _self_test() -> None:
    """Sanity checks that run without a live host.

    Verifies:
    1. Query set loads without error.
    2. No none-tagged query is interception-eligible.
    3. Scoring logic handles known input shapes correctly.
    """
    print("[self-test] loading query set …")
    qs = load_query_set(_DEFAULT_QUERY_SET)
    print(f"[self-test] loaded {len(qs)} queries")

    # Check for interception-eligible none-tagged queries
    failures = []
    for entry in qs:
        if entry["expected_tools"] == [] and entry.get("expected_component") is None:
            if _is_direct_response_interceptable(entry["query"]):
                failures.append(entry["query"])
    if failures:
        print(
            "[self-test] FAIL — the following none-tagged queries are "
            "interception-eligible:\n" + "\n".join(f"  - {q!r}" for q in failures),
            file=sys.stderr,
        )
        sys.exit(1)
    print("[self-test] interception check: PASS")

    # Scoring smoke tests
    oracle = frozenset([
        "get_weather", "get_news", "get_sports_scores", "search_flights",
        "get_stock_info", "search_restaurants", "search_streaming",
        "search_recipes", "get_airport_info", "search_transit",
        "get_train_schedule", "search_events",
    ])

    # Correct single-tool call
    row = build_turn_result(
        {"query": "weather?", "expected_tools": ["get_weather"], "expected_component": "tool_calling_simple"},
        {"metadata": {
            "tool_calls_emitted": {"calls": [{"name": "get_weather", "arguments": {"location": "Baltimore"}}], "filtered_invalid": []},
            "model_component_name": "tool_calling_simple",
            "model_component_used": "qwen3:4b",
            "tokens_per_second": 55.0,
            "node_timings": {},
        }},
        total_latency_ms=350.0,
        tool_oracle=oracle,
    )
    assert row["correct_tools_all"] is True, "expected correct_tools_all=True"
    assert row["valid_structural"] is True, "expected valid_structural=True"
    assert row["error"] is None

    # Wrong tool
    row2 = build_turn_result(
        {"query": "weather?", "expected_tools": ["get_weather"], "expected_component": "tool_calling_simple"},
        {"metadata": {
            "tool_calls_emitted": {"calls": [{"name": "get_news", "arguments": {"query": "weather"}}], "filtered_invalid": []},
            "model_component_name": "tool_calling_simple",
            "model_component_used": "qwen3:4b",
            "tokens_per_second": 50.0,
            "node_timings": {},
        }},
        total_latency_ms=300.0,
        tool_oracle=oracle,
    )
    assert row2["correct_tools_all"] is False, "expected correct_tools_all=False"
    assert row2["valid_structural"] is True, "expected valid_structural=True (valid tool, wrong one)"

    # False positive (none-tagged, tool emitted)
    row3 = build_turn_result(
        {"query": "turn off lights", "expected_tools": [], "expected_component": None},
        {"metadata": {
            "tool_calls_emitted": {"calls": [{"name": "get_weather", "arguments": {"location": "here"}}], "filtered_invalid": []},
            "model_component_name": None,
            "model_component_used": None,
            "tokens_per_second": 40.0,
            "node_timings": {},
        }},
        total_latency_ms=200.0,
        tool_oracle=oracle,
    )
    assert row3["is_false_positive"] is True
    assert row3["correct_tools_all"] is False

    # cache_hit row
    row4 = build_turn_result(
        {"query": "weather?", "expected_tools": ["get_weather"], "expected_component": "tool_calling_simple"},
        {"metadata": {"cache_hit": True, "model_component_name": None}},
        total_latency_ms=5.0,
        tool_oracle=oracle,
    )
    assert row4["error"] == "cache_hit"
    assert row4["cache_hit"] is True
    assert row4["correct_tools_all"] is False

    # http_non_200 error row
    row5 = build_turn_result(
        {"query": "weather?", "expected_tools": ["get_weather"], "expected_component": None},
        response_json=None,
        total_latency_ms=50.0,
        tool_oracle=oracle,
        error_type="http_non_200",
        http_status=503,
    )
    assert row5["error"] == "http_non_200"
    assert row5["correct_tools_all"] is False

    # partial multi-intent
    row6 = build_turn_result(
        {"query": "weather and flights", "expected_tools": ["get_weather", "search_flights"], "expected_component": "tool_calling_complex"},
        {"metadata": {
            "tool_calls_emitted": {"calls": [{"name": "get_weather", "arguments": {"location": "Baltimore"}}], "filtered_invalid": []},
            "model_component_name": "tool_calling_complex",
            "model_component_used": "qwen3:4b",
            "tokens_per_second": 45.0,
            "node_timings": {},
        }},
        total_latency_ms=400.0,
        tool_oracle=oracle,
    )
    assert row6["correct_tools_all"] is False
    assert abs(row6["correct_tools_partial"] - 0.5) < 1e-6

    print("[self-test] scoring logic: PASS")
    print("[self-test] ALL PASS")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ATHENA-57 tool-calling benchmark harness"
    )
    p.add_argument("--host", default="http://localhost:8001",
                   help="Orchestrator base URL (default: http://localhost:8001)")
    p.add_argument("--cell", default="unnamed",
                   help="Cell identifier written into the output filename "
                        "(e.g. qwen3_baseline, gemma4_e4b)")
    p.add_argument("--n", type=int, default=20,
                   help="Runs per query (default: 20; ≥20 for decision-grade)")
    p.add_argument("--query-set", type=Path, default=_DEFAULT_QUERY_SET,
                   help="Path to query_set.yaml (default: bench/query_set.yaml)")
    p.add_argument("--results-dir", type=Path, default=_RESULTS_DIR,
                   help="Output directory for JSONL files")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="Per-request timeout in seconds (default: 60)")
    p.add_argument("--self-test", action="store_true",
                   help="Run in-process smoke tests without a live host and exit")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    if args.self_test:
        _self_test()
        return
    out = run(
        host=args.host,
        cell=args.cell,
        query_set_path=args.query_set,
        n=args.n,
        results_dir=args.results_dir,
        timeout_s=args.timeout,
    )
    print(f"Results written to: {out}")


if __name__ == "__main__":
    main()
