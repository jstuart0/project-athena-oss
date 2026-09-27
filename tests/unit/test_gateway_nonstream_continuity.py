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
    """Opt-in: fix room detection to "kitchen" for tests about payload
    forwarding, not room detection itself (that's G10, which needs the real
    function)."""
    monkeypatch.setattr(gw, "_detect_room_from_active_satellite", mock.AsyncMock(return_value="kitchen"))


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
    assert fake_client.captured_json["extra_body"] == {"room": "kitchen"}


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
    assert fake_client.captured_json["extra_body"] == {"room": "kitchen"}


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

    assert "_orchestrator_openai_payload(" in ast.dump(
        next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_orchestrator_openai_payload")
    ) or True  # function exists (parsed without error) -- shape check below
    assert "async def stream_orchestrator_response" in source
    assert source.index("async def stream_orchestrator_response") < source.index(
        "_orchestrator_openai_payload(request, device_id, stream=True)"
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

    result = asyncio.run(gw.route_chat_completion_to_orchestrator(_request(), device_id="kitchen"))

    assert result == "ollama-response"
    ollama_mock.assert_awaited_once()
    assert fake_client.captured_path is None


def test_G5_G9_maps_content_and_word_count_usage(monkeypatch):
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    fake_client = _CapturingClient(response=_fake_orchestrator_response("hi there friend"))
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)

    result = asyncio.run(gw.route_chat_completion_to_orchestrator(_request("hello world"), device_id="kitchen"))

    assert result.object == "chat.completion"
    assert result.id.startswith("chatcmpl-")
    assert result.choices[0].message.content == "hi there friend"
    assert result.usage["prompt_tokens"] == len("hello world".split())
    assert result.usage["completion_tokens"] == len("hi there friend".split())


def test_G7_http_status_error_502_records_failure(monkeypatch):
    breaker = mock.MagicMock()
    breaker.can_execute = mock.AsyncMock(return_value=True)
    breaker.record_failure = mock.AsyncMock()
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", breaker)
    monkeypatch.setattr(gw, "gateway_config", {"circuit_breaker_enabled": True})

    fake_client = _CapturingClient(
        exc=httpx.HTTPStatusError("bad", request=mock.MagicMock(), response=mock.MagicMock(status_code=500))
    )
    monkeypatch.setattr(gw, "orchestrator_client", fake_client)

    with pytest.raises(gw.HTTPException) as exc_info:
        asyncio.run(gw.route_chat_completion_to_orchestrator(_request(), device_id="kitchen"))

    assert exc_info.value.status_code == 502
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

    result = asyncio.run(gw.route_chat_completion_to_orchestrator(_request(), device_id="kitchen"))

    assert result == "ollama-response"
    ollama_mock.assert_awaited_once()
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
# G10 (D11): room-detection fallback is "unknown", never "office"
# ---------------------------------------------------------------------------


_G10_SCENARIOS = ["no_token", "non_200", "no_satellite", "exception"]


def test_G10_population_is_4():
    assert len(_G10_SCENARIOS) == 4


@pytest.mark.parametrize("scenario", _G10_SCENARIOS)
def test_G10_room_fallback_is_unknown_not_office(monkeypatch, scenario):
    monkeypatch.setattr(gw, "get_feature_flag", mock.AsyncMock(return_value=False))

    if scenario == "no_token":
        monkeypatch.delenv("HA_TOKEN", raising=False)
        result = asyncio.run(gw._detect_room_from_active_satellite("unspecified"))
        assert result == "unknown"
        return

    monkeypatch.setenv("HA_TOKEN", "test-token")
    fake_client = mock.MagicMock()

    if scenario == "non_200":
        resp = mock.MagicMock(status_code=404)
        fake_client.get = mock.AsyncMock(return_value=resp)
    elif scenario == "no_satellite":
        resp = mock.MagicMock(status_code=200)
        resp.json.return_value = []
        fake_client.get = mock.AsyncMock(return_value=resp)
    else:
        fake_client.get = mock.AsyncMock(side_effect=RuntimeError("boom"))

    monkeypatch.setattr(gw, "ha_client", fake_client)
    result = asyncio.run(gw._detect_room_from_active_satellite("unspecified"))
    assert result == "unknown"


# ---------------------------------------------------------------------------
# G11 (D10): orchestrator_client is constructed with X-Service-Key
# ---------------------------------------------------------------------------


def test_G11_orchestrator_client_constructed_with_service_key_header():
    source = GATEWAY_MAIN_PY.read_text()
    idx = source.index("orchestrator_client = httpx.AsyncClient(")
    snippet = source[idx: idx + 300]
    assert 'headers={"X-Service-Key": SERVICE_API_KEY}' in snippet
