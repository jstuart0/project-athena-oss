"""Capture and Conversations messages reach the user, even though other scripts redeclare showError.

follow-me.js (and several others) declare a global showError(containerId, message) after app.js,
so a plain showError(message) call from the capture code is silently dropped. The capture code
uses showCaptureToast, defined once.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parents[2] / "admin" / "frontend"


def _node() -> str:
    candidate = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
    if candidate and subprocess.run([candidate, "--version"], capture_output=True, timeout=15).returncode == 0:
        return candidate
    pytest.fail("a working node interpreter is required (ATHENA_NODE_BIN or PATH)", pytrace=False)


def test_the_toast_helper_is_defined_exactly_once():
    definitions = [p.name for p in FRONTEND.glob("*.js") if re.search(r"function\s+showCaptureToast\s*\(", p.read_text())]
    assert definitions == ["app.js"]


def test_capture_code_never_uses_the_clobbered_globals():
    """showError/showSuccess are redeclared by other scripts, with other signatures."""
    app = (FRONTEND / "app.js").read_text()
    block = app[app.index("const ANALYTICS_STATUS_ON"):app.index("async function loadAssistantProfileSettings")]
    conversations = (FRONTEND / "conversations.js").read_text()
    for name, text in (("app.js capture block", block), ("conversations.js", conversations)):
        assert not re.search(r"\bshow(Error|Success)\(", text), name


HARNESS = r"""
'use strict';
const vm = require('vm');
const fs = require('fs');
const toasts = [];
const ctx = { console, Date, Number, Object, Array, JSON, String, Math, Promise, URLSearchParams, setTimeout,
              encodeURIComponent, localStorage: { getItem: () => null }, document: { getElementById: () => null },
              Athena: { components: { Toast: {
                  success: (m) => toasts.push(['success', m]), error: (m) => toasts.push(['error', m]) } } } };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext('function showError(containerId, message) { /* follow-me.js: writes into a container, shows no toast */ }', ctx);
const app = fs.readFileSync(process.argv[1] + '/app.js', 'utf8');
function cut(name) {
  const m = app.match(new RegExp(String.raw`(?:async )?function ${name}\(.*?\n\}\n`, 's'));
  if (!m) throw new Error('missing ' + name);
  return m[0];
}
for (const name of ['showCaptureToast', 'captureSaveFailureMessage', 'putCaptureSettings']) vm.runInContext(cut(name), ctx);
for (const file of ['escape-html.js', 'conversations.js']) {
  vm.runInContext(fs.readFileSync(process.argv[1] + '/' + file, 'utf8'), ctx, { filename: file });
}
const scenario = JSON.parse(process.argv[2]);
(async () => {
  vm.runInContext(`
    var captureSettings = { debug_enabled: false, retention_days: 30 };
    function renderDebugDetailControls() {}
    function renderRetentionControls() {}
    async function loadCaptureSettings() {}
    async function loadConversationsList() {}
    var hasPermission = () => true;
  `, ctx);
  ctx.apiRequest = async () => { const e = new Error('server words'); e.status = scenario.putStatus; throw e; };
  ctx.fetch = async () => ({ ok: scenario.purgeStatus === 200, status: scenario.purgeStatus,
    json: async () => scenario.purgeStatus === 200 ? { turns: 3, conversations: 2, aborted: null } : { detail: scenario.detail } });
  if (scenario.kind === 'purge') await ctx.convRunPurge({ scope: 'expired' });
  else await ctx.putCaptureSettings(true, 30);
  process.stdout.write(JSON.stringify(toasts));
})();
"""


def _toasts(**scenario):
    proc = subprocess.run([_node(), "-e", HARNESS, str(FRONTEND), json.dumps(scenario)], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[:600]
    return json.loads(proc.stdout)


@pytest.mark.parametrize("status,detail,expected", [
    (409, "A purge is already running.", "A purge is already running; try again in a minute."),
    (409, "The stored retention is invalid; nothing was deleted.", "The saved retention period isn’t valid, so nothing was deleted. Set one in Settings → Privacy."),
    (429, "Too Many Requests", "Too many purge requests. Try again in a minute."),
    (403, "Insufficient permissions", "Only an owner can delete conversations."),
    (500, "Purge failed: OperationalError", "The purge failed."),
])
def test_a_refused_purge_produces_a_visible_error_toast(status, detail, expected):
    assert _toasts(kind="purge", purgeStatus=status, detail=detail) == [["error", expected]]


def test_a_successful_purge_produces_a_success_toast():
    assert _toasts(kind="purge", purgeStatus=200, detail="") == [["success", "Deleted 3 turns in 2 conversations."]]


@pytest.mark.parametrize("status,expected", [
    (422, "Enter a whole number of days from 1 to 3650."),
    (403, "Only an owner can shorten retention."),
    (500, "Couldn’t save capture settings. Nothing was changed."),
])
def test_a_failed_capture_save_produces_a_visible_status_mapped_toast(status, expected):
    assert _toasts(kind="put", putStatus=status, purgeStatus=200, detail="") == [["error", expected]]
