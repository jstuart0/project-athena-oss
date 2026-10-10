"""The Conversations page escapes every value it renders, and labels hidden text.

Runs the real admin/frontend/conversations.js (with the real escape-html.js) in a
Node subprocess against hostile data, then checks the strings the render
functions return. No browser; the render functions are pure string builders.

Node resolution follows test_admin_frontend_escaping.py (ATHENA_NODE_BIN, then
PATH), but a missing interpreter fails here rather than skipping: this repo
allows no new test skips.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "admin" / "frontend"
HOSTILE = "<img src=x onerror=alert(1)>"
QUOTE_BREAKER = "'\"><svg onload=alert(2)>"
SECRET = "SECRETQUERYTEXT"


def _node() -> str:
    candidate = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
    if candidate and subprocess.run([candidate, "--version"], capture_output=True, timeout=15).returncode == 0:
        return candidate
    pytest.fail("a working node interpreter is required (ATHENA_NODE_BIN or PATH)", pytrace=False)


HARNESS = r"""
'use strict';
const vm = require('vm');
const fs = require('fs');
const ctx = { console, Date, Number, Object, Array, JSON, String, Math, Promise, URLSearchParams, setTimeout,
              encodeURIComponent, localStorage: { getItem: () => null },
              document: { getElementById: () => null } };
ctx.window = ctx;
vm.createContext(ctx);
for (const file of ['escape-html.js', 'conversations.js']) {
  vm.runInContext(fs.readFileSync(process.argv[1] + '/' + file, 'utf8'), ctx, { filename: file });
}
const data = JSON.parse(process.argv[2]);
const out = vm.runInContext(`(function(d){
  const r = {};
  r.banners = d.captures.map(c => renderCaptureBanner(c));
  r.listVisible = renderListView(d.list, d.stats, d.captures[0]);
  r.listHidden = renderListView(Object.assign({}, d.list, {text_visible: false}), d.stats, d.captures[0]);
  r.detailVisible = renderDetailView(d.detail);
  r.detailHidden = renderDetailView(Object.assign({}, d.detail, {text_visible: false}));
  r.error = renderError(d.hostile);
  const empty = Object.assign({}, d.list, {items: []});
  r.emptyOff = renderListView(empty, null, d.captures[0]);
  r.emptyOn = renderListView(empty, null, d.captures[4]);
  convState.filters.room = 'kitchen';
  r.emptyFiltered = renderListView(empty, null, d.captures[4]);
  convState.filters.room = '';
  return r;
})`, ctx)(data);
process.stdout.write(JSON.stringify(out));
"""


def _render(data: dict) -> dict:
    proc = subprocess.run(
        [_node(), "-e", HARNESS, str(FRONTEND), json.dumps(data)],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[:600]
    return json.loads(proc.stdout)


def _capture(state, **over):
    base = {"state": state, "analytics_enabled": True, "debug_enabled": False, "debug_enabled_since": None,
            "retention_days": 30, "retention_state": "ok", "next_expiry_at": None, "writer_replicas": 1,
            "dropped": 0, "turn_count": 0, "oldest_turn_at": None, "last_turn_at": None}
    base.update(over)
    return base


def _turn(**over):
    base = {
        "id": QUOTE_BREAKER, "turn_number": 1, "request_id": HOSTILE, "query_text": SECRET, "response_text": "an answer",
        "intent": HOSTILE, "confidence": 0.9, "model_used": HOSTILE, "rag_tools_used": [HOSTILE], "response_time_ms": 1200,
        "tokens_generated": 5, "tokens_per_second": 2.2, "validation_passed": True, "error_message": HOSTILE,
        "source": HOSTILE, "complexity": HOSTILE, "is_fallback": False, "base_knowledge_populated": True,
        "node_timings_ms": {HOSTILE: 4}, "route": HOSTILE, "channel": HOSTILE, "audience_class": HOSTILE,
        "outcome": "error", "text_withheld": False,
        "debug_detail": {HOSTILE: HOSTILE, "caller_trust_tier": HOSTILE, "n": 3, "ok": True, "nested": {"a": HOSTILE}},
        "evaluations": [{"rating": 3, "notes": HOSTILE, "evaluated_by_username": HOSTILE}],
    }
    base.update(over)
    return base


@pytest.fixture(scope="module")
def rendered():
    data = {
        "hostile": HOSTILE,
        "captures": [
            _capture("off", turn_count=7, next_expiry_at=HOSTILE),
            _capture("on_no_writer"), _capture("on_stale"), _capture("on_failing", dropped=4),
            _capture("on_ok", debug_enabled=True, debug_enabled_since=HOSTILE),
            _capture(HOSTILE), _capture("on_ok", retention_state="retention_invalid"),
        ],
        "stats": {"total_conversations": 1, "total_turns": 2, "avg_turns_per_conversation": 2,
                  "by_source": {HOSTILE: 2}, "by_room": {}, "by_intent": {}},
        "list": {
            "items": [{"id": QUOTE_BREAKER, "session_id": HOSTILE, "session_class": HOSTILE, "room": HOSTILE,
                       "user_mode": HOSTILE, "interface_type": HOSTILE, "started_at": "2026-10-01T12:00:00",
                       "ended_at": "2026-10-01T12:05:00", "turn_count": 2, "total_tokens": 3, "first_query": SECRET,
                       "source": HOSTILE},
                      {"id": "w", "session_id": "own-x", "session_class": "owner", "room": None, "user_mode": None,
                       "started_at": None, "ended_at": None, "turn_count": 1, "first_query": "", "source": "analytics"}],
            "total": 2, "page": 1, "per_page": 25, "pages": 1, "text_visible": True,
        },
        "detail": {"id": "c", "session_id": HOSTILE, "session_class": HOSTILE, "room": HOSTILE, "user_mode": HOSTILE,
                   "interface_type": HOSTILE, "started_at": "2026-10-01T12:00:00", "turn_count": 2, "total_tokens": 3,
                   "text_visible": True,
                   "turns": [_turn(), _turn(id="t2", turn_number=2, text_withheld=True, query_text="", response_text=None,
                                            outcome="withheld", route=None, source="debug", error_message=None,
                                            debug_detail=None)]},
    }
    return _render(data)


def _all_strings(rendered):
    for key, value in rendered.items():
        if isinstance(value, list):
            for i, item in enumerate(value):
                yield f"{key}[{i}]", item
        else:
            yield key, value


def test_no_raw_markup_from_any_value_reaches_the_output(rendered):
    for name, html in _all_strings(rendered):
        assert "<img" not in html, name
        assert "<svg" not in html, name
        assert "onerror=alert" not in html.replace("&lt;img src=x onerror=alert(1)&gt;", ""), name
        assert "onload=alert" not in html.replace("&lt;svg onload=alert(2)&gt;", ""), name
    assert "&lt;img" in rendered["detailVisible"] and "&lt;img" in rendered["listVisible"]


def test_a_quote_in_an_id_cannot_break_out_of_a_handler_or_attribute(rendered):
    for key in ("listVisible", "detailVisible"):
        html = rendered[key]
        assert "'\"><svg" not in html, key
    assert "showConversationDetail('" in rendered["listVisible"]


def test_every_banner_state_has_its_own_fixed_message(rendered):
    off, no_writer, stale, failing, ok, unknown, invalid = rendered["banners"]
    assert "Capture is off. Nothing new is being recorded." in off and "7 turns are stored" in off
    assert "no orchestrator has reported in" in no_writer and "DATABASE_URL" in no_writer
    assert "hasn’t reported for a few minutes" in stale
    assert "failing to write conversations. 4 turns have been dropped." in failing
    assert "Capture is on and working." in ok and "Debug detail ON" in ok
    assert "Capture state is unknown." in unknown
    assert "retention period isn’t valid" in invalid
    assert 'role="status"' in off
    assert "Capture is best-effort" in off and "Capture is best-effort" in ok


def test_hidden_text_is_labelled_and_never_rendered(rendered):
    for key in ("listHidden", "detailHidden"):
        html = rendered[key]
        assert "Text hidden: owner only" in html, key
        assert SECRET not in html, key
        assert "an answer" not in html, key
    assert SECRET in rendered["listVisible"] and SECRET in rendered["detailVisible"]


def test_withheld_rows_say_so_explicitly(rendered):
    assert "(text withheld)" in rendered["listVisible"]
    assert "(text withheld)" in rendered["detailVisible"]
    assert "(text withheld)" not in rendered["detailHidden"]


def test_a_row_without_a_route_is_labelled_legacy(rendered):
    assert "legacy debug" in rendered["detailVisible"]


def test_unknown_labels_are_escaped_raw_and_known_ones_are_mapped(rendered):
    assert "Household or unverified" in rendered["listVisible"] and "Owner" in rendered["listVisible"]
    assert "Cut off" not in rendered["detailVisible"] and "Error" in rendered["detailVisible"]
    assert "Withheld" in rendered["detailVisible"]


def test_the_debug_block_shows_non_text_values_to_non_owners(rendered):
    hidden = rendered["detailHidden"]
    assert "Debug detail" in hidden and "yes" in hidden


def test_the_error_page_escapes_its_message(rendered):
    assert "<img" not in rendered["error"] and "&lt;img" in rendered["error"]


def test_the_list_has_no_separate_best_effort_box_and_purge_lives_below_the_list_in_a_disclosure(rendered):
    html = rendered["listVisible"]
    assert html.count("best-effort") == 1 and "Best-effort capture —" not in html
    assert "Manage storage" not in html, "purge controls render only for a user who may purge"


def test_the_empty_state_matches_the_situation(rendered):
    assert "Capture is off, so nothing has been recorded" in rendered["emptyOff"]
    assert "Nothing has been captured yet." in rendered["emptyOn"]
    assert "No conversations match these filters." in rendered["emptyFiltered"]
