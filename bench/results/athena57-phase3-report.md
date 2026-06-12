[report] 20260612T195825Z_qwen3_4b_baseline.jsonl: 600 rows
[report] 20260612T202504Z_qwen3_8b_baseline.jsonl: 600 rows
[report] 20260612T203731Z_gemma4_e4b.jsonl: 600 rows
[report] 20260612T213439Z_gemma4_12b.jsonl: 600 rows
# ATHENA-57 Tool-Calling Benchmark Report

Statistical policy: denominator = ALL attempted turns (incl. errors/timeouts).  Min effective N per component = 20.  Incumbent wins ties.  Error rows without live attribution use fallback (cell_target_component / cell_target_model from the harness --cell arg) — see bench/README.md.

## Component: `micro_probe`

| Cell | N (eff) | Grade | CorrectAll% | PartialR% | ValidStruct% | CorrectArgs% | FP% | p50ms | p90ms | p95ms | TPS | Errors | CacheHits |
|------|---------|-------|------------|-----------|-------------|------------|-----|-------|-------|-------|-----|--------|-----------|
| 20260612T195825Z_qwen3_4b_baseline/qwen3:4b-instruct-2507-q4_K_M | 600 (600) | N=600 | 89.8% | 93.2% | 63.5% | 93.3% | 8.8% | 1341 | 4311 | 5218 | 34.1 | 0 | 0 |
| 20260612T202504Z_qwen3_8b_baseline/qwen3:8b | 600 (600) | N=600 | 86.7% | 91.1% | 56.7% | 86.7% | 0.0% | 851 | 1711 | 4615 | 68.6 | 0 | 0 |
| 20260612T203731Z_gemma4_e4b/gemma4:e4b-it-qat | 600 (600) | N=600 | 58.8% | 61.6% | 42.5% | 68.2% | 23.3% | 5566 | 7923 | 8908 | 77.3 | 0 | 0 |
| 20260612T213439Z_gemma4_12b/gemma4:12b-it-qat | 600 (599) | N=599 | 67.2% | 73.6% | 48.7% | 70.0% | 7.1% | 8630 | 17480 | 21030 | 38.8 | 1 | 0 |

**Error breakdown:**

| Cell | http_non_200 | timeout | malformed_json | cache_hit |
|------|-------------|---------|----------------|-----------|
| 20260612T195825Z_qwen3_4b_baseline/qwen3:4b-instruct-2507-q4_K_M | 0 | 0 | 0 | 0 |
| 20260612T202504Z_qwen3_8b_baseline/qwen3:8b | 0 | 0 | 0 | 0 |
| 20260612T203731Z_gemma4_e4b/gemma4:e4b-it-qat | 0 | 0 | 0 | 0 |
| 20260612T213439Z_gemma4_12b/gemma4:12b-it-qat | 0 | 1 | 0 | 0 |

## Decision Gates

Incumbent cell identified by key containing 'baseline' or 'qwen3'.  Challenger identified by key containing 'gemma4', 'e4b', or '12b'.  Non-target cells are reported above but not gated.  Incumbent wins ties.

**micro_probe** (non-target cells present, not gated: `20260612T202504Z_qwen3_8b_baseline/qwen3:8b`, `20260612T213439Z_gemma4_12b/gemma4:12b-it-qat`)
**micro_probe**
- Incumbent: `20260612T195825Z_qwen3_4b_baseline/qwen3:4b-instruct-2507-q4_K_M`
- Challenger: `20260612T203731Z_gemma4_e4b/gemma4:e4b-it-qat`
- **Verdict: NO-SWAP (tool accuracy insufficient) | G1=FAIL(+-31.0pp)  G2=FAIL(chal_fp=23.3% inc_fp=8.8%)  G3=FAIL(chal_p90=7923ms inc_p90=4311ms regression=83.8%)**

## Per-query flip rates (nondeterminism visibility)

Fraction of runs disagreeing with modal outcome per query.  High flip rate → LLM is uncertain on that query.

### `20260612T195825Z_qwen3_4b_baseline` / `micro_probe` / `qwen3:4b-instruct-2507-q4_K_M`

| Query (first 60 chars) | Flip rate |
|------------------------|-----------|
| 'What did I just ask you?' | 5.0% |
| "What's the weather in Baltimore?" | 0.0% |
| "When's the next flight from BWI to Denver?" | 0.0% |
| "What's the score of the Ravens game?" | 0.0% |
| 'Any news about the election?' | 0.0% |
| "What's Tesla stock at?" | 0.0% |
| 'Find me a good Italian restaurant nearby' | 0.0% |
| "What's playing on streaming right now?" | 0.0% |
| 'Give me a recipe for carbonara' | 0.0% |
| "What's the weather and any flights to Chicago?" | 0.0% |
| 'Ravens score and Tesla stock?' | 0.0% |
| 'News on the storm and restaurants open near me?' | 0.0% |
| "Weather tomorrow and what's on streaming tonight?" | 0.0% |
| 'Plan a trip to Denver next weekend — find flights from BWI, ' | 0.0% |
| "What's the Ravens score, and if they won, find me sports bar" | 0.0% |
| 'Compare Tesla and the latest market news, then tell me if it' | 0.0% |
| "I'm flying into BWI tonight — what's the airport status, the" | 0.0% |
| 'Find a recipe for dinner, then check if the grocery stores n' | 0.0% |
| 'What events are happening this weekend, and how do I get the' | 0.0% |
| 'Turn off the bedroom lights' | 0.0% |
| 'Set the thermostat to 70' | 0.0% |
| 'Lock the front door' | 0.0% |
| 'Dim the living room to 30 percent' | 0.0% |
| "What's the meaning of the word 'serendipity'?" | 0.0% |
| 'Tell me a joke' | 0.0% |
| "What's a good book to read this weekend?" | 0.0% |
| 'What can you help me with?' | 0.0% |
| 'Can you explain how photosynthesis works?' | 0.0% |
| 'Repeat that please' | 0.0% |
| 'Never mind' | 0.0% |

### `20260612T202504Z_qwen3_8b_baseline` / `micro_probe` / `qwen3:8b`

| Query (first 60 chars) | Flip rate |
|------------------------|-----------|
| "What's the weather in Baltimore?" | 0.0% |
| "When's the next flight from BWI to Denver?" | 0.0% |
| "What's the score of the Ravens game?" | 0.0% |
| 'Any news about the election?' | 0.0% |
| "What's Tesla stock at?" | 0.0% |
| 'Find me a good Italian restaurant nearby' | 0.0% |
| "What's playing on streaming right now?" | 0.0% |
| 'Give me a recipe for carbonara' | 0.0% |
| "What's the weather and any flights to Chicago?" | 0.0% |
| 'Ravens score and Tesla stock?' | 0.0% |
| 'News on the storm and restaurants open near me?' | 0.0% |
| "Weather tomorrow and what's on streaming tonight?" | 0.0% |
| 'Plan a trip to Denver next weekend — find flights from BWI, ' | 0.0% |
| "What's the Ravens score, and if they won, find me sports bar" | 0.0% |
| 'Compare Tesla and the latest market news, then tell me if it' | 0.0% |
| "I'm flying into BWI tonight — what's the airport status, the" | 0.0% |
| 'Find a recipe for dinner, then check if the grocery stores n' | 0.0% |
| 'What events are happening this weekend, and how do I get the' | 0.0% |
| 'Turn off the bedroom lights' | 0.0% |
| 'Set the thermostat to 70' | 0.0% |
| 'Lock the front door' | 0.0% |
| 'Dim the living room to 30 percent' | 0.0% |
| "What's the meaning of the word 'serendipity'?" | 0.0% |
| 'Tell me a joke' | 0.0% |
| "What's a good book to read this weekend?" | 0.0% |
| 'What can you help me with?' | 0.0% |
| 'Can you explain how photosynthesis works?' | 0.0% |
| 'Repeat that please' | 0.0% |
| 'What did I just ask you?' | 0.0% |
| 'Never mind' | 0.0% |

### `20260612T203731Z_gemma4_e4b` / `micro_probe` / `gemma4:e4b-it-qat`

| Query (first 60 chars) | Flip rate |
|------------------------|-----------|
| "I'm flying into BWI tonight — what's the airport status, the" | 45.0% |
| "What's a good book to read this weekend?" | 15.0% |
| "When's the next flight from BWI to Denver?" | 10.0% |
| "What's the meaning of the word 'serendipity'?" | 5.0% |
| "What's the weather in Baltimore?" | 0.0% |
| "What's the score of the Ravens game?" | 0.0% |
| 'Any news about the election?' | 0.0% |
| "What's Tesla stock at?" | 0.0% |
| 'Find me a good Italian restaurant nearby' | 0.0% |
| "What's playing on streaming right now?" | 0.0% |
| 'Give me a recipe for carbonara' | 0.0% |
| "What's the weather and any flights to Chicago?" | 0.0% |
| 'Ravens score and Tesla stock?' | 0.0% |
| 'News on the storm and restaurants open near me?' | 0.0% |
| "Weather tomorrow and what's on streaming tonight?" | 0.0% |
| 'Plan a trip to Denver next weekend — find flights from BWI, ' | 0.0% |
| "What's the Ravens score, and if they won, find me sports bar" | 0.0% |
| 'Compare Tesla and the latest market news, then tell me if it' | 0.0% |
| 'Find a recipe for dinner, then check if the grocery stores n' | 0.0% |
| 'What events are happening this weekend, and how do I get the' | 0.0% |
| 'Turn off the bedroom lights' | 0.0% |
| 'Set the thermostat to 70' | 0.0% |
| 'Lock the front door' | 0.0% |
| 'Dim the living room to 30 percent' | 0.0% |
| 'Tell me a joke' | 0.0% |
| 'What can you help me with?' | 0.0% |
| 'Can you explain how photosynthesis works?' | 0.0% |
| 'Repeat that please' | 0.0% |
| 'What did I just ask you?' | 0.0% |
| 'Never mind' | 0.0% |

### `20260612T213439Z_gemma4_12b` / `micro_probe` / `gemma4:12b-it-qat`

| Query (first 60 chars) | Flip rate |
|------------------------|-----------|
| "I'm flying into BWI tonight — what's the airport status, the" | 50.0% |
| 'Plan a trip to Denver next weekend — find flights from BWI, ' | 45.0% |
| "What's the meaning of the word 'serendipity'?" | 45.0% |
| "What's a good book to read this weekend?" | 40.0% |
| 'Dim the living room to 30 percent' | 5.0% |
| "What's the weather in Baltimore?" | 0.0% |
| "When's the next flight from BWI to Denver?" | 0.0% |
| "What's the score of the Ravens game?" | 0.0% |
| 'Any news about the election?' | 0.0% |
| "What's Tesla stock at?" | 0.0% |
| 'Find me a good Italian restaurant nearby' | 0.0% |
| "What's playing on streaming right now?" | 0.0% |
| 'Give me a recipe for carbonara' | 0.0% |
| "What's the weather and any flights to Chicago?" | 0.0% |
| 'Ravens score and Tesla stock?' | 0.0% |
| 'News on the storm and restaurants open near me?' | 0.0% |
| "Weather tomorrow and what's on streaming tonight?" | 0.0% |
| "What's the Ravens score, and if they won, find me sports bar" | 0.0% |
| 'Compare Tesla and the latest market news, then tell me if it' | 0.0% |
| 'Find a recipe for dinner, then check if the grocery stores n' | 0.0% |
| 'What events are happening this weekend, and how do I get the' | 0.0% |
| 'Turn off the bedroom lights' | 0.0% |
| 'Set the thermostat to 70' | 0.0% |
| 'Lock the front door' | 0.0% |
| 'Tell me a joke' | 0.0% |
| 'What can you help me with?' | 0.0% |
| 'Can you explain how photosynthesis works?' | 0.0% |
| 'Repeat that please' | 0.0% |
| 'What did I just ask you?' | 0.0% |
| 'Never mind' | 0.0% |

