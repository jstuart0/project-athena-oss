"""The Emerging Intents page labels withheld sample queries and escapes visible ones.

Runs the real admin/frontend/emerging-intents.js (with escape-html.js) in Node; a missing
interpreter fails rather than skips (this repo allows no new skips).
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

HARNESS = r"""
'use strict';
const vm = require('vm'); const fs = require('fs');
const ctx = { console, Date, Number, Object, Array, JSON, String, Math, Promise, setTimeout,
              localStorage: { getItem: () => null }, document: { getElementById: () => null } };
ctx.window = ctx; vm.createContext(ctx);
for (const f of ['escape-html.js', 'emerging-intents.js']) vm.runInContext(fs.readFileSync(process.argv[1] + '/' + f, 'utf8'), ctx, { filename: f });
const base = { id: 1, canonical_name: 'garage_plan', display_name: 'Garage', description: 'd', status: 'discovered', occurrence_count: 2 };
const data = JSON.parse(process.argv[2]);
const out = vm.runInContext('(function(d){ return { hidden: renderIntentRow(Object.assign({}, d.base, {sample_queries: null, text_visible: false})), visible: renderIntentRow(Object.assign({}, d.base, {sample_queries: [d.hostile, "plain one"], text_visible: true})) }; })', ctx)({ base, hostile: data.hostile });
process.stdout.write(JSON.stringify(out));
"""


def _node() -> str:
    candidate = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
    if candidate and subprocess.run([candidate, "--version"], capture_output=True, timeout=15).returncode == 0:
        return candidate
    pytest.fail("a working node interpreter is required (ATHENA_NODE_BIN or PATH)", pytrace=False)


@pytest.fixture(scope="module")
def rendered():
    proc = subprocess.run([_node(), "-e", HARNESS, str(FRONTEND), json.dumps({"hostile": HOSTILE})],
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[:600]
    return json.loads(proc.stdout)


def test_withheld_samples_say_so_and_show_no_list(rendered):
    assert "Sample queries: Text hidden: owner only" in rendered["hidden"]
    assert "<details" not in rendered["hidden"]


def test_visible_samples_are_listed_and_escaped(rendered):
    assert "plain one" in rendered["visible"] and "Text hidden" not in rendered["visible"]
    assert "<img" not in rendered["visible"] and "&lt;img" in rendered["visible"]
