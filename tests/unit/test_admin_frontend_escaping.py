"""
Behavioural tests for admin/frontend/escape-html.js (ATHENA-67).

These run the real production file in a Node subprocess (not a reimplementation)
so a regression to the shipped file is guaranteed to be caught here.

Node resolution (D3, ATHENA-66 Phase 1)
----------------------------------------
The interpreter is resolved once, at import time, as
`ATHENA_NODE_BIN` env var (set by CI from `actions/setup-node`'s pinned
exact version) or `shutil.which("node")` (a developer laptop). If neither
resolves to a WORKING interpreter (`--version` exits 0):

  - `ATHENA_REQUIRE_NODE` set  -> `pytest.fail()` at collection time. CI sets
    this, so a broken/missing node interpreter is a hard failure, never a
    silent module-wide skip.
  - `ATHENA_REQUIRE_NODE` unset -> `pytest.skip()`, naming the env var. This
    is the ONLY permitted skip in the campaign, and CI can never take it
    (CI always sets ATHENA_REQUIRE_NODE=1).

As shipped (pre-Phase-1), this module used a bare `skipif` on
`node --version`'s return code: node PRESENT-BUT-ERRORING (a broken install,
a stale wrapper) silently skipped the whole module and pytest exited 0,
having asserted nothing about escape-html.js — catalogue instance 7. This
harness closes that: the failure mode of a broken interpreter is now
distinguished from the failure mode of no interpreter at all, and CI's
`ATHENA_REQUIRE_NODE=1` converts either into a hard failure.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "admin" / "frontend"
ESCAPE_HTML_JS = FRONTEND_DIR / "escape-html.js"


def _resolve_node_bin() -> str | None:
    candidate = os.environ.get("ATHENA_NODE_BIN") or shutil.which("node")
    if not candidate:
        return None
    try:
        proc = subprocess.run([candidate, "--version"], capture_output=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return candidate if proc.returncode == 0 else None


NODE_BIN = _resolve_node_bin()

if NODE_BIN is None:
    if os.environ.get("ATHENA_REQUIRE_NODE"):
        pytest.fail(
            "ATHENA_REQUIRE_NODE is set but no working node interpreter was found "
            "(checked ATHENA_NODE_BIN, then PATH via shutil.which). This must fail, "
            "not skip, in CI — a broken interpreter is not evidence escape-html.js works.",
            pytrace=False,
        )
    pytestmark = pytest.mark.skip(
        reason=(
            "no working node interpreter found (checked ATHENA_NODE_BIN, then PATH) "
            "and ATHENA_REQUIRE_NODE is not set. Set ATHENA_REQUIRE_NODE=1 to make a "
            "missing/broken node interpreter a hard failure instead of a skip."
        )
    )


def _run_node(js_expr: str) -> str:
    """
    Load escape-html.js in a bare Node context (no DOM) and evaluate js_expr,
    which must be a single expression producing a JSON-serializable value.
    Returns the parsed JSON result.
    """
    script = f"""
    'use strict';
    global.window = global;
    require({json.dumps(str(ESCAPE_HTML_JS))});
    const result = (function() {{ return {js_expr}; }})();
    process.stdout.write(JSON.stringify(result));
    """
    proc = subprocess.run(
        [NODE_BIN, "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_escape_html_js_exists():
    assert ESCAPE_HTML_JS.is_file(), f"expected {ESCAPE_HTML_JS} to exist"


def test_escape_html_js_has_no_dom_access():
    """escape-html.js must be testable in bare Node — no document/DOM references."""
    text = ESCAPE_HTML_JS.read_text()
    for forbidden in ("document.", "createElement", "innerHTML", "textContent"):
        assert forbidden not in text, f"escape-html.js must not reference {forbidden!r}"


# ---------------------------------------------------------------------------
# escapeHtml: 11 behavioural assertions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("<", "&lt;"),
        (">", "&gt;"),
        ("&", "&amp;"),
        ('"', "&quot;"),
        ("'", "&#39;"),
    ],
)
def test_escape_html_single_chars(raw, expected):
    result = _run_node(f"window.escapeHtml({json.dumps(raw)})")
    assert result == expected


def test_escape_html_byte_pin_single_quote():
    # Load-bearing byte-pin: must be exactly &#39;, NOT &#039; (model-downloads.js variant).
    assert _run_node("window.escapeHtml(\"'\")") == "&#39;"


def test_escape_html_composite_no_raw_chars_survive():
    raw = "<script>alert('x&y\")</script>"
    result = _run_node(f"window.escapeHtml({json.dumps(raw)})")
    for forbidden in ("<", ">"):
        assert forbidden not in result
    assert '"' not in result
    assert "'" not in result
    assert "&" not in result.replace("&lt;", "").replace("&gt;", "").replace(
        "&quot;", ""
    ).replace("&#39;", "").replace("&amp;", "")


def test_escape_html_null_undefined_empty():
    assert _run_node("window.escapeHtml(null)") == ""
    assert _run_node("window.escapeHtml(undefined)") == ""
    assert _run_node("window.escapeHtml('')") == ""


def test_escape_html_plain_text_unchanged():
    assert _run_node('window.escapeHtml("hello world 123")') == "hello world 123"


# ---------------------------------------------------------------------------
# escapeJsAttr: round-trip assertions.
#
# For each payload: simulate the browser's two-stage decode (HTML attribute
# value decode, then JS string-literal parse) and assert the recovered value
# is byte-identical to the original input.
# ---------------------------------------------------------------------------

_ENTITY_MAP = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&quot;": '"',
    "&#39;": "'",
}
_ENTITY_RE = re.compile("|".join(re.escape(k) for k in _ENTITY_MAP))


def _html_attr_decode(s: str) -> str:
    """
    Single left-to-right pass, matching a real HTML parser's attribute-value
    decoding. Chained sequential str.replace() calls would be wrong here:
    they can re-scan already-decoded output (e.g. decoding "&amp;#39;" to "&"
    then, on a later pass, matching the now-adjacent "#39;" against a
    differently-encoded entity) and produce a double-decode that no browser
    performs.
    """
    return _ENTITY_RE.sub(lambda m: _ENTITY_MAP[m.group(0)], s)


def _js_single_quoted_string_parse(escaped_js: str) -> str:
    """Parse escaped_js as the body of a JS single-quoted string literal via Node's own parser."""
    script = f"""
    'use strict';
    const s = '{escaped_js}';
    process.stdout.write(JSON.stringify(s));
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"node failed to parse JS string literal: {proc.stderr}\nliteral body: {escaped_js!r}"
    return json.loads(proc.stdout)


ROUND_TRIP_PAYLOADS = [
    "x');alert(1)//",
    "\\",
    '"',
    " ",
    " ",
    "&#39;",
    "plain text",
    "back\\slash'quote\"end",
    "line1\nline2\rline3",
]


@pytest.mark.parametrize("payload", ROUND_TRIP_PAYLOADS)
def test_escape_js_attr_round_trip(payload):
    escaped = _run_node(f"window.escapeJsAttr({json.dumps(payload)})")
    # Stage 1: browser HTML-attribute-decodes the value before compiling the
    # onclick="..." handler body as JS.
    js_literal_body = _html_attr_decode(escaped)
    # Stage 2: browser parses that decoded text as a JS single-quoted string literal.
    recovered = _js_single_quoted_string_parse(js_literal_body)
    assert recovered == payload


def test_escape_js_attr_null_undefined():
    assert _run_node("window.escapeJsAttr(null)") == ""
    assert _run_node("window.escapeJsAttr(undefined)") == ""


def test_escape_js_attr_backslash_ordering_is_load_bearing():
    """
    A trailing backslash immediately before a quote must not let the
    attacker's backslash re-pair with the escaping backslash for the quote.
    This is exactly the case the load-bearing backslash-first ordering guards.
    """
    payload = "\\'"  # literal: backslash followed by single quote
    escaped = _run_node(f"window.escapeJsAttr({json.dumps(payload)})")
    js_literal_body = _html_attr_decode(escaped)
    recovered = _js_single_quoted_string_parse(js_literal_body)
    assert recovered == payload
