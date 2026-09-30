"""calendar-sources.js edit and sync behaviour, executed in Node
(vm.runInThisContext, as the file is a plain browser <script>) with fetch,
showToast and the edit modal stubbed."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "admin" / "frontend"
NODE_BIN = shutil.which("node")

EDIT_ERROR = "Could not load this source for editing"


def _run(body: str) -> dict:
    """Load calendar-sources.js with stubs, run `body` (async JS that may use
    `toasts`, `modal` and `setFetch`), and return {toasts, modal}."""
    # Fail, don't skip, without node: a skipped behaviour test is a silent green.
    assert NODE_BIN, "node is required on PATH for the calendar-sources frontend tests"
    script = f"""
    'use strict';
    const vm = require('vm');
    const fs = require('fs');
    global.window = global;
    global.location = {{ hash: '' }};
    global.document = {{
        readyState: 'complete', addEventListener: () => {{}},
        getElementById: () => null, createElement: () => ({{}}),
    }};
    for (const f of ['escape-html.js', 'calendar-sources.js']) {{
        vm.runInThisContext(fs.readFileSync({json.dumps(str(FRONTEND))} + '/' + f, 'utf8'), {{ filename: f }});
    }}
    const toasts = [];
    const modal = [];
    global.showToast = (message, type) => toasts.push([String(message), type]);
    global.showEditCalendarSourceModal = (source) => modal.push(source);
    global.loadCalendarSources = () => {{}};
    global.getToken = () => 'token';
    global.console = {{ ...console, error: () => {{}} }};
    const setFetch = (fn) => {{ global.fetch = fn; }};
    vm.runInThisContext('allCalendarSources = [{{"id": 1, "name": "S", "ical_url_masked": "https://feed.example.com/…"}}]');
    (async () => {{
        {body}
        process.stdout.write(JSON.stringify({{ toasts, modal }}));
    }})().catch((e) => {{ console.log(e && e.stack); process.exit(3); }});
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15,
                          env={**os.environ, "TZ": "UTC"})
    assert proc.returncode == 0, f"node failed: {proc.stdout} {proc.stderr}"
    return json.loads(proc.stdout)


_RESPONSES = {
    "403": "setFetch(async () => ({ ok: false, status: 403, json: async () => ({ detail: 'Insufficient permissions' }) }));",
    "500": "setFetch(async () => ({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) }));",
    "rejected": "setFetch(async () => { throw new TypeError('network down'); });",
}


@pytest.mark.parametrize("case", sorted(_RESPONSES))
def test_edit_load_failure_shows_one_error_and_never_opens_the_modal(case):
    out = _run(_RESPONSES[case] + "\nawait editCalendarSource(1);")
    assert out["modal"] == []
    assert out["toasts"] == [[EDIT_ERROR, "error"]]


def test_edit_load_success_opens_the_modal_with_the_full_source():
    full = {"id": 1, "name": "S", "ical_url": "https://feed.example.com/full.ics?token=t"}
    out = _run(
        f"setFetch(async () => ({{ ok: true, status: 200, json: async () => ({json.dumps(full)}) }}));"
        "\nawait editCalendarSource(1);"
    )
    assert out["modal"] == [full]
    assert out["toasts"] == []


def _sync(result: dict) -> list:
    out = _run(
        f"setFetch(async () => ({{ ok: true, status: 200, json: async () => ({json.dumps(result)}) }}));"
        "\nawait syncCalendarSource(1);"
    )
    return [t for t in out["toasts"] if t[1] != "info"]


def test_sync_error_shows_detail_when_there_is_no_message():
    toasts = _sync({"detail": "Insufficient permissions"})
    assert len(toasts) == 1
    message, kind = toasts[0]
    assert kind == "error"
    assert "Insufficient permissions" in message
    assert "undefined" not in message


def test_sync_success_mentions_matched_deleted_entries():
    toasts = _sync({"success": True, "events_added": 0, "events_updated": 9, "events_matched_deleted": 2})
    assert toasts == [["Sync complete: 0 added, 9 updated, 2 matched entries you deleted or cancelled", "success"]]


def test_sync_success_without_matched_deleted_has_no_suffix():
    toasts = _sync({"success": True, "events_added": 1, "events_updated": 2, "events_matched_deleted": 0})
    assert toasts == [["Sync complete: 1 added, 2 updated", "success"]]
