#!/usr/bin/env python3
"""D6 — assert exactly one escapeHtml/escapeJsAttr implementation survives.

Why a script, not a grep
-------------------------
Four DEFINITION forms must be caught (function declaration, assignment to a
function expression/arrow, getter, `Object.defineProperty`, computed-name
assignment) while two EXPORT forms must NOT be flagged (`window.escapeHtml =
escapeHtml;` — bare-identifier RHS; `escapeHtml,` — object-literal shorthand,
`utils.js:531`). A regex keyed only on the name cannot express "assignment
whose RHS is a function expression" without also flagging the canonical
file's own re-exports.

A SECOND, independent signal (librarian H) catches a hand-rolled entity map
under an UNRELATED name (`htmlEncode`, `sanitizeLabel`, ...) — neither a name
match nor a DOM round-trip. That is how this codebase got 20 implementations.
The entity-map body-shape scan fires on ANY function whose body contains 3+
`.replace(/.../g, '&...;')` calls, under any name, EXCLUDING the canonical
file BY PATH (rule 12) — the canonical implementation *is* a 5-call entity
map, so "no entity map survives under any name" is unsatisfiable as written;
only "outside admin/frontend/escape-html.js" is satisfiable.

Exit codes
----------
    0  clean (no violations for the requested --check)
    1  violation(s) found
    2  could not run (bad args, missing files)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRONTEND_DIR = REPO_ROOT / "admin" / "frontend"
BASELINE_PATH = DEFAULT_FRONTEND_DIR / ".escaping-baseline.json"
CANONICAL_FILE = "escape-html.js"

NAMES = ("escapeHtml", "escapeJsAttr")

FN_DECL_RE = re.compile(r"function\s+(escapeHtml|escapeJsAttr)\s*\(")
ASSIGN_FN_RE = re.compile(
    r"(?:const|let|var|window\.|global\.|self\.)?\s*\b(escapeHtml|escapeJsAttr)\b\s*=\s*(function\b|\([^)]*\)\s*=>|\w[\w.]*\s*=>)"
)
GETTER_RE = re.compile(r"get\s+(escapeHtml|escapeJsAttr)\s*\(\s*\)\s*\{")
DEFINE_PROPERTY_RE = re.compile(
    r"Object\.defineProperty\s*\(\s*[\w.]+\s*,\s*['\"](escapeHtml|escapeJsAttr)['\"]\s*,\s*\{[^}]*\bvalue\s*:"
)
COMPUTED_NAME_RE = re.compile(
    r"[\w.]+\[\s*['\"](escapeHtml|escapeJsAttr)['\"]\s*\]\s*=\s*(function\b|\([^)]*\)\s*=>|\w[\w.]*\s*=>)"
)

# Entity-map body shape: 3+ `.replace(/.../g, '&...;')`-shaped calls inside
# one function body, under ANY name.
ENTITY_REPLACE_RE = re.compile(r"\.replace\(\s*/[^/]*/\s*g?\s*,\s*['\"]&[^'\"]*;['\"]")
FUNCTION_BODY_RE = re.compile(r"function\s+([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", re.MULTILINE)

DOM_ROUNDTRIP_MARKERS = ("createElement", "textContent")
# The escape-trick's third, DEFINING marker: the encoded string is obtained
# by READING .innerHTML back (a getter), not by SETTING it. `createElement`
# + `textContent` alone are common, entirely safe DOM-building idioms (a
# toast helper, a generic element factory) that coincidentally contain both
# substrings without ever being an escape implementation. Phase 5 (D6):
# verified false positives on showSuccess/showError (app.js), open
# (drawer.js), updateSessionFilter/showCreateMemoryModal/showEditMemoryModal
# (memory-management.js), showCreateRoomModal/showEditRoomModal
# (room-audio.js), copyUserApiKey (user-api-keys.js), showNotification/
# createElement (utils.js), injectVoiceConfigStyles (voice-config.js) --
# none of these ever reads .innerHTML back; all of them only ever WRITE it
# or never touch it. `(?!\s*=(?!=))` excludes `.innerHTML == ` /
# `.innerHTML === ` comparisons from being mistaken for an assignment.
INNERHTML_READ_RE = re.compile(r"\breturn\s+[\w.\[\]'\"]+\.innerHTML\b\s*(?!=(?!=))")


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def _matching_brace(text: str, open_idx: int) -> int:
    """Pure brace counting, deliberately NOT quote-aware. A naive "skip to
    the matching quote" scan is defeated by a `/'/`-shaped regex literal
    (verified: the canonical escapeHtml body itself contains `.replace(/'/g,
    ...)`) — the same failure mode documented in
    `_frontend_escape_scan.scan_attr_value`. Only breaks on an unbalanced
    `{`/`}` inside a nested string, which does not occur in this codebase's
    escaping-function bodies.
    """
    depth = 0
    i = open_idx
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
                return i
        i += 1
    return -1


def iter_js_files(directory: Path):
    return sorted(directory.glob("*.js"))


def find_definitions(directory: Path) -> list[dict]:
    """Name-matched definitions of escapeHtml/escapeJsAttr (the 4 real forms,
    excluding the 2 export-only forms whose RHS is a bare identifier)."""
    out = []
    for path in iter_js_files(directory):
        text = path.read_text(encoding="utf-8", errors="ignore")
        seen_lines = set()
        for regex, form in (
            (FN_DECL_RE, "function-declaration"),
            (ASSIGN_FN_RE, "assignment"),
            (GETTER_RE, "getter"),
            (DEFINE_PROPERTY_RE, "define-property"),
            (COMPUTED_NAME_RE, "computed-name"),
        ):
            for m in regex.finditer(text):
                ln = line_of(text, m.start())
                key = (ln, m.group(1))
                if key in seen_lines:
                    continue
                seen_lines.add(key)
                out.append(
                    {
                        "file": str(path.relative_to(REPO_ROOT)),
                        "line": ln,
                        "name": m.group(1),
                        "form": form,
                    }
                )
    out.sort(key=lambda d: (d["file"], d["line"]))
    return out


def classify_body_shape(text: str, name_start: int) -> str:
    brace_open = text.find("{", name_start)
    if brace_open == -1:
        return "unknown"
    brace_close = _matching_brace(text, brace_open)
    if brace_close == -1:
        return "unknown"
    body = text[brace_open:brace_close]
    if len(ENTITY_REPLACE_RE.findall(body)) >= 3:
        return "entity-map"
    if all(marker in body for marker in DOM_ROUNDTRIP_MARKERS) and INNERHTML_READ_RE.search(body):
        return "dom-round-trip"
    return "other"


def find_entity_map_any_name(directory: Path, exclude_paths: set[str]) -> list[dict]:
    """librarian H: a hand-rolled entity map under ANY name. Excludes the
    canonical file BY PATH (rule 12), not by shape — the canonical
    implementation IS a 5-call entity map.
    """
    out = []
    for path in iter_js_files(directory):
        rel = str(path.relative_to(REPO_ROOT)) if REPO_ROOT in path.parents or REPO_ROOT == path.parent else str(path)
        if path.name in exclude_paths or rel in exclude_paths or str(path) in exclude_paths:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in FUNCTION_BODY_RE.finditer(text):
            shape = classify_body_shape(text, m.end() - 1)
            if shape == "entity-map":
                out.append(
                    {
                        "file": str(path),
                        "line": line_of(text, m.start()),
                        "name": m.group(1),
                    }
                )
    return out


def find_dom_roundtrip_any_name(directory: Path, exclude_paths: set[str]) -> list[dict]:
    out = []
    for path in iter_js_files(directory):
        if path.name in exclude_paths or str(path) in exclude_paths:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in FUNCTION_BODY_RE.finditer(text):
            shape = classify_body_shape(text, m.end() - 1)
            if shape == "dom-round-trip":
                out.append(
                    {
                        "file": str(path),
                        "line": line_of(text, m.start()),
                        "name": m.group(1),
                    }
                )
    return out


def resolve_exclude_paths(directory: Path, exclude_arg: str | None) -> set[str]:
    excludes = {CANONICAL_FILE}
    if exclude_arg:
        p = Path(exclude_arg)
        excludes.add(p.name)
        excludes.add(str(p))
        excludes.add(str((REPO_ROOT / exclude_arg)))
    return excludes


def cmd_definitions(args) -> tuple[int, dict]:
    defs = find_definitions(args.dir)
    payload = {"definitions": defs, "count": len(defs)}
    if args.expect is not None and len(defs) != args.expect:
        return 1, payload
    if args.expect_in:
        bad = [d for d in defs if not d["file"].endswith(args.expect_in)]
        if bad:
            payload["unexpected_locations"] = bad
            return 1, payload
    return 0, payload


def cmd_body_shape(args) -> tuple[int, dict]:
    excludes = resolve_exclude_paths(args.dir, args.exclude_path)
    findings = find_dom_roundtrip_any_name(args.dir, excludes)
    return (1 if findings else 0), {"dom_round_trip": findings}


def cmd_entity_map_shape(args) -> tuple[int, dict]:
    excludes = resolve_exclude_paths(args.dir, args.exclude_path)
    findings = find_entity_map_any_name(args.dir, excludes)
    return (1 if findings else 0), {"entity_map": findings}


def cmd_baseline_floor(args) -> tuple[int, dict]:
    if not BASELINE_PATH.is_file():
        return 2, {"error": f"{BASELINE_PATH} does not exist"}
    try:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return 2, {"error": f"invalid JSON in {BASELINE_PATH}: {e}"}
    defs = baseline.get("definitions", [])
    if not defs:
        return 1, {"error": "definitions population is empty (vacuity)"}
    if args.min_definitions is not None and len(defs) < args.min_definitions:
        return 1, {
            "error": f"definitions count {len(defs)} < floor {args.min_definitions}"
        }
    if args.named_member:
        members = {f"{d['file']}:{d['line']}" for d in defs}
        if args.named_member not in members:
            return 1, {
                "error": f"named member {args.named_member!r} not present in baseline definitions",
                "members": sorted(members),
            }
    return 0, {"count": len(defs)}


def cmd_unresolved_callers(args) -> tuple[int, dict]:
    # Best-effort: every bare `escapeHtml(` / `escapeJsAttr(` call site must
    # be reachable — i.e. some definition of that name exists in the dir.
    defs = find_definitions(args.dir)
    have = {d["name"] for d in defs}
    missing = [n for n in NAMES if n not in have]
    return (1 if missing else 0), {"missing_definitions_for": missing}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--check",
        choices=["definitions", "body-shape", "entity-map-shape", "baseline-floor", "unresolved-callers"],
        default="definitions",
    )
    ap.add_argument("--dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    ap.add_argument("--exclude-path", default=None)
    ap.add_argument("--expect", type=int, default=None)
    ap.add_argument("--expect-in", "--in", dest="expect_in", default=None)
    ap.add_argument("--min-definitions", type=int, default=None)
    ap.add_argument("--named-member", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.dir.is_dir():
        print(f"ERROR: {args.dir} is not a directory", file=sys.stderr)
        return 2

    handlers = {
        "definitions": cmd_definitions,
        "body-shape": cmd_body_shape,
        "entity-map-shape": cmd_entity_map_shape,
        "baseline-floor": cmd_baseline_floor,
        "unresolved-callers": cmd_unresolved_callers,
    }
    rc, payload = handlers[args.check](args)

    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        status = {0: "PASS", 1: "FAIL", 2: "ERROR"}[rc]
        print(f"{status} [{args.check}]: {json.dumps(payload)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
