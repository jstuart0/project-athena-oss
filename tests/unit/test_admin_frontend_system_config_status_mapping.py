"""ATHENA-113c — system-config.js's mapServiceStatusToUiBucket.

GET /api/status now derives service health from the registry (statuses:
healthy/unhealthy/unconfigured/disabled/pending) instead of probing a
hardcoded "Mac Studio" host, so the System Configuration page's Gateway/
Orchestrator/Ollama cards must classify those new strings into a neutral
("Not Configured") bucket rather than the old binary healthy/Offline split
-- an operator-disabled service or one still starting up is not the same
claim as "this is down".

Executed via vm.runInThisContext rather than require(): system-config.js is
a plain browser <script> (top-level function declarations become globals
only via non-module script execution), so Node's CommonJS module wrapper
would hide mapServiceStatusToUiBucket from the test entirely.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SYSTEM_CONFIG_JS = REPO_ROOT / "admin" / "frontend" / "system-config.js"
STATUS_VOCAB_JS = REPO_ROOT / "admin" / "frontend" / "status-vocab.js"

NODE_BIN = shutil.which("node")
requires_node = pytest.mark.skipif(NODE_BIN is None, reason="node is required for this fixture")


def _map_status(service: dict):
    script = f"""
    'use strict';
    const vm = require('vm');
    const fs = require('fs');
    global.window = global;
    global.document = {{ getElementById: () => null, querySelectorAll: () => [] }};
    global.fetch = () => Promise.resolve({{ ok: false }});
    vm.runInThisContext(fs.readFileSync({json.dumps(str(STATUS_VOCAB_JS))}, 'utf8'), {{ filename: 'status-vocab.js' }});
    global.escapeHtml = (value) => String(value);
    const src = fs.readFileSync({json.dumps(str(SYSTEM_CONFIG_JS))}, 'utf8');
    vm.runInThisContext(src, {{ filename: 'system-config.js' }});
    const result = mapServiceStatusToUiBucket({json.dumps(service)});
    process.stdout.write(JSON.stringify(result));
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@requires_node
@pytest.mark.parametrize("service,expected", [
    ({"healthy": True, "status": "healthy"}, "good"),
    ({"healthy": False, "status": "healthy (auth required)"}, "bad"),  # healthy=False wins over a good-looking word
    ({"healthy": False, "status": "unhealthy"}, "bad"),
    ({"healthy": False, "status": "disabled"}, "neutral"),
    ({"healthy": False, "status": "unconfigured"}, "setup"),
    ({"healthy": False, "status": "not configured"}, "setup"),
    ({"healthy": False, "status": "pending"}, "neutral"),
    ({"healthy": False, "status": "stale"}, "neutral"),  # the checker hasn't reached it: unknown, not down
    ({"healthy": False, "status": "error"}, "offline"),
    ({"healthy": False, "status": "error: HTTP 503"}, "offline"),
    ({"healthy": False, "status": "timeout"}, "offline"),
    ({"healthy": False, "status": "unreachable"}, "offline"),
    ({"healthy": False, "status": "not deployed"}, "offline"),
    ({"healthy": False, "status": "a word nobody has seen"}, "bad"),
    ({"healthy": False}, "neutral"),
])
def test_map_service_status_to_tone(service, expected):
    assert _map_status(service) == expected


@requires_node
def test_healthy_true_always_wins_regardless_of_status_string():
    assert _map_status({"healthy": True, "status": "unconfigured"}) == "good"


def _eval(expression: str):
    script = f"""
    'use strict';
    const vm = require('vm');
    const fs = require('fs');
    global.window = global;
    global.document = {{ getElementById: () => null, querySelectorAll: () => [] }};
    global.fetch = () => Promise.resolve({{ ok: false }});
    global.escapeHtml = (value) => String(value);
    vm.runInThisContext(fs.readFileSync({json.dumps(str(STATUS_VOCAB_JS))}, 'utf8'), {{ filename: 'status-vocab.js' }});
    vm.runInThisContext(fs.readFileSync({json.dumps(str(SYSTEM_CONFIG_JS))}, 'utf8'), {{ filename: 'system-config.js' }});
    process.stdout.write(JSON.stringify({expression}));
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@requires_node
@pytest.mark.parametrize("tone,word,expected", [
    ("good", None, "Healthy"),
    ("bad", "unhealthy", "Unhealthy"),
    ("offline", "not deployed", "Unreachable"),
    ("offline", "timeout", "Not responding"),
    ("offline", None, "Unreachable"),
    ("setup", "unconfigured", "Needs setup"),
    ("setup", "not configured", "Needs setup"),
    ("neutral", "stale", "No recent check"),
    ("neutral", "disabled", "Not monitored"),
    ("neutral", "pending", "Checking…"),
    ("neutral", None, "Not reported"),
    ("bad", "<img src=x>", "<img src=x>"),   # the raw word; the renderer escapes it
])
def test_the_card_label_is_the_shared_label_of_the_word_behind_the_tone(tone, word, expected):
    assert _eval(f"serviceCardLabel({json.dumps(tone)}, {json.dumps(word)})") == expected


@requires_node
@pytest.mark.parametrize("tone,color,icon,pulses", [
    ("good", "green", "✅", False),
    ("bad", "red", "❌", True),
    ("offline", "red", "❌", True),
    ("setup", "amber", "⚠️", False),
    ("neutral", "gray", "➖", False),
])
def test_each_tone_has_its_colour_icon_and_pulse(tone, color, icon, pulses):
    view = _eval(f"SERVICE_CARD_VIEW[{json.dumps(tone)}]")
    assert (view["color"], view["icon"]) == (color, icon)
    assert view["pulse"] == ("motion-safe:animate-pulse" if pulses else "")


@requires_node
def test_unhealthy_is_red_not_yellow_and_the_card_escapes_the_service_name():
    assert _map_status({"healthy": False, "status": "unhealthy"}) == "bad"
    source = SYSTEM_CONFIG_JS.read_text()
    assert "${escapeHtml(svc.name)}" in source and "${svc.name}" not in source
    assert "rounded-full bg-${view.color}-500 ${view.pulse}" in source
