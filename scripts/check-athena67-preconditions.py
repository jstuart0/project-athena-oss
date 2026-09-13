#!/usr/bin/env python3
"""Assert ATHENA-67's delivered state matches what this campaign's plan describes.

Why this exists
----------------
Every population count in the ATHENA-66 plan is arithmetic on state observed
on the (at the time) unmerged `campaign/2026-09-13-deliver-athena-xss-hotfix`
branch. Measuring that state is one script; assuming it survived the merge
unchanged is how the next set of wrong inherited counts gets created.

This is a one-shot reconciliation gate, deleted in Phase 8 once the counts it
protects have been superseded by `.escaping-baseline.json` and the phases
built on it.

Exit codes
----------
    0  every precondition holds
    1  at least one precondition is violated  (findings exist)
    2  the check could not run  (missing files, no git, etc.)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIR = REPO_ROOT / "admin" / "frontend"


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def check_escape_html_js_exists(findings: list[str]) -> None:
    path = FRONTEND_DIR / "escape-html.js"
    if not path.is_file():
        findings.append(f"{path} does not exist")
        return
    text = path.read_text(encoding="utf-8")
    # An IIFE: the two `function` declarations must NOT be top-level — i.e.
    # each must be preceded (ignoring whitespace) by `(function (...` opening
    # the wrapper, not by a bare newline-then-`function` at column 0.
    top_level_fn = re.compile(r"^function\s+(escapeHtml|escapeJsAttr)\s*\(", re.MULTILINE)
    if top_level_fn.search(text):
        findings.append(
            f"{path} declares escapeHtml/escapeJsAttr at top level — not an IIFE"
        )
    if not re.search(r"\(function\s*\(\s*\w*\s*\)\s*\{", text):
        findings.append(f"{path} does not open with an IIFE wrapper `(function (...) {{`")
    if not re.search(r"\}\)\(\s*(window|this|global)\s*\)\s*;?\s*$", text.strip()):
        findings.append(
            str(path) + " does not close with an IIFE invocation `})(window);`"
        )


def check_escape_html_js_last_tag(findings: list[str]) -> None:
    index_html = FRONTEND_DIR / "index.html"
    if not index_html.is_file():
        findings.append(f"{index_html} does not exist")
        return
    html = index_html.read_text(encoding="utf-8")
    tags = re.findall(r'<script\s+src="/([^"]+)"\s*></script>', html)
    if not tags:
        findings.append(f"no local <script src=\"/...\"> tags found in {index_html}")
        return
    last = tags[-1].split("?")[0]
    if last != "escape-html.js":
        findings.append(
            f"escape-html.js is not the last local <script> tag in {index_html} "
            f"(last tag is {last!r})"
        )


def check_dockerfile_copies_escape_html(findings: list[str]) -> None:
    dockerfile = FRONTEND_DIR / "Dockerfile"
    if not dockerfile.is_file():
        findings.append(f"{dockerfile} does not exist")
        return
    text = dockerfile.read_text(encoding="utf-8")
    if not re.search(r"^COPY\s+escape-html\.js\s", text, re.MULTILINE):
        findings.append(f"{dockerfile} has no `COPY escape-html.js` line")


def check_escape_html_attr_absent(findings: list[str]) -> None:
    # Scoped to application source (admin/, apps/, src/) — not this guard
    # script's own tooling directory, which legitimately names the deleted
    # identifier as a string literal in its precondition check and its
    # commit message.
    scan_roots = [REPO_ROOT / "admin", REPO_ROOT / "apps", REPO_ROOT / "src"]
    for root in scan_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_dir() or path.suffix not in (".js", ".py", ".html"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "escapeHtmlAttr" in text:
                findings.append(
                    f"escapeHtmlAttr still present in {path.relative_to(REPO_ROOT)}"
                )


def check_memory_context_no_definitions(findings: list[str]) -> None:
    path = FRONTEND_DIR / "memory-context.js"
    if not path.is_file():
        findings.append(f"{path} does not exist")
        return
    text = path.read_text(encoding="utf-8")
    if re.search(r"function\s+escapeHtml\s*\(", text):
        findings.append(f"{path} still declares a local escapeHtml")


def check_guest_context_uses_escape_js_attr(findings: list[str]) -> None:
    path = FRONTEND_DIR / "guest-context.js"
    if not path.is_file():
        findings.append(f"{path} does not exist")
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 240 or "escapeJsAttr" not in lines[239]:
        findings.append(f"{path}:240 does not use escapeJsAttr")


def check_busters(findings: list[str]) -> None:
    index_html = FRONTEND_DIR / "index.html"
    if not index_html.is_file():
        return  # already reported above
    html = index_html.read_text(encoding="utf-8")
    for name in ("guest-context.js", "memory-context.js"):
        m = re.search(r'<script\s+src="/' + re.escape(name) + r'(\?v=([^"]*))?"', html)
        if not m or not m.group(2):
            findings.append(f"{name}'s <script> tag is missing a ?v= cache-buster")
        elif m.group(2) != "20260913":
            findings.append(
                f"{name}'s cache-buster is {m.group(2)!r}, expected '20260913' (ed8714f)"
            )


CHECKS = [
    check_escape_html_js_exists,
    check_escape_html_js_last_tag,
    check_dockerfile_copies_escape_html,
    check_escape_html_attr_absent,
    check_memory_context_no_definitions,
    check_guest_context_uses_escape_js_attr,
    check_busters,
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not FRONTEND_DIR.is_dir():
        die(f"{FRONTEND_DIR} not found")

    findings: list[str] = []
    for check in CHECKS:
        check(findings)

    if args.json:
        print(json.dumps({"findings": findings}, separators=(",", ":")))
    else:
        if findings:
            print(f"FAIL: {len(findings)} ATHENA-67 precondition(s) violated:")
            for f in findings:
                print(f"  - {f}")
        else:
            print("PASS: ATHENA-67's delivered state matches the plan's table.")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
