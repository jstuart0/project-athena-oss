"""ATHENA-108: shared.service_registry.register_service() must send
X-Service-Key -- POST /api/service-registry/services is gated by
verify_service_or_oidc (dual-auth: X-Service-Key OR OIDC bearer), and a
bare process registering itself has no OIDC session. Without the header
every RAG/service startup registration 401s.

Runtime proof, not a static text scan: real httpx request-building
(headers, URL, query params), faked only at the socket via
httpx.MockTransport -- same technique as
tests/unit/test_base_knowledge_public_caller_headers.py. The AST scan
(tests/unit/test_orchestrator_callers_send_service_key.py) proves the
literal is present in the call's own source text; this proves the real
outgoing request actually carries it as a header.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

os.environ.setdefault("SERVICE_API_KEY", "test-service-key-athena-108")
os.environ.setdefault("ADMIN_API_URL", "http://admin-backend:8080")

from shared.config import _clear_cache_for_tests  # noqa: E402
import shared.service_registry as service_registry_module  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    def __init__(self, status_code: int = 201, response_json=None):
        self.requests: list[httpx.Request] = []
        self._status_code = status_code
        self._response_json = {} if response_json is None else response_json

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self._status_code, json=self._response_json)


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


@pytest.mark.asyncio
async def test_register_service_sends_service_key_header(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=201)
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.register_service("weather", 8010, "Weather RAG")

    assert ok is True
    assert len(transport.requests) == 1
    assert transport.requests[0].headers.get("X-Service-Key") == "test-service-key-athena-108"


@pytest.mark.asyncio
async def test_register_service_warns_and_sends_empty_header_when_key_unset(monkeypatch, caplog):
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=401, response_json={"detail": "unauthorized"})
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.register_service("weather", 8010, "Weather RAG")

    assert ok is False
    assert len(transport.requests) == 1
    # No key configured: the request still goes out (so the 401 is visible
    # in logs/monitoring for whoever is watching that RAG's startup), but
    # it must not silently claim success or fabricate a key.
    assert transport.requests[0].headers.get("X-Service-Key", "") == ""


# ---------------------------------------------------------------------------
# xander diff-review Medium (2026-09-28, batch review): unregister_service's
# POST .../toggle was missed by the ATHENA-108 fix above -- also gated by
# verify_service_or_oidc, also 401s with no X-Service-Key, leaving the row
# enabled=True after shutdown.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unregister_service_sends_service_key_header(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=200)
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.unregister_service("weather")

    assert ok is True
    assert len(transport.requests) == 1
    assert transport.requests[0].url.path == "/api/service-registry/services/weather/toggle"
    assert transport.requests[0].headers.get("X-Service-Key") == "test-service-key-athena-108"


@pytest.mark.asyncio
async def test_unregister_service_warns_and_sends_empty_header_when_key_unset(monkeypatch):
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=401, response_json={"detail": "unauthorized"})
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.unregister_service("weather")

    assert ok is False
    assert len(transport.requests) == 1
    assert transport.requests[0].headers.get("X-Service-Key", "") == ""
