"""ATHENA-114: orchestrator.config_loader's ConversationConfig sends
X-Service-Key on every /api/internal/config/* and /api/internal/analytics/log
request.

dick's investigation (S2) found this client built with no headers at all,
so every conversation/clarification/disambiguation config fetch 422d against
admin-backend's verify_service_api_key gate and silently fell back to
hardcoded defaults on each cache-refresh cycle -- 5x/hr in production logs.

Mirrors tests/unit/test_orchestrator_gateway_keepalive_service_key.py's
MockTransport pattern (recording transport swapped in for httpx.AsyncClient),
adapted to config_loader.py's lighter import surface (no langgraph/
prometheus_client stubbing needed -- this module has none of those
dependencies).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("SERVICE_API_KEY", "test-key-config-loader")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
os.environ.setdefault("REDIS_ENABLED", "false")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import httpx  # noqa: E402

# Other test files in this suite (test_health_probes.py,
# test_orchestrator_gateway_keepalive_service_key.py,
# test_transit_tool_wiring.py) deliberately replace
# sys.modules["orchestrator.config_loader"] with a MagicMock at import time,
# to keep orchestrator.main importable without its heavy runtime deps. If
# any of those files collect before this one (alphabetically,
# test_health_probes.py does), a plain `import orchestrator.config_loader`
# here would silently bind to their mock instead of the real module this
# file exists to test. Force a fresh import of the genuine module.
sys.modules.pop("orchestrator.config_loader", None)

from shared.config import _clear_cache_for_tests  # noqa: E402
import orchestrator.config_loader as config_loader  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={"enabled": True})


def _patch_async_client(monkeypatch, transport: _RecordingTransport) -> None:
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _reset_config():
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


@pytest.fixture
def fresh_config():
    """A ConversationConfig instance independent of the module-level
    singleton, so tests don't leak state via config_loader._config."""
    return config_loader.ConversationConfig()


@pytest.mark.asyncio
async def test_initialize_builds_client_with_service_key_header(monkeypatch, fresh_config):
    monkeypatch.setattr(config_loader, "_SERVICE_API_KEY", "test-key-config-loader")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    await fresh_config.initialize()

    assert fresh_config.http_client.headers.get("X-Service-Key") == "test-key-config-loader"


@pytest.mark.asyncio
async def test_get_conversation_settings_sends_service_key_on_the_wire(monkeypatch, fresh_config):
    monkeypatch.setattr(config_loader, "_SERVICE_API_KEY", "test-key-config-loader")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    await fresh_config.initialize()
    await fresh_config.get_conversation_settings()

    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert "/api/internal/config/conversation" in str(req.url)
    assert req.headers.get("X-Service-Key") == "test-key-config-loader"


@pytest.mark.asyncio
async def test_log_analytics_event_sends_service_key_on_the_wire(monkeypatch, fresh_config):
    monkeypatch.setattr(config_loader, "_SERVICE_API_KEY", "test-key-config-loader")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    await fresh_config.initialize()
    await fresh_config.log_analytics_event("session-1", "query_intent", {})

    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert "/api/internal/analytics/log" in str(req.url)
    assert req.headers.get("X-Service-Key") == "test-key-config-loader"


@pytest.mark.asyncio
async def test_empty_service_api_key_sends_no_header_and_does_not_crash(monkeypatch, fresh_config):
    """An empty SERVICE_API_KEY must not add a blank header or raise --
    the request still goes out (and 422s server-side, per the pre-existing
    _fetch_from_api try/except), and config_loader falls back to defaults."""
    monkeypatch.setattr(config_loader, "_SERVICE_API_KEY", "")
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    await fresh_config.initialize()

    assert "X-Service-Key" not in fresh_config.http_client.headers
