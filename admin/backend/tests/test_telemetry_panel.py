"""admin/frontend/telemetry.js under Node with a minimal DOM stub.

The stub throws on any innerHTML write, so a server value can only ever
reach the page as text. Rendered states: owner (controls), operator (no
controls), environment-locked (toggle disabled, explanation shown), and a
hostile server string that must come out verbatim as text.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

TELEMETRY_JS = Path(__file__).resolve().parents[2] / "frontend" / "telemetry.js"
NODE = shutil.which("node")

HARNESS = r"""
'use strict';
const vm = require('vm');
const fs = require('fs');

class Node {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this._text = '';
    this.attrs = {};
    this.listeners = {};
    this.disabled = false;
    this.className = '';
  }
  set innerHTML(_v) { throw new Error('innerHTML write on ' + this.tagName); }
  get innerHTML() { throw new Error('innerHTML read on ' + this.tagName); }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  all(tag) {
    const out = [];
    const walk = (n) => { if (n.tagName === tag) out.push(n); n.children.forEach(walk); };
    walk(this);
    return out;
  }
}

const panel = new Node('div');
global.window = global;
global.document = {
  createElement: (tag) => new Node(tag),
  getElementById: (id) => (id === 'telemetry-panel' ? panel : null),
};
global.getAuthHeaders = (extra) => Object.assign({}, extra);
global.confirm = () => false;
const status = JSON.parse(process.argv[2]);
global.fetch = async () => ({ ok: true, status: 200, json: async () => status });

vm.runInThisContext(fs.readFileSync(process.argv[1], 'utf8'), { filename: 'telemetry.js' });

(async () => {
  initTelemetryPanel();
  await new Promise((r) => setTimeout(r, 20));
  const buttons = panel.all('BUTTON').map((b) => ({ text: b.textContent, disabled: b.disabled,
    hasClick: typeof b.listeners.click === 'function' }));
  process.stdout.write(JSON.stringify({ text: panel.textContent, buttons,
    pres: panel.all('PRE').map((p) => p.textContent) }));
  destroyTelemetryPanel();
})().catch((e) => { process.stderr.write(String(e && e.stack || e)); process.exit(3); });
"""

BASE = {
    "enabled": True,
    "reason": "enabled",
    "env_locked": False,
    "endpoint": "https://collector.example.org/v1/ping",
    "installation_id": "3f1c9b2e-7d4a-4c1e-9a6b-2f8e5d7c1a90",
    "install_class": "self_hosted_real",
    "provenance": "new",
    "release_channel": "stable",
    "schema_version": 1,
    "last_attempt_at": "2026-09-30T12:00:00+00:00",
    "last_success_at": "2026-09-30T12:00:00+00:00",
    "last_error": None,
    "next_due_at": "2026-10-01T11:00:00+00:00",
    "last_payload": {"event": "first_boot", "schema_version": 1},
    "can_manage": True,
}


def _render(**overrides):
    assert NODE, "node is required for the telemetry panel test"
    status = {**BASE, **overrides}
    proc = subprocess.run([NODE, "-e", HARNESS, str(TELEMETRY_JS), json.dumps(status)],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_owner_sees_controls_and_the_last_payload():
    out = _render()
    assert [b["text"] for b in out["buttons"]] == ["Turn off", "Send now", "Reset telemetry identity"]
    assert all(b["hasClick"] and not b["disabled"] for b in out["buttons"])
    assert "pseudonymous" in out["text"] and "analytics mode" in out["text"]
    assert "https://collector.example.org/v1/ping" in out["text"]
    assert json.loads(out["pres"][0]) == {"event": "first_boot", "schema_version": 1}


def test_operator_sees_no_controls():
    out = _render(can_manage=False)
    assert out["buttons"] == []
    assert "3f1c9b2e-7d4a-4c1e-9a6b-2f8e5d7c1a90" in out["text"]


def test_environment_lock_disables_the_toggle_and_explains():
    out = _render(enabled=False, reason="env_do_not_track", env_locked=True)
    toggle = out["buttons"][0]
    assert toggle["text"] == "Turn on" and toggle["disabled"] is True
    assert all(b["disabled"] for b in out["buttons"])
    assert "Off (DO_NOT_TRACK)" in out["text"]
    assert "Switched off by the environment" in out["text"]


def test_server_strings_are_text_only():
    hostile = '<img src=x onerror="alert(1)">'
    out = _render(endpoint=hostile, reason="endpoint_invalid", enabled=False, env_locked=True,
                  last_error={"type": hostile, "status": None})
    assert out["text"].count(hostile) == 2
