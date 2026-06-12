# ATHENA-57 Benchmark Decision

**Date:** 2026-06-12
**Decision:** NO-SWAP — `qwen3:4b-instruct-2507-q4_K_M` remains on all tool-calling components. No config was mutated.

## Gate table

N=600 per cell (gemma4:12b effective N=599, 1 timeout error). Errors ≤1 in all cells.

| Cell | Model | CorrectAll% | FP% | p90 (ms) | Gate 1 | Gate 2 | Gate 3 |
|---|---|---|---|---|---|---|---|
| Incumbent | qwen3:4b-instruct-2507-q4_K_M | 89.8% | 8.8% | 4,311 | — | — | — |
| Challenger A | qwen3:8b | 86.7% | 0.0% | 1,711 | −3.1pp (informational) | PASS | PASS |
| Challenger B | gemma4:e4b-it-qat | 58.8% | 23.3% | 7,923 | **FAIL −31pp** | **FAIL >15% cap** | **FAIL +84%** |
| Challenger C | gemma4:12b-it-qat | 67.2% | 7.1% | 17,480 | **FAIL −22.6pp** | PASS | **FAIL ~4x** |

Gate thresholds (from `bench/README.md`):
- G1: correct-tool rate must not regress more than −5pp vs. incumbent
- G2: FP rate ≤ 15% absolute AND ≤ incumbent + 5pp relative
- G3: p90 latency ≤ incumbent × 1.10

Both gemma4 challengers fail G1 and G3. gemma4:e4b additionally fails G2. qwen3:8b passes all gates but was not the primary challenger for this campaign; no swap initiated.

Full statistical table: [`athena57-phase3-report.md`](athena57-phase3-report.md).

## Method notes

- Transport: micro-probe Ollama (plan Option B). The live `/query` endpoint has zero fallback triggers configured — `tool_call_node` never does LLM tool selection in production; discovered at Phase 3 pre-flight. Micro-probe isolates model-level tool-call accuracy without that confound.
- Think-suppression parity applied: `think:false` for qwen3 cells, matching production config.
- Temperature: 0.1 across all cells.
- Host: 192.168.10.108, Ollama 0.30.7.
- Live super-complex incumbent is `qwen3:4b-instruct` — not `qwen3:8b` as the preset default suggested. Corrected at campaign start.
- Latency figures are model-level (same transport across cells); relatively comparable within this run, not independently calibrated.

## Operational residue

- `gemma4:e4b-it-qat` and `gemma4:12b-it-qat` remain pulled on host 192.168.10.108 (~13 GB combined). Reclaim with `ollama rm gemma4:e4b-it-qat` and `ollama rm gemma4:12b-it-qat` if desired.
- Orchestrator runs the Phase 1b observability image (digest-pinned, rollback tag `oss-prod-20260511110000`). No image change from this campaign.
