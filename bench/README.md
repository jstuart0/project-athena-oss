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
| `--host` | `http://localhost:8001` | Orchestrator base URL |
| `--cell` | `unnamed` | Cell identifier written into the output filename and every row (`cell_label`). Use a descriptive name like `qwen3_baseline` or `gemma4_e4b`. |
| `--n` | `20` | Runs per query. Use ≥20 for decision-grade results. |
| `--query-set` | `bench/query_set.yaml` | Path to query set YAML. |
| `--results-dir` | `bench/results` | Output directory for JSONL files. |
| `--timeout` | `60.0` | Per-request timeout in seconds. |
| `--self-test` | (flag) | Run in-process smoke tests without a live host and exit. Verifies query set validity, scoring logic, and fallback attribution. |

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

## Decision gates

| Gate | Threshold | Metric |
|------|-----------|--------|
| Gate 1 | Gemma ≥ qwen3 + 5pp | `correct_tools_all` rate |
| Gate 2 (abs) | Gemma FP rate ≤ 15% | `is_false_positive` rate on none-tagged turns |
| Gate 2 (rel) | Gemma FP ≤ qwen3 FP + 5pp | Same |
| Gate 3 | Gemma p90 latency ≤ qwen3 p90 × 1.10 | `total_latency_ms` p90 |

All three gates must pass for a SWAP RECOMMENDED verdict. Incumbent (qwen3) wins ties.
