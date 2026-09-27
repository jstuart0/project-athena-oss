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
    ({"healthy": True, "status": "healthy"}, "healthy"),
    ({"healthy": False, "status": "healthy (auth required)"}, "unhealthy"),  # healthy=False wins
    ({"healthy": False, "status": "unhealthy"}, "unhealthy"),
    ({"healthy": False, "status": "disabled"}, "neutral"),
    ({"healthy": False, "status": "unconfigured"}, "neutral"),
    ({"healthy": False, "status": "not configured"}, "neutral"),
    ({"healthy": False, "status": "pending"}, "neutral"),
    ({"healthy": False, "status": "error"}, "offline"),
    ({"healthy": False, "status": "error: HTTP 503"}, "offline"),
    ({"healthy": False, "status": "timeout"}, "offline"),
    ({"healthy": False, "status": "unreachable"}, "offline"),
])
def test_map_service_status_to_ui_bucket(service, expected):
    assert _map_status(service) == expected


@requires_node
def test_healthy_true_always_wins_regardless_of_status_string():
    assert _map_status({"healthy": True, "status": "unconfigured"}) == "healthy"
