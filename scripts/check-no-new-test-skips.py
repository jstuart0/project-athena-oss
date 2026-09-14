#!/usr/bin/env python3
"""No test skip is added on this branch except the ones pinned here by exact identity.

Why a pinned set rather than a flat ban
---------------------------------------
A flat `! grep -q 'pytest.mark.skip'` over the branch diff is correct in its exit-code
handling but wrong in substance for this campaign, because ATHENA-66 Phase 1 step 8 is
*required* to add a skip — D3's replacement for the shipped module-level `skipif` that
turned a broken node interpreter into a silent module-wide pass (catalogue instance 7).

Banning the fix for the defect is not a gate, it is a contradiction. But dropping the
check, or loosening it to "ignore skips in these files", re-opens the hole the check
exists to close.

So this uses the same idiom the rest of the campaign uses for populations that are
legitimately non-zero — the builder set (D9b), the dangling-tag set (D7), the `style=`
mis-context set (D15): **pin the exact members and assert set equality.** A new skip
anywhere fails. A pinned skip that disappears also fails, because a silent narrowing is
how a pin stops meaning anything.

Each pinned entry carries the reason it is sanctioned and, crucially, why CI cannot
reach it. A skip CI can reach is not sanctionable at any count.

Scope note: this reads COMMITTED history (`git diff <merge-base>..HEAD`), which is what
CI evaluates. Uncommitted working-tree edits are invisible to it by design — so a
CAN-FAIL demonstration of this gate must commit the injected skip, not merely write it
to the working tree. (Doing the latter produces a false PASS, which is catalogue
instance 5 wearing a different hat.)

Usage
-----
    check-no-new-test-skips.py --base <ref>

Exit codes
----------
    0  the added-skip set exactly equals the pinned set
    1  set mismatch  (findings exist)
    2  the check could not run  (bad ref, git failure)

Exit 2 is deliberately distinct from exit 1: a check that could not run is never a pass.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Pinned sanctioned skips. Keyed by (file, marker-expression-substring) so a move within
# the file does not spuriously fail, but a change of mechanism does.
SANCTIONED = {
    (
        "tests/unit/test_admin_frontend_escaping.py",
        "pytestmark = pytest.mark.skip(",
    ): (
        "D3 / Phase 1 step 8. The developer-laptop path when no working node interpreter "
        "is found. UNREACHABLE IN CI: .github/workflows/frontend-escaping.yml exports "
        "ATHENA_REQUIRE_NODE=1, under which the same code path calls pytest.fail() "
        "(collection error, non-zero exit) instead of skipping — and the workflow "
        "separately fails if the word SKIPPED appears in the run output. This skip "
        "REPLACES the shipped skipif that silently passed on a present-but-erroring "
        "node (catalogue instance 7); it is the fix, not the defect."
    ),
    (
        "tests/unit/test_frontend_guard_scripts.py",
        "requires_node = pytest.mark.skipif(",
    ): (
        "Guards one fixture that shells out to node. UNREACHABLE IN CI for the same "
        "reason: the workflow resolves ATHENA_NODE_BIN from `command -v node` after "
        "actions/setup-node pins the version, so NODE_BIN is never None there."
    ),
}

SKIP_RE = re.compile(r"^\+.*pytest\.mark\.skip")


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="main", help="base ref (default: main)")
    args = ap.parse_args()

    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{args.base}^{{commit}}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        die(f"base ref does not resolve: {args.base}")

    mb = subprocess.run(
        ["git", "merge-base", args.base, "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if mb.returncode != 0:
        die(f"merge-base failed: {mb.stderr.strip()}")
    base_sha = mb.stdout.strip()

    diff = subprocess.run(
        ["git", "diff", f"{base_sha}..HEAD", "--", "tests/"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if diff.returncode != 0:
        die(f"git diff failed: {diff.stderr.strip()}")

    # Walk the diff tracking which file each added line belongs to.
    found = set()
    current = None
    for line in diff.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if SKIP_RE.match(line) and current:
            body = line[1:].strip()
            matched = None
            for (f, frag) in SANCTIONED:
                if f == current and frag in body:
                    matched = (f, frag)
                    break
            found.add(matched if matched else (current, body))

    pinned = set(SANCTIONED)
    unsanctioned = found - pinned
    missing = pinned - found

    if unsanctioned or missing:
        print("FAIL: the added-test-skip set does not equal the pinned set.\n")
        for f, body in sorted(unsanctioned):
            print(f"  UNSANCTIONED  {f}\n                {body}")
        if unsanctioned:
            print(
                "\n  A skip CI can reach turns a broken assertion into a green run.\n"
                "  If this skip is genuinely sanctioned, add it to SANCTIONED in this\n"
                "  script with the reason CI cannot reach it — not a reason it is useful."
            )
        for f, frag in sorted(missing):
            print(f"\n  MISSING       {f}\n                {frag}")
        if missing:
            print(
                "\n  A pinned skip disappeared. That may be correct — but a pin that\n"
                "  narrows silently stops meaning anything, so say so deliberately."
            )
        return 1

    print(f"PASS: added-skip set equals the pinned set ({len(pinned)} sanctioned).")
    for f, frag in sorted(pinned):
        print(f"  - {f}: {frag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
