"""A changed admin frontend script must change its ?v= cache-buster.

nginx serves admin .js as `immutable`, so a file whose URL didn't change
never reaches a warm browser. Two checks:

- a content pin for the files this branch changed: the file's hash and its
  buster are recorded together, so editing the file without bumping (and
  re-pinning) fails here, whatever git history is available;
- scripts/check-frontend-cache-busters.py against the merge base with
  origin/main, when that ref exists (CI's shallow checkouts run the script
  in its own workflow instead).
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "admin" / "frontend"
INDEX_HTML = FRONTEND / "index.html"

# file -> (sha256 prefix of its content, the ?v= it must be served with)
PINS = {
    "memory-management.js": ("6b59048872a7f454", "20260929b"),
}


def _buster(name: str) -> str:
    match = re.search(rf'src="/{re.escape(name)}\?v=([^"]+)"', INDEX_HTML.read_text(encoding="utf-8"))
    assert match, f"{name} has no ?v= buster in index.html"
    return match.group(1)


@pytest.mark.parametrize("name", sorted(PINS))
def test_pinned_script_content_matches_its_buster(name):
    digest = hashlib.sha256((FRONTEND / name).read_bytes()).hexdigest()[:16]
    pinned_digest, pinned_buster = PINS[name]
    assert digest == pinned_digest, (
        f"{name} changed: bump its ?v= in admin/frontend/index.html and update PINS "
        f"here to ({digest!r}, <new buster>)"
    )
    assert _buster(name) == pinned_buster


def test_changed_scripts_have_bumped_busters_vs_merge_base():
    base = subprocess.run(
        ["git", "merge-base", "HEAD", "origin/main"], cwd=REPO_ROOT, capture_output=True, text=True
    )
    if base.returncode != 0 or not base.stdout.strip():
        pytest.skip("no origin/main merge base in this checkout")
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check-frontend-cache-busters.py"), "--base", base.stdout.strip()],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
