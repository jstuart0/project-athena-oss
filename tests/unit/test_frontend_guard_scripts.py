"""
Unit tests for the ATHENA-66 frontend-escaping guard scripts (Phase 1).

D6's whole justification is that a regex gets these classifications wrong.
An untested guard is a guard that is wrong in the same way — every fixture
here proves a specific guard branch is CAPABLE of firing, per gate-script
rule 6 ("every gate must be demonstrably able to fail").
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
FRONTEND_DIR = REPO_ROOT / "admin" / "frontend"


def _load(module_file: str):
    name = module_file.replace("-", "_").rstrip(".py")
    if name in sys.modules:
        return sys.modules[name]
    path = SCRIPTS_DIR / module_file
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # required before exec_module: dataclass introspection
    # (and any other module-level code that resolves __module__ via sys.modules)
    # needs the module discoverable by name while it executes.
    spec.loader.exec_module(mod)
    return mod


scan = _load("_frontend_escape_scan.py")
uniqueness = _load("check-escape-html-uniqueness.py")
handler = _load("check-handler-escaping.py")
callee = _load("check-callee-sinks.py")
html_template = _load("check-html-template-escaping.py")

NODE_BIN = shutil.which("node")
requires_node = pytest.mark.skipif(NODE_BIN is None, reason="node is required for this fixture")


# ---------------------------------------------------------------------------
# tessa F4 — mutation_a1_a11: the shipped harness's mutation is demonstrably
# able to fail the behavioural assertions it protects.
# ---------------------------------------------------------------------------


def _run_node_against(path: Path, js_expr: str):
    script = f"""
    'use strict';
    global.window = global;
    require({json.dumps(str(path))});
    const result = (function() {{ return {js_expr}; }})();
    process.stdout.write(JSON.stringify(result));
    """
    proc = subprocess.run([NODE_BIN, "-e", script], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@requires_node
def test_mutation_a1_a11_single_quote_goes_red(tmp_path):
    """Drop the `'` -> `&#39;` replace (D2's byte-pin, catalogue instance 3's
    entity) from a temp copy of the REAL shipped file. The A1-A11 behavioural
    cluster must go red against the mutant — this is the mutation-testing
    demonstration the plan calls "the one load-bearing gate in round 1
    without one."
    """
    original = (FRONTEND_DIR / "escape-html.js").read_text(encoding="utf-8")
    mutated = original.replace(".replace(/'/g, '&#39;')", "")
    assert mutated != original, "mutation target string not found — fixture is stale"

    mutant = tmp_path / "escape-html.js"
    mutant.write_text(mutated, encoding="utf-8")

    # A6 (byte-pin) and the "'" case of the A1-A5 parametrized cluster both
    # assert exactly this value; the mutant must fail it.
    result = _run_node_against(mutant, "window.escapeHtml(\"'\")")
    assert result != "&#39;", "mutation did not turn the byte-pin assertion red"

    # Sanity: the REAL file must still pass, proving the mutation (not some
    # environment quirk) is what causes the failure above.
    real_result = _run_node_against(FRONTEND_DIR / "escape-html.js", "window.escapeHtml(\"'\")")
    assert real_result == "&#39;"


# ---------------------------------------------------------------------------
# tessa F1 — returncode_gate: a real failure that prints no "SKIPPED" must
# be caught by returncode alone, not by text-matching for "SKIPPED".
# ---------------------------------------------------------------------------


def test_returncode_gate_catches_failure_without_skipped_text(tmp_path):
    failing_test = tmp_path / "test_deliberately_failing.py"
    failing_test.write_text(
        "def test_this_fails_and_never_mentions_the_s_word():\n"
        "    assert 1 == 2, 'intentional failure for the returncode gate fixture'\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(failing_test), "-q"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The gate's contract (workflow step "Assert escaping contract has zero
    # skips"): returncode is read FIRST and is sufficient on its own to fail
    # the gate. A naive gate that only greps output for "SKIPPED" would
    # wrongly pass this case — assert the word never appears, so a
    # text-matching-only implementation could not have caught it any other
    # way.
    assert proc.returncode != 0, "the failing test must produce a non-zero returncode"
    assert "SKIPPED" not in proc.stdout and "skipped" not in proc.stdout.lower()
    gate_would_pass_reading_returncode_first = proc.returncode == 0
    assert not gate_would_pass_reading_returncode_first


# ---------------------------------------------------------------------------
# D3 / catalogue instance 7 — require_node_fails_not_skips
# ---------------------------------------------------------------------------


def test_require_node_fails_not_skips(tmp_path):
    stub = tmp_path / "node"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)

    env = dict(os.environ)
    env["ATHENA_REQUIRE_NODE"] = "1"
    env["ATHENA_NODE_BIN"] = str(stub)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_admin_frontend_escaping.py", "-q", "-rs"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode != 0, (
        "ATHENA_REQUIRE_NODE=1 with a broken node interpreter must FAIL, "
        f"not pass. stdout:\n{proc.stdout}"
    )
    assert "SKIPPED" not in proc.stdout


def test_require_node_unset_skips_with_broken_interpreter(tmp_path):
    stub = tmp_path / "node"
    stub.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)

    env = dict(os.environ)
    env.pop("ATHENA_REQUIRE_NODE", None)
    env["ATHENA_NODE_BIN"] = str(stub)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/test_admin_frontend_escaping.py", "-q", "-rs"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0
    assert "SKIPPED" in proc.stdout
    assert "ATHENA_REQUIRE_NODE" in proc.stdout


# ---------------------------------------------------------------------------
# librarian H — fresh_name_entity_map
# ---------------------------------------------------------------------------


def test_fresh_name_entity_map_is_flagged(tmp_path):
    (tmp_path / "sanitizeLabel.js").write_text(
        "function sanitizeLabel(str) {\n"
        "    return String(str)\n"
        "        .replace(/&/g, '&amp;')\n"
        "        .replace(/</g, '&lt;')\n"
        "        .replace(/>/g, '&gt;');\n"
        "}\n",
        encoding="utf-8",
    )
    findings = uniqueness.find_entity_map_any_name(
        tmp_path, uniqueness.resolve_exclude_paths(tmp_path, None)
    )
    assert len(findings) == 1
    assert findings[0]["name"] == "sanitizeLabel"


# ---------------------------------------------------------------------------
# rule 12 — canonical_self_exclusion (path-scoped, not shape-scoped)
# ---------------------------------------------------------------------------


def test_canonical_self_exclusion_is_path_scoped(tmp_path):
    canonical_text = (FRONTEND_DIR / "escape-html.js").read_text(encoding="utf-8")
    (tmp_path / "escape-html.js").write_text(canonical_text, encoding="utf-8")

    import re as _re

    m = _re.search(r"function escapeHtml\(str\) \{.*?\n    \}", canonical_text, _re.DOTALL)
    assert m, "fixture setup: could not locate escapeHtml body in the real canonical file"
    (tmp_path / "copy.js").write_text(m.group(0) + "\n", encoding="utf-8")

    excludes = uniqueness.resolve_exclude_paths(tmp_path, None)
    findings = uniqueness.find_entity_map_any_name(tmp_path, excludes)

    flagged_files = {Path(f["file"]).name for f in findings}
    assert "escape-html.js" not in flagged_files, "the canonical file's own body must not be flagged"
    assert "copy.js" in flagged_files, (
        "a byte-identical copy of the canonical body in ANY OTHER file must be flagged — "
        "exclusion is path-scoped, not shape-scoped"
    )


# ---------------------------------------------------------------------------
# D9b — builder_set_equality
# ---------------------------------------------------------------------------


def test_builder_set_equality_real_tree():
    builders = scan.discover_builders(FRONTEND_DIR)
    found = {b.key(REPO_ROOT) for b in builders}
    assert found == {"admin/frontend/oss-profiles.js:99 actionButton"}


def test_builder_set_equality_flags_a_synthetic_second_builder(tmp_path):
    (tmp_path / "oss-profiles.js").write_text(
        (FRONTEND_DIR / "oss-profiles.js").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "second-builder.js").write_text(
        "function secondBuilder(label, onClick) {\n"
        '    return `<button onclick="${onClick}">${label}</button>`;\n'
        "}\n",
        encoding="utf-8",
    )
    builders = scan.discover_builders(tmp_path)
    found = {b.key(tmp_path) for b in builders}
    assert len(builders) == 2, "a synthetic second builder must be discovered, not silently dropped"
    assert any("secondBuilder" in k for k in found)
    # The real script's --check builder-set compares against the EXPECTED
    # set; a found set with an extra member must fail equality.
    assert found != {"oss-profiles.js:99 actionButton"}


# ---------------------------------------------------------------------------
# xander H2 — ternary_span: a ternary-with-quotes inside a handler span does
# not defeat the depth-tracking parser.
# ---------------------------------------------------------------------------


def test_ternary_span_with_quotes_does_not_defeat_the_parser():
    raw_value = "foo('${cond ? 'a' : 'b'}')"
    interps = scan.classify_span_interpolations(raw_value)
    assert len(interps) == 1
    it = interps[0]
    assert it.position == "quoted"
    assert it.expr == "cond ? 'a' : 'b'"
    # Neither escapeHtml nor escapeJsAttr wraps a raw ternary -> unescaped.
    assert it.escape is None


# ---------------------------------------------------------------------------
# D9 — callee_sink_known_instances: both verified BLOCKER-4 instances.
# ---------------------------------------------------------------------------


def test_callee_sink_known_instances_are_found():
    sites = callee.collect_sites(FRONTEND_DIR)
    adjudications = callee.load_adjudications(callee.DEFAULT_ADJUDICATIONS)

    found = {}
    for file, line, expr, raw_value, offset in sites:
        bucket, detail = callee.resolve_site(file, line, expr, raw_value, offset, adjudications)
        found[(file.name, line)] = (bucket, detail)

    reveal_bucket, reveal_detail = found[("app.js", 1180)]
    assert reveal_bucket == "sink-unescaped"
    assert reveal_detail["callee"] == "revealSecret"

    clone_bucket, clone_detail = found[("escalation.js", 330)]
    assert clone_bucket == "sink-unescaped"
    assert clone_detail["callee"] == "showCloneEscalationPresetModal"


def test_callee_sink_unresolved_is_not_silently_dropped(tmp_path):
    (tmp_path / "widget.js").write_text(
        "function renderWidget(items) {\n"
        '    return `<button onclick="doThing(${items.id}, \'${items.name}\')">Go</button>`;\n'
        "}\n"
        "function doThing(id, { label }) {\n"
        "    return `<div>${label}</div>`;\n"
        "}\n",
        encoding="utf-8",
    )
    sites = callee.collect_sites(tmp_path)
    assert sites, "fixture setup produced no sites"
    bucket, detail = callee.resolve_site(*sites[0], {})
    assert bucket == "unresolved"


def test_callee_sink_ambiguous_same_name_fails_closed_not_guessed(tmp_path):
    """Two files each declare a top-level `sameName` — a real shape in this
    codebase (`showError`, `formatDate`, `getToken`, ... all collide across
    files). Which one wins at runtime depends on `<script>` tag load order,
    which this classifier does not consult. Picking the alphabetically-first
    file is a silent guess; the resolver must land this in `unresolved`
    instead.
    """
    (tmp_path / "aaa_widget.js").write_text(
        "function sameName(value) {\n    return `<div>${value}</div>`;\n}\n",
        encoding="utf-8",
    )
    (tmp_path / "caller_widget.js").write_text(
        "function renderCaller(value) {\n"
        '    return `<button onclick="sameName(\'${value}\')">Go</button>`;\n'
        "}\n",
        encoding="utf-8",
    )
    (tmp_path / "zzz_widget.js").write_text(
        "function sameName(value) {\n    return `<div>${escapeHtml(value)}</div>`;\n}\n",
        encoding="utf-8",
    )
    sites = callee.collect_sites(tmp_path)
    assert sites, "fixture setup produced no sites"
    bucket, detail = callee.resolve_site(*sites[0], {})
    assert bucket == "unresolved"
    assert "ambiguous" in detail["reason"]


def test_callee_sink_dynamic_dispatch_fails_closed(tmp_path):
    """`window[handlerName](...)` has no statically visible callee identifier
    directly followed by `(` — the resolver must not guess a callee and must
    land the site in `unresolved` rather than silently reporting `sink-escaped`.
    """
    (tmp_path / "dynamic_widget.js").write_text(
        "function renderCaller(value, handlerName) {\n"
        '    return `<button onclick="window[handlerName](\'${value}\')">Go</button>`;\n'
        "}\n",
        encoding="utf-8",
    )
    sites = callee.collect_sites(tmp_path)
    assert sites, "fixture setup produced no sites"
    bucket, detail = callee.resolve_site(*sites[0], {})
    assert bucket == "unresolved"


def test_callee_sink_does_not_conflate_unrelated_param_mention_with_sink(tmp_path):
    """A parameter used in a fetch URL / DOM selector, in a callee that
    SEPARATELY assigns an unrelated static string to `.innerHTML`, must not
    be flagged. This is the false-positive shape found in five of the ten
    `sink-unescaped` hits from a bare "both patterns appear somewhere in the
    body" co-occurrence check (Phase 2 trace): the parameter and the sink
    exist in the same function but never touch the same template literal.
    """
    (tmp_path / "widget.js").write_text(
        "function checkStatus(serviceName) {\n"
        "    const el = document.getElementById(`status-${serviceName}`);\n"
        "    el.innerHTML = '<span>Checking...</span>';\n"
        "    fetch(`/api/status/${serviceName}`);\n"
        "}\n"
        "function renderCaller(name) {\n"
        '    return `<button onclick="checkStatus(\'${name}\')">Go</button>`;\n'
        "}\n",
        encoding="utf-8",
    )
    sites = callee.collect_sites(tmp_path)
    assert sites, "fixture setup produced no sites"
    bucket, _detail = callee.resolve_site(*sites[0], {})
    assert bucket == "sink-escaped"


# ---------------------------------------------------------------------------
# C3 — detached_sink: the HTML-template guard keys on template POSITION,
# not on sink function name. emerging-intents.js:231's sink is three
# functions away.
# ---------------------------------------------------------------------------


def test_detached_sink_found_by_position_not_sink_name():
    items = html_template.collect(FRONTEND_DIR, FRONTEND_DIR / "emerging-intents.js")
    lines = {i["line"] for i in items if i["bucket"] == "text-node" and not i["escaped"]}
    assert 231 in lines, (
        "emerging-intents.js:231 must be found by TEMPLATE POSITION even though its "
        "sink (container.innerHTML at :82) is three functions away from the interpolation"
    )


# ---------------------------------------------------------------------------
# Phase 3 — `--scope phase3` was "an unused filter hook, reserved" in Phase 1.
# A `--max 0` gate run against an unimplemented filter is exactly rule 8's
# vacuity failure: it reads green having filtered to nothing. This proves
# the implemented filter genuinely NARROWS the population (scoped != all)
# and genuinely FAILS when an in-scope site still has an unescaped sink.
# ---------------------------------------------------------------------------

_SCOPED_WIDGET = (
    "function inScopeCallee(name) {{\n"
    "    document.getElementById('x').innerHTML = `<div>{name_expr}</div>`;\n"
    "}}\n"
    "function renderInScope(name) {{\n"
    '    return `<button onclick="inScopeCallee(\'${{name}}\')">Go</button>`;\n'
    "}}\n"
)
_UNSCOPED_WIDGET = (
    "function outOfScopeCallee(label) {\n"
    "    document.getElementById('y').innerHTML = `<div>${label}</div>`;\n"
    "}\n"
    "function renderOutOfScope(label) {\n"
    '    return `<button onclick="outOfScopeCallee(\'${label}\')">Go</button>`;\n'
    "}\n"
)


def _write_scope_fixture(tmp_path: Path, in_scope_sink_escaped: bool) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    name_expr = "${escapeHtml(name)}" if in_scope_sink_escaped else "${name}"
    in_scope_file = tmp_path / "in_scope_widget.js"
    in_scope_file.write_text(_SCOPED_WIDGET.format(name_expr=name_expr), encoding="utf-8")
    out_of_scope_file = tmp_path / "out_of_scope_widget.js"
    out_of_scope_file.write_text(_UNSCOPED_WIDGET, encoding="utf-8")

    # The in-scope call site is `in_scope_widget.js:5` — the `onclick=` line
    # in renderInScope. Manifest keys are file:line, relative-`rel()`-style;
    # `check-callee-sinks.rel()` falls back to str(path) outside REPO_ROOT,
    # so a tmp_path fixture's keys are its own absolute paths.
    call_line = 5
    scopes_path = tmp_path / "scopes.json"
    scopes_path.write_text(
        json.dumps({"phase3": [f"{in_scope_file}:{call_line}"]}),
        encoding="utf-8",
    )
    return scopes_path, tmp_path


def test_scope_phase3_narrows_to_the_pinned_population(tmp_path):
    scopes_path, frontend_dir = _write_scope_fixture(tmp_path, in_scope_sink_escaped=False)

    unscoped = callee.collect_sites(frontend_dir)
    adjudications = {}
    unscoped_violations = [
        s for s in unscoped
        if callee.resolve_site(*s, adjudications)[0] == "sink-unescaped"
    ]
    assert len(unscoped_violations) == 2, "fixture setup: both callees must start with an unescaped sink"

    scope_keys = callee.load_phase_scope("phase3", scopes_path)
    scoped = callee.filter_sites_by_scope(unscoped, scope_keys)
    scoped_violations = [
        s for s in scoped
        if callee.resolve_site(*s, adjudications)[0] == "sink-unescaped"
    ]
    assert len(scoped_violations) == 1, (
        "scope=phase3 must narrow to exactly the pinned in-scope site, not the full "
        "population — a scope that returns the same count as unscoped is not filtering"
    )
    assert scoped_violations[0][0].name == "in_scope_widget.js"


def test_scope_phase3_can_fail_when_an_in_scope_sink_is_unescaped(tmp_path):
    # In-scope sink still unescaped: the scoped gate must fail (exit 1).
    scopes_path, frontend_dir = _write_scope_fixture(tmp_path, in_scope_sink_escaped=False)
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "check-callee-sinks.py"),
            "--dir", str(frontend_dir),
            "--phase-scopes", str(scopes_path),
            "--scope", "phase3",
            "--class", "sink-unescaped",
            "--max", "0",
            "--json",
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 1, f"expected FAIL (in-scope sink still unescaped), got rc={proc.returncode}: {proc.stdout} {proc.stderr}"
    payload = json.loads(proc.stdout)
    assert payload["count"] == 1
    assert payload["matches"][0]["callee"] == "inScopeCallee"

    # Fix only the in-scope sink: the scoped gate goes green even though the
    # out-of-scope sink is still unescaped — proving the two are genuinely
    # decoupled, not coincidentally equal.
    scopes_path2, frontend_dir2 = _write_scope_fixture(tmp_path / "fixed", in_scope_sink_escaped=True)
    proc_fixed = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "check-callee-sinks.py"),
            "--dir", str(frontend_dir2),
            "--phase-scopes", str(scopes_path2),
            "--scope", "phase3",
            "--class", "sink-unescaped",
            "--max", "0",
            "--json",
        ],
        capture_output=True, text=True,
    )
    assert proc_fixed.returncode == 0, f"expected PASS once the in-scope sink is escaped: {proc_fixed.stdout} {proc_fixed.stderr}"

    proc_fixed_unscoped = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "check-callee-sinks.py"),
            "--dir", str(frontend_dir2),
            "--class", "sink-unescaped",
            "--max", "0",
            "--json",
        ],
        capture_output=True, text=True,
    )
    assert proc_fixed_unscoped.returncode == 1, (
        "the out-of-scope sink is still unescaped in this fixture — the UNSCOPED "
        "check must still fail even though the scoped one now passes"
    )


def test_scope_phase3_errors_when_manifest_missing(tmp_path):
    proc = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "check-callee-sinks.py"),
            "--dir", str(FRONTEND_DIR),
            "--phase-scopes", str(tmp_path / "does-not-exist.json"),
            "--scope", "phase3",
            "--class", "sink-unescaped",
            "--max", "0",
        ],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2, "a missing scope manifest is could-not-run, never a silent empty-scope pass"
