"""ATHENA-110: orchestrator's own Control Agent caller (`ensure_gateway_running`
in src/orchestrator/main.py) sends X-Service-Key on both the read-only
status GET and the mutating `/process/start` POST.

Prior state: this call site built its httpx.AsyncClient with no headers at
all -- `/process/start/{GATEWAY_PORT}` is now gated by the Control Agent's
`require_service_caller` dependency (ATHENA-110), so an unheadered call
would 401 in production the moment SERVICE_API_KEY is set on the Control
Agent side, silently breaking the orchestrator's gateway keepalive.

Heavy-dependency mocking mirrors tests/unit/test_health_probes.py's
established pattern for importing orchestrator.main without langgraph/
prometheus_client installed.
"""
from __future__ import annotations

import os
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

os.environ.setdefault("SERVICE_API_KEY", "test-key-ca-caller")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from shared.config import get_config as _sync_get_config, _clear_cache_for_tests  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _sync_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules["orchestrator.config_loader"] = _config_loader_mock

import orchestrator.main as _main_module  # noqa: E402

import httpx  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "/process/status/" in str(request.url):
            return httpx.Response(200, json={"running": False, "pid": None})
        if "/process/start/" in str(request.url):
            return httpx.Response(200, json={"success": True, "message": "started"})
        return httpx.Response(200, json={})


def _patch_async_client(monkeypatch, transport: _RecordingTransport) -> None:
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _reset_config(monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    monkeypatch.setenv("START_GATEWAY", "true")
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.mark.asyncio
async def test_ensure_gateway_running_sends_service_key_on_status_and_start(monkeypatch):
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    result = await _main_module.ensure_gateway_running()

    assert result is True
    assert len(transport.requests) == 2
    expected_key = _main_module._SERVICE_API_KEY
    assert expected_key, "test fixture must produce a non-empty _SERVICE_API_KEY"
    for req in transport.requests:
        assert req.headers.get("X-Service-Key") == expected_key, req.url


@pytest.mark.asyncio
async def test_ensure_gateway_running_skips_entirely_when_control_agent_disabled(monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "false")
    _clear_cache_for_tests()
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    result = await _main_module.ensure_gateway_running()

    assert result is True
    assert transport.requests == []
