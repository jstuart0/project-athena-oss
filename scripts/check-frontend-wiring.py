#!/usr/bin/env python3
"""D7 — three-way `.js` <-> Dockerfile `COPY` <-> `<script>` tag parity.

Does NOT re-implement load-order or buster-freshness checks: those already
exist (`scripts/check-frontend-escape-load-order.js`, D1;
`scripts/check-frontend-cache-busters.py`, D5) and are invoked as their own
CI steps. Rule 11 — one assertion, one implementation; round 2 planned to
rebuild both under new names and that is exactly the failure this rule
closes. This script omits those checks entirely rather than duplicating
them.

Handles the `COPY auth.js /usr/share/nginx/html/admin-auth.js` rename —
without it, `admin-auth.js` looks like a third dangling tag (it is real:
`Dockerfile:8`), and `auth.js` looks like an orphaned source file (it isn't:
it is deployed under a different name).

The "dangling" set is `<script>` tags whose referenced filename has no
corresponding DEPLOYED file (after applying Dockerfile renames) — verified
today to be exactly `{mode-audit.js, notifications.js}`, tags for files that
don't exist yet (ATHENA-62). Pinned by set-equality (rule 8): a THIRD
dangling tag appearing is a regression; either of the two being fixed (a
file finally landing) must also update this guard, which is the point.

Exit codes
----------
    0  clean for the requested --check
    1  violation(s) found
    2  could not run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRONTEND_DIR = REPO_ROOT / "admin" / "frontend"

EXPECTED_DANGLING = {"mode-audit.js", "notifications.js"}

COPY_RE = re.compile(r"^COPY\s+([\w.-]+\.js)\s+\S*/([\w.-]+\.js)\s*$", re.MULTILINE)
TAG_RE = re.compile(r'<script\s+src="/([\w.-]+\.js)(\?v=([^"]*))?"')


def source_files(frontend_dir: Path) -> set[str]:
    return {p.name for p in frontend_dir.glob("*.js")}


def dockerfile_renames(frontend_dir: Path) -> dict[str, str]:
    """Map SOURCE filename -> DEPLOYED filename, from Dockerfile COPY lines."""
    dockerfile = frontend_dir / "Dockerfile"
    if not dockerfile.is_file():
        return {}
    text = dockerfile.read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for m in COPY_RE.finditer(text)}


def index_html_tags(frontend_dir: Path) -> list[tuple[str, str | None]]:
    """List of (deployed_name, buster_or_None) for every local <script> tag."""
    index_html = frontend_dir / "index.html"
    if not index_html.is_file():
        return []
    text = index_html.read_text(encoding="utf-8")
    return [(m.group(1), m.group(3)) for m in TAG_RE.finditer(text)]


def compute_parity(frontend_dir: Path) -> dict:
    sources = source_files(frontend_dir)
    renames = dockerfile_renames(frontend_dir)
    deployed_names = {renames.get(name, name) for name in sources}
    tags = index_html_tags(frontend_dir)
    tagged_names = {name for name, _buster in tags}

    dangling = sorted(tagged_names - deployed_names)
    # A source file is "orphaned" (copied but never tagged) if its deployed
    # name has no matching tag at all.
    orphaned_deployed = sorted(deployed_names - tagged_names)

    return {
        "sources": sorted(sources),
        "deployed_names": sorted(deployed_names),
        "tagged_names": sorted(tagged_names),
        "dangling": dangling,
        "orphaned_deployed": orphaned_deployed,
        "renames": renames,
    }


def cmd_three_way(args) -> tuple[int, dict]:
    parity = compute_parity(args.dir)
    payload = {
        "dangling": parity["dangling"],
        "orphaned_deployed": parity["orphaned_deployed"],
        "renames": parity["renames"],
    }
    errors = []
    if set(parity["dangling"]) != EXPECTED_DANGLING:
        errors.append(
            f"dangling set {parity['dangling']} != expected {sorted(EXPECTED_DANGLING)}"
        )
    if parity["orphaned_deployed"]:
        errors.append(f"deployed files with no <script> tag: {parity['orphaned_deployed']}")
    if errors:
        return 1, {**payload, "errors": errors}
    return 0, payload


def cmd_buster_presence(args) -> tuple[int, dict]:
    tags = index_html_tags(args.dir)
    parity = compute_parity(args.dir)
    missing = [
        name
        for name, buster in tags
        if buster is None and name not in EXPECTED_DANGLING and name != "admin-auth.js"
    ]
    payload = {"missing_buster": missing}
    return (1 if missing else 0), payload


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", choices=["three-way", "buster-presence"], default="three-way")
    ap.add_argument("--dir", type=Path, default=DEFAULT_FRONTEND_DIR)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.dir.is_dir():
        print(f"ERROR: {args.dir} is not a directory", file=sys.stderr)
        return 2

    handlers = {"three-way": cmd_three_way, "buster-presence": cmd_buster_presence}
    rc, payload = handlers[args.check](args)

    if args.json:
        print(json.dumps(payload, separators=(",", ":")))
    else:
        status = {0: "PASS", 1: "FAIL"}[rc]
        print(f"{status} [{args.check}]: {json.dumps(payload)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
