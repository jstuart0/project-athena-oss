"""Closed-world guards for conversation capture (style of test_query_entry_closed_world.py).

* every route that runs the owner-PIN check is a captured route, and the reverse;
* the PIN branch writes a content-free marker and nothing else, and the turn is
  bound only after it;
* no other module reads conversation rows back; the old recorder and its
  environment switches are gone;
* SQL is never built by formatting.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from . import _public_audience_harness as h
from orchestrator import turn_capture as tc
from shared.route_walk import iter_api_routes

REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "src" / "orchestrator" / "main.py"
SHARED_CAPTURE = REPO / "src" / "shared" / "conversation_capture.py"
ADMIN_CAPTURE = REPO / "admin" / "backend" / "app" / "services" / "conversation_capture.py"
PIN_CALL = "handle_owner_mode_utterance"


def _endpoints():
    """{path: FunctionDef} for every module-level function registered with @app.post/get."""
    tree = ast.parse(MAIN.read_text())
    found = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and isinstance(deco.func.value, ast.Name)
                    and deco.func.value.id == "app" and deco.args and isinstance(deco.args[0], ast.Constant)):
                found[deco.args[0].value] = node
    return found


def _calls(node, name):
    return [n for n in ast.walk(node)
            if isinstance(n, ast.Call) and (getattr(n.func, "attr", None) == name or getattr(n.func, "id", None) == name)]


def _pin_functions():
    """Endpoint path -> FunctionDef, for endpoints whose own source runs the PIN check."""
    return {path: fn for path, fn in _endpoints().items() if _calls(fn, PIN_CALL)}


def test_the_pin_check_endpoints_are_exactly_the_captured_endpoints():
    pinned = set(_pin_functions())
    decorated = {path for path, fn in _endpoints().items()
                 if any(isinstance(d, ast.Call) and getattr(d.func, "id", None) == "captures_turn" for d in fn.decorator_list)}
    assert len(pinned) >= 4 and "/query/stream/v2" in pinned
    assert pinned == decorated

    served = {w.path for w in iter_api_routes(h.main.app) if getattr(w.route, "endpoint", None) is not None
              and getattr(w.route.endpoint, tc.ROUTE_MARKER, None)}
    assert served == pinned, "the served routes carrying the capture marker are the PIN-check routes"


def test_exactly_four_decorated_routes_with_distinct_names_except_v1():
    labels = {getattr(w.route.endpoint, tc.ROUTE_MARKER) for w in iter_api_routes(h.main.app)
              if getattr(getattr(w.route, "endpoint", None), tc.ROUTE_MARKER, None)}
    assert labels == {"query", "query_stream", "query_stream_v2", "v1_chat_completions"}
    assert len(re.findall(r"@captures_turn\(", MAIN.read_text())) == 4


def _pin_branch(fn):
    """The ``if outcome is not None:`` statement that follows the PIN call."""
    for node in ast.walk(fn):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "outcome" and isinstance(node.test.ops[0], ast.IsNot)):
            return node
    raise AssertionError("no PIN branch")


@pytest.mark.parametrize("path", sorted(_pin_functions()))
def test_the_pin_branch_binds_only_a_withheld_marker_and_the_turn_is_bound_after_it(path):
    fn = _pin_functions()[path]
    branch = _pin_branch(fn)
    withheld = _calls(branch, "bind_withheld")
    binds = _calls(branch, "bind")
    assert len(withheld) == 1 and binds == [], "the PIN branch has exactly one bind_withheld( and no bind("
    pin_line = _calls(fn, PIN_CALL)[0].lineno
    all_binds = [c for c in _calls(fn, "bind") if c not in binds]
    assert all_binds, "the turn is bound"
    assert min(c.lineno for c in all_binds) > pin_line, "bind( comes after the PIN check"
    assert min(c.lineno for c in all_binds) > branch.lineno


def test_the_pin_branches_of_all_four_routes_are_covered_by_name():
    assert {"/query", "/query/stream", "/query/stream/v2", "/v1/chat/completions"} == set(_pin_functions())


def test_bind_withheld_cannot_receive_text():
    import inspect

    assert not set(inspect.signature(tc.TurnCapture.bind_withheld).parameters) & {
        "query", "text", "answer", "message", "messages", "response"}


def test_audience_class_is_never_a_caller_supplied_parameter():
    import inspect

    for method in (tc.TurnCapture.bind, tc.TurnCapture.bind_withheld):
        assert "audience_class" not in inspect.signature(method).parameters


def test_only_the_capture_modules_touch_conversation_turns_as_a_table():
    pattern = re.compile(r"\b(FROM|INTO|JOIN|UPDATE)\s+conversation_turns", re.IGNORECASE)
    allowed = {REPO / "src" / "orchestrator" / "turn_capture.py", SHARED_CAPTURE}
    offenders = [str(p.relative_to(REPO)) for p in (REPO / "src").rglob("*.py")
                 if p not in allowed and pattern.search(p.read_text())]
    assert offenders == [], "nothing reads captures back into prompts, sessions or the cache"
    assert pattern.search(SHARED_CAPTURE.read_text()), "the pattern does find the real writer"


def test_main_has_no_old_recorder_no_legacy_database_vars_and_no_debug_env_switch():
    source = MAIN.read_text()
    assert "ATHENA_DB_" not in source
    assert 'os.getenv("ATHENA_DEBUG_MODE"' not in source
    for gone in ("_record_conversation_turn", "_get_analytics_pool", "_get_analytics_mode_enabled",
                 "_should_capture_analytics", "_get_analytics_source", "_analytics_pool"):
        assert gone not in source, gone
    assert "_looks_like_fallback" in source and "_FALLBACK_RESPONSE_MARKERS" in source, "the semantic-cache guard stays"


def _sql_violations(path):
    tree = ast.parse(path.read_text())
    problems, constants = [], []

    def sql_expr(node, where):
        if isinstance(node, ast.JoinedStr):
            problems.append((where, node.lineno, "f-string"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            problems.append((where, node.lineno, "% formatting"))
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "format":
            problems.append((where, node.lineno, ".format()"))

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "").endswith("_SQL"):
            constants.append(node.targets[0].id)
            sql_expr(node.value, node.targets[0].id)
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in {"execute", "fetch", "fetchrow", "fetchval", "text"} and node.args:
                sql_expr(node.args[0], name)
    return problems, constants


def test_sql_is_never_built_by_formatting_in_either_capture_module():
    problems, constants = [], []
    for path in (SHARED_CAPTURE, ADMIN_CAPTURE):
        found, names = _sql_violations(path)
        problems += found
        constants += names
    assert problems == []
    assert len(constants) >= 4 and "WRITE_TURN_SQL" in constants, constants


def test_intervals_use_make_interval_never_string_concatenation():
    for path in (SHARED_CAPTURE, ADMIN_CAPTURE):
        source = path.read_text()
        assert not re.search(r"interval\s*'[^']*'\s*\|\|", source, re.IGNORECASE), path
        assert not re.search(r"\|\|\s*'[^']*'\s*::\s*interval", source, re.IGNORECASE), path
    assert "make_interval(days => :days)" in ADMIN_CAPTURE.read_text()
    assert "make_interval(mins => 5)" in SHARED_CAPTURE.read_text()
