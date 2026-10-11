"""The /v1 follow-up surface: the gateway marks a believed voice device, the
orchestrator reads that mark only from a service-key caller.

Static checks pin who may set and read `supports_followup` /
`voice_device_id` on the /v1 path (each with a negative twin proving the
check can fail); the round trip builds the body with the real gateway
builder and posts it to the real orchestrator app."""
from __future__ import annotations

import ast
import asyncio
import json
import os
import sys
import unittest.mock as mock
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
GATEWAY_MAIN = REPO / "src" / "gateway" / "main.py"
ORCH_MAIN = REPO / "src" / "orchestrator" / "main.py"
DEVICE = "ab" * 16


def _function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"function {name} not found")


def _names_in(test: ast.AST) -> set:
    return {n.id for n in ast.walk(test) if isinstance(n, ast.Name)}


def _is_true_constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _subscript_key(node: ast.AST):
    return node.slice.value if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) else None


# (a) -----------------------------------------------------------------------

def builder_sets_follow_up_only_as_true_under_voice_device_id(source: str) -> list:
    """Problems with how a payload builder sets supports_followup."""
    problems = []
    func = _function(ast.parse(source), "_orchestrator_openai_payload")

    def walk(node, guards):
        if isinstance(node, ast.If):
            for child in node.body:
                walk(child, guards + [_names_in(node.test)])
            for child in node.orelse:
                walk(child, guards)
            return
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _subscript_key(target) == "supports_followup":
                    if not _is_true_constant(node.value):
                        problems.append("supports_followup is not the literal True")
                    if not any("voice_device_id" in g for g in guards):
                        problems.append("supports_followup is not under a voice_device_id guard")
        for child in ast.iter_child_nodes(node):
            walk(child, guards)

    walk(func, [])
    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "supports_followup" and not _is_true_constant(value):
                    problems.append("supports_followup set in a dict literal that is not True")
    return problems


def builder_voice_device_id_is_the_parameter(source: str) -> list:
    func = _function(ast.parse(source), "_orchestrator_openai_payload")
    params = {a.arg for a in func.args.args + func.args.kwonlyargs}
    problems = []
    if "voice_device_id" not in params:
        problems.append("voice_device_id is not a parameter")
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if _subscript_key(target) == "voice_device_id":
                    if not (isinstance(node.value, ast.Name) and node.value.id == "voice_device_id"):
                        problems.append("voice_device_id is not the parameter")
    return problems


def handlers_feed_voice_device_id_only_from_the_marker(source: str) -> list:
    """In each /v1 route handler, voice_device_id is assigned only by
    unpacking _speaker_room_for_request(device_id=<the marker>)."""
    tree = ast.parse(source)
    problems = []
    for name in ("chat_completions", "responses_api"):
        func = _function(tree, name)
        marker_assignments = 0
        for node in ast.walk(func):
            if not isinstance(node, ast.Assign):
                continue
            names = {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
            call = node.value if isinstance(node.value, ast.Call) else None
            if call is not None and isinstance(call, ast.Await):
                call = call.value
            value = node.value.value if isinstance(node.value, ast.Await) else node.value
            if "voice_device_id" in names:
                ok = (
                    isinstance(value, ast.Call)
                    and getattr(value.func, "id", None) == "_speaker_room_for_request"
                    and any(
                        kw.arg == "device_id" and isinstance(kw.value, ast.Name) and kw.value.id == "marker_device_id"
                        for kw in value.keywords
                    )
                )
                if not ok:
                    problems.append(f"{name}: voice_device_id is not from the marker")
            if "marker_device_id" in names:
                marker_assignments += 1
                ok = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Attribute)
                    and value.func.attr == "extract_device_marker"
                )
                if not ok:
                    problems.append(f"{name}: marker_device_id is not from extract_device_marker")
        if marker_assignments != 1:
            problems.append(f"{name}: expected one marker assignment, found {marker_assignments}")
    return problems


# (c) -----------------------------------------------------------------------

def orchestrator_reads_follow_up_keys_only_when_service_authenticated(source: str) -> list:
    func = _function(ast.parse(source), "chat_completions")
    problems = []

    def is_service_guard(test):
        return (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name) and test.left.id == "service_authenticated"
            and len(test.ops) == 1 and isinstance(test.ops[0], ast.Is)
            and _is_true_constant(test.comparators[0])
        )

    def walk(node, guarded):
        if isinstance(node, ast.If):
            inner = guarded or is_service_guard(node.test)
            for child in node.body:
                walk(child, inner)
            for child in node.orelse:
                walk(child, guarded)
            return
        if isinstance(node, ast.Constant) and node.value in ("voice_device_id", "supports_followup", "trusted_voice") and not guarded:
            problems.append(f"{node.value!r} read outside `if service_authenticated is True`")
        for child in ast.iter_child_nodes(node):
            walk(child, guarded)

    for stmt in func.body:
        walk(stmt, False)
    # the keyword arguments to QueryRequest(...) are Name loads, not strings
    return problems


def test_the_builder_sets_the_follow_up_flag_only_as_true_under_the_device_guard():
    assert builder_sets_follow_up_only_as_true_under_voice_device_id(GATEWAY_MAIN.read_text()) == []


def test_the_builder_forwards_only_its_voice_device_id_parameter():
    assert builder_voice_device_id_is_the_parameter(GATEWAY_MAIN.read_text()) == []


def test_negative_twin_a_builder_passing_a_request_value_fails_the_check():
    planted = '''
def _orchestrator_openai_payload(request, room=None, *, stream, channel, voice_device_id=None):
    payload = {"extra_body": {}}
    if voice_device_id:
        payload["extra_body"]["voice_device_id"] = voice_device_id
        payload["extra_body"]["supports_followup"] = request.supports_followup
    return payload
'''
    assert "supports_followup is not the literal True" in builder_sets_follow_up_only_as_true_under_voice_device_id(planted)


def test_negative_twin_an_unguarded_flag_fails_the_check():
    planted = '''
def _orchestrator_openai_payload(request, room=None, *, stream, channel, voice_device_id=None):
    payload = {"extra_body": {"supports_followup": True}}
    payload["extra_body"]["supports_followup"] = True
    return payload
'''
    assert builder_sets_follow_up_only_as_true_under_voice_device_id(planted)


def test_negative_twin_a_builder_forwarding_something_else_fails_the_check():
    planted = '''
def _orchestrator_openai_payload(request, room=None, *, stream, channel, voice_device_id=None):
    payload = {"extra_body": {}}
    payload["extra_body"]["voice_device_id"] = request.user
    return payload
'''
    assert builder_voice_device_id_is_the_parameter(planted)


def test_the_route_handlers_feed_the_device_only_from_the_prompt_marker():
    assert handlers_feed_voice_device_id_only_from_the_marker(GATEWAY_MAIN.read_text()) == []


def test_negative_twin_a_handler_taking_the_device_from_the_request_fails_the_check():
    planted = '''
async def chat_completions(request, raw_request):
    marker_device_id, cleaned, _, _ = speaker_room.extract_device_marker(request.messages)
    room, voice_device_id = request.user, request.session_id
async def responses_api(request, raw_request):
    marker_device_id, cleaned, _, _ = speaker_room.extract_device_marker(request.messages)
    room, voice_device_id = await _speaker_room_for_request(raw_request, device_id=marker_device_id)
'''
    assert handlers_feed_voice_device_id_only_from_the_marker(planted)


def test_the_orchestrator_reads_the_follow_up_keys_only_for_a_service_key_caller():
    assert orchestrator_reads_follow_up_keys_only_when_service_authenticated(ORCH_MAIN.read_text()) == []


def test_negative_twin_an_unguarded_read_fails_the_check():
    planted = '''
async def chat_completions(request, service_authenticated):
    voice_device_id = (request.extra_body or {}).get("voice_device_id")
    if service_authenticated is True:
        supports_followup = (request.extra_body or {}).get("supports_followup")
'''
    problems = orchestrator_reads_follow_up_keys_only_when_service_authenticated(planted)
    assert problems == ["'voice_device_id' read outside `if service_authenticated is True`"]


def test_negative_twin_a_truthy_guard_instead_of_is_true_fails_the_check():
    planted = '''
async def chat_completions(request, service_authenticated):
    if service_authenticated:
        voice_device_id = (request.extra_body or {}).get("voice_device_id")
'''
    assert orchestrator_reads_follow_up_keys_only_when_service_authenticated(planted)


# Round trip ----------------------------------------------------------------

@pytest.fixture
def harness(monkeypatch):
    from . import _fast_path_harness as fp
    from . import _public_audience_harness as h

    rig = fp.install(monkeypatch, slow="record")
    seen = {"states": [], "queries": []}

    async def stream_run(state):
        seen["states"].append(state)
        state.intent = h.IntentCategory.GENERAL_INFO
        state.answer = "ok"
        return state

    class Graph:
        async def ainvoke(self, state):
            seen["states"].append(state)
            return {"intent": h.IntentCategory.GENERAL_INFO, "answer": "ok", "confidence": 1.0,
                    "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True}

    original = h.main.QueryRequest

    class Recording(original):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            seen["queries"].append(self)

    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", stream_run)
    monkeypatch.setattr(h.main, "orchestrator_graph", Graph())
    monkeypatch.setattr(h.main, "QueryRequest", Recording)
    return h, seen


def _gateway_body(*, stream, voice_device_id, trusted_voice=False):
    sys.modules.setdefault("prometheus_client", mock.MagicMock())
    import gateway.main as gw
    from shared.output_channel import OutputChannel

    request = gw.ChatCompletionRequest(
        model="m", stream=stream, messages=[gw.ChatMessage(role="user", content="turn on the lights")],
        session_id="followup-session-1",
    )
    return gw._orchestrator_openai_payload(
        request, "study", stream=stream, channel=OutputChannel.SPEECH, voice_device_id=voice_device_id,
        trusted_voice=trusted_voice,
    )


def _post(h, body, headers):
    async def go():
        transport = httpx.ASGITransport(app=h.main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post("/v1/chat/completions", json=body, headers=headers)

    return asyncio.run(go())


def _rewrite_session(body):
    body = json.loads(json.dumps(body))
    body["session_id"] = "explicit-followup-1"
    return body


def test_round_trip_stream_a_believed_device_is_a_follow_up_surface(harness):
    h, seen = harness
    response = _post(h, _rewrite_session(_gateway_body(stream=True, voice_device_id=DEVICE)), h.service_headers())
    assert response.status_code == 200, response.text
    state = seen["states"][-1]
    assert state.supports_followup is True and state.caller_fingerprint is not None


def test_round_trip_non_stream_the_device_reaches_the_query_request(harness):
    h, seen = harness
    response = _post(h, _rewrite_session(_gateway_body(stream=False, voice_device_id=DEVICE)), h.service_headers())
    assert response.status_code == 200, response.text
    query = seen["queries"][-1]
    assert query.voice_device_id == DEVICE and query.supports_followup is True


@pytest.mark.parametrize("stream", [True, False])
def test_without_the_service_key_the_device_is_ignored(harness, stream):
    h, seen = harness
    h.main._shared_config.get_config()  # warm
    body = _rewrite_session(_gateway_body(stream=stream, voice_device_id=DEVICE))
    with mock.patch.dict(os.environ, {"ORCHESTRATOR_INGRESS_AUTH": "warn"}):
        h.shared_config._clear_cache_for_tests()
        response = _post(h, body, {})
    h.shared_config._clear_cache_for_tests()
    assert response.status_code == 200, response.text
    if stream:
        state = seen["states"][-1]
        assert state.supports_followup is False and state.caller_fingerprint is None
    else:
        query = seen["queries"][-1]
        assert query.voice_device_id is None and query.supports_followup is False


@pytest.mark.parametrize("bad", ["A" * 32, "ab" * 8, "ab" * 17, 12345, None])
def test_a_malformed_device_id_is_ignored_even_with_the_key(harness, bad):
    h, seen = harness
    body = _rewrite_session(_gateway_body(stream=False, voice_device_id=DEVICE))
    body["extra_body"]["voice_device_id"] = bad
    response = _post(h, body, h.service_headers())
    assert response.status_code == 200, response.text
    query = seen["queries"][-1]
    assert query.voice_device_id is None and query.supports_followup is False


def test_the_follow_up_flag_without_a_device_is_ignored(harness):
    h, seen = harness
    body = _rewrite_session(_gateway_body(stream=False, voice_device_id=None))
    body["extra_body"]["supports_followup"] = True
    response = _post(h, body, h.service_headers())
    assert response.status_code == 200, response.text
    assert seen["queries"][-1].supports_followup is False


@pytest.mark.parametrize("stream", [True, False])
def test_the_trusted_voice_flag_reaches_the_turn_only_from_a_service_key_caller(harness, stream):
    h, seen = harness
    body = _rewrite_session(_gateway_body(stream=stream, voice_device_id=None, trusted_voice=True))
    assert body["extra_body"]["trusted_voice"] is True
    response = _post(h, body, h.service_headers())
    assert response.status_code == 200, response.text
    flagged = seen["states"][-1] if stream else seen["queries"][-1]
    assert flagged.trusted_voice is True

    with mock.patch.dict(os.environ, {"ORCHESTRATOR_INGRESS_AUTH": "warn"}):
        h.shared_config._clear_cache_for_tests()
        response = _post(h, _rewrite_session(body), {})
    h.shared_config._clear_cache_for_tests()
    assert response.status_code == 200, response.text
    unflagged = seen["states"][-1] if stream else seen["queries"][-1]
    assert unflagged.trusted_voice is False


def test_the_gateway_payload_carries_trusted_voice_only_when_set():
    assert "trusted_voice" not in _gateway_body(stream=False, voice_device_id=None)["extra_body"]
