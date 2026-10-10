"""apiRequest turns a FastAPI error body into a readable message.

A 422 validation error carries `detail` as a list of {loc, msg, ...} entries;
`new Error(detail)` used to render it as "[object Object]". The function under
test is cut out of the real admin/frontend/app.js and run in Node.
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


def _node() -> str:
    candidate = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
    if candidate and subprocess.run([candidate, "--version"], capture_output=True, timeout=15).returncode == 0:
        return candidate
    pytest.fail("a working node interpreter is required (ATHENA_NODE_BIN or PATH)", pytrace=False)


def _format(detail, status=422):
    source = APP_JS.read_text()
    match = re.search(r"function formatApiErrorDetail\(detail, status\) \{.*?\n\}\n", source, re.DOTALL)
    assert match, "formatApiErrorDetail not found in app.js"
    script = match.group(0) + f"\nprocess.stdout.write(JSON.stringify(formatApiErrorDetail({json.dumps(detail)}, {status})));"
    proc = subprocess.run([_node(), "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr[:500]
    return json.loads(proc.stdout)


def test_a_422_detail_list_is_joined_with_the_last_loc_segment():
    detail = [
        {"loc": ["body", "retention_days"], "msg": "Input should be greater than or equal to 1", "type": "greater_than_equal"},
        {"loc": ["body", "debug_enabled"], "msg": "Field required", "type": "missing"},
    ]
    assert _format(detail) == "retention_days: Input should be greater than or equal to 1; debug_enabled: Field required"


def test_an_entry_without_loc_keeps_just_its_message():
    assert _format([{"msg": "Something is wrong"}]) == "Something is wrong"


def test_a_string_detail_is_unchanged():
    assert _format("Only an owner can shorten retention.", 403) == "Only an owner can shorten retention."


def test_an_object_with_a_message_uses_it():
    assert _format({"error": "x", "message": "A purge is already running."}, 409) == "A purge is already running."


@pytest.mark.parametrize("detail", [None, {}, [], {"error": "x"}, [{"nope": 1}]])
def test_anything_else_falls_back_to_the_status(detail):
    assert _format(detail, 500) == "HTTP 500"


def test_api_request_uses_the_formatter_and_never_throws_the_raw_detail():
    body = re.search(r"async function apiRequest\(.*?\n\}\n", APP_JS.read_text(), re.DOTALL).group(0)
    assert "formatApiErrorDetail(error.detail, response.status)" in body
    assert "new Error(error.detail" not in body
