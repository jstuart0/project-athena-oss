"""Closed world: every query entry point reaches `_fast_path_turn` after the owner-PIN check.

Routes are read from the running app (`shared.route_walk.iter_api_routes`),
their bodies from `main.py`'s AST. In each function (the handler or a nested
stream generator) that calls `handle_owner_mode_utterance`, `_fast_path_turn`
must follow it.

Coordination with the request-capture work: when a function also calls a
capture `bind(...)`, the order must be PIN < bind < `_fast_path_turn`, and in
a stream generator the fast-path exit block must call `answered(...)` before
its first `yield`. Those clauses stay dormant until `bind` exists in the
function; the self-test below feeds synthetic sources with and without
`bind` so each clause is proven to discriminate now. `_fast_path_turn`'s own
body must never mention capture.
"""
from __future__ import annotations

import ast
import types
import typing

import pytest

from . import _public_audience_harness as h

def _unwrap(annotation):
    """Optional[X] / X | None -> X."""
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if typing.get_origin(annotation) in (typing.Union, types.UnionType) and len(args) == 1:
        return args[0]
    return annotation


PIN = "handle_owner_mode_utterance"
FAST = "_fast_path_turn"
BIND = "bind"
ANSWERED = "answered"
FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _call_name(call: ast.Call):
    return getattr(call.func, "id", None) or getattr(call.func, "attr", None)


def _own_nodes(fn):
    """Nodes of `fn` excluding the bodies of functions nested inside it."""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, FUNCTION_NODES + (ast.Lambda,)):
                stack.append(child)


def _calls(nodes, name):
    return sorted(
        (n for n in nodes if isinstance(n, ast.Call) and _call_name(n) == name),
        key=lambda n: (n.lineno, n.col_offset),
    )


def _fast_exit_block(own, fast_call):
    """The `if <result of _fast_path_turn> is not None:` block that follows the call."""
    assigned = None
    for node in own:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Await) and node.value.value is fast_call:
            assigned = node.targets[0].id
    if assigned is None:
        return None
    candidates = [
        n for n in own
        if isinstance(n, ast.If) and n.lineno > fast_call.lineno
        and any(isinstance(t, ast.Name) and t.id == assigned for t in ast.walk(n.test))
    ]
    return min(candidates, key=lambda n: n.lineno) if candidates else None


def violations(fn) -> list:
    """Wiring violations in one function body (nested functions are checked by the caller)."""
    own = list(_own_nodes(fn))
    pins, fasts, binds = _calls(own, PIN), _calls(own, FAST), _calls(own, BIND)
    problems = []
    if not pins:
        return problems
    if not fasts:
        return [f"{fn.name}: calls {PIN} but never {FAST}"]
    if fasts[0].lineno <= pins[0].lineno:
        problems.append(f"{fn.name}: {FAST} runs before {PIN}")
    if binds:
        if not (pins[0].lineno < binds[0].lineno < fasts[0].lineno):
            problems.append(f"{fn.name}: order must be {PIN} < {BIND} < {FAST}")
        if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in own):
            block = _fast_exit_block(own, fasts[0])
            if block is None:
                problems.append(f"{fn.name}: no exit block after {FAST}")
            else:
                inner = list(ast.walk(block))
                answered = _calls(inner, ANSWERED)
                yields = sorted((n for n in inner if isinstance(n, (ast.Yield, ast.YieldFrom))), key=lambda n: n.lineno)
                if not answered or (yields and answered[0].lineno > yields[0].lineno):
                    problems.append(f"{fn.name}: the fast-path exit must call {ANSWERED}() before its first yield")
    return problems


def all_violations(tree_or_fn) -> list:
    functions = [n for n in ast.walk(tree_or_fn) if isinstance(n, FUNCTION_NODES)]
    return [p for fn in functions for p in violations(fn)]


# --- synthetic sources: the capture clauses discriminate ---------------------------------

NO_BIND_GOOD = """
async def gen():
    outcome = await handle_owner_mode_utterance(q, a, b)
    if outcome is not None:
        yield 1
        return
    fast = await _fast_path_turn(q)
    if fast is not None:
        yield 2
        return
"""
NO_BIND_FAST_FIRST = """
async def gen():
    fast = await _fast_path_turn(q)
    outcome = await handle_owner_mode_utterance(q, a, b)
"""
NO_BIND_MISSING = """
async def gen():
    outcome = await handle_owner_mode_utterance(q, a, b)
    yield 1
"""
BIND_GOOD = """
async def gen():
    outcome = await handle_owner_mode_utterance(q, a, b)
    capture = bind(request)
    fast = await _fast_path_turn(q)
    if fast is not None:
        capture.answered(fast.text)
        yield 2
        return
"""
BIND_AFTER_FAST = """
async def gen():
    outcome = await handle_owner_mode_utterance(q, a, b)
    fast = await _fast_path_turn(q)
    capture = bind(request)
    if fast is not None:
        capture.answered(fast.text)
        yield 2
"""
BIND_BEFORE_PIN = """
async def gen():
    capture = bind(request)
    outcome = await handle_owner_mode_utterance(q, a, b)
    fast = await _fast_path_turn(q)
    if fast is not None:
        capture.answered(fast.text)
        yield 2
"""
BIND_NO_ANSWERED = """
async def gen():
    outcome = await handle_owner_mode_utterance(q, a, b)
    capture = bind(request)
    fast = await _fast_path_turn(q)
    if fast is not None:
        yield 2
"""
BIND_ANSWERED_AFTER_YIELD = """
async def gen():
    outcome = await handle_owner_mode_utterance(q, a, b)
    capture = bind(request)
    fast = await _fast_path_turn(q)
    if fast is not None:
        yield 2
        capture.answered(fast.text)
"""
# /query-shaped: a plain coroutine, so the answered() rule (stream exits only) doesn't apply
BIND_NON_GENERATOR_GOOD = """
async def handler():
    outcome = await handle_owner_mode_utterance(q, a, b)
    capture = bind(request)
    fast = await _fast_path_turn(q)
    if fast is not None:
        return fast
"""


def _check(source):
    return all_violations(ast.parse(source))


def test_self_test_without_bind():
    assert _check(NO_BIND_GOOD) == []
    assert _check(NO_BIND_FAST_FIRST)
    assert _check(NO_BIND_MISSING)


def test_self_test_with_bind():
    assert _check(BIND_GOOD) == []
    assert _check(BIND_NON_GENERATOR_GOOD) == []
    for bad in (BIND_AFTER_FAST, BIND_BEFORE_PIN, BIND_NO_ANSWERED, BIND_ANSWERED_AFTER_YIELD):
        assert _check(bad), bad


def test_capture_clauses_are_dormant_without_bind():
    """The same sources minus `bind` pass, so the clauses activate only with it."""
    for source in (BIND_NO_ANSWERED, BIND_ANSWERED_AFTER_YIELD, BIND_AFTER_FAST):
        assert _check(source.replace("    capture = bind(request)\n", "")) == [], source


# --- the real tree ------------------------------------------------------------------------


@pytest.fixture(scope="module")
def tree():
    return ast.parse(h.MAIN_PY.read_text(encoding="utf-8"))


def _endpoints():
    from shared.route_walk import iter_api_routes

    found = {}
    for walked in iter_api_routes(h.main.app):
        hints = typing.get_type_hints(walked.route.endpoint)
        hints.pop("return", None)
        if any(_unwrap(t) in (h.main.QueryRequest, h.main.OpenAIChatRequest) for t in hints.values()):
            found[walked.route.endpoint.__name__] = walked.path
    return found


def test_every_query_entry_point_reaches_the_fast_path_after_the_pin_check(tree):
    endpoints = _endpoints()
    assert len(endpoints) >= 4
    assert {"process_query", "process_query_stream", "process_query_stream_v2", "chat_completions"} <= set(endpoints)
    top = {n.name: n for n in tree.body if isinstance(n, FUNCTION_NODES)}
    total_pins = 0
    for name in endpoints:
        fn = top[name]
        everything = list(ast.walk(fn))
        pins = _calls(everything, PIN)
        fasts = _calls(everything, FAST)
        if name == "chat_completions":
            assert _calls(everything, "process_query"), "the non-streaming branch must delegate to process_query"
        else:
            assert pins, f"{name} has no owner-PIN check"
        assert len(fasts) == len(pins), f"{name}: one {FAST} per {PIN}"
        total_pins += len(pins)
        assert all_violations(fn) == []
    assert total_pins == len(_calls(list(ast.walk(tree)), PIN)) == 4


def test_fast_path_turn_never_mentions_capture(tree):
    fn = next(n for n in tree.body if isinstance(n, FUNCTION_NODES) and n.name == FAST)
    mentioned = {
        getattr(n, "id", None) or getattr(n, "attr", None)
        for n in ast.walk(fn)
        if isinstance(n, (ast.Name, ast.Attribute))
    }
    assert not {m for m in mentioned if m and ("capture" in m.lower() or m == ANSWERED)}
    for token in ("capture", "answered("):
        assert token not in ast.unparse(fn).replace("fast_path_answered_total", "")


def test_a_route_that_skips_the_fast_path_would_fail():
    """Mutation guard on the real shape: dropping the call from a real handler is caught."""
    source = h.MAIN_PY.read_text(encoding="utf-8")
    mutated = source.replace("fast = await _fast_path_turn(", "fast = await _unrelated(", 1)
    assert mutated != source
    assert all_violations(ast.parse(mutated))
