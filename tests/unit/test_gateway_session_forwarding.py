"""Red/green contract for ATHENA-88 phase 2 (F88): gateway identity
forwarding (user/session_id/room) and the per-IP new-conversation limiter.

Plan Phase 2 tests 18-22 (+19b). Test contract Phase 2 C18-C21, C25-C26.

In-process import of gateway.main, with prometheus_client stubbed (matches
the plan's own stubbing note) — module-level code in gateway/main.py only
reads config and builds the FastAPI app; no network I/O happens at import.
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, "src")

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-session-forwarding")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from gateway.conversation_limiter import NewConversationLimiter  # noqa: E402

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_MAIN_PY = REPO_ROOT / "src" / "gateway" / "main.py"


# ---------------------------------------------------------------------------
# 18. test_stream_payload_forwards_identity
# ---------------------------------------------------------------------------

class _CapturingStreamCtx:
    def __init__(self):
        pass

    async def __aenter__(self):
        resp = mock.MagicMock()
        resp.raise_for_status = mock.MagicMock()

        async def _aiter_lines():
            return
            yield  # pragma: no cover - makes this an async generator

        resp.aiter_lines = _aiter_lines
        return resp

    async def __aexit__(self, *exc_info):
        return False


class _FakeOrchestratorClient:
    def __init__(self):
        self.captured_json = None

    def stream(self, method, path, json=None, timeout=None):
        self.captured_json = json
        return _CapturingStreamCtx()


def test_stream_payload_forwards_identity(monkeypatch):
    fake_client = _FakeOrchestratorClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)
    monkeypatch.setattr(gw, "orchestrator_timeout", 5)

    request = gw.ChatCompletionRequest(
        model="m",
        messages=[gw.ChatMessage(role="user", content="hi")],
        stream=True,
        user="u1",
        session_id="explicit-a",
    )

    async def _run():
        async for _ in gw.stream_orchestrator_response(request, device_id="kitchen"):
            pass

    asyncio.run(_run())

    payload = fake_client.captured_json
    assert payload["user"] == "u1"
    assert payload["session_id"] == "explicit-a"
    assert payload["room"] == "kitchen"
    assert payload["extra_body"] == {"room": "kitchen"}


def test_stream_payload_omits_unset_identity(monkeypatch):
    fake_client = _FakeOrchestratorClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)
    monkeypatch.setattr(gw, "orchestrator_timeout", 5)

    request = gw.ChatCompletionRequest(
        model="m",
        messages=[gw.ChatMessage(role="user", content="hi")],
        stream=True,
    )

    async def _run():
        async for _ in gw.stream_orchestrator_response(request, device_id=None):
            pass

    asyncio.run(_run())

    payload = fake_client.captured_json
    assert "user" not in payload
    assert "session_id" not in payload
    assert "room" not in payload


# ---------------------------------------------------------------------------
# 19. test_responses_request_carries_identity
# 19b. test_chat_completion_request_user_field_preexists
# ---------------------------------------------------------------------------

def test_responses_request_carries_identity():
    request = gw.ResponsesAPIRequest(
        model="m",
        input=[{"role": "user", "content": "hi"}, {"role": "user", "content": "there"}],
        stream=True,
        user="u1",
        session_id="explicit-a",
    )
    chat_request = gw._responses_to_chat_request(request)

    assert isinstance(chat_request, gw.ChatCompletionRequest)
    assert chat_request.user == "u1"
    assert chat_request.session_id == "explicit-a"
    assert [m.content for m in chat_request.messages] == ["hi", "there"]


def test_chat_completion_request_user_field_preexists():
    request = gw.ChatCompletionRequest(
        model="m", messages=[gw.ChatMessage(role="user", content="hi")], user="u1"
    )
    assert request.user == "u1"


# ---------------------------------------------------------------------------
# 20. test_new_conversation_limiter
# ---------------------------------------------------------------------------

class _FakeClock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_new_conversation_limiter():
    clock = _FakeClock()
    limiter = NewConversationLimiter(per_minute=2, max_keys=2, clock=clock)

    assert limiter.allow("A") is True
    assert limiter.allow("A") is True
    assert limiter.allow("A") is False

    clock.advance(61)
    assert limiter.allow("A") is True

    assert limiter.allow("B") is True
    assert limiter.allow("C") is True
    assert len(limiter) <= 2


# ---------------------------------------------------------------------------
# 21. test_limiter_applies_to_both_routes_first_turn_only
# ---------------------------------------------------------------------------

def _find_function(tree: ast.Module, name: str):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _call_lines(node: ast.AST, name: str):
    lines = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            func = n.func
            if (isinstance(func, ast.Name) and func.id == name) or (
                isinstance(func, ast.Attribute) and func.attr == name
            ):
                lines.append(n.lineno)
    return lines


def test_limiter_applies_to_both_routes_first_turn_only():
    tree = ast.parse(GATEWAY_MAIN_PY.read_text())

    chat_completions = _find_function(tree, "chat_completions")
    responses_api = _find_function(tree, "responses_api")

    assert _call_lines(chat_completions, "_check_new_conversation_limit")
    assert _call_lines(responses_api, "_check_new_conversation_limit")

    chat_limit_line = _call_lines(chat_completions, "_check_new_conversation_limit")[0]
    chat_room_line = _call_lines(chat_completions, "_detect_room_from_active_satellite")[0]
    assert chat_limit_line < chat_room_line

    resp_limit_line = _call_lines(responses_api, "_check_new_conversation_limit")[0]
    resp_convert_line = _call_lines(responses_api, "_responses_to_chat_request")[0]
    resp_room_line = _call_lines(responses_api, "_detect_room_from_active_satellite")[0]
    assert resp_convert_line < resp_limit_line < resp_room_line


def test_check_new_conversation_limit_first_turn_only(monkeypatch):
    limiter = NewConversationLimiter(per_minute=1, max_keys=10)
    monkeypatch.setattr(gw, "new_conversation_limiter", limiter)

    one_user_msg = [gw.ChatMessage(role="user", content="hi")]
    two_user_msgs = [
        gw.ChatMessage(role="user", content="hi"),
        gw.ChatMessage(role="assistant", content="hello"),
        gw.ChatMessage(role="user", content="again"),
    ]

    async def _run():
        # First one-user-message call consumes budget and passes.
        await gw._check_new_conversation_limit("1.2.3.4", one_user_msg, None)
        # Second one-user-message call from the same host is over budget.
        with pytest.raises(HTTPException) as exc_info:
            await gw._check_new_conversation_limit("1.2.3.4", one_user_msg, None)
        assert exc_info.value.status_code == 429

        # A follow-up turn (2+ user messages) never consumes budget, even
        # though the limiter above is already exhausted for this host.
        await gw._check_new_conversation_limit("1.2.3.4", two_user_msgs, None)

        # An explicit session_id never consumes budget either.
        await gw._check_new_conversation_limit("1.2.3.4", one_user_msg, "explicit-a")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 22. test_route_level_429_on_both_routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/chat/completions", "/v1/responses"])
def test_route_level_429_on_both_routes(monkeypatch, path):
    limiter = NewConversationLimiter(per_minute=1, max_keys=10)
    # TestClient reports the client host as "testclient"; pre-consume it.
    assert limiter.allow("testclient") is True
    monkeypatch.setattr(gw, "new_conversation_limiter", limiter)

    room_sentinel = mock.AsyncMock(side_effect=HTTPException(status_code=418))
    monkeypatch.setattr(gw, "_detect_room_from_active_satellite", room_sentinel)
    monkeypatch.setattr(gw, "validate_api_key", mock.AsyncMock(return_value=True))

    client = TestClient(gw.app)

    if path == "/v1/chat/completions":
        first_turn_body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        follow_up_body = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "again"},
            ],
        }
    else:
        first_turn_body = {"model": "m", "input": [{"role": "user", "content": "hi"}]}
        follow_up_body = {
            "model": "m",
            "input": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
                {"role": "user", "content": "again"},
            ],
        }

    response = client.post(path, json=first_turn_body)
    assert response.status_code == 429
    room_sentinel.assert_not_awaited()

    response = client.post(path, json=follow_up_body)
    assert response.status_code == 418
    room_sentinel.assert_awaited()
