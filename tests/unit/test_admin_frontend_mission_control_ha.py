"""Mission Control shows the Home Assistant card (the same one as the Dashboard).

Loads status-vocab.js and mission-control.js into one Node context with a stub
DOM and exercises the real refresh(): independent failure states, the skeleton
only on the first load and on an explicit refresh, no rewrite of an unchanged
card, and tiles that carry their status in words.
"""
from __future__ import annotations

import json
import re

import pytest

from .frontend_node import DOM_STUB, FRONTEND, requires_node, run_scripts

pytestmark = requires_node

ESCAPE = FRONTEND / "escape-html.js"
VOCAB = FRONTEND / "status-vocab.js"
MISSION = FRONTEND / "mission-control.js"
INDEX = FRONTEND / "index.html"

PRELUDE = DOM_STUB + """
global.__nodes = {};
global.__writes = [];
const node = (id) => {
    if (!__nodes[id]) {
        let html = '';
        __nodes[id] = { id, get innerHTML() { return html; }, set innerHTML(v) { html = v; __writes.push([id, v]); }, style: {}, classList: { add() {}, remove() {} } };
    }
    return __nodes[id];
};
global.document = { getElementById: node, querySelectorAll: () => [], querySelector: () => null, createElement: () => ({ style: {} }) };
global.Athena = { pages: {}, components: { Toast: { error() {} } }, api: null, charts: null };
global.__calls = [];
"""


def run_page(script: str):
    """Load the page, then run `script` (an async function body returning JSON)."""
    expression = "(async () => {" + script + "})()"
    return run_scripts([ESCAPE, VOCAB, MISSION], expression, prelude=PRELUDE)


HA_OK = {"state": "connected", "reason": "ok", "areas": 9, "checked_at": "2026-10-11T12:00:00+00:00",
         "last_outcome_at": "2026-10-11T11:58:00+00:00", "last_success_at": "2026-10-11T11:58:00+00:00", "coverage": "cluster", "gateway": "ok"}
DASHBOARD = {"voice_health": {"healthy": 1, "total": 1, "percentage": 100, "stale_count": 0}, "traffic": {"requests_per_minute": 1.5, "total_24h": 10, "history": []}, "pending_actions": [], "alerts": {"total": 0},
             "services": [{"name": "weather", "status": "healthy"}, {"name": "<img src=x>", "status": "stale"}, {"name": "gw", "status": "something new"}]}

SETUP = f"""
    global.__ha = {json.dumps(HA_OK)};
    global.__dash = {json.dumps(DASHBOARD)};
    global.__haFails = false; global.__dashFails = false;
    Athena.api = async (path) => {{
        __calls.push(path);
        if (path === '/api/service-registry/home-assistant') {{ if (__haFails) throw new Error('boom'); return __ha; }}
        if (path === '/api/dashboard') {{ if (__dashFails) throw new Error('down'); return __dash; }}
        throw new Error('unexpected ' + path);
    }};
    const mc = () => Athena.pages.MissionControl;
    const ha = () => __nodes['mc-ha-status'] ? __nodes['mc-ha-status'].innerHTML : null;
    const haWrites = () => __writes.filter(([id]) => id === 'mc-ha-status').length;
    __nodes['mc-ha-status'] = null; node('mc-ha-status');
"""


def test_the_page_registers_and_exposes_the_closed_refresh_the_retry_button_calls():
    out = run_page("return [typeof Athena.pages.MissionControl.refresh, typeof window.MissionControl.refresh];")
    assert out == ["function", "function"]


def test_refresh_fetches_both_sources_and_renders_the_mission_surface():
    out = run_page(SETUP + "await mc().refresh(); return {calls: __calls, html: ha()};")
    assert sorted(out["calls"]) == ["/api/dashboard", "/api/service-registry/home-assistant"]
    assert 'id="mc-ha-status-heading"' in out["html"] and 'id="ha-status-heading"' not in out["html"]
    assert "Connected" in out["html"]


def test_each_failure_is_independent():
    ha_down = run_page(SETUP + "__haFails = true; await mc().refresh(); return {html: ha(), dash: __nodes['service-status-grid'] ? __nodes['service-status-grid'].innerHTML : null};")
    assert "Status unavailable" in ha_down["html"]
    assert 'onclick="Athena.pages.MissionControl.refresh()"' in ha_down["html"] and 'id="mc-ha-status-heading"' in ha_down["html"]
    assert "weather" in ha_down["dash"], "the service grid still renders"
    dash_down = run_page(SETUP + "__dashFails = true; await mc().refresh(); return {html: ha()};")
    assert "Connected" in dash_down["html"], "the card still renders when the dashboard call fails"


def test_the_skeleton_shows_on_the_first_load_and_on_an_explicit_refresh_but_not_on_the_timer():
    out = run_page(SETUP + """
        await mc().refresh({quiet: true});               // first load: the skeleton is allowed
        const afterFirst = __writes.filter(([id, v]) => id === 'mc-ha-status').map(([, v]) => /Loading Home Assistant status/.test(v));
        __writes.length = 0;
        __ha = Object.assign({}, __ha, {areas: 10});     // a changed card
        await mc().refresh({quiet: true});               // the timer
        const afterTimer = __writes.filter(([id]) => id === 'mc-ha-status').map(([, v]) => /Loading Home Assistant status/.test(v));
        __writes.length = 0;
        await mc().refresh();                            // the Refresh button
        const afterExplicit = __writes.filter(([id]) => id === 'mc-ha-status').map(([, v]) => /Loading Home Assistant status/.test(v));
        return {afterFirst, afterTimer, afterExplicit};
    """)
    assert out["afterFirst"] == [True, False]
    assert out["afterTimer"] == [False], "the timer rewrites the card once, never the skeleton"
    assert out["afterExplicit"][0] is True


def test_an_unchanged_card_is_not_rewritten_so_focus_survives_the_timer():
    out = run_page(SETUP + """
        await mc().refresh({quiet: true});
        __writes.length = 0;
        await mc().refresh({quiet: true});
        await mc().refresh({quiet: true});
        return haWrites();
    """)
    assert out == 0


def test_the_card_has_no_live_region():
    html = run_page(SETUP + "await mc().refresh(); return ha();")
    assert "aria-live" not in html and 'role="status"' not in html


def test_each_heading_id_appears_exactly_once_across_index_html_and_the_rendered_surfaces():
    index = INDEX.read_text(encoding="utf-8")
    mission_card = run_page(SETUP + "await mc().refresh(); return ha();")
    mission_failure = run_page(SETUP + "__haFails = true; await mc().refresh(); return ha();")
    dashboard_card = run_scripts([ESCAPE, VOCAB], "renderHomeAssistantCard(summarizeHomeAssistant({state: 'connected', reason: 'ok'}), 'dashboard')")
    dashboard_failure = run_scripts([ESCAPE, VOCAB], "renderHomeAssistantFailure('dashboard')")
    for surface in (mission_card, mission_failure):
        assert (index + surface).count('id="mc-ha-status-heading"') == 1
        assert (index + surface).count('id="ha-status-heading"') == 0
    for surface in (dashboard_card, dashboard_failure):
        assert (index + surface).count('id="ha-status-heading"') == 1
        assert (index + surface).count('id="mc-ha-status-heading"') == 0
    # every tab is in the DOM at once, so both cards together carry each id once
    assert (index + mission_card + dashboard_card).count('id="mc-ha-status-heading"') == 1
    assert (index + mission_card + dashboard_card).count('id="ha-status-heading"') == 1


def test_the_section_is_named_by_its_own_heading():
    source = MISSION.read_text(encoding="utf-8")
    assert '<section id="mc-ha-status" class="mb-6" aria-labelledby="mc-ha-status-heading"></section>' in source


def test_service_tiles_carry_their_status_in_words_and_escape_the_name():
    out = run_page(SETUP + "await mc().refresh(); return __nodes['service-status-grid'].innerHTML;")
    assert '<span class="sr-only">: Healthy</span>' in out
    assert '<span class="sr-only">: No recent check</span>' in out
    assert '<span class="sr-only">: something new</span>' in out
    assert 'title="Healthy"' in out and 'title="No recent check"' in out
    assert "<img" not in out and "&lt;img src=x&gt;" in out
    assert "animate-pulse" not in out.replace("motion-safe:animate-pulse", "")
    tiles = re.findall(r'<div class="flex flex-col items-center p-3[^>]*title="([^"]*)"', out)
    assert tiles == ["Healthy", "No recent check", "something new"]


def test_only_a_problem_pulses_and_never_without_the_motion_guard():
    out = run_page(SETUP + "await mc().refresh(); return __nodes['service-status-grid'].innerHTML;")
    assert out.count("motion-safe:animate-pulse") == 1   # the one unknown word reads as a problem


@pytest.mark.parametrize("status,pulses", [("healthy", False), ("stale", False), ("disabled", False), ("pending", False),
                                           ("unconfigured", False), ("unhealthy", True), ("timeout", True), ("zzz", True)])
def test_pulse_by_tone(status, pulses):
    got = run_scripts([VOCAB], f"TONE_PULSE[statusInfo({json.dumps(status)}).tone] || ''")
    assert (got == "motion-safe:animate-pulse") is pulses


def test_the_refresh_timer_is_quiet_and_the_refresh_button_is_not():
    out = run_page(SETUP + """
        const timers = [];
        global.setInterval = (fn, ms) => { timers.push([fn, ms]); return 1; };
        global.AppState = { isAuthenticated: () => true };
        node('mission-control-content');
        await mc().init();
        const initial = haWrites();
        __ha = Object.assign({}, __ha, {areas: 10});
        __writes.length = 0;
        await timers[0][0]();                      // the timer fires
        const skeletonOnTimer = __writes.some(([id, v]) => id === 'mc-ha-status' && /Loading Home Assistant status/.test(v));
        __writes.length = 0;
        const button = /onclick="Athena\\.pages\\.MissionControl\\.refresh\\(\\)"/.test(__nodes['mission-control-content'].innerHTML);
        await mc().refresh();                      // what the Refresh button runs
        const skeletonOnButton = __writes.some(([id, v]) => id === 'mc-ha-status' && /Loading Home Assistant status/.test(v));
        return {timers: timers.length, interval: timers[0][1], initial, skeletonOnTimer, button, skeletonOnButton};
    """)
    assert out == {"timers": 1, "interval": 60000, "initial": 2, "skeletonOnTimer": False, "button": True, "skeletonOnButton": True}
