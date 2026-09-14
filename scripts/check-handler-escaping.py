#!/usr/bin/env python3
"""Handler-attribute escaping guard — wrong-primitive / unescaped-quoted /
bare-expression populations, the D9b handler-text builder pass, D11's
replace-vs-wrap rule, and the D15 mis-context census.

Populations
-----------
For every `on<event>=` handler span (both quote delimiters, template-literal
AND string-concatenation construction — see `_frontend_escape_scan`), each
`${...}` / concatenated interpolation is classified:

    wrong-primitive    quoted position, wrapped in escapeHtml(...)   [violation]
    unescaped-quoted   quoted position, no escaping call at all      [violation]
    correct            quoted position, wrapped in escapeJsAttr(...) [OK]
    bare-expression    bare-expression position (ratchet, not fixed — D4)

D9b extends this to handler-text BUILDERS (`oss-profiles.js:99 actionButton`):
a function whose entire handler span is one opaque `${identifier}` with no
visible call. Each call site's corresponding argument is resolved and
classified identically, tagged `scope=builder`. The builder SET is pinned by
set-equality (rule 8) — a new builder or a removed one is a violation.

Exit codes
----------
    0  clean (no violations for the requested --class/--check)
    1  violation(s) found
    2  could not run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _frontend_escape_scan as scan  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRONTEND_DIR = REPO_ROOT / "admin" / "frontend"

EXPECTED_BUILDER_SET = {"admin/frontend/oss-profiles.js:99 actionButton"}

# D11 near-misses: hand-rolled `.replace(` that must NOT be "fixed" — not
# quote-escaping, or already the canonical entity-map definitions.
D11_NEAR_MISSES = {
    ("features.js", 472),
    ("model-downloads.js", 432),
    ("conversations.js", 551),
    ("app.js", 4067),
}

HANDROLLED_REPLACE_RE = re.compile(
    r"""\.replace\(\s*/\\?['"]/g\s*,\s*["']\\\\?['"]["']\s*\)"""
)


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


_strip_outer_delims = scan.strip_outer_delims
Builder = scan.Builder
discover_builders = scan.discover_builders


def builder_routed_interpolations(directory: Path, builders: list[scan.Builder]) -> list[dict]:
    out = []
    for builder in builders:
        text = builder.file.read_text(encoding="utf-8", errors="ignore")
        for call_start, _args_start, args_text in scan.find_calls(text, builder.func_name):
            args = scan.split_top_level(args_text, seps=",")
            if len(args) <= builder.param_idx:
                continue
            arg_raw = args[builder.param_idx]
            inner = _strip_outer_delims(arg_raw)
            if "${" not in inner:
                continue
            call_line = scan.line_of(text, call_start)
            for it in scan.classify_template_interpolations(inner):
                out.append(
                    {
                        "file": rel(builder.file),
                        "line": call_line,
                        "expr": it.expr,
                        "position": it.position,
                        "escape": it.escape,
                        "scope": "builder",
                    }
                )
    return out


def direct_interpolations(directory: Path) -> list[dict]:
    out = []
    for path in scan.iter_frontend_js_files(directory):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for span in scan.find_handler_spans(text):
            if scan.is_builder_marker(span.raw_value):
                continue  # the marker itself carries no data to classify
            for it in scan.classify_span_interpolations(span.raw_value):
                out.append(
                    {
                        "file": rel(path),
                        "line": span.line,
                        "expr": it.expr,
                        "position": it.position,
                        "escape": it.escape,
                        "scope": "direct",
                    }
                )
    return out


def all_interpolations(directory: Path) -> tuple[list[dict], list[Builder]]:
    builders = discover_builders(directory)
    return direct_interpolations(directory) + builder_routed_interpolations(directory, builders), builders


def classify(interp: dict) -> str:
    if interp["position"] == "bare":
        return "bare-expression"
    if interp["escape"] == "escapeJsAttr":
        return "correct"
    if interp["escape"] == "escapeHtml":
        return "wrong-primitive"
    return "unescaped-quoted"


def cmd_class(args) -> tuple[int, dict]:
    interps, builders = all_interpolations(args.dir)
    matches = [i for i in interps if classify(i) == args.klass]
    if args.scope:
        matches = [i for i in matches if i["scope"] == args.scope]

    payload = {"class": args.klass, "count": len(matches), "matches": matches}

    if args.require_population_seen is not None:
        # Population floor (rule 8): the SCOPE (e.g. builder-only) query must
        # itself be evaluated against a non-trivial overall population, or a
        # scoped --max 0 could pass vacuously because the builder was never
        # discovered at all (e.g. a regression in `discover_builders`).
        #
        # Measured across ALL classes within the scope, not just --class.
        # Filtering by --klass here would make the floor equal the very
        # count Phase 3/6 tighten to zero: the wrong-primitive population
        # WITHIN the builder scope is 6 pre-fix and (correctly) 0 once this
        # phase lands, so a --klass-filtered floor of 20 would become
        # permanently unsatisfiable the moment the fix it is meant to gate
        # actually ships. The builder scope's TOTAL interpolation count (6
        # wrong-primitive + 14 unescaped-quoted = 20, D9b) does not shrink
        # when an individual site's escaping is fixed — only its
        # classification changes — so it is the population `discover_
        # builders()` regressing to zero would actually zero out.
        if args.scope:
            population = [i for i in interps if i["scope"] == args.scope]
        else:
            population = [i for i in interps if classify(i) == args.klass]
        if len(population) < args.require_population_seen:
            return 1, {
                **payload,
                "error": (
                    f"population floor not met: saw {len(population)} total "
                    f"{'scope=' + args.scope if args.scope else args.klass} "
                    f"interpolations, expected >= {args.require_population_seen}"
                ),
            }

    if args.max is not None and len(matches) > args.max:
        return 1, payload
    return 0, payload


def cmd_builder_set(args) -> tuple[int, dict]:
    builders = discover_builders(args.dir)
    found = {b.key(REPO_ROOT) for b in builders}
    expected = set(args.expect) if args.expect else EXPECTED_BUILDER_SET
    payload = {"found": sorted(found), "expected": sorted(expected)}
    if found != expected:
        payload["missing"] = sorted(expected - found)
        payload["unexpected"] = sorted(found - expected)
        return 1, payload
    return 0, payload


def cmd_no_nested_replace(args) -> tuple[int, dict]:
    violations = []
    for path in scan.iter_frontend_js_files(args.dir):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"\bescapeJsAttr\s*\(", text):
            open_idx = text.index("(", m.start())
            close_idx = scan.find_matching_paren(text, open_idx)
            if close_idx == -1:
                continue
            arg_text = text[open_idx + 1 : close_idx]
            if ".replace(" in arg_text:
                violations.append({"file": rel(path), "line": scan.line_of(text, m.start())})
    return (1 if violations else 0), {"violations": violations}


def cmd_no_handrolled_escape(args) -> tuple[int, dict]:
    violations = []
    for path in scan.iter_frontend_js_files(args.dir):
        text = path.read_text(encoding="utf-8", errors="ignore")
        spans = scan.find_handler_spans(text)
        for m in HANDROLLED_REPLACE_RE.finditer(text):
            ln = scan.line_of(text, m.start())
            if (path.name, ln) in D11_NEAR_MISSES:
                continue
            inside_span = any(s.value_start <= m.start() <= s.value_end for s in spans)
            if inside_span:
                violations.append({"file": rel(path), "line": ln})
    return (1 if violations else 0), {"violations": violations}


def cmd_jsattr_confinement(args) -> tuple[int, dict]:
    violations = []
    builders = discover_builders(args.dir)
    builders_by_file: dict[Path, list[Builder]] = {}
    for b in builders:
        builders_by_file.setdefault(b.file, []).append(b)

    for path in scan.iter_frontend_js_files(args.dir):
        text = path.read_text(encoding="utf-8", errors="ignore")
        spans = scan.find_handler_spans(text)
        span_ranges = [(s.value_start, s.value_end) for s in spans]

        arg_ranges = []
        for b in builders_by_file.get(path, []):
            for call_start, args_start, args_text in scan.find_calls(text, b.func_name):
                arg_list = scan.split_top_level(args_text, seps=",")
                if len(arg_list) <= b.param_idx:
                    continue
                offset = sum(len(a) + 1 for a in arg_list[: b.param_idx])
                arg_ranges.append((args_start + offset, args_start + offset + len(arg_list[b.param_idx])))

        for m in re.finditer(r"\bescapeJsAttr\s*\(", text):
            pos = m.start()
            if any(s <= pos <= e for s, e in span_ranges) or any(s <= pos <= e for s, e in arg_ranges):
                continue
            violations.append({"file": rel(path), "line": scan.line_of(text, pos)})
    return (1 if violations else 0), {"violations": violations}


MIS_CONTEXT_PATTERNS = {
    "href-javascript": re.compile(r'href\s*=\s*["\']javascript:'),
    "srcdoc": re.compile(r"\bsrcdoc\s*="),
    "set-attribute-on": re.compile(r"""\.setAttribute\(\s*['"]on[a-z]+['"]"""),
    "eval-function-settimeout": re.compile(
        r"\beval\s*\(|new\s+Function\s*\(|setTimeout\(\s*['\"]"
    ),
    "script-body-template": re.compile(r"<script\b[^>]*>\s*\$\{"),
}
STYLE_INTERP_RE = re.compile(r'style="[^"]*\$\{[^"]*"')


def mis_context_census(directory: Path) -> dict:
    out = {k: [] for k in MIS_CONTEXT_PATTERNS}
    out["style"] = []
    for path in scan.iter_frontend_js_files(directory):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for key, pattern in MIS_CONTEXT_PATTERNS.items():
            for m in pattern.finditer(text):
                out[key].append({"file": rel(path), "line": scan.line_of(text, m.start())})
        for m in STYLE_INTERP_RE.finditer(text):
            out["style"].append(f"{rel(path)}:{scan.line_of(text, m.start())}")
    return out


def cmd_mis_context(args) -> tuple[int, dict]:
    census = mis_context_census(args.dir)
    payload = {k: (v if k == "style" else len(v)) for k, v in census.items()}
    errors = []
    if args.expect_static:
        for spec in args.expect_static:
            key, _, want = spec.partition("=")
            want_n = int(want)
            got_n = len(census.get(key, []))
            if got_n != want_n:
                errors.append(f"{key}: expected {want_n}, got {got_n}")
    if args.expect_set_from:
        baseline = json.loads(Path(args.expect_set_from).read_text(encoding="utf-8"))
        expected_style_set = set(baseline.get("mis_context", {}).get("style_set", []))
        got_style_set = set(census["style"])
        if expected_style_set and got_style_set != expected_style_set:
            errors.append(
                f"style= set changed: added={sorted(got_style_set - expected_style_set)} "
                f"removed={sorted(expected_style_set - got_style_set)}"
            )
    if errors:
        return 1, {**payload, "errors": errors}
    return 0, payload


def cmd_app3309(args) -> tuple[int, dict]:
    path = args.dir / "app.js"
    if not path.is_file():
        return 2, {"error": f"{path} not found"}
    text = path.read_text(encoding="utf-8", errors="ignore")
    spans = [s for s in scan.find_handler_spans(text) if s.line == 3309]
    if not spans:
        return 1, {"error": "no handler span at app.js:3309"}
    span = spans[0]
    if span.quote != '"':
        return 1, {"error": f"expected double-quoted outer attribute (Phase 6 switches app.js:3309's delimiter for consistency), got {span.quote!r}"}
    has_escape_html_call = bool(re.search(r"escapeHtml\s*\(\s*JSON\.stringify", span.raw_value))
    has_replace = ".replace(" in span.raw_value and "escapeHtml" in span.raw_value
    if not has_escape_html_call or has_replace:
        return 1, {
            "error": "app.js:3309 does not use escapeHtml(JSON.stringify(config)) with no .replace",
            "raw_value": span.raw_value,
        }
    return 0, {"raw_value": span.raw_value}


def cmd_display_shape(args) -> tuple[int, dict]:
    # No converted span may show a double-escaped output shape (e.g.
    # `escapeJsAttr(escapeHtml(x))` or `escapeHtml(escapeJsAttr(x))`).
    violations = []
    interps, _builders = all_interpolations(args.dir)
    for i in interps:
        if re.match(r"^(escapeHtml|escapeJsAttr)\s*\(\s*(escapeHtml|escapeJsAttr)\s*\(", i["expr"]):
            violations.append(i)
    return (1 if violations else 0), {"violations": violations}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--class",
        dest="klass",
        choices=["wrong-primitive", "unescaped-quoted", "bare-expression", "correct"],
        default=None,
    )
    ap.add_argument(
        "--check",
        choices=[
            "builder-set",
            "no-nested-replace",
            "no-handrolled-escape",
            "jsattr-confinement",
            "mis-context",
            "app3309",
            "display-shape",
        ],
        default=None,
    )
    ap.add_argument("--scope", choices=["builder", "direct"], default=None)
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--require-population-seen", type=int, default=None)
    ap.add_argument("--expect", action="append", default=None)
    ap.add_argument("--expect-static", action="append", default=None)
    ap.add_argument("--expect-set-from", default=None)
    ap.add_argument("--dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.dir.is_dir():
        print(f"ERROR: {args.dir} is not a directory", file=sys.stderr)
        return 2

    if args.klass:
        rc, payload = cmd_class(args)
        label = args.klass
    elif args.check == "builder-set":
        rc, payload = cmd_builder_set(args)
        label = args.check
    elif args.check == "no-nested-replace":
        rc, payload = cmd_no_nested_replace(args)
        label = args.check
    elif args.check == "no-handrolled-escape":
        rc, payload = cmd_no_handrolled_escape(args)
        label = args.check
    elif args.check == "jsattr-confinement":
        rc, payload = cmd_jsattr_confinement(args)
        label = args.check
    elif args.check == "mis-context":
        rc, payload = cmd_mis_context(args)
        label = args.check
    elif args.check == "app3309":
        rc, payload = cmd_app3309(args)
        label = args.check
    elif args.check == "display-shape":
        rc, payload = cmd_display_shape(args)
        label = args.check
    else:
        print("ERROR: one of --class or --check is required", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        status = {0: "PASS", 1: "FAIL", 2: "ERROR"}[rc]
        print(f"{status} [{label}]: {json.dumps(payload)[:2000]}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
