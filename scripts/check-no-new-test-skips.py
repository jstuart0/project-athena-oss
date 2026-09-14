#!/usr/bin/env python3
"""No unsanctioned test skip exists, and no pinned skip has silently disappeared.

Why a pinned allowlist rather than a flat ban
----------------------------------------------
A flat `! grep -q 'pytest.mark.skip'` over the branch diff is correct in its exit-code
handling but wrong in substance for this campaign, because ATHENA-66 Phase 1 step 8 is
*required* to add a skip — D3's replacement for the shipped module-level `skipif` that
turned a broken node interpreter into a silent module-wide pass (catalogue instance 7).

Banning the fix for the defect is not a gate, it is a contradiction. But dropping the
check, or loosening it to "ignore skips in these files", re-opens the hole the check
exists to close.

Each pinned entry carries the reason it is sanctioned and, crucially, why CI cannot
reach it. A skip CI can reach is not sanctionable at any count.

Two independent obligations, checked two different ways (ATHENA-71)
---------------------------------------------------------------------
The original version of this script asserted *set equality* between "skips added in
base..HEAD" and the pinned set. That conflated two obligations that only coincide while
a campaign branch is unmerged:

    (a) no unsanctioned skip gets added
    (b) a sanctioned skip doesn't silently disappear

Set equality against the diff enforces both *only when the diff is exactly the
originating commit* — the moment the campaign merges to main, base==HEAD for the push
that landed it, the diff is empty, and an empty set can never equal a non-empty pinned
set. Every subsequent PR inherits the same failure: its base is post-merge main, which
already contains the pins, so it *adds* none either. The check went permanently red.

So the two obligations are now checked independently:

    (a) is diff-scoped: every skip ADDED in base..HEAD must be a member of the pinned
        allowlist (subset, not equality — an empty added-set trivially satisfies it).
    (b) is tree-scoped: every pinned entry must be found, verbatim, in HEAD's checked-
        out tree — independent of `--base` entirely, so it holds on a PR, on a direct
        push to main, and when re-run locally with no diff in play at all.

Scope note: (a) reads COMMITTED history (`git diff <merge-base>..HEAD`); (b) reads
HEAD's committed tree (`git show HEAD:<path>`). Both are invisible to uncommitted
working-tree edits by design — so a CAN-FAIL demonstration of this gate must commit the
injected change, not merely write it to the working tree. (Doing the latter produces a
false PASS, which is catalogue instance 5 wearing a different hat.)

Usage
-----
    check-no-new-test-skips.py --base <ref>

`--base` only affects (a). On a push to main with no meaningful prior state (a new
branch's first push, `before` is the all-zeros SHA), the caller should pass `--base
HEAD` — an empty added-set is correct there, and (b) still runs regardless.

Exit codes
----------
    0  no unsanctioned skip added, and every pinned skip is present in HEAD's tree
    1  a finding exists (unsanctioned addition, or a pinned skip is missing)
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
SKIP_RE_ANY = re.compile(r"pytest\.mark\.skip")


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def _resolve_commit(ref: str, label: str) -> None:
    probe = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if probe.returncode != 0:
        die(f"{label} does not resolve to a commit: {ref}")


def scan_added_skips(base_sha: str) -> set:
    """(a) Every skip line ADDED in base_sha..HEAD, minus the ones that match a
    pinned (file, fragment) identity. Non-empty means an unsanctioned skip landed.
    """
    diff = subprocess.run(
        ["git", "diff", f"{base_sha}..HEAD", "--", "tests/"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if diff.returncode != 0:
        die(f"git diff failed: {diff.stderr.strip()}")

    unsanctioned = set()
    current = None
    for line in diff.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if line.startswith("+++ /dev/null"):
            current = None
            continue
        if SKIP_RE.match(line) and current:
            body = line[1:].strip()
            if not any(f == current and frag in body for (f, frag) in SANCTIONED):
                unsanctioned.add((current, body))
    return unsanctioned


def check_tree_presence(pinned: set) -> set:
    """(b) Every pinned (file, fragment) that is NOT found verbatim in HEAD's
    committed tree. Independent of --base by design — holds on a PR, on a direct
    push to main, and with no diff in play at all.
    """
    missing = set()
    for (f, frag) in pinned:
        show = subprocess.run(
            ["git", "show", f"HEAD:{f}"], cwd=REPO_ROOT, capture_output=True, text=True,
        )
        if show.returncode != 0:
            missing.add((f, frag))
            continue
        if not any(frag in line for line in show.stdout.splitlines() if SKIP_RE_ANY.search(line)):
            missing.add((f, frag))
    return missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--base", default="main",
        help="base ref for the added-skip diff (default: main). Only affects (a); "
             "(b) always checks HEAD's tree regardless of --base.",
    )
    args = ap.parse_args()

    _resolve_commit("HEAD", "HEAD")
    _resolve_commit(args.base, "base ref")

    mb = subprocess.run(
        ["git", "merge-base", args.base, "HEAD"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if mb.returncode != 0:
        die(f"merge-base failed: {mb.stderr.strip()}")
    base_sha = mb.stdout.strip()

    unsanctioned = scan_added_skips(base_sha)
    missing = check_tree_presence(set(SANCTIONED))

    if unsanctioned or missing:
        print("FAIL:\n")
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
                "\n  A pinned skip is no longer present in HEAD's tree. That may be\n"
                "  correct — but a pin that silently narrows stops meaning anything,\n"
                "  so remove it from SANCTIONED in the same change that removes the skip."
            )
        return 1

    print(
        f"PASS: no unsanctioned skip added since {args.base}; "
        f"all {len(SANCTIONED)} pinned skip(s) present in HEAD's tree."
    )
    for f, frag in sorted(SANCTIONED):
        print(f"  - {f}: {frag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
