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


def test_static_set_owner_pin_clears_inputs_on_every_exit_path():
    """codex review on ee7e02a (Medium): the 6-digit-format validation branch
    previously `return`ed without clearing the inputs -- only the
    PINs-do-not-match branch and the try/catch paths cleared. Every exit now
    clears: the two validation-error branches route through showFieldError
    (which clears as its first statement, finally-style), and the try/catch
    success/error paths clear directly."""
    fn = _extract_block(GUEST_MODE_JS, "async function setOwnerPin(")

    show_field_error = _extract_block_from_source(fn, "const showFieldError = (message) => {")
    assert re.search(r"^\s*clearInputs\(\);", show_field_error, re.MULTILINE), (
        "showFieldError must clear the inputs as its first action"
    )

    assert "showFieldError('PIN must be exactly 6 digits.');" in fn
    assert "showFieldError('PINs do not match.');" in fn

    # Success path (inside try, before the toast) and the catch (error) path
    # each clear directly -- neither goes through showFieldError since
    # neither is a field-level validation message.
    assert re.search(r"clearInputs\(\);\s*safeShowToast\('Owner PIN set', 'success'\);", fn)
    assert re.search(r"catch \(error\) \{\s*clearInputs\(\);", fn)


def _extract_block_from_source(source: str, marker: str) -> str:
    """Like _extract_block, but operates on an already-extracted source
    string (e.g. a function body) instead of reading a file from disk."""
    start = source.index(marker)
    brace_start = source.index("{", start + len(marker) - 1)
    depth = 0
    for i in range(brace_start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AssertionError(f"unbalanced braces extracting {marker!r}")


# --- Memory vector store status and rebuild (memory-management.js) ----------

MEMORY_JS = FRONTEND_DIR / "memory-management.js"
HOSTILE = '<img src=x onerror=alert(1)> \\ " \''


def _qdrant_status(health, can_rebuild):
    sources = [ESCAPE_HTML_JS.read_text(), _extract_block(MEMORY_JS, "function renderQdrantStatus(")]
    return _run_node_with_sources(
        sources, f"renderQdrantStatus({json.dumps(health)}, {json.dumps(can_rebuild)})")


def _reindex_result(report):
    sources = [ESCAPE_HTML_JS.read_text(), _extract_block(MEMORY_JS, "function renderReindexResult(")]
    return _run_node_with_sources(sources, f"renderReindexResult({json.dumps(report)})")


def _assert_neutralised(html):
    assert "<img" not in html
    assert "&lt;img" in html
    assert HOSTILE not in html


def _health(status, **extra):
    base = {
        "status": status, "state": "ready", "url": "http://qdrant:6333", "collection": "athena_memories",
        "points_count": 3, "pg_live_count": 3, "pending_count": 0, "in_sync": status == "healthy",
        "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "collection_embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
        "model_recorded": True, "foreign_points": 0, "foreign_point_ids_sample": [],
    }
    base.update(extra)
    return base


def test_qdrant_status_healthy_shows_counts_and_escapes_url():
    html = _qdrant_status(_health("healthy", url=HOSTILE), True)
    _assert_neutralised(html)
    assert "3 of 3" in html
    assert "Rebuild" not in html


def test_qdrant_status_degraded_owner_gets_enabled_rebuild():
    html = _qdrant_status(_health("degraded", points_count=1, pending_count=2, detail=HOSTILE), True)
    _assert_neutralised(html)
    assert "rebuildMemoryVectors()" in html
    assert "disabled" not in html
    assert "2 not searchable" in html


def test_qdrant_status_degraded_operator_button_disabled():
    html = _qdrant_status(_health("degraded", pending_count=1), False)
    assert "Rebuild" in html
    assert "disabled" in html and 'title="Owner only"' in html
    assert "rebuildMemoryVectors()" not in html


def test_qdrant_status_unavailable_escapes_error_and_detail():
    html = _qdrant_status(_health("unavailable", state="unavailable", error=HOSTILE, detail=HOSTILE,
                                  points_count=None), True)
    _assert_neutralised(html)
    assert "Unavailable" in html and "keyword" in html.lower()


def test_qdrant_status_error_escapes_model_and_foreign_sample():
    html = _qdrant_status(_health("error", state="model_mismatch", collection_embedding_model=HOSTILE,
                                  foreign_points=2, foreign_point_ids_sample=[HOSTILE, "abc"]), True)
    _assert_neutralised(html)
    assert "abc" in html
    assert "rebuildMemoryVectors()" not in html


def test_qdrant_status_numbers_are_coerced():
    html = _qdrant_status(_health("degraded", points_count=HOSTILE, pg_live_count=HOSTILE,
                                  pending_count=HOSTILE), True)
    assert "<img" not in html and "onerror" not in html


def test_reindex_result_success_summarises_counts():
    html = _reindex_result({"mode": "missing", "embedded": 2, "already_present": 5, "failed": 0,
                            "orphans_pruned": 1})
    assert "2 embedded" in html and "1 orphan" in html


def test_reindex_result_busy_shows_retry_after():
    html = _reindex_result({"error": "reindex_busy", "retry_after_seconds": 42})
    assert "42" in html


def test_reindex_result_busy_retry_after_is_coerced():
    html = _reindex_result({"error": "reindex_busy", "retry_after_seconds": HOSTILE})
    assert "<img" not in html and "onerror" not in html


def test_reindex_result_aborted_escapes():
    html = _reindex_result({"error": "reindex_aborted", "aborted": HOSTILE, "embedded": 1})
    _assert_neutralised(html)


def test_reindex_result_refused_escapes_state():
    html = _reindex_result({"error": "reindex_refused", "refused": HOSTILE})
    _assert_neutralised(html)


def test_reindex_result_forbidden_says_owner_only():
    html = _reindex_result({"error": "forbidden"})
    assert "owner" in html.lower()


def test_reindex_result_unknown_error_escaped():
    _assert_neutralised(_reindex_result({"error": HOSTILE}))


def test_qdrant_status_wiring_uses_manage_infrastructure():
    source = MEMORY_JS.read_text()
    body = _extract_block(MEMORY_JS, "function updateQdrantStatus(")
    assert "renderQdrantStatus(" in body and "hasPermission('manage_infrastructure')" in body
    assert "/vector-store/reindex?mode=missing" in source


# --- Review round 1: Rebuild whenever not in sync; rebuild HTTP errors -------

def test_qdrant_status_partial_scan_offers_rebuild():
    html = _qdrant_status(_health("degraded", in_sync=None, sync_scan="partial"), True)
    assert "rebuildMemoryVectors()" in html
    assert "partial" in html.lower() or "incomplete" in html.lower()


def test_qdrant_status_green_only_when_in_sync_is_true():
    html = _qdrant_status(_health("healthy", in_sync=None), True)
    assert "Vectors in sync" not in html and "rebuildMemoryVectors()" in html


def test_qdrant_status_escapes_missing_and_orphan_samples():
    html = _qdrant_status(_health("degraded", missing_count=1, missing_vector_ids_sample=[HOSTILE],
                                  orphan_count=1, orphan_point_ids_sample=[HOSTILE]), True)
    _assert_neutralised(html)
    assert "1 missing" in html and "1 orphan" in html


def _reindex_outcome(status, body):
    fn = _extract_block(MEMORY_JS, "function reindexOutcome(")
    return _run_node_with_source(fn, f"reindexOutcome({json.dumps(status)}, {json.dumps(body)})")


@pytest.mark.parametrize("status, body", [
    (401, {"detail": "Authentication required"}),
    (422, {"detail": [{"msg": "bad mode"}]}),
    (500, {}),
], ids=["401", "422", "500"])
def test_rebuild_http_errors_are_reported_as_errors(status, body):
    outcome = _reindex_outcome(status, body)
    assert outcome["error"]
    html = _reindex_result(outcome)
    assert "Rebuilt" not in html and "failed" in html.lower()


def test_rebuild_outcome_passes_through_success_and_conflicts():
    assert _reindex_outcome(200, {"embedded": 3})["embedded"] == 3
    assert _reindex_outcome(409, {"error": "reindex_busy", "retry_after_seconds": 5})["error"] == "reindex_busy"
    assert _reindex_outcome(403, {"detail": "Insufficient permissions"}) == {"error": "forbidden"}


# ---------------------------------------------------------------------------
# Home Assistant status card and "not monitored" rows (campaign
# 2026-10-11-deliver-athena-ha-health-status, Phase 3). Pure helpers from
# app.js, service-control.js and mission-control.js run in Node with the real
# escape-html.js; TZ is pinned to UTC so '3:12 pm' and '27 Sep' are stable.
# ---------------------------------------------------------------------------

import ast  # noqa: E402

MISSION_CONTROL_JS = FRONTEND_DIR / "mission-control.js"
VOICE_PIPELINES_JS = FRONTEND_DIR / "voice-pipelines.js"
HA_STATUS_PY = Path(__file__).resolve().parents[2] / "admin" / "backend" / "app" / "services" / "ha_status.py"
CHECKED_AT = "2026-10-11T12:00:00+00:00"


def _run_node_utc(sources: list[str], js_expr: str):
    script = f"""
    'use strict';
    global.window = global;
    {chr(10).join(sources)}
    Promise.resolve((function() {{ return {js_expr}; }})()).then((result) => {{
        process.stdout.write(JSON.stringify(result === undefined ? "__undefined__" : result));
    }});
    """
    proc = subprocess.run(
        [NODE_BIN, "-e", script], capture_output=True, text=True, timeout=30,
        env={**os.environ, "TZ": "UTC"},
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}\n---script---\n{script}"
    return json.loads(proc.stdout)


def _app_ha_sources() -> list[str]:
    """escape-html.js plus app.js's status helpers, the Home Assistant card
    and the registry-row helpers (everything between the vocabulary comment
    and loadStatus)."""
    source = APP_JS.read_text()
    start = source.index("// Status vocabulary shared with Service Control")
    start = source.rindex("// ----", 0, start)
    end = source.index("async function loadStatus() {")
    return [
        ESCAPE_HTML_JS.read_text(),
        _extract_function_source(APP_JS, "serviceStatus"),
        _extract_function_source(APP_JS, "summarizeServices"),
        source[start:end],
    ]


def _ha(expr: str):
    return _run_node_utc(_app_ha_sources(), expr)


def _plain(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html or "")


def _ha_payload(**changes):
    base = {
        "state": "connected", "reason": "ok", "areas": 9,
        "last_outcome_at": "2026-10-11T11:58:00+00:00", "last_success_at": "2026-10-11T11:58:00+00:00",
        "coverage": "cluster", "replicas_reporting": 2, "checked_at": CHECKED_AT,
        "gateway": "ok", "gateway_sampled": True,
    }
    base.update(changes)
    return base


def _summ(**changes):
    return _ha(f"summarizeHomeAssistant({json.dumps(_ha_payload(**changes))})")


# -- the reason map ---------------------------------------------------------------

def _python_reasons() -> set[str]:
    tree = ast.parse(HA_STATUS_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "REASONS" for t in node.targets):
            call = node.value
            assert isinstance(call, ast.Call) and call.func.id == "frozenset"
            return {el.value for el in call.args[0].elts}
    raise AssertionError("REASONS not found in ha_status.py")


# Reasons the backend can already report whose card copy lands with the frontend
# phase (status-vocab.js). Until then an unknown reason reads "Status unknown".
# The frontend phase empties this set and the length below becomes 16.
PENDING_JS_REASONS = {"ip_banned"}


def test_the_js_reason_map_has_exactly_the_python_reason_set():
    js_keys = set(_ha("Object.keys(HA_REASON_COPY)"))
    assert js_keys == _python_reasons() - PENDING_JS_REASONS
    assert len(js_keys) == 15
    assert "gateway_token_rejected" not in js_keys and "gateway_token_rejected" not in _python_reasons()


HA_COPY = {
    "ok": ("Connected", None, None),
    "waiting": ("No contact yet",
                "Athena hasn't talked to Home Assistant since it started. This updates after the first voice command.", None),
    "token_rejected": ("Token rejected",
                       "Home Assistant turned down Athena's access token, so Athena can't control your home.",
                       "Create a long-lived access token in Home Assistant (your profile → Security), save it under External API Keys → home-assistant, then restart the orchestrator."),
    "forbidden": ("Access blocked",
                  "Home Assistant refused Athena. This usually means it blocked Athena's address after failed sign-ins.",
                  "Remove Athena's address from Home Assistant's ip_bans.yaml and restart Home Assistant, then check the token."),
    "unreachable": ("Can't reach Home Assistant",
                    "Athena couldn't connect. Home Assistant may be off, restarting, or at a different address.",
                    "Check that Home Assistant is running and that the address under External API Keys → home-assistant is right."),
    "timeout": ("Not responding",
                "Athena connected, but Home Assistant didn't answer in time. It may be busy or starting up.", None),
    "tls_error": ("Secure connection failed",
                  "Athena reached Home Assistant but couldn't set up the HTTPS connection. Check the address uses the right scheme (http or https) and port.", None),
    "ha_error": ("Home Assistant error",
                 "Home Assistant is running but returned an error to Athena. Its own log will say why.", None),
    "registry_unavailable": ("Connected, rooms not loaded",
                             "Commands reach Home Assistant, but Athena couldn't load its areas, so it can't tell which room you're in.",
                             "Restart the orchestrator to try again."),
    "registry_refused": ("Connected, rooms not loaded",
                         "Home Assistant turned down Athena's token when it loaded areas earlier. Commands work now.",
                         "Restart the orchestrator to load rooms."),
    "not_configured": ("Not set up",
                       "Add your Home Assistant address and token so Athena can control your home.", "BUTTON"),
    "orchestrator_unreachable": ("Status unavailable",
                                 "The admin panel couldn't reach Athena's orchestrator to ask about Home Assistant. Home Assistant itself may be fine.", None),
    "orchestrator_refused": ("Status unavailable",
                             "The orchestrator refused the admin panel's service key. Home Assistant itself may be fine.",
                             "Check SERVICE_API_KEY matches on admin-backend and the orchestrator."),
    "orchestrator_blocked": ("Status unavailable",
                             "The orchestrator's address isn't on the admin panel's allowed list. Home Assistant itself may be fine.",
                             "Allow the orchestrator's address in HEALTH_POLL_ALLOWED_PRIVATE_HOSTS."),
    "orchestrator_outdated": ("Status unavailable",
                              "The orchestrator is an older version that doesn't report this yet.", "Update the orchestrator."),
}


@pytest.mark.parametrize("reason", sorted(HA_COPY))
def test_each_reason_reads_its_binding_copy(reason):
    headline, detail, action = HA_COPY[reason]
    out = _summ(reason=reason, state="unknown", last_success_at=None, last_outcome_at=None)
    assert out["headline"] == headline
    assert out["detail"] == detail
    if action == "BUTTON":
        assert out["action"].startswith('<button type="button" onclick="showTab(\'external-api-keys\')"')
        assert ">Set up Home Assistant</button>" in out["action"]
    else:
        assert out["action"] == action


def test_the_copy_names_no_certificate_and_stays_in_the_owners_words():
    out = _ha("Object.values(HA_REASON_COPY)")
    blob = json.dumps(out).lower()
    assert "certificate" not in blob
    assert "gateway_token_rejected" not in blob


# -- classes -------------------------------------------------------------------------

@pytest.mark.parametrize("state,dot,text", [
    ("connected", "bg-green-500", "text-green-400"),
    ("attention", "bg-amber-500", "text-amber-400"),
    ("failing", "bg-red-500", "text-red-400"),
    ("not_configured", "bg-gray-500", "text-gray-400"),
    ("unknown", "bg-gray-500", "text-gray-400"),
    ("zzz", "bg-gray-500", "text-gray-400"),
    ("__proto__", "bg-gray-500", "text-gray-400"),
    ("constructor", "bg-gray-500", "text-gray-400"),
    ("<img src=x>", "bg-gray-500", "text-gray-400"),
])
def test_each_state_maps_to_its_class_pair_and_an_unknown_state_is_grey(state, dot, text):
    out = _summ(state=state)
    assert (out["dotClass"], out["textClass"]) == (dot, text)


@pytest.mark.parametrize("reason", [r for r in sorted(HA_COPY) if r.startswith("orchestrator_")] + ["waiting"])
def test_an_unknown_state_and_orchestrator_failures_are_grey_and_never_headed_home_assistant(reason):
    out = _summ(state="unknown", reason=reason)
    assert (out["dotClass"], out["textClass"]) == ("bg-gray-500", "text-gray-400")
    assert not out["headline"].startswith("Home Assistant")


def test_a_hostile_reason_reads_status_unknown_and_is_never_echoed():
    out = _summ(reason="<img src=x>", state="<img src=x>", gateway="<img src=x>", coverage="<img src=x>")
    assert out["headline"] == "Status unknown"
    assert out["detail"] is None and out["action"] is None
    assert "<img" not in json.dumps(out)
    assert _summ(reason="constructor")["headline"] == "Status unknown"
    assert _summ(reason="__proto__")["headline"] == "Status unknown"


def test_a_missing_or_malformed_payload_reads_status_unknown_not_a_crash():
    for payload in ("null", "undefined", "[]", "'text'", "42"):
        out = _ha(f"summarizeHomeAssistant({payload})")
        assert out["headline"] == "Status unknown" and out["dotClass"] == "bg-gray-500"


# -- the idle sentence and the meta line --------------------------------------------------

def test_ok_aged_forty_minutes_is_green_with_the_idle_sentence():
    out = _summ(last_success_at="2026-10-11T11:20:00+00:00", last_outcome_at="2026-10-11T11:20:00+00:00")
    assert out["textClass"] == "text-green-400"
    assert out["detail"] == ("Athena checks Home Assistant whenever it uses it. Nothing has needed it since "
                             "11:20 am, so this is the latest it knows.")


def test_ok_aged_five_minutes_has_no_detail():
    assert _summ(last_success_at="2026-10-11T11:55:00+00:00")["detail"] is None


def test_the_idle_sentence_uses_a_twelve_hour_clock_with_pm():
    out = _summ(last_success_at="2026-10-11T00:12:00+00:00", last_outcome_at="2026-10-11T00:12:00+00:00",
                checked_at="2026-10-11T15:12:00+00:00")
    assert "since 12:12 am" in out["detail"]
    out = _summ(last_success_at="2026-10-11T15:12:00+00:00", last_outcome_at="2026-10-11T15:12:00+00:00",
                checked_at="2026-10-11T23:12:00+00:00")
    assert "since 3:12 pm" in out["detail"]


def test_a_connected_link_says_last_contact_with_one_time():
    meta = _plain(_summ()["meta"])
    assert meta.startswith("Last contact 2m ago")
    assert "last worked" not in meta


def test_token_rejected_says_rejected_and_when_it_last_worked():
    out = _summ(state="failing", reason="token_rejected",
                last_outcome_at="2026-10-11T11:58:00+00:00", last_success_at="2026-10-11T09:00:00+00:00")
    assert _plain(out["meta"]).startswith("Rejected 2m ago · last worked 3h ago")


def test_another_failure_says_seen_and_a_missing_last_success_drops_the_last_text():
    seen = _summ(state="failing", reason="timeout", last_outcome_at="2026-10-11T11:58:00+00:00",
                 last_success_at="2026-10-11T09:00:00+00:00")
    assert _plain(seen["meta"]).startswith("Seen 2m ago · last worked 3h ago")
    never = _summ(state="failing", reason="token_rejected", last_outcome_at="2026-10-11T11:58:00+00:00",
                  last_success_at=None)
    assert _plain(never["meta"]).startswith("Rejected 2m ago")
    assert "last worked" not in never["meta"]


def test_a_registry_problem_beside_a_newer_success_reads_last_contact_not_seen():
    out = _summ(state="attention", reason="registry_unavailable", areas=None)
    assert _plain(out["meta"]).startswith("Last contact 2m ago")


@pytest.mark.parametrize("areas,expected", [(0, "0 areas"), (1, "1 area"), (9, "9 areas")])
def test_the_area_count_and_its_singular(areas, expected):
    assert expected in _plain(_summ(areas=areas)["meta"]).split(" · ")


def test_a_connected_link_with_no_area_count_says_rooms_not_loaded():
    assert "rooms not loaded" in _plain(_summ(areas=None)["meta"]).split(" · ")


@pytest.mark.parametrize("areas", ["7", -1, None, "<img src=x>", True])
def test_a_bad_area_count_is_not_printed(areas):
    meta = _plain(_summ(areas=areas, state="failing", reason="timeout")["meta"])
    assert "undefined" not in meta and "NaN" not in meta and "<img" not in meta and "areas" not in meta


def test_a_replica_only_view_says_so_with_a_tooltip():
    meta = _summ(coverage="replica")["meta"]
    assert "this replica only" in _plain(meta)
    assert "title=\"Athena couldn&#39;t combine what all its orchestrator copies have seen" in meta
    assert "this replica only" not in _summ(coverage="cluster")["meta"]


@pytest.mark.parametrize("gateway,text", [
    ("not_checked", "gateway not checked"), ("refused", "gateway check failed"), ("unavailable", "gateway check failed"),
])
def test_the_gateway_note_is_muted_and_never_changes_the_headline_or_dot(gateway, text):
    base, out = _summ(), _summ(gateway=gateway)
    assert text in _plain(out["meta"])
    assert (out["headline"], out["dotClass"], out["textClass"]) == (base["headline"], base["dotClass"], base["textClass"])


@pytest.mark.parametrize("gateway", ["ok", "disabled", "unknown", "zzz", None])
def test_other_gateway_words_add_no_note(gateway):
    assert "gateway" not in _plain(_summ(gateway=gateway)["meta"])


def test_times_are_time_elements_with_the_exact_time_on_hover():
    meta = _summ()["meta"]
    assert '<time datetime="2026-10-11T11:58:00+00:00" title="' in meta


# -- formatAge -------------------------------------------------------------------------

@pytest.mark.parametrize("iso,expected", [
    ("2026-10-11T11:59:30+00:00", "just now"),
    ("2026-10-11T11:59:01+00:00", "just now"),
    ("2026-10-11T11:59:00+00:00", "1m ago"),
    ("2026-10-11T11:48:00+00:00", "12m ago"),
    ("2026-10-11T11:00:01+00:00", "59m ago"),
    ("2026-10-11T11:00:00+00:00", "1h ago"),
    ("2026-10-11T09:00:00+00:00", "3h ago"),
    ("2026-10-10T12:00:01+00:00", "23h ago"),
    ("2026-10-10T12:00:00+00:00", "10 Oct"),
    ("2026-09-27T12:00:00+00:00", "27 Sep"),
    ("2025-09-27T12:00:00+00:00", "27 Sep 2025"),
    ("2026-10-11T12:05:00+00:00", "just now"),   # in the future against the server clock
])
def test_format_age_bands(iso, expected):
    assert _ha(f"formatAge({json.dumps(iso)}, {json.dumps(CHECKED_AT)})") == expected


@pytest.mark.parametrize("bad", ["yesterday", "", "NaN", "2026-13-45T99:99:99Z"])
def test_format_age_returns_null_for_an_unreadable_time_and_time_tag_is_empty(bad):
    assert _ha(f"formatAge({json.dumps(bad)}, {json.dumps(CHECKED_AT)})") is None
    assert _ha(f"timeTag({json.dumps(bad)}, {json.dumps(CHECKED_AT)})") == ""


def test_time_tag_escapes_a_hostile_time_attribute():
    html = _ha("timeTag('2026-10-11T11:58:00+00:00\"><img src=x onerror=alert(1)>', " + json.dumps(CHECKED_AT) + ")")
    assert "<img" not in html


# -- the shared status vocabulary ------------------------------------------------------------

SIX_CODES = ["healthy", "unhealthy", "unconfigured", "pending", "stale", "disabled"]
SIX_WORDS = ["Healthy", "Unhealthy", "Needs setup", "Checking…", "No recent check", "Not monitored"]


def _service_control_status_label(code: str) -> str:
    return _run_node_utc([_extract_block(SERVICE_CONTROL_JS, "function statusLabel(")], f"statusLabel({json.dumps(code)})")


def test_the_dashboard_and_service_control_status_words_are_equal_on_the_six_codes():
    app_words = [_ha(f"statusLabel({json.dumps(code)})") for code in SIX_CODES]
    control_words = [_service_control_status_label(code) for code in SIX_CODES]
    assert app_words == control_words == SIX_WORDS


def test_an_unknown_status_code_is_passed_through_for_the_caller_to_escape():
    assert _ha("statusLabel('weird')") == "weird"
    assert _ha("statusLabel('constructor')") == "constructor"


def test_monitoring_label_for_a_switched_off_row():
    assert _ha(f"monitoringLabel({{last_health_check: null}}, {json.dumps(CHECKED_AT)})") == "Not monitored · never checked"
    assert _ha(f"monitoringLabel({{last_health_check: '2026-09-27T08:00:00+00:00'}}, {json.dumps(CHECKED_AT)})") == \
        "Not monitored · last checked 27 Sep"


# -- the poller banner --------------------------------------------------------------------------

NOW_MS = "Date.parse('2026-10-11T12:00:00+00:00')"


def _banner(payload):
    return _ha(f"pollerBanner({json.dumps(payload)}, {NOW_MS})")


def _row(monitoring, checked="2026-10-11T09:02:00+00:00", enabled=True):
    return {"name": "x", "enabled": enabled, "monitoring": monitoring, "last_health_check": checked}


def test_every_enabled_row_stale_shows_one_banner_naming_the_newest_check():
    html = _banner({"overall_health": "unknown", "services": [_row("stale"), _row("stale", "2026-10-11T09:14:00+00:00")]})
    assert "role=\"status\"" in html
    assert "Health checks haven't run since 09:14. The statuses below may be out of date." in html


def test_one_live_row_shows_no_banner_and_neither_does_an_empty_list():
    assert _banner({"overall_health": "degraded", "services": [_row("stale"), _row("live")]}) == ""
    assert _banner({"overall_health": "unknown", "services": []}) == ""
    assert _banner({"overall_health": "unknown", "services": [_row("off", enabled=False)]}) == ""


def test_an_unknown_rollup_with_nothing_ever_checked_says_so():
    html = _banner({"overall_health": "unknown", "services": [_row("pending", None)]})
    assert "Health checks haven't run yet." in html


def test_an_old_last_check_reads_as_a_date_not_a_clock():
    html = _banner({"overall_health": "unknown", "services": [_row("stale", "2026-09-27T09:02:00+00:00")]})
    assert "since 27 Sep." in html


# -- summarizeServices ----------------------------------------------------------------------------

def test_an_unknown_overall_health_is_grey_not_red():
    out = _ha("summarizeServices({overall_health: 'unknown', services: [], healthy_services: 0, enabled_services: 0, disabled_services: 0})")
    assert out["overallClass"] == "text-gray-400" and out["overallText"] == "UNKNOWN"
    assert _ha("summarizeServices({overall_health: 'unhealthy', services: [], healthy_services: 0})")["overallClass"] == "text-red-400"


# -- the rendered dashboard -------------------------------------------------------------------------

def _load_status(registry, ha_payload=None, ha_error=False):
    """Run the real loadStatus against a stub DOM and API; returns what it wrote."""
    sources = _app_ha_sources() + [_extract_block(APP_JS, "async function loadStatus(")]
    prelude = f"""
    const nodes = {{}};
    const el = (id) => (nodes[id] = nodes[id] || {{ id, innerHTML: '', textContent: '', className: '', style: {{}} }});
    global.document = {{ getElementById: el }};
    global.infoIcon = () => '';
    const registry = {json.dumps(registry)};
    const ha = {json.dumps(ha_payload)};
    global.apiRequest = async (path) => {{
        if (path === '/api/service-registry/home-assistant') {{ if ({json.dumps(ha_error)}) throw new Error('boom'); return ha; }}
        if (path === '/api/service-registry/services') {{ if (registry === null) throw new Error('registry down <b>'); return registry; }}
        throw new Error('unexpected ' + path);
    }};
    """
    expr = "loadStatus().then(() => ({ha: el('ha-status-container').innerHTML, services: el('services-by-group-container').innerHTML, errors: el('error-container').innerHTML, health: el('stat-health').textContent}))"
    return _run_node_utc([prelude, *sources], expr)


def _registry(*services, overall="degraded"):
    return {"services": list(services), "overall_health": overall, "healthy_services": 0,
            "enabled_services": len(services), "disabled_services": 0}


def _service(name="weather", **changes):
    base = {"name": name, "display_name": name.title(), "host": f"{name}-svc", "port": 8010, "enabled": True,
            "health_status": "healthy", "monitoring": "live", "service_type": "rag",
            "cache_ttl": 300, "timeout": 5, "last_health_check": "2026-10-11T11:59:00+00:00"}
    base.update(changes)
    return base


def test_hostile_display_name_host_and_health_message_come_out_escaped():
    evil = "<img src=x onerror=alert(1)>"
    out = _load_status(_registry(_service(display_name=evil, host=evil, health_message=evil)), _ha_payload())
    assert "<img" not in out["services"]
    assert "&lt;img src=x onerror=alert(1)&gt;" in out["services"]


def test_the_card_renders_when_the_registry_call_fails_and_the_registry_when_the_card_fails():
    registry_down = _load_status(None, _ha_payload())
    assert "Connected" in registry_down["ha"] and "Failed to load system status: registry down &lt;b&gt;" in registry_down["errors"]
    card_down = _load_status(_registry(_service()), None, ha_error=True)
    assert "Status unavailable" in card_down["ha"] and "Couldn&#39;t load" not in card_down["ha"]
    assert "Couldn't load Home Assistant status." in card_down["ha"]
    assert 'onclick="loadStatus()"' in card_down["ha"] and ">Try again</button>" in card_down["ha"]
    assert "Weather" in card_down["services"]


def test_the_card_markup_follows_the_binding_structure():
    ha = _load_status(_registry(_service()), _ha_payload())["ha"]
    assert 'id="ha-status-heading"' in ha and ">Home Assistant</h3>" in ha
    assert 'aria-hidden="true"' in ha and "bg-green-500" in ha and "text-green-400" in ha
    assert "style=" not in ha


def test_a_stale_enabled_row_is_grey_labelled_no_recent_check_and_not_an_error_colour():
    out = _load_status(_registry(_service(health_status="stale", monitoring="stale"), overall="unknown"), _ha_payload())
    assert "No recent check" in out["services"]
    assert "bg-gray-500" in out["services"] and "bg-red-500" not in out["services"] and "text-red-400" not in out["services"]
    assert out["health"] == "UNKNOWN"
    assert "Health checks haven&#39;t run" not in out["errors"] and "Health checks haven't run since" in out["errors"]


def test_a_switched_off_row_reads_not_monitored_with_its_old_error_dated_and_grey():
    off = _service("ha-api", enabled=False, health_status="disabled", monitoring="off", last_error="timeout:old failure",
                   last_health_check="2026-09-27T08:00:00+00:00", last_observed_health=None)
    out = _load_status(_registry(_service(), off, overall="healthy"), _ha_payload())["services"]
    assert ">Not monitored</h3>" in out
    assert "These checks are turned off in Service Control, so Athena doesn&#39;t watch them." not in out
    assert "These checks are turned off in Service Control, so Athena doesn't watch them." in out
    assert "check off" in out and "disabled</span>" not in out
    assert "Not monitored · last checked" in out
    assert "Last result before it was turned off: timeout:old failure" in out
    assert ">Disabled<" not in out and "healthy" not in out.split("Not monitored")[1].lower().replace("these checks", "")


def test_a_switched_off_row_never_checked_says_never_checked():
    off = _service("ha-api", enabled=False, health_status="disabled", monitoring="off", last_health_check=None)
    assert "Not monitored · never checked" in _load_status(_registry(_service(), off), _ha_payload())["services"]


# -- Service Control ---------------------------------------------------------------------------------

def _sc(expr: str):
    sources = [
        ESCAPE_HTML_JS.read_text(),
        _extract_block(SERVICE_CONTROL_JS, "const RUN_STATE_BADGES"),
        _extract_block(SERVICE_CONTROL_JS, "function _isNativelyRunning("),
        _extract_block(SERVICE_CONTROL_JS, "function _healthLine("),
        _extract_block(SERVICE_CONTROL_JS, "function statusLabel("),
        _extract_line(SERVICE_CONTROL_JS, "const _SC_MONTHS = "),
        _extract_line(SERVICE_CONTROL_JS, "const _SC_MONTHS_LONG = "),
        _extract_block(SERVICE_CONTROL_JS, "function _scAgeWords("),
        _extract_block(SERVICE_CONTROL_JS, "function servicePollerBanner("),
        _extract_block(SERVICE_CONTROL_JS, "function _notMonitoredText("),
        _extract_block(SERVICE_CONTROL_JS, "function renderStatusCell("),
        _extract_block(SERVICE_CONTROL_JS, "function _freshnessChip("),
        _extract_block(SERVICE_CONTROL_JS, "function _buildStatusAriaLabel("),
    ]
    return _run_node_utc(sources, expr)


NOW = "Date.parse('2026-10-11T12:00:00+00:00')"


def _extract_line(file_path: Path, prefix: str) -> str:
    """The single source line starting with `prefix` (a one-line `const X = [...]`)."""
    lines = [line for line in file_path.read_text().splitlines() if line.startswith(prefix)]
    assert len(lines) == 1, (prefix, lines)
    return lines[0]


def test_freshness_chip_is_silent_for_an_off_pending_or_unchecked_row():
    for row in ({"monitoring": "off", "last_health_check": "2026-09-27T08:00:00+00:00"},
                {"monitoring": "pending", "last_health_check": "2026-10-11T08:00:00+00:00"},
                {"monitoring": "stale", "last_health_check": None}):
        assert _sc(f"_freshnessChip({json.dumps(row)}, {NOW})") is None


def test_freshness_chip_for_a_stale_row_is_grey_with_the_checker_tooltip_and_no_red_ladder():
    chip = _sc(f"_freshnessChip({{monitoring: 'stale', last_health_check: '2026-10-11T11:48:00+00:00'}}, {NOW})")
    assert chip == {"text": "last checked 12m ago", "colorClass": "text-gray-500",
                    "title": "Athena's background checker hasn't reached this service recently."}
    old = _sc(f"_freshnessChip({{monitoring: 'stale', last_health_check: '2026-09-27T08:00:00+00:00'}}, {NOW})")
    assert old["colorClass"] == "text-gray-500" and old["text"] == "last checked 14 days ago".replace("14 days ago", "27 Sep")
    source = SERVICE_CONTROL_JS.read_text()
    assert "text-red-500" not in source and "poller may be stalled" not in source


def test_freshness_chip_for_a_live_row_is_the_plain_grey_age_between_a_minute_and_the_threshold():
    assert _sc(f"_freshnessChip({{monitoring: 'live', last_health_check: '2026-10-11T11:59:30+00:00'}}, {NOW})") is None
    chip = _sc(f"_freshnessChip({{monitoring: 'live', last_health_check: '2026-10-11T11:57:00+00:00'}}, {NOW})")
    assert chip["text"] == "checked 3m ago" and chip["colorClass"] == "text-gray-500"


def test_health_line_for_a_stale_row_is_grey_no_recent_check():
    assert _sc("_healthLine({run_state: 'running', health_status: 'stale'})") == {"text": "No recent check", "cls": "text-gray-400"}


def test_an_off_row_never_says_healthy_and_shows_no_red_text():
    row = {"run_state": "disabled", "monitoring": "off", "health_status": "disabled", "last_observed_health": None,
           "last_error": "timeout:old failure", "last_health_check": "2026-09-27T08:00:00+00:00"}
    html = _sc(f"(() => {{ const _n = Date.now; Date.now = () => {NOW}; try {{ return renderStatusCell({json.dumps(row)}); }} finally {{ Date.now = _n; }} }})()")
    assert "Not monitored · last checked 27 Sep" in html
    assert "Last result before it was turned off: timeout:old failure" in html
    assert "text-red-400" not in html and "ealthy" not in html


def test_a_live_row_keeps_its_red_error_line():
    row = {"run_state": "stopped", "monitoring": "live", "health_status": "unhealthy", "last_error": "timeout:slow"}
    assert "text-red-400 mt-1\">timeout:slow" in _sc(f"renderStatusCell({json.dumps(row)})")


@pytest.mark.parametrize("args,expected", [
    (("Home Assistant API", "disabled", "2026-09-27T08:00:00+00:00", None, "off"),
     "Home Assistant API: not monitored, last checked 27 September."),
    (("Weather", "stale", "2026-10-11T11:48:00+00:00", None, "stale"),
     "Weather: no recent check, last checked 12 minutes ago."),
    (("Weather", "healthy", "2026-10-11T11:57:00+00:00", 12, "live"),
     "Weather: healthy, checked 3 minutes ago, response 12ms."),
    (("Weather", "stale", "2026-10-11T08:00:00+00:00", None, "stale"),
     "Weather: no recent check, last checked 4 hours ago."),
    (("Weather", "disabled", None, None, "off"), "Weather: not monitored, never checked."),
])
def test_the_aria_label_matches_the_screen_in_spelled_out_words(args, expected):
    assert _sc(f"_buildStatusAriaLabel({', '.join(json.dumps(a) for a in args)}, {NOW})") == expected


def test_the_aria_label_never_prints_raw_seconds_or_says_healthy_for_an_off_row():
    label = _sc(f"_buildStatusAriaLabel('X', 'healthy', '2026-09-27T08:00:00+00:00', 5, 'off', {NOW})")
    assert "seconds" not in label and "healthy" not in label


def test_the_service_control_banner_appears_only_when_every_enabled_row_is_stale():
    rows = [_row("stale"), _row("stale", "2026-10-11T09:14:00+00:00")]
    html = _sc(f"servicePollerBanner({json.dumps(rows)}, {NOW})")
    assert "Health checks haven't run since 09:14. The statuses below may be out of date." in html
    assert _sc(f"servicePollerBanner({json.dumps([_row('stale'), _row('live')])}, {NOW})") == ""
    assert _sc(f"servicePollerBanner([], {NOW})") == ""


def test_the_refresh_button_is_disabled_on_a_switched_off_row():
    source = SERVICE_CONTROL_JS.read_text()
    assert "const refreshOff = service.monitoring === 'off';" in source
    assert 'disabled title="Turn the check on to refresh it"' in source
    off_line = next(line for line in source.splitlines() if 'disabled title="Turn the check on to refresh it"' in line)
    assert "onclick" not in off_line, "the off button must not be clickable"


# -- Mission Control --------------------------------------------------------------------------------------

def _tile(status: str):
    return _run_node_utc([_extract_block(MISSION_CONTROL_JS, "function serviceTileStyle(")], f"serviceTileStyle({json.dumps(status)})")


@pytest.mark.parametrize("status,color,pulse,title", [
    ("healthy", "green", False, ""),
    ("degraded", "yellow", True, ""),
    ("stale", "gray", False, "No recent check"),
    ("pending", "gray", False, ""),
    ("disabled", "gray", False, ""),
    ("unhealthy", "red", True, ""),
    ("unreachable", "red", True, ""),
    ("unconfigured", "gray", False, "Needs setup"),
])
def test_mission_control_tile_colour_and_pulse(status, color, pulse, title):
    assert _tile(status) == {"color": color, "pulse": pulse, "title": title}


# -- the escaping gate is not vacuous on the new markup ------------------------------------------------------

def test_the_handler_escaping_gate_fails_once_on_a_planted_bad_handler(tmp_path):
    """Red-first: the same gate command CI runs, pointed at a scratch copy with
    one deliberately unescaped quoted handler, fails; on the real tree it passes."""
    import sys

    repo = Path(__file__).resolve().parents[2]
    gate = [sys.executable, str(repo / "scripts" / "check-handler-escaping.py"), "--class", "unescaped-quoted", "--max", "0"]
    real = subprocess.run([*gate, "--dir", str(FRONTEND_DIR)], capture_output=True, text=True, timeout=120)
    assert real.returncode == 0, real.stdout + real.stderr
    scratch = tmp_path / "frontend"
    shutil.copytree(FRONTEND_DIR, scratch)
    with open(scratch / "app.js", "a", encoding="utf-8") as handle:
        handle.write("\nfunction zzPlanted(name) { return `<button onclick=\"zz('${name}')\">x</button>`; }\n")
    planted = subprocess.run([*gate, "--dir", str(scratch)], capture_output=True, text=True, timeout=120)
    assert planted.returncode == 1, planted.stdout + planted.stderr


# -- Mission Control's aggregate colour ---------------------------------------------------------------

def _voice_view(data):
    return _run_node_utc([_extract_block(MISSION_CONTROL_JS, "function voiceHealthView(")], f"voiceHealthView({json.dumps(data)})")


def test_the_aggregate_is_grey_and_says_not_checked_when_there_is_no_live_row():
    view = _voice_view({"healthy": 0, "total": 0, "percentage": None, "stale_count": 3})
    assert view == {"valueText": "—", "valueClass": "text-gray-400", "label": "Not checked recently", "sparkColor": "#6b7280"}
    # the old shape (0 for nothing counted) must not be red either
    assert _voice_view({"healthy": 0, "total": 0, "percentage": 0, "stale_count": 0})["valueClass"] == "text-gray-400"
    for color in (view["valueClass"], view["sparkColor"]):
        assert "red" not in color and color != "#ef4444"


@pytest.mark.parametrize("healthy,total,pct,value_class,spark", [
    (10, 10, 100, "text-green-400", "#22c55e"),
    (9, 10, 90, "text-green-400", "#22c55e"),
    (7, 10, 70, "text-yellow-400", "#f59e0b"),
    (6, 10, 60, "text-red-400", "#ef4444"),
    (0, 4, 0, "text-red-400", "#ef4444"),   # a real 0% of live rows is still red
])
def test_the_aggregate_keeps_its_colours_when_rows_are_live(healthy, total, pct, value_class, spark):
    view = _voice_view({"healthy": healthy, "total": total, "percentage": pct, "stale_count": 0})
    assert (view["valueClass"], view["sparkColor"], view["valueText"]) == (value_class, spark, f"{healthy}/{total}")
    assert view["label"] == f"{pct}% healthy"


def test_the_aggregate_label_names_the_rows_not_checked_recently():
    assert _voice_view({"healthy": 5, "total": 5, "percentage": 100, "stale_count": 2})["label"] == \
        "100% healthy · 2 not checked recently"
