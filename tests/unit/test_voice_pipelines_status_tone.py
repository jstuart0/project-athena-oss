"""voice-pipelines.js statusTone: a service that is not reporting is grey, not red.

GET /api/status now also says a service is `stale` (the health checker hasn't
reached it recently), `disabled`, `pending` or `unconfigured`. None of those is
"down", so the voice pages must not paint them red. The tone is a closed
set, so a hostile or unknown status can only ever pick one of three classes.

The function lives inside the page's IIFE, so it is extracted by name and run in
Node (no DOM needed), the same way the other frontend helpers are tested.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
VOICE_PIPELINES_JS = REPO_ROOT / "admin" / "frontend" / "voice-pipelines.js"

# A missing node is a hard failure here, never a skip (the frontend-escaping job
# always provides it).
NODE_BIN = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
assert NODE_BIN, "node is required for the voice-pipelines status tone tests (set ATHENA_NODE_BIN)"


def _block(marker: str) -> str:
    source = VOICE_PIPELINES_JS.read_text(encoding="utf-8")
    start = source.index(marker)
    if marker.startswith("const "):
        return source[start:source.index(";", start) + 1]
    brace = source.index("{", source.index(")", start))
    depth = 0
    for index in range(brace, len(source)):
        depth += {"{": 1, "}": -1}.get(source[index], 0)
        if depth == 0:
            return source[start:index + 1]
    raise AssertionError(f"unbalanced braces after {marker!r}")


def _run(expression: str):
    sources = [_block("const NEUTRAL_STATUSES"), _block("function statusTone("), _block("function statusWord(")]
    script = f"""
    'use strict';
    {chr(10).join(sources)}
    process.stdout.write(JSON.stringify({expression}));
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.parametrize("service,tone", [
    ({"healthy": True, "status": "healthy"}, "green"),
    ({"healthy": False, "status": "running"}, "green"),
    ({"healthy": False, "status": "running (auth required)"}, "green"),
    ({"healthy": False, "status": "stale"}, "gray"),
    ({"healthy": False, "status": "disabled"}, "gray"),
    ({"healthy": False, "status": "pending"}, "gray"),
    ({"healthy": False, "status": "unconfigured"}, "gray"),
    ({"healthy": False, "status": "not configured"}, "gray"),
    (None, "gray"),
    ({"healthy": False, "status": "unhealthy"}, "red"),
    ({"healthy": False, "status": "timeout"}, "red"),
    ({"healthy": False, "status": "a status nobody has seen"}, "red"),
    ({"healthy": False, "status": "<img src=x>"}, "red"),
    ({"healthy": False}, "red"),
])
def test_status_tone(service, tone):
    assert _run(f"statusTone({json.dumps(service)})") == tone


def test_tone_is_always_one_of_the_three_classes():
    for status in ("stale", "x", "constructor", "__proto__", ""):
        assert _run(f"statusTone({{healthy: false, status: {json.dumps(status)}}})") in {"green", "gray", "red"}


@pytest.mark.parametrize("service,word", [
    ({"healthy": True}, "healthy"),
    ({"healthy": False, "status": "stale"}, "No recent check"),
    ({"healthy": False, "status": "unhealthy"}, "unhealthy"),
    ({"healthy": False}, "not configured"),
    (None, "not configured"),
])
def test_status_word(service, word):
    assert _run(f"statusWord({json.dumps(service)})") == word


def test_both_voice_page_renderers_use_the_tone():
    source = VOICE_PIPELINES_JS.read_text(encoding="utf-8")
    assert "const statusColor = statusTone(service);" in source
    assert "statusTone(s)" in source
    assert "s.healthy ? 'bg-green-500' : 'bg-red-500'" not in source
