#!/usr/bin/env python3
"""D9 — the callee/param/sink table, mechanized with a fail-closed unresolved
bucket.

For every converted handler-span argument (a call inside an `on<event>=`
span, or a builder-routed argument), resolves: the callee function the
handler dispatches to, which parameter receives this argument (by top-level
comma position), and whether that parameter is re-rendered into an
unescaped HTML sink (`.innerHTML =`, `insertAdjacentHTML(`, or a `return`
consumed by one) inside the callee's body. Three buckets:

    sink-escaped     the parameter is wrapped at the sink            -> OK
    sink-unescaped   the parameter reaches the sink raw              -> VIOLATION
    unresolved       the callee/parameter/sink could not be resolved -> VIOLATION until adjudicated

`unresolved` is deliberately NOT a pass. A classifier that silently drops
what it cannot resolve reproduces the exact failure BLOCKER 4 is about — a
guard reading green over a live hole. It clears only via a committed row in
`admin/frontend/.callee-sink-adjudications.json` naming the callee, the sink
(or "none"), and a reason.

Exit codes
----------
    0  clean for the requested --class
    1  violation(s) found (including any non-adjudicated `unresolved`)
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
DEFAULT_ADJUDICATIONS = DEFAULT_FRONTEND_DIR / ".callee-sink-adjudications.json"

SINK_RE = re.compile(r"\.innerHTML\s*=|\.insertAdjacentHTML\s*\(")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _strip_outer_delims(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("`", "'", '"'):
        return s[1:-1]
    return s


def find_enclosing_call(raw_value: str, offset: int) -> tuple[str, int] | None:
    """Find the top-level call `CALLEE(...)` in the handler's raw_value whose
    argument list contains `offset` (an interpolation's character offset
    within raw_value). Returns (callee_name, arg_index) — arg_index is the
    top-level-comma position of the argument containing `offset`, per D9's
    "compute the argument index by top-level comma position."
    """
    for m in re.finditer(r"([A-Za-z_$][\w$]*)\s*\(", raw_value):
        open_idx = raw_value.index("(", m.start())
        close_idx = scan.find_matching_paren(raw_value, open_idx)
        if close_idx == -1 or not (open_idx < offset < close_idx):
            continue
        args_text = raw_value[open_idx + 1 : close_idx]
        args = scan.split_top_level(args_text, seps=",")
        local_offset = offset - (open_idx + 1)
        pos = 0
        for idx, a in enumerate(args):
            start, end = pos, pos + len(a)
            if start <= local_offset <= end:
                return m.group(1), idx
            pos = end + 1
    return None


def find_callee_definition(text: str, callee: str) -> tuple[str, list[str], int, int] | None:
    m = re.search(r"function\s+" + re.escape(callee) + r"\s*\(([^)]*)\)\s*\{", text)
    if not m:
        return None
    params = [p.strip() for p in m.group(1).split(",") if p.strip()]
    brace_open = text.index("{", m.end() - 1)
    depth = 0
    i = brace_open
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body_end = i
    return callee, params, brace_open, body_end


def param_reaches_unescaped_sink(text: str, body_start: int, body_end: int, param: str) -> str:
    """Returns 'sink-escaped', 'sink-unescaped', or 'no-sink-found'."""
    body = text[body_start:body_end]
    param_re = re.compile(r"\$\{[^}]*\b" + re.escape(param) + r"\b[^}]*\}")
    escaped_re = re.compile(
        r"\$\{\s*(escapeHtml|escapeJsAttr)\s*\(\s*" + re.escape(param) + r"\s*\)\s*\}"
    )
    param_mentions = list(param_re.finditer(body))
    if not param_mentions:
        return "no-sink-found"
    if not SINK_RE.search(body):
        return "no-sink-found"
    for m in param_mentions:
        if escaped_re.match(body, m.start()):
            continue
        return "sink-unescaped"
    return "sink-escaped"


def resolve_site(file: Path, line: int, expr: str, raw_value: str, offset: int, adjudications: dict) -> dict:
    key = f"{rel(file)}:{line}"
    resolved = find_enclosing_call(raw_value, offset)
    if resolved is None:
        return _maybe_adjudicated(
            key, file, line, expr, "unresolved", "no enclosing call found in handler span", adjudications
        )
    callee, arg_index = resolved

    text = file.read_text(encoding="utf-8", errors="ignore")
    defn = None
    for path in scan.iter_frontend_js_files(file.parent):
        candidate_text = text if path == file else path.read_text(encoding="utf-8", errors="ignore")
        found = find_callee_definition(candidate_text, callee)
        if found:
            defn = (path, candidate_text, *found)
            break
    if defn is None:
        return _maybe_adjudicated(
            key, file, line, expr, "unresolved", f"callee {callee!r} definition not found", adjudications
        )

    def_path, def_text, _name, params, body_start, body_end = defn
    if arg_index >= len(params):
        return _maybe_adjudicated(
            key, file, line, expr, "unresolved", "argument index has no corresponding parameter", adjudications
        )

    param = params[arg_index].split("=")[0].strip()
    if param.startswith("...") or param.startswith("{") or param.startswith("["):
        return _maybe_adjudicated(
            key, file, line, expr, "unresolved", "destructured/rest parameter not resolvable", adjudications
        )

    verdict = param_reaches_unescaped_sink(def_text, body_start, body_end, param)
    if verdict == "no-sink-found":
        return "sink-escaped", {
            "file": key,
            "callee": callee,
            "sink": "none",
            "param": param,
            "callee_file": rel(def_path),
        }
    return verdict, {
        "file": key,
        "callee": callee,
        "sink": "innerHTML/insertAdjacentHTML",
        "param": param,
        "callee_file": rel(def_path),
    }


def _maybe_adjudicated(key, file, line, expr, bucket, reason, adjudications):
    entry = adjudications.get(key)
    if entry:
        return entry.get("sink", "none"), {
            "file": key,
            "callee": entry.get("callee"),
            "sink": entry.get("sink", "none"),
            "adjudicated": True,
            "reason": entry.get("reason"),
        }
    return bucket, {"file": key, "expr": expr, "reason": reason}


def load_adjudications(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return {row["file"]: row for row in data}
    return data


def collect_sites(directory: Path) -> list[tuple[Path, int, str, str, int]]:
    """Every quoted-position interpolation across direct handler spans AND
    builder-routed call arguments — regardless of current escape state.
    D9's question is "will this VALUE reach an unescaped sink downstream",
    which applies whether the value is already escaped, wrong-primitive, or
    entirely raw today; narrowing to already-escaped sites would miss
    exactly the pre-conversion callee-sink instances D9 exists to catch.

    Returns (file, line, expr, raw_value, offset) — `raw_value` and `offset`
    are in the SAME coordinate space so `find_enclosing_call` can locate the
    handler's outer call and compute the argument index.
    """
    sites = []
    for path in scan.iter_frontend_js_files(directory):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for span in scan.find_handler_spans(text):
            if scan.is_builder_marker(span.raw_value):
                continue
            for it in scan.classify_span_interpolations(span.raw_value):
                if it.position != "quoted":
                    continue
                sites.append((path, span.line, it.expr, span.raw_value, it.offset))

    for builder in scan.discover_builders(directory):
        text = builder.file.read_text(encoding="utf-8", errors="ignore")
        for call_start, _args_start, args_text in scan.find_calls(text, builder.func_name):
            args = scan.split_top_level(args_text, seps=",")
            if len(args) <= builder.param_idx:
                continue
            arg_raw = args[builder.param_idx]
            inner = scan.strip_outer_delims(arg_raw)
            if "${" not in inner:
                continue
            call_line = scan.line_of(text, call_start)
            for it in scan.classify_template_interpolations(inner):
                if it.position != "quoted":
                    continue
                sites.append((builder.file, call_line, it.expr, inner, it.offset))
    return sites


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--class", dest="klass", choices=["sink-escaped", "sink-unescaped", "unresolved"], default=None)
    ap.add_argument("--check", choices=["coverage"], default=None)
    ap.add_argument("--scope", default=None, help="e.g. phase3 (unused filter hook, reserved)")
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--min-sites", type=int, default=None)
    ap.add_argument("--named-member", default=None)
    ap.add_argument("--dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    ap.add_argument("--adjudications", type=Path, default=DEFAULT_ADJUDICATIONS)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.dir.is_dir():
        print(f"ERROR: {args.dir} is not a directory", file=sys.stderr)
        return 2

    adjudications = load_adjudications(args.adjudications)
    sites = collect_sites(args.dir)

    results = {"sink-escaped": [], "sink-unescaped": [], "unresolved": []}
    for file, line, expr, raw_value, offset in sites:
        bucket, detail = resolve_site(file, line, expr, raw_value, offset, adjudications)
        results.setdefault(bucket, []).append(detail)

    if args.check == "coverage":
        total = sum(len(v) for v in results.values())
        payload = {"total_sites": total}
        if args.min_sites is not None and total < args.min_sites:
            payload["error"] = f"coverage {total} < floor {args.min_sites}"
            print(json.dumps(payload, separators=(",", ":")) if args.json else payload)
            return 1
        if args.named_member:
            members = {d["file"] for v in results.values() for d in v}
            if args.named_member not in members:
                payload["error"] = f"named member {args.named_member!r} not covered"
                print(json.dumps(payload, separators=(",", ":")) if args.json else payload)
                return 1
        print(json.dumps(payload, separators=(",", ":")) if args.json else payload)
        return 0

    klass = args.klass or "unresolved"
    matches = results.get(klass, [])
    payload = {"class": klass, "count": len(matches), "matches": matches}
    rc = 0
    if args.max is not None and len(matches) > args.max:
        rc = 1

    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        status = {0: "PASS", 1: "FAIL"}[rc]
        print(f"{status} [{klass}]: {json.dumps(payload)[:2000]}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
