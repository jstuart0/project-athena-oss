"""ATHENA-89 Phase 3b — DC9: RAGClient surfaces a non-200 body's detail/error
(capped at 200 chars) in the returned RAGResponse.error, instead of a bare
status code the LLM can't act on.

Two levels: a fake HTTP transport for the capping/truncation behavior in
isolation, and a real round trip (RAGClient -> asyncio.wait_for -> a fake
pooled client backed by httpx.ASGITransport -> the actual amtrak FastAPI app)
with DEFAULT_AMTRAK_STATION unset, proving the orchestrator's tool-execution
path would see the amtrak app's real 400 detail text end to end. Only the
HTTP transport is mocked (via ASGITransport / a fake http_pool); RAGClient,
the amtrak app, and its config resolution are all real.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import httpx
import pytest

sys.path.insert(0, "src")

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

from orchestrator.rag_client import RAGClient  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"


def _import_amtrak_app(monkeypatch, **env):
    """Load src/rag/amtrak/main.py under a private module name (it's
    literally named main.py, same collision every RAG service test hits)."""
    monkeypatch.delenv("DEFAULT_AMTRAK_STATION", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _clear_cache_for_tests()

    path = _SRC / "rag" / "amtrak" / "main.py"
    unique_name = "_test_dc9_amtrak_main"
    if unique_name in sys.modules:
        module = sys.modules[unique_name]
        module.__spec__.loader.exec_module(module)
        return module
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeHttpPool:
    """Stands in for orchestrator.http_pool.get_http_pool() -- returns a
    single pre-built httpx.AsyncClient regardless of the pool name asked
    for, so RAGClient.request()'s `await self.http_pool.get_client("rag")`
    resolves to whatever transport the test wired up."""

    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def get_client(self, _pool_name: str) -> httpx.AsyncClient:
        return self._client


def _client_with_fake_transport(status_code: int, json_body):
    """A minimal fake pooled client for the capping/truncation sub-cases
    that don't need a real ASGI app -- only the response shape matters."""

    class _FakeResponse:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            if json_body is _NO_BODY:
                raise ValueError("no body")
            return json_body

    class _FakePooledClient:
        async def request(self, method, url, params=None, json=None, headers=None):
            return _FakeResponse()

    return _FakePooledClient()


_NO_BODY = object()


@pytest.fixture(autouse=True)
def _reset_config_cache():
    yield
    _clear_cache_for_tests()


# ---------------------------------------------------------------------------
# DC9a/b: capping and truncation in isolation (fake transport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_DC9a_400_with_detail_appends_capped_detail_to_error():
    rag_client = RAGClient(service_urls={"amtrak": "http://amtrak-svc"})
    rag_client._http_pool = _FakeHttpPool(
        _client_with_fake_transport(400, {"detail": "DEFAULT_AMTRAK_STATION is not configured"})
    )

    resp = await rag_client.request(
        "amtrak", "GET", "/amtrak/schedule",
        skip_circuit_breaker=True, skip_rate_limit=True,
    )

    assert resp.success is False
    assert resp.status_code == 400
    assert resp.error == "Service returned status 400: DEFAULT_AMTRAK_STATION is not configured"


@pytest.mark.asyncio
async def test_DC9b_400_without_detail_unchanged_prefix_only():
    rag_client = RAGClient(service_urls={"amtrak": "http://amtrak-svc"})
    rag_client._http_pool = _FakeHttpPool(_client_with_fake_transport(400, _NO_BODY))

    resp = await rag_client.request(
        "amtrak", "GET", "/amtrak/schedule",
        skip_circuit_breaker=True, skip_rate_limit=True,
    )

    assert resp.success is False
    assert resp.error == "Service returned status 400"


@pytest.mark.asyncio
async def test_DC9c_error_detail_capped_at_200_chars():
    long_detail = "x" * 500
    rag_client = RAGClient(service_urls={"amtrak": "http://amtrak-svc"})
    rag_client._http_pool = _FakeHttpPool(_client_with_fake_transport(400, {"detail": long_detail}))

    resp = await rag_client.request(
        "amtrak", "GET", "/amtrak/schedule",
        skip_circuit_breaker=True, skip_rate_limit=True,
    )

    assert resp.error == f"Service returned status 400: {'x' * 200}"


# ---------------------------------------------------------------------------
# DC9d: real round trip -- RAGClient -> ASGITransport -> the actual amtrak
# FastAPI app, with DEFAULT_AMTRAK_STATION unset (mocks only the transport)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_DC9d_amtrak_round_trip_surfaces_real_400_detail(monkeypatch):
    amtrak_module = _import_amtrak_app(monkeypatch)

    asgi_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=amtrak_module.app),
        base_url="http://amtrak-svc",
    )
    try:
        rag_client = RAGClient(service_urls={"amtrak": "http://amtrak-svc"})
        rag_client._http_pool = _FakeHttpPool(asgi_client)

        resp = await rag_client.request(
            "amtrak", "GET", "/amtrak/schedule", params={"destination": "NYP"},
            skip_circuit_breaker=True, skip_rate_limit=True,
        )

        assert resp.success is False
        assert resp.status_code == 400
        assert "DEFAULT_AMTRAK_STATION" in resp.error
    finally:
        await asgi_client.aclose()
