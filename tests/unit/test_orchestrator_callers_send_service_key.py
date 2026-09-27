"""ATHENA-89 Phase 3b — AU4: every in-repo caller of an orchestrator route
that requires `X-Service-Key` (D10/DC12) actually sends it.

Static text/regex scan, no mocking, no git dependency (walks the tree
directly rather than shelling out to `git ls-files`, per tessa's P3 FIX).
Population and named members per the plan/contract's r3 AU4 text plus the
DC12 amendment (gateway warmup client + admin-backend `/admin/*` callers).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCAN_ROOTS = (
    REPO_ROOT / "src" / "gateway",
    REPO_ROOT / "apps" / "jarvis-web" / "backend",
    REPO_ROOT / "admin" / "backend" / "app",
    REPO_ROOT / "apps" / "chat-embed",
)

# Files that *define* /sessions-shaped routes of their own rather than call
# the orchestrator's -- excluded so they don't false-positive the scan.
_EXCLUDED = {
    REPO_ROOT / "src" / "gateway" / "livekit_routes.py",
    REPO_ROOT / "admin" / "backend" / "app" / "routes" / "pipeline_events.py",
}

# f-string-aware: matches the route literal whether it's a plain string or
# embedded in an f-string next to ORCHESTRATOR_URL/GATEWAY_URL-style bases.
_ROUTE_PATTERN = re.compile(
    r"""/query(?:/stream(?:/v2)?)?["'?]"""
    r"""|/v1/chat/completions"""
    r"""|/sessions"""
    r"""|/session/[^"']*?/warmup"""
    r"""|/admin/(?:invalidate-feature-cache|invalidate-model-cache"""
    r"""|reset-circuit-breaker|reset-all-circuits)"""
)


def _iter_py_files():
    for root in _SCAN_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            if path in _EXCLUDED:
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


def _find_matches():
    """Returns {file: [line_no, ...]} for every file with >=1 route-literal
    match, and the flat list of (file, line_no) call sites."""
    matches_by_file: dict[Path, list[int]] = {}
    call_sites: list[tuple[Path, int]] = []
    for path in _iter_py_files():
        text = path.read_text()
        lines = text.splitlines()
        hits = [i + 1 for i, line in enumerate(lines) if _ROUTE_PATTERN.search(line)]
        if hits:
            matches_by_file[path] = hits
            call_sites.extend((path, ln) for ln in hits)
    return matches_by_file, call_sites


def test_AU4_population_floor_met():
    matches_by_file, call_sites = _find_matches()
    assert len(matches_by_file) >= 5, sorted(str(p.relative_to(REPO_ROOT)) for p in matches_by_file)
    assert len(call_sites) >= 7, call_sites


def test_AU4_named_members_present():
    matches_by_file, _ = _find_matches()
    rel_names = {str(p.relative_to(REPO_ROOT)) for p in matches_by_file}
    expected_named = {
        "src/gateway/wyoming_bridge.py",
        "admin/backend/app/routes/sms_webhook.py",
        "apps/jarvis-web/backend/main.py",
        "src/gateway/main.py",  # covers both orchestrator_client uses and the warmup client
        "src/gateway/livekit_integration.py",
    }
    missing = expected_named - rel_names
    assert not missing, f"expected named callers not matched by the scan: {missing}"


def test_AU4_every_matched_file_sends_service_key_header():
    matches_by_file, _ = _find_matches()
    unauthenticated = [
        str(p.relative_to(REPO_ROOT))
        for p in matches_by_file
        if "X-Service-Key" not in p.read_text()
    ]
    assert not unauthenticated, (
        "these files build a request to an orchestrator route gated by "
        "require_service_caller but never send X-Service-Key: "
        f"{unauthenticated}"
    )
