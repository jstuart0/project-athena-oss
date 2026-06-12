"""ATHENA-57 Phase 2 — Tool-calling benchmark harness.

Drives either the orchestrator's POST /query endpoint (transport=query, default)
or a direct Ollama /api/chat micro-probe (transport=ollama, plan Option B
fallback) over the fixed query set (bench/query_set.yaml) and records per-turn
results as JSONL under bench/results/.

Usage
-----
    # Default transport: /query (requires Phase 1b live)
    python scripts/bench_tool_calling.py \\
        --host http://localhost:8001 \\
        --cell qwen3_baseline \\
        --n 20 \\
        --query-set bench/query_set.yaml

    # Micro-probe transport: direct Ollama (plan Option B fallback)
    python scripts/bench_tool_calling.py \\
        --transport ollama \\
        --host http://localhost:11434 \\
        --model qwen3:4b-instruct-2507-q4_K_M \\
        --cell qwen3_4b_baseline \\
        --n 20

    python scripts/bench_tool_calling.py --self-test

    # Print the 20 OpenAI-format tool schemas (for inspection)
    python scripts/bench_tool_calling.py --dump-tools

Transport contract
------------------
  query (default):
    - Every POST /query includes temperature=0.1 and skip_semantic_cache=true.
    - Tool calls from metadata.tool_calls_emitted.calls (Phase 1b field).
    - Stratification key: metadata.model_component_name.
    - Model attribution: metadata.model_component_used.
    - tok/s from metadata.tokens_per_second → llm_tokens_per_second.
    - cache_hit rows written as error="cache_hit", counted in denominator.

  ollama (micro-probe, plan Option B):
    - POST {host}/api/chat with pinned model, all 20 tool schemas, stream=false,
      options.temperature=0.1.
    - qwen3 models: payload["think"]=False (mirrors llm_router.py:1057-1059).
    - gemma/other models: no think key (matches prod behaviour).
    - Tool calls parsed from response.message.tool_calls.
    - Hallucinated names (not in oracle) → equivalent of filtered_invalid.
    - Malformed args: attempt json.loads; if still not a dict, recorded as
      malformed_json error type.
    - model_component_name set to "micro_probe" (constant).
    - model_component_used set to the --model flag value.
    - Latency = wall-clock of the POST (model-level latency, NOT end-to-end
      /query latency). Gate 3 is applied relatively between cells on the same
      transport only.
    - tok/s from response.eval_count / (eval_duration / 1e9) when present.
    - transport="ollama" field added to every row (additive).

JSONL schema per turn
---------------------
See plan section "JSONL per-turn schema" for the authoritative definition.
Fields: query, expected_tools, expected_component, tools_emitted,
tools_filtered_invalid, valid_structural, correct_tools_all,
correct_tools_partial, correct_args, is_false_positive, total_latency_ms,
node_timings, model_component_name, model_component_used, cache_hit,
llm_tokens_per_second, temperature, skip_semantic_cache, error, model_used,
transport (str: "query" or "ollama"),
run_index (int, 1-based run number within the query),
query_id (str, the id field from query_set.yaml),
cell_label (str, the --cell argument used for this run).

Attribution-fallback rule (error rows)
---------------------------------------
For http_non_200, timeout, malformed_json, and cache_hit rows the live
response carries no model_component_name / model_component_used (the
orchestrator never reached the tool-call node).  To keep these rows in the
correct per-component cell for denominator counting, build_turn_result accepts
optional cell_target_component and cell_target_model keyword arguments and
uses them as fallback attribution when the response fields are absent.  The
fallback is populated from:
  - cell_target_component: query_entry["expected_component"] (advisory; the
    observed component from a successful response may differ)
  - cell_target_model: the --cell argument (the harness-operator-supplied
    identity of the model under test, e.g. "qwen3_baseline" or "gemma4_e4b")
bench_report.py documents this rule in its header comment.

For ollama transport error rows, cell_target_component is always "micro_probe"
and cell_target_model is the --model flag value.
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


def _load_tool_schemas() -> List[Dict[str, Any]]:
    """Return the 20 OpenAI-format tool schemas from TOOL_DEFINITIONS.

    Used by the ollama micro-probe transport to send the real production
    tool list directly to Ollama /api/chat.

    Import strategy: lazy-import get_rag_tools from rag_tools.py (which itself
    imports env-var-keyed service URLs, but does NOT open network connections at
    import time).  If the import fails (e.g. a transitive dep is missing), raise
    ImportError with a clear message so the operator can set PYTHONPATH
    correctly rather than receiving a confusing AttributeError.

    Callers: _post_ollama (micro-probe), --dump-tools CLI flag.
    """
    try:
        from orchestrator.rag_tools import get_rag_tools
        schemas = get_rag_tools()  # no args → all tools, no guest filter
        if not schemas:
            raise RuntimeError(
                "_load_tool_schemas: get_rag_tools() returned an empty list — "
                "TOOL_DEFINITIONS may be empty or rag_tools.py is misconfigured."
            )
        return schemas
    except ImportError as exc:
        raise ImportError(
            f"Cannot import get_rag_tools from orchestrator.rag_tools: {exc}\n"
            "Set PYTHONPATH to include the repo src/ directory before running:\n"
            "  PYTHONPATH=/path/to/os-project-athena/src "
            "python scripts/bench_tool_calling.py --transport ollama ..."
        ) from exc


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

    emitted_by_name: Dict[str, Any] = {
        tc["name"]: tc.get("arguments") for tc in tools_emitted if "name" in tc
    }
    for tool_name in expected_tools:
        if tool_name not in emitted_by_name:
            return False
        args = emitted_by_name[tool_name]
        # Defensive: malformed-args calls may arrive with a non-dict value
        # (e.g. a raw JSON string, or None).  Treat non-dict as missing all
        # required params so malformed calls never incorrectly pass metric C.
        if not isinstance(args, dict):
            if _REQUIRED_PARAMS.get(tool_name):
                return False
            args = {}
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
    cell_label: Optional[str] = None,
    cell_target_component: Optional[str] = None,
    cell_target_model: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a single JSONL row from a raw /query response.

    For error rows the scoring fields are set to failure values and the
    error field is populated.  Error rows count in all rate denominators.

    cell_label, cell_target_component, cell_target_model are used as fallback
    attribution when the response does not carry model_component_name /
    model_component_used (http_non_200, timeout, malformed_json, cache_hit rows).
    See the module docstring "Attribution-fallback rule" for the full contract.
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
        "cell_label": cell_label,
        "error": error_type,
    }

    if error_type is not None:
        # Error/timeout row: keep failure scoring values, record http_status.
        # Apply fallback attribution so the row lands in the correct per-component
        # denominator in bench_report.py.  cell_target_component comes from
        # query_entry["expected_component"] (advisory); cell_target_model is the
        # --cell label passed in by _run_async — it's the operator-declared model
        # identity for this run (e.g. "qwen3_baseline", "gemma4_e4b").
        if cell_target_component is not None:
            row["model_component_name"] = cell_target_component
        if cell_target_model is not None:
            row["model_component_used"] = cell_target_model
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
# HTTP runners (async)
# ---------------------------------------------------------------------------

def _build_ollama_payload(
    model: str,
    query: str,
    tool_schemas: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Build the Ollama /api/chat payload for the micro-probe transport.

    Replicates llm_router.py:1042-1059 exactly:
    - stream: false
    - options.temperature: 0.1
    - think: False injected for qwen3 models only (llm_router.py:1057-1059)
    - No think key for gemma/other models (matches prod behaviour)
    """
    payload: Dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": query}],
        "tools": tool_schemas,
        "stream": False,
        "options": {"temperature": 0.1},
    }
    # Mirror llm_router.py:1057-1059 — qwen3 think-suppression
    if "qwen3" in model.lower():
        payload["think"] = False
    return payload


def _parse_ollama_tool_calls(
    response_json: Dict[str, Any],
    tool_oracle: frozenset,
) -> tuple[List[Dict], List[Dict], Optional[str]]:
    """Parse tool_calls from an Ollama /api/chat response.

    Returns (tools_emitted, tools_filtered_invalid, error_type).

    Ollama response shape:
      {"message": {"tool_calls": [{"function": {"name": str, "arguments": dict|str}}]}}

    Handling rules (mirror /query path semantics):
    - arguments not a dict: attempt json.loads; if still not a dict → error_type="malformed_json"
      and the call is moved to filtered_invalid.
    - name not in oracle (hallucinated): moved to filtered_invalid, not in tools_emitted.
    - A response with no tool_calls key (or null) → empty tools_emitted (no-tool turn).
    """
    tools_emitted: List[Dict] = []
    tools_filtered_invalid: List[Dict] = []
    error_type: Optional[str] = None

    message = response_json.get("message") or {}
    raw_calls = message.get("tool_calls") or []

    for tc in raw_calls:
        fn = tc.get("function") or {}
        name = fn.get("name", "")
        args = fn.get("arguments", {})

        # Attempt to normalise args to a dict if it came back as a string
        if not isinstance(args, dict):
            if isinstance(args, str):
                try:
                    parsed = json.loads(args)
                    if isinstance(parsed, dict):
                        args = parsed
                    else:
                        # json.loads succeeded but not a dict (e.g. a list)
                        tools_filtered_invalid.append(
                            {"name": name, "malformed": True, "raw_arguments": str(fn.get("arguments", ""))}
                        )
                        error_type = "malformed_json"
                        continue
                except (json.JSONDecodeError, TypeError):
                    tools_filtered_invalid.append(
                        {"name": name, "malformed": True, "raw_arguments": str(fn.get("arguments", ""))}
                    )
                    error_type = "malformed_json"
                    continue
            else:
                tools_filtered_invalid.append(
                    {"name": name, "malformed": True, "raw_arguments": repr(fn.get("arguments"))}
                )
                error_type = "malformed_json"
                continue

        # Hallucinated name — not in oracle
        if tool_oracle and name not in tool_oracle:
            tools_filtered_invalid.append({"name": name, "malformed": False})
            continue

        tools_emitted.append({"name": name, "arguments": args})

    return tools_emitted, tools_filtered_invalid, error_type


async def _post_ollama(
    client,  # httpx.AsyncClient
    host: str,
    model: str,
    query: str,
    tool_schemas: List[Dict[str, Any]],
    tool_oracle: frozenset,
    timeout_s: float = 120.0,
) -> tuple[Optional[Dict], float, Optional[str], Optional[int]]:
    """POST to Ollama /api/chat (micro-probe transport).

    Returns (synthetic_response_json, latency_ms, error_type, http_status).

    The returned synthetic_response_json mimics the shape that build_turn_result
    expects for the /query path, so the same scoring/JSONL path is reused:
      {
        "metadata": {
          "tool_calls_emitted": {"calls": [...], "filtered_invalid": [...]},
          "model_component_name": "micro_probe",
          "model_component_used": <model>,
          "tokens_per_second": <float|None>,
          "node_timings": {},
          "cache_hit": False,
        }
      }

    Latency is wall-clock of the POST only (model-level, not end-to-end /query).
    Gate 3 is applied relatively between cells on the same transport.
    """
    url = f"{host.rstrip('/')}/api/chat"
    payload = _build_ollama_payload(model, query, tool_schemas)
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

        # tok/s from eval_count / (eval_duration / 1e9)
        tokens_per_second: Optional[float] = None
        eval_count = data.get("eval_count")
        eval_duration = data.get("eval_duration")  # nanoseconds
        if eval_count and eval_duration and eval_duration > 0:
            tokens_per_second = eval_count / (eval_duration / 1e9)

        # Parse tool calls — returns (emitted, filtered_invalid, error_type)
        tools_emitted, tools_filtered_invalid, parse_error = _parse_ollama_tool_calls(
            data, tool_oracle
        )

        synthetic = {
            "metadata": {
                "tool_calls_emitted": {
                    "calls": tools_emitted,
                    "filtered_invalid": tools_filtered_invalid,
                },
                "model_component_name": "micro_probe",
                "model_component_used": model,
                "tokens_per_second": tokens_per_second,
                "node_timings": {},
                "cache_hit": False,
                "model_used": None,
            }
        }
        # malformed_json is an error_type but a partial response — still write the row
        return synthetic, latency_ms, parse_error, resp.status_code

    except Exception as exc:
        latency_ms = (time.monotonic() - t0) * 1000.0
        exc_name = type(exc).__name__.lower()
        if "timeout" in exc_name or "read" in exc_name:
            return None, latency_ms, "timeout", None
        return None, latency_ms, "timeout", None


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
    transport: str = "query",
    model: Optional[str] = None,
) -> Path:
    """Run the benchmark and write results JSONL.  Returns the output path.

    transport: "query" (default, POST /query) or "ollama" (micro-probe,
    POST /api/chat directly with pinned model + tool schemas).
    model: required for transport="ollama"; the pinned model tag.
    """
    try:
        import httpx
    except ImportError:
        raise ImportError("httpx is required: pip install httpx")

    # For ollama transport, load tool schemas once before the run loop.
    tool_schemas: Optional[List[Dict[str, Any]]] = None
    if transport == "ollama":
        if not model:
            raise ValueError("--model is required when --transport ollama is used")
        tool_schemas = _load_tool_schemas()
        print(
            f"[bench] transport=ollama  model={model}  tool_schemas={len(tool_schemas)}"
        )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = results_dir / f"{timestamp}_{cell}.jsonl"
    results_dir.mkdir(parents=True, exist_ok=True)

    total_queries = len(query_set)
    total_turns = total_queries * n
    print(
        f"[bench] cell={cell}  queries={total_queries}  n={n}  "
        f"total_turns={total_turns}  host={host}  transport={transport}"
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
                    if transport == "ollama":
                        resp_json, latency_ms, err_type, http_status = await _post_ollama(
                            client,
                            host,
                            model,  # type: ignore[arg-type]  # validated above
                            q,
                            tool_schemas,  # type: ignore[arg-type]
                            tool_oracle,
                            timeout_s=timeout_s,
                        )
                        # For ollama, cell_target_component is always "micro_probe"
                        target_component = "micro_probe"
                        target_model = model
                    else:
                        resp_json, latency_ms, err_type, http_status = await _post_query(
                            client, host, q, timeout_s=timeout_s
                        )
                        target_component = entry.get("expected_component")
                        target_model = cell

                    row = build_turn_result(
                        query_entry=entry,
                        response_json=resp_json,
                        total_latency_ms=latency_ms,
                        tool_oracle=tool_oracle,
                        error_type=err_type,
                        http_status=http_status,
                        cell_label=cell,
                        cell_target_component=target_component,
                        cell_target_model=target_model,
                    )
                    # Annotate run index / query id / transport
                    row["run_index"] = run_i
                    row["query_id"] = entry.get("id", f"q{qi}")
                    row["transport"] = transport

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
    transport: str = "query",
    model: Optional[str] = None,
) -> Path:
    """Run the benchmark synchronously.  Returns the path to the JSONL file.

    transport: "query" (default) or "ollama" (micro-probe, plan Option B).
    model: required when transport="ollama"; the pinned Ollama model tag.
    """
    import asyncio
    tool_oracle = _load_tool_oracle()
    qs = load_query_set(query_set_path)
    return asyncio.run(
        _run_async(
            host, cell, qs, n, results_dir, tool_oracle, timeout_s,
            transport=transport, model=model,
        )
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

    # malformed-args in tools_emitted: non-dict args must not score as correct
    row7 = build_turn_result(
        {"query": "weather?", "expected_tools": ["get_weather"], "expected_component": "tool_calling_simple"},
        {"metadata": {
            # The orchestrator would have moved a malformed-args call to filtered_invalid,
            # so tools_emitted.calls would be empty.  Simulate a call that slipped through.
            "tool_calls_emitted": {
                "calls": [{"name": "get_weather", "arguments": "not-a-dict"}],
                "filtered_invalid": [],
            },
            "model_component_name": "tool_calling_simple",
            "model_component_used": "qwen3:4b",
            "tokens_per_second": 50.0,
            "node_timings": {},
        }},
        total_latency_ms=300.0,
        tool_oracle=oracle,
    )
    # valid_structural: args is not dict → False (existing behaviour of _valid_structural)
    assert row7["valid_structural"] is False, "malformed args: valid_structural must be False"
    # correct_args: non-dict args with required params → False
    assert row7["correct_args"] is False, "malformed args: correct_args must be False"

    # fallback attribution for error rows
    row8 = build_turn_result(
        {"query": "weather?", "expected_tools": ["get_weather"], "expected_component": "tool_calling_simple"},
        response_json=None,
        total_latency_ms=60000.0,
        tool_oracle=oracle,
        error_type="timeout",
        cell_label="qwen3_baseline",
        cell_target_component="tool_calling_simple",
        cell_target_model="qwen3_baseline",
    )
    assert row8["error"] == "timeout"
    assert row8["model_component_name"] == "tool_calling_simple", \
        "error row must carry fallback component name"
    assert row8["model_component_used"] == "qwen3_baseline", \
        "error row must carry fallback model tag"
    assert row8["cell_label"] == "qwen3_baseline"

    print("[self-test] scoring logic: PASS")

    # --- Micro-probe transport ---
    # _build_ollama_payload: qwen3 → think:False; gemma → no think key
    payload_qwen3 = _build_ollama_payload(
        "qwen3:4b-instruct-2507-q4_K_M",
        "What's the weather?",
        [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
    )
    assert payload_qwen3["think"] is False, "qwen3 must have think:False"
    assert payload_qwen3["options"]["temperature"] == 0.1
    assert len(payload_qwen3["tools"]) == 1
    assert payload_qwen3["stream"] is False

    payload_gemma = _build_ollama_payload(
        "gemma4:e4b-it-qat",
        "What's the weather?",
        [{"type": "function", "function": {"name": "get_weather", "parameters": {}}}],
    )
    assert "think" not in payload_gemma, "gemma must NOT have think key"
    assert payload_gemma["options"]["temperature"] == 0.1

    # _parse_ollama_tool_calls: normal call
    normal_resp = {
        "message": {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": {"location": "Baltimore"}}}
            ]
        }
    }
    emitted, filtered, err = _parse_ollama_tool_calls(normal_resp, oracle)
    assert len(emitted) == 1
    assert emitted[0]["name"] == "get_weather"
    assert emitted[0]["arguments"] == {"location": "Baltimore"}
    assert filtered == []
    assert err is None

    # _parse_ollama_tool_calls: hallucinated name → filtered_invalid
    hall_resp = {
        "message": {
            "tool_calls": [
                {"function": {"name": "get_unicorn_prices", "arguments": {}}}
            ]
        }
    }
    emitted2, filtered2, err2 = _parse_ollama_tool_calls(hall_resp, oracle)
    assert emitted2 == [], "hallucinated name must not appear in tools_emitted"
    assert len(filtered2) == 1
    assert filtered2[0]["name"] == "get_unicorn_prices"
    assert err2 is None  # hallucination is not malformed_json

    # _parse_ollama_tool_calls: malformed args (string that parses as non-dict)
    malformed_resp = {
        "message": {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": "not-valid-json"}}
            ]
        }
    }
    emitted3, filtered3, err3 = _parse_ollama_tool_calls(malformed_resp, oracle)
    assert emitted3 == []
    assert len(filtered3) == 1
    assert filtered3[0]["malformed"] is True
    assert err3 == "malformed_json"

    # _parse_ollama_tool_calls: string args that json.loads to a dict (valid)
    str_args_resp = {
        "message": {
            "tool_calls": [
                {"function": {"name": "get_weather", "arguments": '{"location": "Denver"}'}}
            ]
        }
    }
    emitted4, filtered4, err4 = _parse_ollama_tool_calls(str_args_resp, oracle)
    assert len(emitted4) == 1
    assert emitted4[0]["arguments"] == {"location": "Denver"}
    assert filtered4 == []
    assert err4 is None

    # _parse_ollama_tool_calls: no tool_calls → empty (no-tool turn)
    no_tool_resp = {"message": {"content": "I don't need a tool for that."}}
    emitted5, filtered5, err5 = _parse_ollama_tool_calls(no_tool_resp, oracle)
    assert emitted5 == []
    assert filtered5 == []
    assert err5 is None

    # Synthetic micro-probe response flows through build_turn_result correctly
    synthetic = {
        "metadata": {
            "tool_calls_emitted": {
                "calls": [{"name": "get_weather", "arguments": {"location": "Baltimore"}}],
                "filtered_invalid": [],
            },
            "model_component_name": "micro_probe",
            "model_component_used": "gemma4:e4b-it-qat",
            "tokens_per_second": 72.5,
            "node_timings": {},
            "cache_hit": False,
            "model_used": None,
        }
    }
    row_mp = build_turn_result(
        {"query": "weather?", "expected_tools": ["get_weather"], "expected_component": None},
        synthetic,
        total_latency_ms=800.0,
        tool_oracle=oracle,
        cell_label="gemma4_e4b",
        cell_target_component="micro_probe",
        cell_target_model="gemma4:e4b-it-qat",
    )
    assert row_mp["model_component_name"] == "micro_probe"
    assert row_mp["model_component_used"] == "gemma4:e4b-it-qat"
    assert row_mp["correct_tools_all"] is True
    assert row_mp["valid_structural"] is True
    assert row_mp["llm_tokens_per_second"] == 72.5
    assert row_mp["error"] is None

    print("[self-test] micro-probe transport: PASS")
    print("[self-test] ALL PASS")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="ATHENA-57 tool-calling benchmark harness"
    )
    p.add_argument("--host", default="http://localhost:8001",
                   help="Base URL. For transport=query: orchestrator (default: "
                        "http://localhost:8001). For transport=ollama: Ollama host "
                        "(e.g. http://localhost:11434).")
    p.add_argument("--cell", default="unnamed",
                   help="Cell identifier written into the output filename "
                        "and every row's cell_label field "
                        "(e.g. qwen3_baseline, gemma4_e4b)")
    p.add_argument("--n", type=int, default=20,
                   help="Runs per query (default: 20; ≥20 for decision-grade)")
    p.add_argument("--query-set", type=Path, default=_DEFAULT_QUERY_SET,
                   help="Path to query_set.yaml (default: bench/query_set.yaml)")
    p.add_argument("--results-dir", type=Path, default=_RESULTS_DIR,
                   help="Output directory for JSONL files")
    p.add_argument("--timeout", type=float, default=60.0,
                   help="Per-request timeout in seconds (default: 60; "
                        "ollama transport often needs 120+)")
    p.add_argument(
        "--transport", choices=["query", "ollama"], default="query",
        help=(
            "Transport to use. 'query' (default): POST /query against the "
            "orchestrator (requires Phase 1b live). 'ollama': micro-probe — "
            "POST /api/chat directly to Ollama with the pinned model and the "
            "real 20-tool schema, bypassing the orchestrator entirely "
            "(plan Option B fallback)."
        ),
    )
    p.add_argument(
        "--model", default=None,
        help=(
            "Pinned model tag (required for --transport ollama). "
            "The exact Ollama model tag to benchmark, e.g. "
            "'qwen3:4b-instruct-2507-q4_K_M' or 'gemma4:e4b-it-qat'. "
            "Written to model_component_used in every JSONL row."
        ),
    )
    p.add_argument("--self-test", action="store_true",
                   help="Run in-process smoke tests without a live host and exit")
    p.add_argument(
        "--dump-tools", action="store_true",
        help=(
            "Print the 20 OpenAI-format tool schemas sourced from "
            "get_rag_tools() and exit. Useful for inspecting what the "
            "micro-probe sends to Ollama. Requires PYTHONPATH to include src/."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    if args.dump_tools:
        schemas = _load_tool_schemas()
        print(json.dumps(schemas, indent=2))
        print(f"\n[dump-tools] {len(schemas)} schemas loaded from get_rag_tools()")
        return

    if args.self_test:
        _self_test()
        return

    if args.transport == "ollama" and not args.model:
        print(
            "error: --model is required when --transport ollama is used.\n"
            "Example: --model qwen3:4b-instruct-2507-q4_K_M",
            file=sys.stderr,
        )
        sys.exit(1)

    out = run(
        host=args.host,
        cell=args.cell,
        query_set_path=args.query_set,
        n=args.n,
        results_dir=args.results_dir,
        timeout_s=args.timeout,
        transport=args.transport,
        model=args.model,
    )
    print(f"Results written to: {out}")


if __name__ == "__main__":
    main()
