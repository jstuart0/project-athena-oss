"""ATHENA-114: orchestrator.rag_client's fetch_service_urls_from_registry
sends X-Service-Key on its /api/internal/config/rag-services fetch.

Same bug class as config_loader.py (dick's S2 investigation): this client
built with no headers at all, so every registry fetch 422'd against
admin-backend's verify_service_api_key gate and silently fell back to the
hardcoded RAG_SERVICE_URL_MAP constants. Found by widening
test_orchestrator_callers_send_service_key.py's AST scan to src/orchestrator
while fixing config_loader.py; fixed here with the same pattern.

Mirrors tests/unit/test_orchestrator_config_loader_service_key.py's
MockTransport pattern.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("SERVICE_API_KEY", "test-key-rag-client")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import httpx  # noqa: E402

# See test_orchestrator_config_loader_service_key.py's identical comment:
# sibling test files replace sys.modules["orchestrator.config_loader"] with
# a MagicMock, which doesn't touch orchestrator.rag_client directly, but
# force a fresh import here too for the same defensive reason (some of
# those files also import orchestrator.main, which imports rag_client).
sys.modules.pop("orchestrator.rag_client", None)

from shared.config import _clear_cache_for_tests  # noqa: E402
import orchestrator.rag_client as rag_client  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    def __init__(self, json_body=None):
        self.requests: list[httpx.Request] = []
        self._json_body = json_body if json_body is not None else {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self._json_body)


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
async def test_fetch_service_urls_sends_service_key_on_the_wire(monkeypatch):
    monkeypatch.setattr(rag_client, "_SERVICE_API_KEY", "test-key-rag-client")
    transport = _RecordingTransport({"weather": "http://weather:8010"})
    _patch_async_client(monkeypatch, transport)

    result = await rag_client.fetch_service_urls_from_registry()

    assert result == {"weather": "http://weather:8010"}
    assert len(transport.requests) == 1
    req = transport.requests[0]
    assert "/api/internal/config/rag-services" in str(req.url)
    assert req.headers.get("X-Service-Key") == "test-key-rag-client"


@pytest.mark.asyncio
async def test_empty_service_api_key_sends_no_header_and_falls_back_gracefully(monkeypatch):
    """An empty SERVICE_API_KEY must not add a blank header or raise -- the
    request still goes out (422s server-side), and the function falls back
    to RAG_SERVICE_URL_MAP rather than crashing."""
    monkeypatch.setattr(rag_client, "_SERVICE_API_KEY", "")
    transport = _RecordingTransport()

    def _422_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        def _handler(request):
            transport.requests.append(request)
            return httpx.Response(422, json={"detail": "missing X-Service-Key"})
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(_handler))
    monkeypatch.setattr(httpx, "AsyncClient", _422_factory)

    result = await rag_client.fetch_service_urls_from_registry()

    assert len(transport.requests) == 1
    assert "X-Service-Key" not in transport.requests[0].headers
    assert result == rag_client.RAG_SERVICE_URL_MAP
