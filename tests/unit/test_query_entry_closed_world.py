"""Closed world over the query entry points (V1.4).

Every route handler taking a QueryRequest must resolve authorization with
the request's caller_trust, build its context only through
build_query_context, and bind its session with a caller class. A new entry
point that skips any of these fails here.
"""
from __future__ import annotations

import ast
import types
import typing

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from . import _public_audience_harness as h

ALLOWED_EXCEPTIONS: set[str] = set()


def test_unwrap_handles_optional():
    assert _unwrap(typing.Optional[h.main.QueryRequest]) is h.main.QueryRequest
    assert _unwrap(h.main.QueryRequest | None) is h.main.QueryRequest


def _unwrap(annotation):
    """Optional[X] / X | None -> X."""
    args = [a for a in typing.get_args(annotation) if a is not type(None)]
    if typing.get_origin(annotation) in (typing.Union, types.UnionType) and len(args) == 1:
        return args[0]
    return annotation


def _query_request_endpoints():
    """Every registered route whose endpoint takes a QueryRequest, read from
    the running app, not from source text."""
    found = {}
    for route in h.main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        hints = typing.get_type_hints(route.endpoint)
        hints.pop("return", None)
        if any(_unwrap(t) is h.main.QueryRequest for t in hints.values()):
            found[route.endpoint.__name__] = route.path
    return found


def _functions_by_name(tree):
    return {fn.name: fn for fn in tree.body if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_named(fn, name):
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and (getattr(n.func, "id", None) == name or getattr(n.func, "attr", None) == name)
    ]


def test_every_query_entry_point_is_closed():
    endpoints = _query_request_endpoints()
    assert len(endpoints) >= 3
    assert {"process_query", "process_query_stream", "process_query_stream_v2"} <= set(endpoints)
    functions = _functions_by_name(ast.parse(h.MAIN_PY.read_text(encoding="utf-8")))
    for name in endpoints:
        if name in ALLOWED_EXCEPTIONS:
            continue
        fn = functions[name]
        authz = _calls_named(fn, "resolve_request_authorization")
        assert authz, fn.name
        for call in authz:
            kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
            assert kw.get("caller_trust") == "request.caller_trust", fn.name
        assert _calls_named(fn, "build_query_context"), fn.name
        source = ast.unparse(fn)
        assert "dict(request.context)" not in source, fn.name
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Subscript) and ast.unparse(target.value) == "query_context":
                        raise AssertionError(f"{fn.name} writes query_context[...] directly")
        sessions = _calls_named(fn, "get_or_create_session")
        assert sessions and all("caller_class" in {k.arg for k in c.keywords} for c in sessions), fn.name


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.mark.parametrize("path", ["/query", "/query/stream", "/query/stream/v2"])
def test_public_cannot_control_even_when_guest_profile_allows(monkeypatch, path):
    """Behavioural matrix: the guest profile allows control and switch, the
    house is in guest mode, and a public caller asks to turn on the garage
    relay. The state handed to the pipeline denies both the intent and the
    write."""
    from orchestrator.mode_permission import authorize_ha_write, check_intent_permission

    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="guest")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    captured = []

    class _Graph:
        async def ainvoke(self, state):
            captured.append(state)
            return {"intent": h.IntentCategory.CONTROL, "answer": "ok", "confidence": 1.0,
                    "citations": [], "request_id": "r", "node_timings": {}}

    async def _stream_run(state):
        captured.append(state)
        state.answer = "ok"
        return state

    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", _stream_run)
    client = TestClient(h.main.app)
    with client.stream(
        "POST", path,
        json={"query": "turn on the garage relay", "caller_trust": "web_public"},
        headers=h.service_headers(),
    ) as resp:
        assert resp.status_code == 200
        list(resp.iter_text())
    assert len(captured) == 1
    permissions = captured[0].permissions
    assert check_intent_permission(h.IntentCategory.CONTROL, permissions) is False
    assert authorize_ha_write("switch", "turn_on", {"entity_id": "switch.garage_relay"}, permissions).allowed is False
