"""ATHENA-89 Phase 3 — gateway non-streaming F97 continuity (D1-B, D10 G11,
D11 G10, D12 G2b/G2c).

Covers plan/contract G1-G11. In-process import of gateway.main, with
prometheus_client stubbed -- module-level code in gateway/main.py only reads
config and builds the FastAPI app; no network I/O happens at import (same
harness as tests/unit/test_gateway_session_forwarding.py).
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest

sys.path.insert(0, "src")

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-gateway-nonstream-continuity")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

import gateway.main as gw  # noqa: E402
from shared.output_channel import OutputChannel  # noqa: E402
from gateway.conversation_limiter import NewConversationLimiter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
GATEWAY_MAIN_PY = REPO_ROOT / "src" / "gateway" / "main.py"


@pytest.fixture(autouse=True)
def _permissive_gateway(monkeypatch):
    """Keep the per-IP new-conversation limiter and global rate limiter out
    of the way -- these tests are about F97 payload forwarding, not rate
    limiting (already covered by test_gateway_session_forwarding.py)."""
    monkeypatch.setattr(gw, "new_conversation_limiter", NewConversationLimiter(per_minute=100000))
    monkeypatch.setattr(gw, "global_rate_limiter", None)
    yield


@pytest.fixture
def _fixed_room(monkeypatch):
    """Opt-in: fix the speaker room to "kitchen" for tests about payload
    forwarding, not room resolution (test_gateway_speaker_room.py)."""
    monkeypatch.setattr(
        gw.speaker_room, "resolve_speaker_room",
        mock.AsyncMock(return_value=gw.speaker_room.SpeakerRoom("kitchen", "map")),
    )


def _fake_orchestrator_response(content: str = "hi"):
    resp = mock.MagicMock()
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    return resp


class _CapturingClient:
    def __init__(self, response=None, exc: Exception | None = None):
        self.captured_path = None
        self.captured_json = None
        self._response = response or _fake_orchestrator_response()
        self._exc = exc

    async def post(self, path, json=None, **kwargs):
        self.captured_path = path
        self.captured_json = json
        if self._exc is not None:
            raise self._exc
        return self._response


# ---------------------------------------------------------------------------
# G1 / G2: full-payload forwarding to /v1/chat/completions and /v1/responses
# ---------------------------------------------------------------------------


def test_G1_chat_completions_nonstream_forwards_full_payload(monkeypatch, _fixed_room):
    fake_client = _CapturingClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)

    client = TestClient(gw.app)
    body = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
        "stream": False,
        "user": "ha-conv-1",
    }
    resp = client.post("/v1/chat/completions", json=body)

    assert resp.status_code == 200
    assert fake_client.captured_path == "/v1/chat/completions"
    assert fake_client.captured_json["stream"] is False
    assert len(fake_client.captured_json["messages"]) == 4
    assert fake_client.captured_json["user"] == "ha-conv-1"
    assert fake_client.captured_json["room"] == "kitchen"
    assert fake_client.captured_json["extra_body"] == {"room": "kitchen", "interface_type": "text"}


def test_G2_responses_api_nonstream_forwards_full_payload(monkeypatch, _fixed_room):
    fake_client = _CapturingClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)

    client = TestClient(gw.app)
    body = {
        "model": "m",
        "input": [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ],
        "stream": False,
        "user": "ha-conv-1",
    }
    resp = client.post("/v1/responses", json=body)

    assert resp.status_code == 200
    assert fake_client.captured_path == "/v1/chat/completions"
    assert fake_client.captured_json["stream"] is False
    assert len(fake_client.captured_json["messages"]) == 4
    assert fake_client.captured_json["user"] == "ha-conv-1"
    assert fake_client.captured_json["room"] == "kitchen"
    assert fake_client.captured_json["extra_body"] == {"room": "kitchen", "interface_type": "text"}


# ---------------------------------------------------------------------------
# G2b / G2c: Responses converter rules (D12)
# ---------------------------------------------------------------------------


def test_G2b_converter_accepts_input_text_skips_function_items():
    request = gw.ResponsesAPIRequest(
        model="m",
        input=[
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            {"type": "function_call_output", "output": "tool result", "call_id": "c1"},
        ],
    )
    chat_request = gw._responses_to_chat_request(request)
    user_messages = [m for m in chat_request.messages if m.role == "user"]
    assert len(user_messages) == 1
    assert user_messages[0].content == "hello"


def test_G2c_previous_response_id_400_never_calls_orchestrator(monkeypatch, _fixed_room):
    fake_client = _CapturingClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)

    client = TestClient(gw.app)
    resp = client.post("/v1/responses", json={
        "model": "m",
        "input": "hi",
        "previous_response_id": "resp_abc123",
    })

    assert resp.status_code == 400
    assert fake_client.captured_path is None  # orchestrator never called


# ---------------------------------------------------------------------------
# G3: AST scoping proof
# ---------------------------------------------------------------------------


def _calls_named(tree: ast.AST, func_def_name: str, called_name: str) -> list:
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_def_name:
            return [
                n for n in ast.walk(node)
                if isinstance(n, ast.Call) and getattr(n.func, "id", None) == called_name
            ]
    raise AssertionError(f"function {func_def_name} not found")


def test_G3_route_to_orchestrator_scoped_to_ha_conversation_only():
    source = GATEWAY_MAIN_PY.read_text()
    tree = ast.parse(source)

    assert _calls_named(tree, "chat_completions", "route_to_orchestrator") == []
    assert _calls_named(tree, "responses_api", "route_to_orchestrator") == []
    assert len(_calls_named(tree, "ha_conversation", "route_to_orchestrator")) == 1

    assert next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_orchestrator_openai_payload"
    )  # raises StopIteration (failing the test) if the function is missing
    assert "async def stream_orchestrator_response" in source
    assert source.index("async def stream_orchestrator_response") < source.index(
        "_orchestrator_openai_payload(\n            request, room, stream=True, channel=channel, voice_device_id=voice_device_id,\n            trusted_voice=trusted_voice,\n        )"
    )


# ---------------------------------------------------------------------------
# G4 / G7 / G8 / G5 / G9: route_chat_completion_to_orchestrator failure semantics
# ---------------------------------------------------------------------------


def _request(content: str = "hi") -> "gw.ChatCompletionRequest":
    return gw.ChatCompletionRequest(model="m", messages=[gw.ChatMessage(role="user", content=content)])


def test_G4_breaker_open_falls_back_to_ollama_never_posts(monkeypatch):
    breaker = mock.MagicMock()
    breaker.can_execute = mock.AsyncMock(return_value=False)
    breaker.state = mock.MagicMock(value="open")
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})

    fake_client = _CapturingClient()
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)
    ollama_mock = mock.AsyncMock(return_value="ollama-response")
    monkeypatch.setattr(gw, "route_to_ollama", ollama_mock)

    result = asyncio.run(gw.route_chat_completion_to_orchestrator(_request(), room="kitchen", channel=OutputChannel.TEXT))

    assert result == "ollama-response"
    ollama_mock.assert_awaited_once_with(mock.ANY, channel=OutputChannel.TEXT)
    assert fake_client.captured_path is None


def test_G5_G9_maps_content_and_word_count_usage(monkeypatch):
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    fake_client = _CapturingClient(response=_fake_orchestrator_response("hi there friend"))
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)

    result = asyncio.run(gw.route_chat_completion_to_orchestrator(_request("hello world"), room="kitchen", channel=OutputChannel.TEXT))

    assert result.object == "chat.completion"
    assert result.id.startswith("chatcmpl-")
    assert result.choices[0].message.content == "hi there friend"
    assert result.usage["prompt_tokens"] == len("hello world".split())
    assert result.usage["completion_tokens"] == len("hi there friend".split())


def _run_status_error(monkeypatch, status):
    breaker = mock.MagicMock()
    breaker.can_execute = mock.AsyncMock(return_value=True)
    breaker.record_failure = mock.AsyncMock()
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})

    fake_client = _CapturingClient(
        exc=httpx.HTTPStatusError("bad", request=mock.MagicMock(), response=mock.MagicMock(status_code=status))
    )
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)
    ollama_mock = mock.AsyncMock()
    monkeypatch.setattr(gw, "route_to_ollama", ollama_mock)

    result = asyncio.run(gw.route_chat_completion_to_orchestrator(_request(), room="kitchen", channel=OutputChannel.TEXT))
    ollama_mock.assert_not_awaited()
    return result, breaker


def test_G7_deterministic_500_is_spoken_and_never_opens_the_breaker(monkeypatch):
    result, breaker = _run_status_error(monkeypatch, 500)

    assert result.choices[0].message.content == gw.SPOKEN_FAILURE_TEXT
    breaker.record_failure.assert_not_awaited()


def test_G7_503_is_spoken_and_records_failure(monkeypatch):
    result, breaker = _run_status_error(monkeypatch, 503)

    assert result.choices[0].message.content == gw.SPOKEN_FAILURE_TEXT
    breaker.record_failure.assert_awaited_once()


def test_G8_generic_exception_falls_back_to_ollama_records_failure(monkeypatch):
    breaker = mock.MagicMock()
    breaker.can_execute = mock.AsyncMock(return_value=True)
    breaker.record_failure = mock.AsyncMock()
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})

    fake_client = _CapturingClient(exc=httpx.ConnectError("down"))
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)
    ollama_mock = mock.AsyncMock(return_value="ollama-response")
    monkeypatch.setattr(gw, "route_to_ollama", ollama_mock)

    result = asyncio.run(gw.route_chat_completion_to_orchestrator(_request(), room="kitchen", channel=OutputChannel.TEXT))

    assert result == "ollama-response"
    ollama_mock.assert_awaited_once_with(mock.ANY, channel=OutputChannel.TEXT)
    breaker.record_failure.assert_awaited_once()


# ---------------------------------------------------------------------------
# G6: trusted-proxy-unset warning
# ---------------------------------------------------------------------------


def test_G6_warn_if_trusted_proxy_unset_logs_once(monkeypatch):
    # Patch gw.logger.warning directly rather than structlog.testing.
    # capture_logs(): this repo's configure_logging() rebinds a process-wide
    # "service" context per call, so once another service module has been
    # imported in the same test session, capture_logs()'s scoping becomes
    # unreliable (observed: reliable in isolation, flaky in a full-suite run).
    calls = []
    monkeypatch.setattr(gw.logger, "warning", lambda event, **kw: calls.append({"event": event, **kw}))
    assert gw._warn_if_trusted_proxy_unset("") is True
    assert len([c for c in calls if c["event"] == "trusted_proxy_cidrs_unset"]) == 1


def test_G6_no_warning_when_set(monkeypatch):
    calls = []
    monkeypatch.setattr(gw.logger, "warning", lambda event, **kw: calls.append({"event": event, **kw}))
    assert gw._warn_if_trusted_proxy_unset("10.244.0.0/16") is False
    assert not [c for c in calls if c["event"] == "trusted_proxy_cidrs_unset"]


# ---------------------------------------------------------------------------
# G11 (D10): orchestrator_client is constructed with X-Service-Key
# ---------------------------------------------------------------------------


def test_G11_orchestrator_client_constructed_with_service_key_header():
    source = GATEWAY_MAIN_PY.read_text()
    idx = source.index("orchestrator_client = httpx.AsyncClient(")
    snippet = source[idx: idx + 300]
    # Through the helper, so an unset or unusable key installs no default
    # header (tests/unit/test_service_key_headers.py pins the behaviour).
    assert "headers=service_key_headers()," in snippet


# ---------------------------------------------------------------------------
# DC12 (P3b, xander): gateway's ad-hoc warmup client sends X-Service-Key
# ---------------------------------------------------------------------------


def test_DC12_warmup_session_sends_service_key_header(monkeypatch):
    session_mgr = mock.MagicMock()
    session_mgr.get_session_for_device = mock.AsyncMock(return_value="sess-warm-1")
    monkeypatch.setattr(gw, "device_session_mgr", session_mgr)

    captured = {}

    class _FakeAsyncClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, headers=None, **kwargs):
            captured["url"] = url
            captured["headers"] = headers
            return mock.MagicMock()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)

    asyncio.run(gw._warmup_session("device-1"))

    # The key as configured when the call is made, not the constant the
    # module captured when it was imported.
    from shared.config import get_config

    assert captured["headers"] == {"X-Service-Key": get_config().service_api_key}
    assert captured["headers"]["X-Service-Key"]
    assert captured["url"].endswith("/session/sess-warm-1/warmup")


# ---------------------------------------------------------------------------
# ATHENA-115: /ha/conversation used to raise NameError("HAResponseContent")
# AFTER a device command had already executed (hank reproduced it live: a
# light turns on, then the response 500s). HAResponseContent/HASpeechContent/
# HAPlainSpeech were never defined anywhere in this codebase -- HAConversation
# Response.response is a plain Dict[str, Any] (see HAConversationResponse's
# own field type). Fixed by building that dict directly via the shared
# _ha_response_payload helper, mirroring the shape the orchestrator-routed
# success path already built correctly.
# ---------------------------------------------------------------------------


def _fake_session_mgr(session_id="sess-ha-1"):
    session_mgr = mock.MagicMock()
    session_mgr.get_session_for_device = mock.AsyncMock(return_value=session_id)
    session_mgr.update_session_for_device = mock.AsyncMock()
    return session_mgr


def test_ATHENA_115_fastpath_command_executes_and_returns_200_with_ha_response_shape(monkeypatch, _fixed_room):
    """With ha_simple_command_fastpath on, a locally answered command (time,
    greeting) returns 200 in HA's expected response shape -- not a 500 from a
    NameError raised after the command already ran."""
    async def _flags(flag_name, default=False):
        return {"ha_simple_command_fastpath": True}.get(flag_name, False)

    monkeypatch.setattr(gw, "get_feature_flag", _flags)
    monkeypatch.setattr(gw, "device_session_mgr", _fake_session_mgr())
    monkeypatch.setattr(gw, "detect_simple_command", mock.AsyncMock(return_value=("greeting", {"type": "greeting_hello"})))
    monkeypatch.setattr(gw, "execute_simple_command", mock.AsyncMock(return_value="Hello! How can I help you?"))

    client = TestClient(gw.app)
    resp = client.post("/ha/conversation", json={
        "text": "hello",
        "device_id": "kitchen",
        "language": "en",
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["response"]["response_type"] == "action_done"
    assert body["response"]["speech"]["plain"]["speech"] == "Hello! How can I help you?"
    assert body["response"]["data"] == {"success": True, "targets": []}
    assert body["response"]["language"] == "en"
    assert body["continue_conversation"] is False


def test_ATHENA_115_orchestrator_routed_reply_returns_200_with_ha_response_shape(monkeypatch, _fixed_room):
    """End-to-end with feature-flagged fast paths off: a mocked orchestrator
    reply must still produce a 200 in HA's expected response shape (the
    already-working path this refactor must not regress)."""
    async def _flags(flag_name, default=False):
        return False

    monkeypatch.setattr(gw, "get_feature_flag", _flags)
    monkeypatch.setattr(gw, "device_session_mgr", _fake_session_mgr())

    fake_client = _CapturingClient(_fake_orchestrator_json_response("Turning on the light.", "sess-orch-1"))
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)

    client = TestClient(gw.app)
    resp = client.post("/ha/conversation", json={
        "text": "turn on the light",
        "device_id": "kitchen",
        "language": "en",
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["response"]["response_type"] == "action_done"
    assert body["response"]["speech"]["plain"]["speech"] == "Turning on the light."
    assert body["conversation_id"] == "sess-orch-1"


def _fake_orchestrator_json_response(answer: str, session_id: str):
    resp = mock.MagicMock()
    resp.raise_for_status = mock.MagicMock()
    resp.json.return_value = {"answer": answer, "session_id": session_id}
    return resp
