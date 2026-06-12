# ATHENA-57 Benchmark — bench/

This directory contains the query set, results, and tooling for the Gemma4 QAT A/B
tool-calling trial (ATHENA-57).

---

## Running the harness

```
python scripts/bench_tool_calling.py \
    --host http://localhost:8001 \
    --cell qwen3_baseline \
    --n 20 \
    --query-set bench/query_set.yaml \
    --results-dir bench/results
```

### Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--host` | `http://localhost:8001` | Base URL. For `--transport query`: orchestrator URL. For `--transport ollama`: Ollama host (e.g. `http://localhost:11434`). |
| `--cell` | `unnamed` | Cell identifier written into the output filename and every row (`cell_label`). Use a descriptive name like `qwen3_baseline` or `gemma4_e4b`. |
| `--n` | `20` | Runs per query. Use ≥20 for decision-grade results. |
| `--query-set` | `bench/query_set.yaml` | Path to query set YAML. |
| `--results-dir` | `bench/results` | Output directory for JSONL files. |
| `--timeout` | `60.0` | Per-request timeout in seconds. Ollama transport often needs 120+. |
| `--transport` | `query` | Transport: `query` (POST /query, default) or `ollama` (micro-probe, plan Option B). |
| `--model` | (none) | Pinned model tag. Required for `--transport ollama`. Written to `model_component_used` in every row (e.g. `gemma4:e4b-it-qat`). |
| `--self-test` | (flag) | Run in-process smoke tests without a live host and exit. Verifies query set validity, scoring logic, fallback attribution, and micro-probe payload/parsing. |
| `--dump-tools` | (flag) | Print the 20 OpenAI-format tool schemas sourced from `get_rag_tools()` and exit. Requires PYTHONPATH to include `src/`. |

### Self-test mode

```
python scripts/bench_tool_calling.py --self-test
```

Runs entirely in-process (no network, no live orchestrator). Use to validate the
install before a live run.

---

## Running the report

```
python scripts/bench_report.py \
    bench/results/20260612T120000Z_qwen3_baseline.jsonl \
    bench/results/20260612T130000Z_gemma4_e4b.jsonl
```

Or summarise a single file:

```
python scripts/bench_report.py bench/results/20260612T120000Z_qwen3_baseline.jsonl
```

---

## JSONL schema (per turn)

Each row written by the harness has the following fields:

| Field | Type | Description |
|-------|------|-------------|
| `query` | str | Query text |
| `expected_tools` | list[str] | Expected tool names from the query set |
| `expected_component` | str\|null | Advisory expected component from the query set |
| `tools_emitted` | list[dict] | Calls from `metadata.tool_calls_emitted.calls` |
| `tools_filtered_invalid` | list[dict] | Calls from `metadata.tool_calls_emitted.filtered_invalid` |
| `valid_structural` | bool | Metric A: ≥1 emitted call names a real tool and args are a dict |
| `correct_tools_all` | bool | Metric B (headline): every expected tool present in the emitted set |
| `correct_tools_partial` | float | \|expected ∩ emitted\| / \|expected\| |
| `correct_args` | bool | Metric C (report-only): required params are present and non-empty |
| `is_false_positive` | bool | True iff none-tagged turn emitted any tool call |
| `total_latency_ms` | float | Wall-clock turn latency in milliseconds |
| `node_timings` | dict | Per-node timing from `metadata.node_timings` |
| `model_component_name` | str\|null | Component the router resolved (from response or fallback) |
| `model_component_used` | str\|null | Actual model tag used (from response or `--cell` fallback) |
| `cache_hit` | bool | True iff the response was served from the semantic cache |
| `llm_tokens_per_second` | float\|null | Sourced from `metadata.tokens_per_second` |
| `temperature` | float | Always 0.1 (bound per plan) |
| `skip_semantic_cache` | bool | Always True (bound per plan) |
| `model_used` | str\|null | `metadata.model_used` (ModelTier string, not component model) |
| `cell_label` | str\|null | The `--cell` argument used for this run |
| `run_index` | int | 1-based run number within the query (1..N) |
| `query_id` | str | The `id` field from `query_set.yaml` |
| `error` | str\|null | `timeout`, `http_non_200`, `malformed_json`, `cache_hit`, or null |
| `http_status` | int | Present on `http_non_200` rows |

---

## Environment contract (binding per plan)

Every POST `/query` sent by the harness includes:

- `"temperature": 0.1` — overrides `OrchestratorState` default of 0.7 so both
  cells are comparable and pinned low.
- `"skip_semantic_cache": true` — prevents semantic cache from serving cached
  responses instead of the LLM, which would poison N≥2 repeats. The harness also
  rejects any row where `metadata.cache_hit` is true (belt-and-suspenders).

---

## Attribution-fallback rule (error rows)

For `http_non_200`, `timeout`, `malformed_json`, and `cache_hit` rows the live
response carries no `model_component_name` / `model_component_used` (the
orchestrator never reached the tool-call node). To keep these rows in the correct
per-component cell for denominator counting, `build_turn_result` accepts:

- `cell_target_component` — populated from `query_entry["expected_component"]`
  (advisory; the observed component from a successful response may differ, but
  errors are attributed to the query's intended component)
- `cell_target_model` — the `--cell` argument (the operator-declared model
  identity for this run, e.g. `"qwen3_baseline"` or `"gemma4_e4b"`)

These fallback values are written to `model_component_name` and
`model_component_used` on error rows. `bench_report.py`'s `aggregate()` then
includes them in the correct cell's denominator.

**Impact**: error/timeout/cache_hit rates can look higher than a naive analysis
(which would exclude these rows). This is correct — the plan's statistical policy
says the denominator = ALL attempted turns including errors. Excluding error rows
would make denominators smaller and correct-tool rates look better than reality.

---

## Committed-results policy

`bench/results/*.jsonl` are **committed to the repository**. The query set uses
synthetic queries (no real user data), so committing results is appropriate and
provides a reproducible audit trail for the decision. Do not add `bench/results/`
to `.gitignore`.

---

## Micro-probe transport (`--transport ollama`)

The micro-probe transport is the plan's documented **Option B fallback**, used
when the `/query` Phase 1b observability surfaces (`metadata.tool_calls_emitted`,
`skip_semantic_cache`, etc.) cannot land in production. It bypasses the
orchestrator entirely and posts directly to Ollama.

### When to use it

Use `--transport ollama` when:
- Phase 1b is not yet deployed and you need tool-calling quality numbers now.
- You want to isolate the tool-choice decision from routing/caching noise
  (useful for early model screening before investing in a full Phase 3 run).
- Production has zero fallback triggers configured and `/query` skips tool
  selection entirely ("No fallback triggers configured, skipping tool calling").

Do NOT use it as a substitute for the full `/query` run once Phase 1b is live —
it does not measure end-to-end turn latency (TTFT-sensitive), does not exercise
the intent router, and does not test the production cache path.

### What it measures vs `/query`

| Dimension | `/query` (transport=query) | Micro-probe (transport=ollama) |
|-----------|---------------------------|-------------------------------|
| Tool-choice correctness | Yes (with Phase 1b) | Yes |
| End-to-end turn latency | Yes (TTFT-sensitive) | No — model-level only |
| Intent routing / complexity stratification | Yes | No |
| Semantic cache behaviour | Yes (skip_semantic_cache) | Not applicable |
| Component attribution | metadata.model_component_name | Always "micro_probe" |

### Think-suppression parity

The micro-probe replicates `llm_router.py:1057-1059` exactly:

```python
if "qwen3" in model.lower():
    payload["think"] = False
```

- qwen3 models: `"think": false` is injected into the Ollama payload.
- gemma / other models: no `think` key (matches prod behaviour — no branch
  exists in `llm_router._generate_ollama_with_tools` for non-qwen3 models).

### Latency caveat

`total_latency_ms` in micro-probe rows is the wall-clock time of a single
Ollama `/api/chat` POST (model-level latency). It is **NOT** comparable to
`/query` latency, which includes intent classification, routing, synthesis, and
all other orchestrator overhead. Gate 3 (`p90 ≤ incumbent × 1.10`) is valid
only when comparing cells **on the same transport** — never cross-transport.

### JSONL difference

Micro-probe rows carry one additional field:

| Field | Value |
|-------|-------|
| `transport` | `"ollama"` |
| `model_component_name` | `"micro_probe"` (constant) |
| `model_component_used` | pinned model tag (e.g. `"gemma4:e4b-it-qat"`) |

`bench_report.py` treats `"micro_probe"` as the single component for a
micro-probe cell. The per-component minimum-N logic fires against the full
cell's effective N, not a stratified sub-population (because no router
stratification exists for a pinned-model direct call). The incumbent/challenger
gate pairing heuristic works identically — it keys on the cell label (e.g.
`qwen3_4b_baseline` vs `gemma4_e4b`), not the component name.

### Running the micro-probe

```bash
# Baseline cell (qwen3:4b)
python scripts/bench_tool_calling.py \
    --transport ollama \
    --host http://localhost:11434 \
    --model qwen3:4b-instruct-2507-q4_K_M \
    --cell qwen3_4b_baseline \
    --n 20

# Challenger cell (gemma4:e4b)
python scripts/bench_tool_calling.py \
    --transport ollama \
    --host http://localhost:11434 \
    --model gemma4:e4b-it-qat \
    --cell gemma4_e4b \
    --n 20

# Inspect the 20 tool schemas that are sent to Ollama
python scripts/bench_tool_calling.py --dump-tools
```

PYTHONPATH must include `src/` so `get_rag_tools` is importable:

```bash
PYTHONPATH=/path/to/os-project-athena/src \
    python scripts/bench_tool_calling.py --transport ollama ...
```

---

## Decision gates

| Gate | Threshold | Metric |
|------|-----------|--------|
| Gate 1 | Gemma ≥ qwen3 + 5pp | `correct_tools_all` rate |
| Gate 2 (abs) | Gemma FP rate ≤ 15% | `is_false_positive` rate on none-tagged turns |
| Gate 2 (rel) | Gemma FP ≤ qwen3 FP + 5pp | Same |
| Gate 3 | Gemma p90 latency ≤ qwen3 p90 × 1.10 | `total_latency_ms` p90 |

All three gates must pass for a SWAP RECOMMENDED verdict. Incumbent (qwen3) wins ties.
