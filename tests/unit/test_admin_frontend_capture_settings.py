"""Settings -> Privacy capture controls: retention Save rules and the debug toggle.

The functions are cut out of the real admin/frontend/app.js and run in Node
against a tiny fake document.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "admin" / "frontend" / "app.js"
FUNCTIONS = ("retentionInputValue", "onRetentionInput", "renderDebugDetailControls", "captureRetentionInvalid",
             "saveDebugDetail", "captureSaveFailureMessage")


def _node() -> str:
    candidate = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
    if candidate and subprocess.run([candidate, "--version"], capture_output=True, timeout=15).returncode == 0:
        return candidate
    pytest.fail("a working node interpreter is required (ATHENA_NODE_BIN or PATH)", pytrace=False)


def _source():
    text = APP_JS.read_text()
    parts = []
    for name in FUNCTIONS:
        match = re.search(rf"(?:async )?function {name}\(.*?\n\}}\n", text, re.DOTALL)
        assert match, name
        parts.append(match.group(0))
    return "\n".join(parts)


def _run(scenario: str):
    script = f"""
const els = {{}};
function el(id, props) {{ els[id] = Object.assign({{ id, value: '', textContent: '', disabled: false, checked: false,
  attrs: {{}}, setAttribute(k, v) {{ this.attrs[k] = v; }} }}, props || {{}}); return els[id]; }}
global.document = {{ getElementById: (id) => els[id] || null }};
{_source()}
let captureSettings = null;
let owner = false;
global.hasPermission = (p) => owner && p === 'delete';
{scenario}
"""
    proc = subprocess.run([_node(), "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[:600]
    return json.loads(proc.stdout)


def _retention(stored, typed, owner):
    return _run(f"""
el('retention-days-input', {{ value: {json.dumps(typed)} }});
el('retention-save-btn'); el('retention-help');
captureSettings = {{ retention_days: {stored} }};
owner = {str(owner).lower()};
onRetentionInput();
process.stdout.write(JSON.stringify({{ disabled: els['retention-save-btn'].disabled, help: els['retention-help'].textContent }}));
""")


def test_an_operator_can_raise_retention():
    assert _retention(30, "365", owner=False)["disabled"] is False


def test_an_operator_cannot_shorten_retention_and_is_told_why():
    got = _retention(365, "30", owner=False)
    assert got == {"disabled": True, "help": "Only an owner can shorten retention."}


def test_an_owner_can_shorten_retention():
    assert _retention(365, "30", owner=True)["disabled"] is False


@pytest.mark.parametrize("typed", ["", "0", "3651", "abc", "30.5", "-4", "1e2", " "])
def test_an_invalid_value_cannot_be_saved(typed):
    got = _retention(30, typed, owner=True)
    assert got["disabled"] is True and "1 to 3650" in got["help"]


def test_an_unchanged_value_has_nothing_to_save():
    assert _retention(30, "30", owner=True)["disabled"] is True


def test_the_debug_toggle_is_disabled_with_a_reason_while_analytics_is_off():
    got = _run("""
el('debug-detail-toggle'); el('debug-detail-label'); el('debug-detail-status');
captureSettings = { debug_enabled: true, debug_enabled_since: null };
renderDebugDetailControls(false);
const t = els['debug-detail-toggle'];
process.stdout.write(JSON.stringify({ disabled: t.disabled, status: els['debug-detail-status'].textContent, checked: t.checked }));
""")
    assert got["disabled"] is True and got["status"] == "Turn on Analytics Mode first."


def test_the_debug_toggle_is_enabled_and_dated_while_analytics_is_on():
    got = _run("""
el('debug-detail-toggle'); el('debug-detail-label'); el('debug-detail-status');
captureSettings = { debug_enabled: true, debug_enabled_since: '2026-10-05T12:00:00Z', retention_days: 30 };
renderDebugDetailControls(true);
const t = els['debug-detail-toggle'];
process.stdout.write(JSON.stringify({ disabled: t.disabled, status: els['debug-detail-status'].textContent,
  checked: t.checked, aria: t.attrs['aria-checked'], label: els['debug-detail-label'].textContent }));
""")
    assert got["disabled"] is False and got["checked"] is True and got["aria"] == "true" and got["label"] == "On"
    assert got["status"].startswith("Debug detail ON since ")


def test_the_debug_toggle_is_disabled_with_a_reason_while_retention_is_invalid():
    for settings in ("{ debug_enabled: false, retention_state: 'retention_invalid', retention_days: null }",
                     "{ debug_enabled: false, retention_state: 'ok', retention_days: null }"):
        got = _run(f"""
el('debug-detail-toggle'); el('debug-detail-label'); el('debug-detail-status');
captureSettings = {settings};
renderDebugDetailControls(true);
process.stdout.write(JSON.stringify({{ disabled: els['debug-detail-toggle'].disabled, status: els['debug-detail-status'].textContent }}));
""")
        assert got == {"disabled": True, "status": "Set a retention period first."}


def test_saving_the_debug_switch_never_sends_a_fallback_retention():
    got = _run("""
el('analytics-mode-toggle', { checked: true });
el('debug-detail-toggle'); el('debug-detail-label'); el('debug-detail-status');
const sent = [];
global.putCaptureSettings = async (debug, days) => { sent.push([debug, days]); return true; };
captureSettings = { debug_enabled: false, retention_state: 'retention_invalid', retention_days: null };
(async () => {
  await saveDebugDetail(true);
  captureSettings = { debug_enabled: false, retention_state: 'ok', retention_days: 365 };
  await saveDebugDetail(true);
  process.stdout.write(JSON.stringify(sent));
})();
""")
    assert got == [[True, 365]], "nothing is sent while retention is invalid, and the stored value is what goes out"


@pytest.mark.parametrize("status,message", [
    (422, "Enter a whole number of days from 1 to 3650."),
    (403, "Only an owner can shorten retention."),
    (500, "Couldn’t save capture settings. Nothing was changed."),
    (None, "Couldn’t save capture settings. Nothing was changed."),
])
def test_a_failed_save_gets_a_fixed_message_by_status(status, message):
    got = _run(f"process.stdout.write(JSON.stringify(captureSaveFailureMessage({{ status: {json.dumps(status)}, message: 'server words' }})));")
    assert got == message
