#!/usr/bin/env python3
"""Assert every admin/frontend/*.js file changed on this branch has a BUMPED cache-buster.

Why this exists
---------------
ATHENA-67 changed `guest-context.js` and `memory-context.js` to close two live XSS, but left their
`<script>` tags in `index.html` untouched — `memory-context.js?v=20260108` and a bare
`guest-context.js`. `admin/frontend/nginx.conf` serves `.js` with `Cache-Control: public, immutable`,
which per RFC 8246 means the browser does not revalidate **even on a user-initiated reload**.

So the security fix shipped, passed every test, and did not reach any browser holding a warm cache —
for up to an hour, with no recovery path the operator could trigger. The new file `escape-html.js` WAS
fetched (new URL, no cache history), so the served page was a *mixed vintage*: a canonical global that
worked, next to a cached `memory-context.js` still carrying its vulnerable closure-local `escapeHtml`.

Changing the file is not the deliverable. Changing what the browser executes is the deliverable.
A file whose URL did not change is, to every warm client, not changed at all.

This is a gate, not a linter: it exits non-zero and says exactly which file is stale.

Usage
-----
    check-frontend-cache-busters.py [--base <ref>]      # default: origin/main, falls back to main

Exit codes
----------
    0  every changed .js carries a buster that differs from its value on <base>
    1  at least one changed .js is stale or unbustered   (findings exist)
    2  the check could not run   (bad ref, git failure, missing index.html)

Exit 2 is deliberately distinct from exit 1: a check that could not run must never be read as a pass.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_HTML = REPO_ROOT / "admin" / "frontend" / "index.html"


def die(msg: str) -> "None":
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if proc.returncode != 0:
        die(f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def resolve_base(requested: "str | None") -> str:
    candidates = [requested] if requested else ["origin/main", "main"]
    for ref in candidates:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return ref
    die(f"none of these refs resolve: {', '.join(c for c in candidates if c)}")


def buster_for(html: str, basename: str) -> "str | None | bool":
    """Return the ?v= value, None if the tag exists unbustered, or False if there is no tag."""
    m = re.search(
        r'<script\s+src="/' + re.escape(basename) + r'(\?v=([^"]*))?"', html
    )
    if not m:
        return False
    return m.group(2) if m.group(1) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="base ref to diff against (default: origin/main, then main)")
    args = ap.parse_args()

    if not INDEX_HTML.is_file():
        die(f"{INDEX_HTML} not found")

    base = resolve_base(args.base)
    merge_base = git("merge-base", base, "HEAD").strip()
    if not merge_base:
        die(f"could not compute merge-base against {base}")

    changed = [
        line.strip()
        for line in git(
            "diff", "--name-only", f"{merge_base}..HEAD", "--", "admin/frontend/*.js"
        ).splitlines()
        if line.strip()
    ]

    if not changed:
        print(f"No admin/frontend/*.js changed vs {base} ({merge_base[:8]}). Nothing to check.")
        return 0

    head_html = INDEX_HTML.read_text(encoding="utf-8")
    base_html = git("show", f"{merge_base}:admin/frontend/index.html")

    findings = []
    for path in sorted(changed):
        name = Path(path).name
        head_v = buster_for(head_html, name)
        base_v = buster_for(base_html, name)

        if head_v is False:
            # No tag at all. Not every .js is a top-level entry point, so this is informational.
            print(f"  --  {name}: no <script> tag in index.html (not an entry point) — skipped")
            continue
        if head_v is None:
            findings.append(
                f"{name}: tag has NO ?v= cache-buster. A warm-cache browser keeps the old file."
            )
            continue
        if base_v is not False and head_v == base_v:
            findings.append(
                f"{name}: ?v={head_v} is UNCHANGED from base. The URL is the same, so to every "
                f"warm-cache client this file did not change — and nginx sends 'immutable', so a "
                f"reload will not recover it."
            )
            continue
        print(f"  OK  {name}: ?v={base_v or '(none)'} -> ?v={head_v}")

    if findings:
        print(f"\nFAIL: {len(findings)} changed file(s) will not reach warm-cache clients:\n")
        for f in findings:
            print(f"  - {f}")
        print(
            "\nFix: bump the ?v= value on each tag in admin/frontend/index.html.\n"
            "Changing the file is not the deliverable; changing what the browser executes is."
        )
        return 1

    print(f"\nPASS: every admin/frontend/*.js changed vs {base} carries a bumped cache-buster.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
