"""ATHENA-57 Phase 2 — Benchmark report generator.

Reads one or more JSONL files produced by bench_tool_calling.py and emits a
markdown comparison table stratified by model_component_name × model_component_used.

Statistical policy (per plan)
------------------------------
- Denominator for ALL rates = every attempted turn, including errors/timeouts/
  cache_hit rows.  Error rows contribute failures (not-correct, not-FP).
- Gate 1 — correct-tool rate (correct_tools_all): Gemma must improve by ≥5pp.
- Gate 2 — false-positive rate: Gemma ≤ qwen3 + 5pp AND absolute ≤ 15%.
- Gate 3 — latency: p90 must not regress by > 10% vs qwen3.
- Per-component effective N ≥ 20 for a component decision to be grade.
  Components below 20 are flagged NON-DECISION-GRADE.
- Incumbent (qwen3) wins ties.

Usage
-----
    python scripts/bench_report.py bench/results/20260612T120000Z_qwen3_baseline.jsonl \\
                                   bench/results/20260612T130000Z_gemma4_e4b.jsonl

    # Or summarise a single file:
    python scripts/bench_report.py bench/results/20260612T120000Z_qwen3_baseline.jsonl
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Minimum effective N for a component decision to be grade
MIN_EFFECTIVE_N = 20

# Decision gate thresholds
GATE1_MIN_IMPROVEMENT_PP = 5.0     # correct_tools_all improvement in pp
GATE2_MAX_ABSOLUTE_FP = 15.0       # absolute FP rate cap (%)
GATE2_MAX_RELATIVE_DELTA_PP = 5.0  # challenger FP ≤ incumbent FP + 5pp
GATE3_MAX_LATENCY_REGRESSION = 0.10  # p90 must not regress by > 10%


# ---------------------------------------------------------------------------
# JSONL loader
# ---------------------------------------------------------------------------

def load_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    with open(path) as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(
                    f"  [warn] {path.name}:{i}: JSON parse error — {exc}",
                    file=sys.stderr,
                )
    return rows


# ---------------------------------------------------------------------------
# Per-component cell aggregator
# ---------------------------------------------------------------------------

def _percentile(sorted_vals: List[float], p: float) -> float:
    """Return the p-th percentile (0–100) of a pre-sorted list."""
    if not sorted_vals:
        return float("nan")
    n = len(sorted_vals)
    idx = (p / 100.0) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_vals[-1]
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


class ComponentCellStats:
    """Aggregated stats for one (component_name, model_tag) cell."""

    def __init__(self, component_name: str, model_tag: str):
        self.component_name = component_name
        self.model_tag = model_tag

        # Denominators
        self.total_turns: int = 0     # all turns including errors
        self.error_count: int = 0
        self.cache_hit_count: int = 0
        self.timeout_count: int = 0
        self.http_non_200_count: int = 0
        self.malformed_json_count: int = 0

        # Scoring numerators (errors count as failures)
        self.correct_all_sum: int = 0
        self.partial_sum: float = 0.0
        self.valid_structural_sum: int = 0
        self.correct_args_sum: int = 0
        self.false_positive_sum: int = 0

        # Denominator for FP gate (none-tagged turns only)
        self.fp_denominator: int = 0

        # Latency (only for non-error turns)
        self.latencies_ms: List[float] = []
        self.tokens_per_second: List[float] = []

        # Per-query flip tracking: {query_text: [correct_tools_all bool per run]}
        self._per_query_results: Dict[str, List[bool]] = defaultdict(list)

    def add_row(self, row: Dict[str, Any]) -> None:
        self.total_turns += 1
        err = row.get("error")
        if err == "cache_hit":
            self.cache_hit_count += 1
        elif err == "timeout":
            self.timeout_count += 1
        elif err == "http_non_200":
            self.http_non_200_count += 1
        elif err == "malformed_json":
            self.malformed_json_count += 1
        if err is not None:
            self.error_count += 1

        # Scoring — errors contribute as False/0 to rates
        c_all = bool(row.get("correct_tools_all", False))
        self.correct_all_sum += int(c_all)
        self.partial_sum += float(row.get("correct_tools_partial", 0.0))
        self.valid_structural_sum += int(bool(row.get("valid_structural", False)))
        self.correct_args_sum += int(bool(row.get("correct_args", False)))

        # FP tracking: only turns where expected_tools == []
        expected = row.get("expected_tools", [])
        if expected == [] or expected is None:
            self.fp_denominator += 1
            if bool(row.get("is_false_positive", False)):
                self.false_positive_sum += 1

        # Latency / tok/s (non-error turns only)
        if err is None:
            lat = row.get("total_latency_ms")
            if lat is not None:
                self.latencies_ms.append(float(lat))
            tps = row.get("llm_tokens_per_second")
            if tps is not None:
                self.tokens_per_second.append(float(tps))

        # Flip tracking
        self._per_query_results[row.get("query", "?")].append(c_all)

    @property
    def effective_n(self) -> int:
        """Non-error turns."""
        return self.total_turns - self.error_count

    @property
    def is_decision_grade(self) -> bool:
        return self.effective_n >= MIN_EFFECTIVE_N

    @property
    def correct_tool_rate(self) -> float:
        if self.total_turns == 0:
            return float("nan")
        return (self.correct_all_sum / self.total_turns) * 100.0

    @property
    def correct_tool_partial_rate(self) -> float:
        if self.total_turns == 0:
            return float("nan")
        return (self.partial_sum / self.total_turns) * 100.0

    @property
    def valid_structural_rate(self) -> float:
        if self.total_turns == 0:
            return float("nan")
        return (self.valid_structural_sum / self.total_turns) * 100.0

    @property
    def correct_args_rate(self) -> float:
        if self.total_turns == 0:
            return float("nan")
        return (self.correct_args_sum / self.total_turns) * 100.0

    @property
    def fp_rate(self) -> float:
        if self.fp_denominator == 0:
            return float("nan")
        return (self.false_positive_sum / self.fp_denominator) * 100.0

    @property
    def latency_p50(self) -> float:
        return _percentile(sorted(self.latencies_ms), 50)

    @property
    def latency_p90(self) -> float:
        return _percentile(sorted(self.latencies_ms), 90)

    @property
    def latency_p95(self) -> float:
        return _percentile(sorted(self.latencies_ms), 95)

    @property
    def mean_tps(self) -> float:
        if not self.tokens_per_second:
            return float("nan")
        return sum(self.tokens_per_second) / len(self.tokens_per_second)

    def per_query_flip_rates(self) -> Dict[str, float]:
        """Fraction of runs disagreeing with the modal outcome, per query."""
        result = {}
        for q, outcomes in self._per_query_results.items():
            if not outcomes:
                result[q] = float("nan")
                continue
            modal = outcomes.count(True) > len(outcomes) / 2
            disagreements = sum(1 for o in outcomes if o != modal)
            result[q] = disagreements / len(outcomes)
        return result


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate(rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str], ComponentCellStats]:
    """Group rows into (component_name, model_tag) cells.

    Rows where model_component_name or model_component_used is None are
    excluded from per-component stratification (they never entered the
    tool_call_node) but are tracked as an "unattributed" bucket for
    completeness.
    """
    cells: Dict[Tuple[str, str], ComponentCellStats] = {}
    unattributed = 0
    for row in rows:
        cname = row.get("model_component_name")
        mtag = row.get("model_component_used")
        if cname is None or mtag is None:
            unattributed += 1
            continue
        key = (cname, mtag)
        if key not in cells:
            cells[key] = ComponentCellStats(cname, mtag)
        cells[key].add_row(row)
    if unattributed:
        print(
            f"  [info] {unattributed} rows excluded from stratification "
            "(model_component_name or model_component_used is None — "
            "HA-control / cached / non-tool-path turns).",
            file=sys.stderr,
        )
    return cells


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def _fmt(val: float, decimals: int = 1, suffix: str = "") -> str:
    if math.isnan(val):
        return "n/a"
    return f"{val:.{decimals}f}{suffix}"


def _grade_badge(cell: ComponentCellStats) -> str:
    if not cell.is_decision_grade:
        return f"⚠ NON-GRADE (N={cell.effective_n})"
    return f"N={cell.effective_n}"


def _apply_gates(
    incumbent: Optional[ComponentCellStats],
    challenger: Optional[ComponentCellStats],
) -> str:
    """Return a verdict string for a challenger vs incumbent pair."""
    if incumbent is None or challenger is None:
        return "INSUFFICIENT DATA"
    if not incumbent.is_decision_grade or not challenger.is_decision_grade:
        return "NON-DECISION-GRADE"

    # Gate 1
    g1_delta = challenger.correct_tool_rate - incumbent.correct_tool_rate
    g1_pass = g1_delta >= GATE1_MIN_IMPROVEMENT_PP

    # Gate 2
    chal_fp = challenger.fp_rate
    inc_fp = incumbent.fp_rate
    if math.isnan(chal_fp) or math.isnan(inc_fp):
        g2_pass = False
        g2_note = "no FP data"
    else:
        g2_pass = chal_fp <= GATE2_MAX_ABSOLUTE_FP and chal_fp <= inc_fp + GATE2_MAX_RELATIVE_DELTA_PP
        g2_note = f"chal_fp={chal_fp:.1f}% inc_fp={inc_fp:.1f}%"

    # Gate 3
    inc_p90 = incumbent.latency_p90
    chal_p90 = challenger.latency_p90
    if math.isnan(inc_p90) or math.isnan(chal_p90) or inc_p90 == 0:
        g3_pass = True  # no data → cannot penalise
        g3_note = "no latency data"
    else:
        regress = (chal_p90 - inc_p90) / inc_p90
        g3_pass = regress <= GATE3_MAX_LATENCY_REGRESSION
        g3_note = f"chal_p90={chal_p90:.0f}ms inc_p90={inc_p90:.0f}ms regression={regress*100:.1f}%"

    gates = (
        f"G1={'PASS' if g1_pass else 'FAIL'}(+{g1_delta:.1f}pp)  "
        f"G2={'PASS' if g2_pass else 'FAIL'}({g2_note})  "
        f"G3={'PASS' if g3_pass else 'FAIL'}({g3_note})"
    )
    if g1_pass and g2_pass and g3_pass:
        verdict = "SWAP RECOMMENDED"
    elif g1_pass and g2_pass and not g3_pass:
        verdict = "NO-SWAP (latency regression — user override required)"
    elif g1_pass and not g2_pass:
        verdict = "NO-SWAP (FP gate failure)"
    elif not g1_pass:
        verdict = "NO-SWAP (tool accuracy insufficient)"
    else:
        verdict = "NO-SWAP"

    # Incumbent wins ties
    if g1_delta == 0.0 and g2_pass and g3_pass:
        verdict = "NO-SWAP (tie — incumbent wins)"

    return f"{verdict} | {gates}"


def build_report(
    files: List[Path],
    label_map: Optional[Dict[str, str]] = None,
) -> str:
    """Build a markdown report from one or more JSONL files.

    label_map maps filename stem → human label (optional; filename stem used otherwise).
    """
    all_cells: Dict[Tuple[str, str, str], ComponentCellStats] = {}
    # key: (source_label, component_name, model_tag)

    for fpath in files:
        label = (label_map or {}).get(fpath.stem, fpath.stem)
        rows = load_jsonl(fpath)
        print(f"[report] {fpath.name}: {len(rows)} rows")
        cells = aggregate(rows)
        for (cname, mtag), stats in cells.items():
            all_cells[(label, cname, mtag)] = stats

    lines: List[str] = []
    lines.append("# ATHENA-57 Tool-Calling Benchmark Report\n")
    lines.append(
        "Statistical policy: denominator = ALL attempted turns (incl. errors/timeouts).  "
        f"Min effective N per component = {MIN_EFFECTIVE_N}.  "
        "Incumbent wins ties.\n"
    )

    # Collect all unique (component, model_tag) pairs across files
    components: Dict[str, Dict[str, ComponentCellStats]] = defaultdict(dict)
    # components[component_name][f"{label}/{model_tag}"] = stats
    label_tags: List[Tuple[str, str, str]] = []
    for (label, cname, mtag), stats in sorted(all_cells.items()):
        key = f"{label}/{mtag}"
        components[cname][key] = stats
        label_tags.append((label, cname, mtag))

    # Per-component table
    for cname in sorted(components.keys()):
        comp_cells = components[cname]
        lines.append(f"## Component: `{cname}`\n")

        # Stats table
        lines.append(
            "| Cell | N (eff) | Grade | CorrectAll% | PartialR% | ValidStruct% | "
            "CorrectArgs% | FP% | p50ms | p90ms | p95ms | TPS | Errors | CacheHits |"
        )
        lines.append("|------|---------|-------|------------|-----------|-------------|"
                     "------------|-----|-------|-------|-------|-----|--------|-----------|")
        for key in sorted(comp_cells.keys()):
            c = comp_cells[key]
            lines.append(
                f"| {key} "
                f"| {c.total_turns} ({c.effective_n}) "
                f"| {_grade_badge(c)} "
                f"| {_fmt(c.correct_tool_rate, suffix='%')} "
                f"| {_fmt(c.correct_tool_partial_rate, suffix='%')} "
                f"| {_fmt(c.valid_structural_rate, suffix='%')} "
                f"| {_fmt(c.correct_args_rate, suffix='%')} "
                f"| {_fmt(c.fp_rate, suffix='%')} "
                f"| {_fmt(c.latency_p50, 0)} "
                f"| {_fmt(c.latency_p90, 0)} "
                f"| {_fmt(c.latency_p95, 0)} "
                f"| {_fmt(c.mean_tps, 1)} "
                f"| {c.error_count} "
                f"| {c.cache_hit_count} |"
            )
        lines.append("")

        # Error breakdown
        any_errors = any(c.error_count > 0 for c in comp_cells.values())
        if any_errors:
            lines.append("**Error breakdown:**\n")
            lines.append("| Cell | http_non_200 | timeout | malformed_json | cache_hit |")
            lines.append("|------|-------------|---------|----------------|-----------|")
            for key in sorted(comp_cells.keys()):
                c = comp_cells[key]
                lines.append(
                    f"| {key} "
                    f"| {c.http_non_200_count} "
                    f"| {c.timeout_count} "
                    f"| {c.malformed_json_count} "
                    f"| {c.cache_hit_count} |"
                )
            lines.append("")

    # Decision gate section (requires exactly two cells per component)
    lines.append("## Decision Gates\n")
    lines.append(
        "Gates applied per component when exactly two cells are present.  "
        "Incumbent wins ties.\n"
    )

    # Heuristic: label containing "baseline" or "qwen3" is the incumbent
    for cname in sorted(components.keys()):
        comp_cells = components[cname]
        if len(comp_cells) != 2:
            lines.append(
                f"**{cname}**: {len(comp_cells)} cell(s) present — "
                "gate comparison requires exactly 2.\n"
            )
            continue

        keys = sorted(comp_cells.keys())
        # Identify incumbent by label heuristic
        inc_key = next(
            (k for k in keys if "baseline" in k.lower() or "qwen3" in k.lower()),
            keys[0],
        )
        chal_key = next(k for k in keys if k != inc_key)

        verdict = _apply_gates(comp_cells[inc_key], comp_cells[chal_key])
        lines.append(f"**{cname}**")
        lines.append(f"- Incumbent: `{inc_key}`")
        lines.append(f"- Challenger: `{chal_key}`")
        lines.append(f"- **Verdict: {verdict}**\n")

    # Per-query flip rate section
    lines.append("## Per-query flip rates (nondeterminism visibility)\n")
    lines.append(
        "Fraction of runs disagreeing with modal outcome per query.  "
        "High flip rate → LLM is uncertain on that query.\n"
    )
    for (label, cname, mtag), stats in sorted(all_cells.items()):
        flip_rates = stats.per_query_flip_rates()
        if not flip_rates:
            continue
        lines.append(f"### `{label}` / `{cname}` / `{mtag}`\n")
        lines.append("| Query (first 60 chars) | Flip rate |")
        lines.append("|------------------------|-----------|")
        for q, fr in sorted(flip_rates.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {q[:60]!r} | {_fmt(fr * 100, suffix='%')} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    p = argparse.ArgumentParser(
        description="ATHENA-57 benchmark report generator"
    )
    p.add_argument(
        "jsonl_files",
        nargs="+",
        type=Path,
        help="One or more bench/results/*.jsonl files to aggregate",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write markdown report to this file (default: print to stdout)",
    )
    args = p.parse_args()

    md = build_report(args.jsonl_files)
    if args.out:
        args.out.write_text(md)
        print(f"Report written to: {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()
