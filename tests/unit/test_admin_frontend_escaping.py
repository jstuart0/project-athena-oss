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


# ---------------------------------------------------------------------------
# codex r2 F1 -- end-to-end delivery proof for a FIXED alias site. Not just
# "escapeJsAttr round-trips in isolation" (proven above, and unaffected by
# the bug) but "the actual template shape at a fixed call site delivers the
# raw value byte-identically to the callee, and that value still makes it
# into a request payload/URL correctly." Exercises service-control.js's
# `toggleRagService`/`checkRagServiceHealth` shape end to end: template
# render -> browser attribute decode -> JS string-literal parse -> the real
# callee body executes and the value reaches `encodeURIComponent`.
# ---------------------------------------------------------------------------


def _simulate_onclick_call(rendered_attr_value: str, callee_stub_js: str) -> str:
    """`rendered_attr_value` is the RAW (undecoded) text that ended up inside
    the onclick="..." attribute value in the rendered HTML -- exactly what a
    real browser's HTML parser handed to it. Applies the two-stage decode
    (HTML attribute decode, then JS statement parse/execute against a stub
    callee) and returns whatever the stub callee recorded, round-tripped
    through JSON so the assertion below compares real values, not source text.
    """
    js_source = _html_attr_decode(rendered_attr_value)
    script = f"""
    'use strict';
    let captured;
    {callee_stub_js}
    {js_source}
    process.stdout.write(JSON.stringify(captured));
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"node failed executing decoded handler body: {proc.stderr}\nsource: {js_source!r}"
    return json.loads(proc.stdout)


def test_delivery_proof_fixed_alias_site_service_control_toggle():
    """service-control.js:673 (post-fix): `toggleRagService('${escapeJsAttr(rawName)}')`.

    Renders the REAL template shape with the raw value that reproduces the
    regression codex found (`svc&one` -- an ampersand, so a double-escape
    would leave `&amp;` in the delivered string), runs the real
    escape-html.js, then proves: (1) the callee receives the value
    byte-identical to the raw input, and (2) that value still builds the
    correct API request URL via encodeURIComponent -- the actual downstream
    consequence of the regression (wrong resource, or a failed request).
    """
    raw_name = "svc&one"
    escaped = _run_node(f"window.escapeJsAttr({json.dumps(raw_name)})")
    rendered_attr_value = f"toggleRagService('{escaped}')"

    delivered = _simulate_onclick_call(
        rendered_attr_value,
        "function toggleRagService(serviceName) { captured = serviceName; }",
    )
    assert delivered == raw_name, (
        f"callee must receive the RAW value byte-identical: got {delivered!r}, expected {raw_name!r}"
    )

    # The actual downstream consequence in service-control.js's toggleRagService:
    # `apiRequest('/api/service-registry/services/${encodeURIComponent(serviceName)}/toggle')`.
    # Prove the delivered value still builds the correct request path.
    expected_url_segment = _run_node(f"encodeURIComponent({json.dumps(raw_name)})")
    delivered_url_segment = _run_node(f"encodeURIComponent({json.dumps(delivered)})")
    assert delivered_url_segment == expected_url_segment, (
        "the payload must still be intact at the point it builds the API request URL"
    )


def test_delivery_proof_pre_fix_shape_would_have_mangled_the_same_payload():
    """Negative control proving the proof above actually discriminates the
    regression: re-run the identical scenario through the SHIPPED
    pre-fix shape (`escapeJsAttr(escapeHtml(rawName))`) and confirm the
    callee receives entity text, not the raw value -- gate-script rule 6,
    a positive assertion is only meaningful alongside proof it can fail.
    """
    raw_name = "svc&one"
    double_escaped = _run_node(
        f"window.escapeJsAttr(window.escapeHtml({json.dumps(raw_name)}))"
    )
    rendered_attr_value = f"toggleRagService('{double_escaped}')"

    delivered = _simulate_onclick_call(
        rendered_attr_value,
        "function toggleRagService(serviceName) { captured = serviceName; }",
    )
    assert delivered != raw_name, (
        "sanity check failed: the pre-fix double-escape shape must NOT deliver "
        "the raw value -- if it does, this proof can no longer distinguish the fix"
    )
    assert delivered == "svc&amp;one", f"expected the documented mangled form, got {delivered!r}"


# ---------------------------------------------------------------------------
# ATHENA-99: dashboard service badges must never render the literal text
# "undefined". app.js:934-944 and :2358-2363 read `service.status`, but
# /api/service-registry/services returns `health_status` (NULL normalised
# to 'pending' server-side) and, after DC10, `unconfigured` -- `status`
# is a field the API has never sent. Both call sites now go through
# serviceStatus(service), a pure function with no DOM dependency, so it's
# tested directly in Node rather than requiring the whole app.js (which
# touches `document`/`window.location` at module scope and would need a
# full DOM stub to load at all).
# ---------------------------------------------------------------------------

APP_JS = FRONTEND_DIR / "app.js"


def _extract_function_source(file_path: Path, function_name: str) -> str:
    """Extract a single top-level `function NAME(...) { ... }` block by
    brace-matching from its `function` keyword to the closing brace --
    robust to nested braces in the body, unlike a naive regex."""
    source = file_path.read_text()
    marker = f"function {function_name}("
    start = source.index(marker)
    brace_start = source.index("{", start)
    depth = 0
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unbalanced braces extracting {function_name} from {file_path}")


def _run_node_with_source(js_source: str, js_expr: str):
    """Evaluate js_expr in a bare Node context after loading js_source
    (arbitrary JS text, not necessarily a whole file) -- no `require()`,
    no DOM stub, since the functions under test here have no DOM
    dependency at all."""
    script = f"""
    'use strict';
    {js_source}
    const result = (function() {{ return {js_expr}; }})();
    process.stdout.write(JSON.stringify(result === undefined ? "__undefined__" : result));
    """
    proc = subprocess.run(
        [NODE_BIN, "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


def test_service_status_helper_exists():
    source = APP_JS.read_text()
    assert "function serviceStatus(" in source, (
        "expected a serviceStatus(service) helper in app.js (ATHENA-99)"
    )


@pytest.mark.parametrize("service,expected", [
    ({"health_status": "healthy"}, "healthy"),
    ({"health_status": "unconfigured"}, "unconfigured"),
    ({"health_status": None, "status": "offline"}, "offline"),  # legacy fallback
    ({}, "unknown"),  # registry-shaped object lacking BOTH fields entirely
    ({"name": "weather", "host": "athena-rag-weather", "port": 8010}, "unknown"),
])
def test_service_status_never_returns_undefined(service, expected):
    """The exact regression this ticket closes: a registry entry with
    neither health_status nor status must resolve to 'unknown', never the
    literal string "undefined" (or JS `undefined` itself)."""
    fn_source = _extract_function_source(APP_JS, "serviceStatus")
    result = _run_node_with_source(fn_source, f"serviceStatus({json.dumps(service)})")
    assert result == expected
    assert result != "__undefined__"
    assert "undefined" not in str(result)


def test_dashboard_badge_call_sites_use_service_status_helper():
    """Static check that both known call sites (the Dashboard tab's
    per-group cards and the RAG Services tab's registry cards) compute
    status via the helper rather than reading service.status directly,
    and render it through escapeHtml."""
    source = APP_JS.read_text()

    # Both card-rendering blocks must call serviceStatus(service).
    assert source.count("const status = serviceStatus(service);") >= 2, (
        "expected both app.js dashboard-card render sites to call "
        "serviceStatus(service) -- found fewer than 2"
    )

    # And the resolved status text must be escaped when rendered.
    assert "escapeHtml(status)" in source


def test_no_raw_service_status_interpolation_remains_in_app_js():
    """The literal bug: `${service.status}` interpolated bare (no helper,
    no escaping) renders "undefined" for any /api/service-registry/services
    entry, since that endpoint has never sent a `status` field."""
    source = APP_JS.read_text()
    assert "${service.status}" not in source


# ---------------------------------------------------------------------------
# ATHENA-112 P3: summarizeServices(payload), a pure function with no DOM
# dependency, extracted out of loadStatus() so the dashboard's stat-card
# text/classes and the enabled-service groupings (including the `disabled`
# group) are covered directly in Node -- same extraction pattern as
# serviceStatus() above.
# ---------------------------------------------------------------------------

def _registry_service(name, *, enabled, health_status, service_type="api"):
    return {
        "name": name,
        "display_name": name.title(),
        "enabled": enabled,
        "health_status": health_status,
        "service_type": service_type,
        "host": "127.0.0.1",
        "port": 8000,
    }


def _summarize(payload):
    fn_source = _extract_function_source(APP_JS, "summarizeServices")
    return _run_node_with_source(fn_source, f"summarizeServices({json.dumps(payload)})")


def test_summarize_services_helper_exists():
    source = APP_JS.read_text()
    assert "function summarizeServices(" in source, (
        "expected a summarizeServices(payload) helper in app.js (ATHENA-112 P3)"
    )


def test_three_enabled_healthy_two_disabled_summary():
    """3 enabled healthy + 2 disabled: healthy text '3', enabled text
    '3 (2 disabled)', overall class healthy, and the two disabled rows
    appear ONLY in the disabled group -- never in Core/RAG/Database."""
    payload = {
        "services": [
            _registry_service("gateway", enabled=True, health_status="healthy"),
            _registry_service("orchestrator", enabled=True, health_status="healthy"),
            _registry_service("mode", enabled=True, health_status="healthy"),
            _registry_service("old-svc-a", enabled=False, health_status="healthy"),
            _registry_service("old-svc-b", enabled=False, health_status="unhealthy"),
        ],
        "total_services": 5,
        "enabled_services": 3,
        "disabled_services": 2,
        "healthy_services": 3,
        "overall_health": "healthy",
    }
    result = _summarize(payload)

    assert result["healthyText"] == "3"
    assert result["enabledText"] == "3 (2 disabled)"
    assert result["overallClass"] == "text-green-400"
    assert result["overallText"] == "HEALTHY"

    disabled_names = {s["name"] for s in result["disabled"]}
    assert disabled_names == {"old-svc-a", "old-svc-b"}

    grouped_names = set()
    for services in result["groups"].values():
        grouped_names.update(s["name"] for s in services)
    assert grouped_names == {"gateway", "orchestrator", "mode"}
    assert not (grouped_names & disabled_names), (
        "a disabled row leaked into an enabled group (Core/RAG/Database)"
    )


def test_one_enabled_unhealthy_reads_degraded():
    """1 enabled unhealthy among enabled healthy rows and zero disabled rows:
    overall class degraded, enabled text has no '(N disabled)' suffix."""
    payload = {
        "services": [
            _registry_service("gateway", enabled=True, health_status="healthy"),
            _registry_service("orchestrator", enabled=True, health_status="healthy"),
            _registry_service("broken-svc", enabled=True, health_status="unhealthy"),
        ],
        "total_services": 3,
        "enabled_services": 3,
        "disabled_services": 0,
        "healthy_services": 2,
        "overall_health": "degraded",
    }
    result = _summarize(payload)

    assert result["healthyText"] == "2"
    assert result["enabledText"] == "3"
    assert result["overallClass"] == "text-yellow-400"
    assert result["overallText"] == "DEGRADED"
    assert result["disabled"] == []


def test_load_status_uses_summarize_services_helper():
    """Static check: loadStatus() must render from summarizeServices(data)
    rather than recomputing the grouping/stat-card logic inline."""
    source = APP_JS.read_text()
    assert "const summary = summarizeServices(data);" in source


# ---------------------------------------------------------------------------
# codex diff review MEDIUM: integrations.js read the service-registry
# envelope as if it were a bare array. `/api/service-registry/services`
# returns {services, total_services, ...} -- `ragStatus?.find()` silently
# resolves to undefined against a plain object (no TypeError, since `?.`
# short-circuits on `find` not existing), so this fallback path never
# resolved "connected" for any RAG service integration.
# ---------------------------------------------------------------------------

INTEGRATIONS_JS = FRONTEND_DIR / "integrations.js"


def test_integrations_js_reads_services_array_from_envelope():
    source = INTEGRATIONS_JS.read_text()
    assert "(ragStatus?.services || []).find(" in source, (
        "expected integrations.js to unwrap the {services, ...} envelope "
        "before calling .find() -- ragStatus?.find() silently no-ops against "
        "a plain object (codex diff review)"
    )
    # The regression this closes: calling .find() directly on the envelope
    # object (not its .services array) rather than on the comment describing
    # it -- scoped to the actual call expression, not any mention in prose.
    assert "const service = ragStatus?.find(" not in source, (
        "regression: ragStatus?.find() treats the envelope object as an array"
    )


# ---------------------------------------------------------------------------
# ATHENA-118 Phase 4 -- T12/T13/T14 (test contract). Pure functions only
# (groupServiceControlRows, renderServiceActions, renderStatusCell,
# ollamaModelsMessage) -- no DOM, no fetch mock, per the contract's own
# "Mocking strategy" note. The escaping round-trip (T13 #5) uses the real
# escape-html.js, not a stand-in, per the contract and CLAUDE.md.
# ---------------------------------------------------------------------------

SERVICE_CONTROL_JS = FRONTEND_DIR / "service-control.js"


def _extract_block(file_path: Path, marker: str) -> str:
    """Generalizes _extract_function_source to any top-level brace-delimited
    declaration (`function NAME(`, `const NAME = {`, ...) reachable by a
    unique marker string, by brace-matching from the first `{` after the
    marker to its balanced close (plus a trailing `;` if present).

    For a `function NAME(` marker, the search for the body's opening brace
    skips past the parameter list first -- a destructured parameter (e.g.
    `function f({ a, b }) {`) puts a `{` inside the parens, which a naive
    "first `{` after the marker" search would grab instead of the actual
    function body."""
    source = file_path.read_text()
    start = source.index(marker)
    search_from = start + len(marker)
    if marker.startswith("function") and marker.rstrip().endswith("("):
        paren_depth = 1  # the marker's own trailing '(' already opened one
        i = search_from
        while paren_depth > 0:
            if source[i] == "(":
                paren_depth += 1
            elif source[i] == ")":
                paren_depth -= 1
            i += 1
        search_from = i
    brace_start = source.index("{", search_from)
    depth = 0
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                if end < len(source) and source[end] == ";":
                    end += 1
                return source[start:end]
    raise AssertionError(f"unbalanced braces extracting {marker!r} from {file_path}")


def _run_node_with_sources(sources: list[str], js_expr: str):
    """Like _run_node_with_source, but concatenates several extracted
    blocks (in dependency order) before evaluating js_expr. `global.window
    = global` is set first so escape-html.js's IIFE (which assigns onto
    its `global` parameter) attaches escapeHtml/escapeJsAttr the same way
    it does in a real page load."""
    script = f"""
    'use strict';
    global.window = global;
    {chr(10).join(sources)}
    const result = (function() {{ return {js_expr}; }})();
    process.stdout.write(JSON.stringify(result === undefined ? "__undefined__" : result));
    """
    proc = subprocess.run(
        [NODE_BIN, "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n---script---\n{script}"
    return json.loads(proc.stdout)


def _group_rows(rows):
    fn_source = _extract_block(SERVICE_CONTROL_JS, "function groupServiceControlRows(")
    return _run_node_with_source(fn_source, f"groupServiceControlRows({json.dumps(rows)})")


def _render_status_cell(row):
    sources = [
        ESCAPE_HTML_JS.read_text(),
        _extract_block(SERVICE_CONTROL_JS, "const RUN_STATE_BADGES"),
        _extract_block(SERVICE_CONTROL_JS, "function _isNativelyRunning("),
        _extract_block(SERVICE_CONTROL_JS, "function _healthLine("),
        _extract_block(SERVICE_CONTROL_JS, "function renderStatusCell("),
    ]
    return _run_node_with_sources(sources, f"renderStatusCell({json.dumps(row)})")


def _manager_sources():
    return [
        ESCAPE_HTML_JS.read_text(),
        _extract_block(SERVICE_CONTROL_JS, "const K8S_REASON_TEXT"),
        _extract_block(SERVICE_CONTROL_JS, "function k8sReasonText("),
        _extract_block(SERVICE_CONTROL_JS, "function _managerNoteInfo("),
        _extract_block(SERVICE_CONTROL_JS, "function _managerBadgeLabel("),
        _extract_block(SERVICE_CONTROL_JS, "function _managerBadge("),
        _extract_block(SERVICE_CONTROL_JS, "function _managerNoteSentenceHtml("),
    ]


def _render_service_actions(row):
    sources = _manager_sources() + [
        _extract_block(SERVICE_CONTROL_JS, "const ACTION_LABELS"),
        _extract_block(SERVICE_CONTROL_JS, "const ACTION_CLASSES"),
        _extract_block(SERVICE_CONTROL_JS, "function renderServiceActions("),
    ]
    return _run_node_with_sources(sources, f"renderServiceActions({json.dumps(row)})")


def _ollama_models_message(health, models_error):
    fn_source = _extract_block(SERVICE_CONTROL_JS, "function ollamaModelsMessage(")
    return _run_node_with_source(
        fn_source,
        f"ollamaModelsMessage({json.dumps(health)}, {json.dumps(models_error)})",
    )


# --- T12: groupServiceControlRows -------------------------------------------

def test_t12_group_service_control_rows_trusts_server_group_not_name():
    """A floor of 6 rows across the three groups, and a row whose `group`
    is 'rag' but whose `name` is the deliberately misleading 'orchestrator'
    still lands in the rag bucket -- catches the client re-deriving group
    from the row name instead of trusting row.group (D17 regression)."""
    rows = [
        {"name": "athena-gateway", "group": "core"},
        {"name": "athena-orchestrator", "group": "core"},
        {"name": "redis", "group": "infrastructure"},
        {"name": "qdrant", "group": "infrastructure"},
        {"name": "athena-rag-weather", "group": "rag"},
        # Deliberately misleading: name says "orchestrator" (a core service
        # name), but the server says this row is RAG.
        {"name": "orchestrator", "group": "rag"},
    ]
    assert len(rows) >= 6
    grouped = _group_rows(rows)
    rag_names = [r["name"] for r in grouped["rag"]]
    core_names = [r["name"] for r in grouped["core"]]
    assert "orchestrator" in rag_names, (
        "row.group='rag' must win over the misleading name -- client-side "
        "re-derivation from the name would silently reopen the RAG-row-"
        "rendered-in-Core bug D17 exists to close"
    )
    assert "orchestrator" not in core_names


# --- T13: renderServiceActions / renderStatusCell ---------------------------

def test_t13_manager_none_shows_managed_externally_and_zero_buttons():
    row = {"name": "some-external-thing", "manager": "none", "manager_note": "managed_externally", "actions": []}
    html = _render_service_actions(row)
    assert "Managed externally" in html
    assert "<button" not in html


def test_t13_kubernetes_stop_restart_exactly_two_buttons_no_start():
    row = {"name": "athena-gateway", "manager": "kubernetes", "manager_note": None, "actions": ["stop", "restart"]}
    html = _render_service_actions(row)
    assert html.count("<button") == 2
    assert not re.search(r"requestServiceAction\([^)]*'start'\)", html), (
        "no button may dispatch the 'start' action when actions=['stop','restart'] "
        "(a naive substring check on 'start' would false-positive on 'restart')"
    )


def test_t13_disabled_and_native_state_both_present():
    row = {"run_state": "disabled", "native_state": "0/0 pods", "last_error": None}
    html = _render_status_cell(row)
    assert "Disabled" in html
    assert "0/0 pods" in html


def test_t13_render_status_cell_escapes_native_state_and_last_error():
    """tessa P4 mid-build High: renderStatusCell escapes native_state and
    last_error, but stripping either escapeHtml call left all 51 pre-P4b
    tests green -- no test actually round-tripped a hostile payload through
    THIS function. Written against the H2 rewrite's new shape (health line
    included), so it can't silently pass against stale code either."""
    hostile = '<img src=x onerror=alert(1)> \\ " \''
    row = {
        "run_state": "stopped",
        "native_state": hostile,
        "last_error": hostile,
        "health_status": "unhealthy",
    }
    html = _render_status_cell(row)

    # No live tag boundary survives -- '<' and '>' are the characters that
    # matter for breaking out of a text node; the inert word "onerror="
    # appearing as escaped text content is not itself a vulnerability.
    assert "<img" not in html
    assert "&lt;img" in html
    assert "&gt;" in html
    # The raw payload must appear nowhere unescaped in the output.
    assert hostile not in html


def test_t13_escaping_round_trip_action_name_and_note_escaping():
    hostile_name = "svc'&\"<x>"
    row = {
        "name": hostile_name,
        "manager": "control_agent",
        "manager_note": "note<script>",
        "actions": ["stop"],
    }
    html = _render_service_actions(row)

    # Assertion 1: the action name round-trips through the onclick handler.
    m = re.search(r"requestServiceAction\('(.*?)', 'stop'\)", html)
    assert m, f"expected a requestServiceAction(...) onclick handler, got: {html}"
    js_literal_body = _html_attr_decode(m.group(1))
    recovered = _js_single_quoted_string_parse(js_literal_body)
    assert recovered == hostile_name

    # Assertion 2: the manager badge's title (built from manager_note,
    # escaped) contains no raw '<' -- a distinct bug class from assertion 1
    # (a fix that escapes one and not the other is a real regression).
    title_match = re.search(r'title="([^"]*)"', html)
    assert title_match, f"expected a title= attribute on the manager badge, got: {html}"
    assert "<" not in title_match.group(1)


# --- T14: ollamaModelsMessage -------------------------------------------

def test_t14_offline_message_contains_unreachable_not_dead_literal():
    health = {"manager": "control_agent", "healthy": False, "status": "offline"}
    message = _ollama_models_message(health, None)
    assert "unreachable" in message
    assert "offline - start it" not in message


def test_t14_models_endpoint_error_when_reachable_but_models_call_failed():
    health = {"manager": "control_agent", "healthy": True, "status": "healthy"}
    message = _ollama_models_message(health, "connection reset")
    assert "models endpoint" in message


def test_t14_returns_null_not_empty_string_when_reachable_and_no_error():
    health = {"manager": "control_agent", "healthy": True, "status": "healthy"}
    message = _ollama_models_message(health, None)
    assert message is None


def test_t14_loading_message_when_health_is_null():
    message = _ollama_models_message(None, None)
    assert "Loading" in message


def test_t14_manager_none_but_healthy_still_shows_models_table():
    """ruby H8: the manager only governs start/stop/restart -- load/unload
    hit the Ollama URL directly regardless of manager. manager==='none'
    must NOT hide the table, or model management breaks for the default
    OSS deployment (Control Agent and Kubernetes both off)."""
    health = {"manager": "none", "manager_note": "managed_externally", "healthy": True, "status": "healthy"}
    message = _ollama_models_message(health, None)
    assert message is None


def test_t14_fetch_failure_is_distinct_from_a_real_offline_status():
    """ruby H8: /ollama/health itself failing (network down between the
    browser and admin-backend) must read differently than Ollama itself
    reporting offline/error -- fetchFailed distinguishes the two."""
    health = {"manager": "none", "manager_note": "health_check_failed", "healthy": False, "status": "error", "fetchFailed": True}
    offline_health = {"manager": "control_agent", "healthy": False, "status": "offline"}

    message = _ollama_models_message(health, None)
    offline_message = _ollama_models_message(offline_health, None)

    assert "admin-backend" in message
    assert message != offline_message


def test_t14_render_ollama_models_table_renders_loading_when_health_null():
    """renderOllamaModelsTable() needs `document`, so it isn't cleanly
    extractable as pure JS for this Node harness (contract's own escape
    hatch) -- static source assertion instead: it must delegate to
    ollamaModelsMessage(ollamaHealth, ollamaModelsError) rather than
    re-deriving a "Loading" check inline, so the pure-function assertions
    above actually describe its rendered behavior."""
    source = SERVICE_CONTROL_JS.read_text()
    assert "ollamaModelsMessage(ollamaHealth, ollamaModelsError)" in source


# --- Static gates (grep-equivalent, not Node) -------------------------------

def test_static_no_dead_pre_phase4_call_sites_remain():
    source = SERVICE_CONTROL_JS.read_text()
    forbidden_patterns = [
        r"apiRequest\('/api/services'\)",
        r"/api/service-registry/services'\)",
        r"action_type=",
        r"SERVICE_MACROS",
        r"function executeMacro",
    ]
    for pattern in forbidden_patterns:
        assert not re.search(pattern, source), f"expected {pattern!r} to be gone from service-control.js"


def test_static_ollama_host_is_escaped():
    source = SERVICE_CONTROL_JS.read_text()
    assert "Host: ${ollamaHealth.host" not in source


def test_static_ollama_loaders_only_awaited_inside_refresh_panel():
    source = SERVICE_CONTROL_JS.read_text()
    assert len(re.findall(r"await loadOllamaHealth\(\)|await loadOllamaModels\(\)", source)) == 0


def test_static_restart_timeline_element_present_exactly_once():
    source = (FRONTEND_DIR / "index.html").read_text()
    assert source.count('id="restart-timeline"') == 1


# ---------------------------------------------------------------------------
# ruby M1 / tessa P4 mid-build Medium: the typed-confirm exact-match gating
# had no test at all. typedConfirmMatches is a pure function extracted from
# showServiceConfirmModal specifically so this is testable without jsdom;
# the modal's own DOM wiring (createElement + textContent, never innerHTML
# interpolation of the target name) is covered by a static source check.
# ---------------------------------------------------------------------------

def _typed_confirm_matches(expected, value):
    fn_source = _extract_block(SERVICE_CONTROL_JS, "function typedConfirmMatches(")
    return _run_node_with_source(fn_source, f"typedConfirmMatches({json.dumps(expected)}, {json.dumps(value)})")


def test_typed_confirm_matches_exact_case_sensitive_match():
    assert _typed_confirm_matches("athena-orchestrator", "athena-orchestrator") is True


@pytest.mark.parametrize("value", ["Athena-Orchestrator", "athena-orchestrato", "athena-orchestrator ", ""])
def test_typed_confirm_matches_rejects_anything_else(value):
    assert _typed_confirm_matches("athena-orchestrator", value) is False


def test_typed_confirm_matches_null_expected_never_matches():
    assert _typed_confirm_matches(None, "") is False
    assert _typed_confirm_matches(None, None) is False


def test_static_typed_confirm_input_built_via_create_element_and_text_content():
    """The typed-confirm label uses textContent (never innerHTML string
    interpolation of the server-resolved target name) and the input is
    built with document.createElement -- both inside the requireTyped
    branch of showServiceConfirmModal."""
    source = SERVICE_CONTROL_JS.read_text()
    modal_fn = _extract_block(SERVICE_CONTROL_JS, "function showServiceConfirmModal(")
    assert "document.createElement('label')" in modal_fn
    assert "document.createElement('input')" in modal_fn
    assert "label.textContent = `Type \"${requireTyped}\" to confirm:`;" in modal_fn
    assert "typedConfirmMatches(requireTyped" in modal_fn


def test_static_request_service_action_uses_confirm_name_not_manager_target():
    """codex diff review r2 High #1: the typed-confirm target must be
    row.confirm_name (server-resolved, e.g. 'process:8010' for a Control
    Agent process) -- never derived from row.manager_target, which for a
    CA process is just the bare port number and would make an owner
    unable to ever type a matching confirmation."""
    source = SERVICE_CONTROL_JS.read_text()
    fn = _extract_block(SERVICE_CONTROL_JS, "function requestServiceAction(")
    assert "row.confirm_required ? row.confirm_name : null" in fn
    assert "row.manager_target || row.name" not in fn


# ---------------------------------------------------------------------------
# Guest Mode owner PIN (mozart jackson, urgent fix blocking ATHENA-69
# rollout): admin/frontend/guest-mode.js rendered a "PIN must be set again"
# notice with no input anywhere to actually set one. setOwnerPin() reads two
# password inputs, validates client-side, and sends only {owner_pin} to the
# existing PATCH /api/guest-mode/config endpoint. The PIN itself is never a
# candidate for HTML/DOM/console interpolation in this file, so these are
# static source checks (grep-equivalent, not Node/jsdom), matching the
# service-control.js typed-confirm precedent above.
# ---------------------------------------------------------------------------

GUEST_MODE_JS = FRONTEND_DIR / "guest-mode.js"


def test_static_set_owner_pin_request_body_carries_only_owner_pin():
    fn = _extract_block(GUEST_MODE_JS, "async function setOwnerPin(")
    assert "JSON.stringify({ owner_pin: pin })" in fn
    assert "method: 'PATCH'" in fn
    assert "/api/guest-mode/config" in fn


def test_static_set_owner_pin_validates_six_digits_and_confirmation_match():
    fn = _extract_block(GUEST_MODE_JS, "async function setOwnerPin(")
    assert "/^[0-9]{6}$/.test(pin)" in fn
    assert "pin !== confirmPin" in fn


def test_static_owner_pin_value_never_reaches_console_or_innerhtml():
    """The raw PIN (the `pin`/`confirmPin` locals in setOwnerPin, and the
    `owner-pin-input`/`owner-pin-confirm` field values) must never be logged
    or written into rendered HTML anywhere in guest-mode.js."""
    source = GUEST_MODE_JS.read_text()
    assert not re.search(r"console\.\w+\([^)]*\b(pin|confirmPin)\b", source)
    assert not re.search(r"innerHTML\s*[+]?=[^;]*\b(pin|confirmPin)\b", source)
    # The only interpolation of the *inputs themselves* is reading .value in
    # setOwnerPin -- never writing a value back into a template literal.
    assert "${pin}" not in source
    assert "${confirmPin}" not in source


def test_static_owner_pin_inputs_are_password_type_with_no_autocomplete():
    source = (FRONTEND_DIR / "index.html").read_text()
    for input_id in ("owner-pin-input", "owner-pin-confirm"):
        m = re.search(rf'<input[^>]*id="{input_id}"[^>]*>', source)
        assert m, f"expected an <input id=\"{input_id}\"> in index.html"
        tag = m.group(0)
        assert 'type="password"' in tag
        assert 'autocomplete="off"' in tag
        assert 'inputmode="numeric"' in tag
        assert 'pattern="[0-9]{6}"' in tag


def test_static_set_owner_pin_button_wired_and_bare_expression():
    """onclick="setOwnerPin()" takes no arguments -- neither escapeHtml nor
    escapeJsAttr applies (rule 3, the bare-expression/no-argument case), and
    there is nothing here for check-handler-escaping.py to flag."""
    source = (FRONTEND_DIR / "index.html").read_text()
    assert 'onclick="setOwnerPin()"' in source


def test_static_set_owner_pin_clears_inputs_on_both_success_and_error():
    fn = _extract_block(GUEST_MODE_JS, "async function setOwnerPin(")
    # Two calls inside the try block (success path) and one inside the catch
    # (error path) -- three call sites plus the definition itself.
    assert fn.count("clearInputs();") >= 2
    assert re.search(r"catch \(error\) \{\s*clearInputs\(\);", fn)
