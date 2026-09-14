#!/usr/bin/env python3
"""D14 — classify HTML-template interpolations by POSITION, not by sink name.

`emerging-intents.js:231` is the reason this exists: it is raw, it is the
most reachable interpolation in its cluster, and its sink (`container.
innerHTML` at `:82`) is THREE FUNCTIONS AWAY from the interpolation site. A
guard matching `insertAdjacentHTML` or `.innerHTML =` on the same line finds
neither `:231` nor its sibling `:353` — the escaping decision has to be made
where the markup is assembled, which is knowable from the interpolation's
POSITION inside the template literal, independent of where that literal is
eventually assigned.

Buckets, per `${...}` interpolation inside a backtick template literal that
contains HTML markup (`<tag` ... `>`):

    text-node          between tags, e.g. `<p>${x}</p>`
    quoted-attribute   inside a plain (non-`on*=`) quoted attribute value
    handler-span       inside an `on*=` attribute — delegated to
                        check-handler-escaping.py, not counted here
    inert              numeric/`.length`/arithmetic — safe by construction

Over-approximation is deliberate and safe here (documented tradeoff): a
false positive costs one redundant `escapeHtml()` call; a false negative
costs an XSS. The asymmetry is total.

Exit codes
----------
    0  clean for the requested --class/--check
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

HTML_MARKER_RE = re.compile(r"<[a-zA-Z][\w-]*[\s/>]")
ATTR_OPEN_RE = re.compile(r"([\w-]+)\s*=\s*(['\"])")
INERT_RE = re.compile(r"^[\d\s+\-*/().]+$|\.length$|^Math\.\w+\(")
INNERHTML_ASSIGN_RE = re.compile(r"\.innerHTML\s*=(?!=)")
FORBIDDEN_SINK_RE = re.compile(r"\.outerHTML\s*=|\bdocument\.write\s*\(")


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def find_backtick_templates(text: str) -> list[tuple[int, int]]:
    """(start, end) pairs — start points just after the opening backtick,
    end at the closing backtick. `${...}` regions inside are skipped
    (brace-counted, quote-agnostic — see `_frontend_escape_scan` for why)
    so a nested backtick inside an interpolation doesn't terminate early.
    """
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "`":
            start = i + 1
            j = start
            while j < n:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    j += 2
                    continue
                if c == "$" and j + 1 < n and text[j + 1] == "{":
                    depth = 1
                    j += 2
                    while j < n and depth > 0:
                        cc = text[j]
                        if cc == "\\" and j + 1 < n:
                            j += 2
                            continue
                        if cc == "{":
                            depth += 1
                        elif cc == "}":
                            depth -= 1
                        j += 1
                    continue
                if c == "`":
                    out.append((start, j))
                    i = j + 1
                    break
                j += 1
            else:
                i = j
                continue
        else:
            i += 1
    return out


def classify_position(body: str, offset: int) -> str:
    last_open = body.rfind("<", 0, offset)
    if last_open == -1:
        return "text-node"
    tag_segment = body[last_open:offset]
    if ">" in tag_segment:
        # A '>' appears between the last '<' and here — normally that means
        # the tag already closed and we're back in text. (Edge case: a '>'
        # inside a quoted attribute value earlier in the tag; not handled —
        # over-approximation toward text-node is the safe direction.)
        last_gt = tag_segment.rfind(">")
        remainder = tag_segment[last_gt:]
        if "<" not in remainder:
            return "text-node"
        tag_segment = remainder
    attr_match = None
    for m in ATTR_OPEN_RE.finditer(tag_segment):
        attr_match = m
    if attr_match is None:
        return "inert"
    quote = attr_match.group(2)
    after_open = tag_segment[attr_match.end() :]
    if quote in after_open:
        return "inert"
    attr_name = attr_match.group(1)
    if re.match(r"on[a-z]+$", attr_name, re.IGNORECASE):
        return "handler-span"
    return "quoted-attribute"


def collect(directory: Path, file_filter: Path | None) -> list[dict]:
    out = []
    files = [file_filter] if file_filter else scan.iter_frontend_js_files(directory)
    for path in files:
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for start, end in find_backtick_templates(text):
            body = text[start:end]
            if not HTML_MARKER_RE.search(body):
                continue
            for b_start, b_end in scan.find_top_level_dollar_braces(body):
                expr = body[b_start + 2 : b_end - 1].strip()
                bucket = classify_position(body, b_start)
                if bucket in ("text-node", "quoted-attribute") and INERT_RE.match(expr):
                    bucket = "inert"
                out.append(
                    {
                        "file": rel(path),
                        "line": scan.line_of(text, start + b_start),
                        "expr": expr[:80],
                        "bucket": bucket,
                        "escaped": bool(re.match(r"^(escapeHtml|escapeJsAttr)\s*\(", expr)),
                    }
                )
    return out


def cmd_class(args) -> tuple[int, dict]:
    if args.klass == "innerhtml-sink":
        matches = []
        files = [args.file] if args.file else scan.iter_frontend_js_files(args.dir)
        for path in files:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in INNERHTML_ASSIGN_RE.finditer(text):
                matches.append({"file": rel(path), "line": scan.line_of(text, m.start())})
        payload = {"class": args.klass, "count": len(matches)}
        rc = 1 if (args.max is not None and len(matches) > args.max) else 0
        return rc, payload

    items = collect(args.dir, args.file)
    if args.klass == "data-attribute":
        matches = [i for i in items if i["bucket"] == "quoted-attribute" and not i["escaped"]]
    else:
        matches = [i for i in items if i["bucket"] == args.klass and not i["escaped"]]
    payload = {"class": args.klass, "count": len(matches), "matches": matches[:50]}
    rc = 1 if (args.max is not None and len(matches) > args.max) else 0
    return rc, payload


def cmd_forbidden_sink(args) -> tuple[int, dict]:
    matches = []
    for path in scan.iter_frontend_js_files(args.dir):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in FORBIDDEN_SINK_RE.finditer(text):
            matches.append({"file": rel(path), "line": scan.line_of(text, m.start())})
    return (1 if matches else 0), {"matches": matches}


def cmd_primitive_matches_position(args) -> tuple[int, dict]:
    items = collect(args.dir, args.file)
    violations = []
    for i in items:
        if i["bucket"] == "text-node" and i["expr"].startswith("escapeJsAttr("):
            violations.append(i)
        if i["bucket"] == "quoted-attribute" and i["expr"].startswith("escapeJsAttr("):
            violations.append(i)
    return (1 if violations else 0), {"violations": violations}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--class",
        dest="klass",
        choices=["text-node", "quoted-attribute", "data-attribute", "inert", "innerhtml-sink"],
        default=None,
    )
    ap.add_argument("--check", choices=["forbidden-sink", "primitive-matches-position"], default=None)
    ap.add_argument("--file", type=Path, default=None)
    ap.add_argument("--dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    ap.add_argument("--max", type=int, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.file and not args.file.is_absolute():
        args.file = REPO_ROOT / args.file

    if args.klass:
        rc, payload = cmd_class(args)
        label = args.klass
    elif args.check == "forbidden-sink":
        rc, payload = cmd_forbidden_sink(args)
        label = args.check
    elif args.check == "primitive-matches-position":
        rc, payload = cmd_primitive_matches_position(args)
        label = args.check
    else:
        print("ERROR: one of --class or --check is required", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        status = {0: "PASS", 1: "FAIL"}[rc]
        print(f"{status} [{label}]: {json.dumps(payload)[:2000]}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
