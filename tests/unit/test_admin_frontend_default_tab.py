"""ATHENA-113a — Mission Control is the declared default landing tab.

dick's investigation (.mozart/investigations/active/2026-09-27-diagnose-athena-
mission-control.md, H5) found `app.js`'s no-hash fallback read 'dashboard'
while `index.html` (sidebar button comment + pre-applied `sidebar-item-active`,
and the `#tab-mission-control` container comment) both declare Mission Control
as "(default landing page)". Static, regex-based assertions -- no browser/DOM
required -- since app.js is a large script with heavy DOM-load-time
dependencies unsuited to a bare `node -e` harness (see
tests/unit/test_frontend_guard_scripts.py's requires_node fixtures for the
cases where that harness IS appropriate: small, self-contained files).
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_JS = REPO_ROOT / "admin" / "frontend" / "app.js"
INDEX_HTML = REPO_ROOT / "admin" / "frontend" / "index.html"


def _app_js_source() -> str:
    return APP_JS.read_text(encoding="utf-8")


def test_default_initial_tab_is_mission_control():
    source = _app_js_source()
    m = re.search(r"const initialTab = hash \|\| '([^']+)';", source)
    assert m, "could not find the `const initialTab = hash || '...'` fallback in app.js"
    assert m.group(1) == "mission-control", (
        f"default landing tab must be 'mission-control' per index.html's declared "
        f"intent (sidebar-item-active pre-applied, '(default landing page)' comments "
        f"at index.html:1531-1534/1882); got {m.group(1)!r}"
    )


def test_mission_control_case_initializes_the_page():
    source = _app_js_source()
    m = re.search(r"case 'mission-control':\s*\n\s*if \(window\.Athena\?\.pages\?\.MissionControl\?\.init\)\s*\{\s*\n\s*Athena\.pages\.MissionControl\.init\(\);", source)
    assert m, "showTab's 'mission-control' case must call Athena.pages.MissionControl.init()"


def test_dashboard_case_still_loads_status():
    """#dashboard (the separate 'Service Status' tab) must keep working --
    this fix changes only the no-hash fallback, not the dashboard route."""
    source = _app_js_source()
    m = re.search(r"case 'dashboard':\s*\n\s*loadStatus\(\);", source)
    assert m, "showTab's 'dashboard' case must still call loadStatus()"


def test_hash_navigation_to_dashboard_still_shows_dashboard_tab():
    """index.html must still declare a distinct #tab-dashboard the
    'dashboard' case can toggle into view -- a plain #dashboard deep link
    keeps working independent of the no-hash default."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert 'id="tab-dashboard"' in html
    assert 'data-route="dashboard"' in html


def test_tab_permission_map_still_covers_both_tabs():
    source = _app_js_source()
    m = re.search(r"const TAB_PERMISSION_MAP = \{(.*?)\};", source, re.DOTALL)
    assert m, "could not find TAB_PERMISSION_MAP in app.js"
    body = m.group(1)
    assert "'mission-control': 'read:dashboard'" in body
    assert "'dashboard': 'read:dashboard'" in body
