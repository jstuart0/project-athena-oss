"""ATHENA-110: admin-backend's Control Agent callers send X-Service-Key.

The Control Agent's mutating routes (`/process/*`, `/docker/*`,
`/ollama/start|stop|restart`, `/huggingface/download|import-to-ollama|
downloaded` DELETE) are now gated by `require_service_caller`
(src/control_agent/auth.py). Every admin-backend function that talks to
the Control Agent must send `X-Service-Key`, or those calls 401/503 in
production the moment the Control Agent's own SERVICE_API_KEY is set.

Runtime proof, not a static text scan: real httpx request-building
(headers, URL, JSON body), faked only at the socket via
httpx.MockTransport -- same technique as
tests/unit/test_base_knowledge_public_caller_headers.py in the repo root.
"""
from __future__ import annotations

import httpx
import pytest

from shared.config import _clear_cache_for_tests, get_config

from app.routes import service_control, model_downloads

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    def __init__(self, response_json=None, status_code: int = 200):
        self.requests: list[httpx.Request] = []
        self._response_json = {} if response_json is None else response_json
        self._status_code = status_code

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status_code, json=self._response_json)


def _patch_async_client(monkeypatch, transport: _RecordingTransport) -> None:
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


class _FakeUser:
    def has_permission(self, _perm: str) -> bool:
        return True


@pytest.fixture(autouse=True)
def _control_agent_enabled(monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-for-hardening-tests")
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.mark.asyncio
async def test_docker_service_action_sends_service_key(monkeypatch):
    transport = _RecordingTransport({"success": True, "message": "ok"})
    _patch_async_client(monkeypatch, transport)

    success, message = await service_control.docker_service_action("athena-example", "start")

    assert success is True, message
    assert len(transport.requests) == 1
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_process_service_action_sends_service_key(monkeypatch):
    transport = _RecordingTransport({"success": True, "message": "ok"})
    _patch_async_client(monkeypatch, transport)

    success, message = await service_control.process_service_action(8000, "start")

    assert success is True, message
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_launchd_ollama_action_sends_service_key(monkeypatch):
    transport = _RecordingTransport({"success": True, "message": "ok"})
    _patch_async_client(monkeypatch, transport)

    success, message = await service_control.launchd_service_action("ollama", "restart")

    assert success is True, message
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_get_containers_status_sends_service_key(monkeypatch):
    """Read-only /docker/list isn't gated, but the client is built with the
    header regardless -- consistent with every other Control Agent client
    in this file, and harmless on a route that ignores it."""
    transport = _RecordingTransport([])
    _patch_async_client(monkeypatch, transport)

    result = await service_control.get_containers_status(current_user=_FakeUser())

    assert result == []
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_call_control_agent_sends_service_key_for_mutating_hf_download(monkeypatch):
    transport = _RecordingTransport({
        "job_id": "job-1", "status": "pending", "progress_percent": 0,
        "downloaded_bytes": 0, "total_bytes": 0, "error": None,
    })
    _patch_async_client(monkeypatch, transport)

    success, result = await model_downloads.call_control_agent(
        "POST", "/huggingface/download",
        json_data={"repo_id": "org/model", "filename": "m.gguf"},
    )

    assert success is True, result
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_call_control_agent_sends_service_key_for_mutating_hf_import(monkeypatch):
    transport = _RecordingTransport({"success": True, "message": "imported"})
    _patch_async_client(monkeypatch, transport)

    success, result = await model_downloads.call_control_agent(
        "POST", "/huggingface/import-to-ollama",
        json_data={"gguf_path": "/tmp/x.gguf", "model_name": "custom"},
    )

    assert success is True, result
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_call_control_agent_sends_service_key_for_mutating_hf_delete(monkeypatch):
    transport = _RecordingTransport({"success": True, "message": "deleted"})
    _patch_async_client(monkeypatch, transport)

    success, result = await model_downloads.call_control_agent(
        "DELETE", "/huggingface/downloaded", params={"file_path": "/tmp/x.gguf"},
    )

    assert success is True, result
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_call_control_agent_sends_service_key_for_readonly_hf_route(monkeypatch):
    """call_control_agent funnels every HF call -- read and mutating -- through
    one httpx.AsyncClient construction, so the read-only /huggingface/downloaded
    (GET) carries the header too, even though its own CA route isn't gated."""
    transport = _RecordingTransport([])
    _patch_async_client(monkeypatch, transport)

    success, result = await model_downloads.call_control_agent("GET", "/huggingface/downloaded")

    assert success is True, result
    assert transport.requests[0].headers.get("X-Service-Key") == get_config().service_api_key


@pytest.mark.asyncio
async def test_call_control_agent_omits_header_when_key_unset(monkeypatch):
    """control_agent_headers() returns {} (not a literal-empty-string
    header) when SERVICE_API_KEY is unset -- confirms the helper's
    documented empty-key behaviour end to end."""
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    _clear_cache_for_tests()
    transport = _RecordingTransport([])
    _patch_async_client(monkeypatch, transport)

    success, _result = await model_downloads.call_control_agent("GET", "/huggingface/downloaded")

    assert success is True
    assert "X-Service-Key" not in transport.requests[0].headers
