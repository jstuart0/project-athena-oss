"""ATHENA-91 P3b (C1-C3): runtime proof that both /api/base-knowledge/public
callers actually send X-Service-Key on the wire -- the AU4 AST scan
(tests/unit/test_orchestrator_callers_send_service_key.py) proves the
literal is present in the call's own source text; this proves the real
outgoing httpx request actually carries it as a header, built the same way
production code builds it.

Transport harness: real httpx request-building (headers, URL, query
string), faked only at the socket via httpx.MockTransport -- same
technique as tests/unit/test_llm_router_stream_parity.py. `_REAL_ASYNC_CLIENT`
is bound to httpx.AsyncClient BEFORE any monkeypatch runs, so the factory's
own httpx.AsyncClient(...) call doesn't recurse into itself once patched.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

os.environ.setdefault("SERVICE_API_KEY", "test-service-key-p3b")
os.environ.setdefault("ADMIN_API_URL", "http://admin-backend:8080")

from shared.config import _clear_cache_for_tests, get_config  # noqa: E402
from shared.admin_url import _clear_cache_for_tests as _clear_admin_url_cache  # noqa: E402
import shared.admin_config as admin_config_module  # noqa: E402

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    """Records every httpx.Request that passes through, and answers with a
    generic 200 unless the request targets /api/base-knowledge/public,
    which gets `public_response` (default: an empty JSON array -- that
    route's real response shape)."""

    def __init__(self, public_response=None):
        self.requests: list[httpx.Request] = []
        self._public_response = [] if public_response is None else public_response

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if "/api/base-knowledge/public" in str(request.url):
            return httpx.Response(200, json=self._public_response)
        return httpx.Response(200, json={})

    def public_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if "/api/base-knowledge/public" in str(r.url)]


def _patch_async_client(monkeypatch, transport: _RecordingTransport) -> None:
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _reset_config_and_singleton():
    _clear_cache_for_tests()
    _clear_admin_url_cache()
    admin_config_module._admin_client = None
    yield
    _clear_cache_for_tests()
    _clear_admin_url_cache()
    admin_config_module._admin_client = None


# ---------------------------------------------------------------------------
# C1 -- shared.admin_config.AdminConfigClient.get_base_knowledge
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_C1_admin_config_get_base_knowledge_sends_service_key(monkeypatch):
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    client = admin_config_module.AdminConfigClient(
        admin_url="http://admin-backend:8080", api_key="test-service-key-p3b"
    )
    result = await client.get_base_knowledge()
    assert result == []

    public_requests = transport.public_requests()
    assert len(public_requests) == 1
    req = public_requests[0]
    assert req.headers.get("X-Service-Key") == "test-service-key-p3b"
    # X-API-Key is ALSO present -- it's the client's default header, sent on
    # every request through self.client (used by other admin routes that
    # accept it). Harmless here: verify_service_or_oidc checks
    # X-Service-Key first and returns True on a match without ever looking
    # at X-API-Key (service_auth.py's dispatcher order).
    assert req.headers.get("X-API-Key") == "test-service-key-p3b"


# ---------------------------------------------------------------------------
# C2 -- src/rag/directions/main.py's own direct fetch, inside lifespan()
# ---------------------------------------------------------------------------

def _import_directions_main(unique_name: str):
    """Fresh-import directions/main.py under a private module name -- it's
    literally named main.py, the same collision every RAG-service test hits
    (see test_rag_client_dc9.py::_import_amtrak_app)."""
    path = _SRC / "rag" / "directions" / "main.py"
    if unique_name in sys.modules:
        module = sys.modules[unique_name]
        module.__spec__.loader.exec_module(module)
        return module
    spec = importlib.util.spec_from_file_location(unique_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_C2_directions_lifespan_sends_service_key(monkeypatch):
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    directions_module = _import_directions_main("_test_p3b_directions_main")
    monkeypatch.setattr(directions_module, "startup_service", AsyncMock())
    monkeypatch.setattr(directions_module, "unregister_service", AsyncMock())
    fake_cache = MagicMock()
    fake_cache.connect = AsyncMock()
    fake_cache.disconnect = AsyncMock()
    monkeypatch.setattr(directions_module, "CacheClient", MagicMock(return_value=fake_cache))

    fake_app = MagicMock()
    ctx = directions_module.lifespan(fake_app)
    await ctx.__aenter__()
    try:
        public_requests = transport.public_requests()
        assert len(public_requests) == 1
        req = public_requests[0]
        assert req.headers.get("X-Service-Key") == get_config().service_api_key
        # This is a bare httpx.AsyncClient with no default headers at all
        # (unlike admin_config's shared client) -- X-API-Key is never sent
        # on this path, so there's nothing "harmless or not" to check; it
        # simply isn't there.
        assert "X-API-Key" not in req.headers
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_C2_directions_parses_real_list_shape_and_populates_default_origin(monkeypatch):
    """codex P3b FIX: /api/base-knowledge/public returns a JSON list
    directly, not {"items": [...]}. data.get("items", []) on a list raises
    AttributeError, silently swallowed by lifespan's broad except -- so
    BASE_KNOWLEDGE was never populated and get_default_origin() never
    worked, regardless of the auth fix. This proves the parse now handles
    the REAL shape, not just a shape the old code happened to expect."""
    real_shape_response = [
        {
            "id": 1, "category": "location", "key": "default_location",
            "value": "Denver, CO", "applies_to": "both", "priority": 0,
            "extra_metadata": None, "enabled": True, "description": None,
            "created_at": None, "updated_at": None,
        },
    ]
    transport = _RecordingTransport(public_response=real_shape_response)
    _patch_async_client(monkeypatch, transport)

    directions_module = _import_directions_main("_test_p3b_directions_main_shape")
    monkeypatch.setattr(directions_module, "startup_service", AsyncMock())
    monkeypatch.setattr(directions_module, "unregister_service", AsyncMock())
    fake_cache = MagicMock()
    fake_cache.connect = AsyncMock()
    fake_cache.disconnect = AsyncMock()
    monkeypatch.setattr(directions_module, "CacheClient", MagicMock(return_value=fake_cache))

    fake_app = MagicMock()
    ctx = directions_module.lifespan(fake_app)
    await ctx.__aenter__()
    try:
        assert directions_module.BASE_KNOWLEDGE.get("default_location") == "Denver, CO"
        assert directions_module.get_default_origin() == "Denver, CO"
        # P3c: the fetch itself asks the admin API to pre-filter to
        # enabled rows.
        public_req = transport.public_requests()[0]
        assert "enabled=true" in str(public_req.url)
    finally:
        await ctx.__aexit__(None, None, None)


async def _run_lifespan_with_public_response(monkeypatch, unique_name: str, public_response):
    transport = _RecordingTransport(public_response=public_response)
    _patch_async_client(monkeypatch, transport)

    directions_module = _import_directions_main(unique_name)
    monkeypatch.setattr(directions_module, "startup_service", AsyncMock())
    monkeypatch.setattr(directions_module, "unregister_service", AsyncMock())
    fake_cache = MagicMock()
    fake_cache.connect = AsyncMock()
    fake_cache.disconnect = AsyncMock()
    monkeypatch.setattr(directions_module, "CacheClient", MagicMock(return_value=fake_cache))

    fake_app = MagicMock()
    ctx = directions_module.lifespan(fake_app)
    await ctx.__aenter__()
    return directions_module, ctx, transport


@pytest.mark.asyncio
async def test_P3c_default_origin_falls_back_to_default_city_when_row_disabled(monkeypatch):
    """A default_location row that's disabled (?enabled=true should have
    excluded it server-side; this proves the client doesn't trust that
    alone -- see the `item.get("enabled", True)` filter in lifespan)."""
    monkeypatch.setenv("DEFAULT_CITY", "Boulder")
    _clear_cache_for_tests()

    disabled_row = [{
        "id": 1, "category": "location", "key": "default_location",
        "value": "Denver, CO", "applies_to": "both", "priority": 0,
        "extra_metadata": None, "enabled": False, "description": None,
        "created_at": None, "updated_at": None,
    }]
    directions_module, ctx, _ = await _run_lifespan_with_public_response(
        monkeypatch, "_test_p3c_directions_disabled", disabled_row
    )
    try:
        assert directions_module.get_default_origin() == "Boulder"
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_P3c_default_origin_falls_back_to_default_city_when_row_empty(monkeypatch):
    """A default_location row that's enabled but whose value is empty (or
    whitespace-only, e.g. after a Memory & Context 'clear' save) must not
    surface as a blank origin."""
    monkeypatch.setenv("DEFAULT_CITY", "Boulder")
    _clear_cache_for_tests()

    empty_row = [{
        "id": 1, "category": "location", "key": "default_location",
        "value": "   ", "applies_to": "both", "priority": 0,
        "extra_metadata": None, "enabled": True, "description": None,
        "created_at": None, "updated_at": None,
    }]
    directions_module, ctx, _ = await _run_lifespan_with_public_response(
        monkeypatch, "_test_p3c_directions_empty", empty_row
    )
    try:
        assert directions_module.get_default_origin() == "Boulder"
    finally:
        await ctx.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_P3c_default_origin_uses_non_empty_enabled_row(monkeypatch):
    monkeypatch.delenv("DEFAULT_CITY", raising=False)
    _clear_cache_for_tests()

    good_row = [{
        "id": 1, "category": "location", "key": "default_location",
        "value": "Denver, CO", "applies_to": "both", "priority": 0,
        "extra_metadata": None, "enabled": True, "description": None,
        "created_at": None, "updated_at": None,
    }]
    directions_module, ctx, _ = await _run_lifespan_with_public_response(
        monkeypatch, "_test_p3c_directions_good", good_row
    )
    try:
        assert directions_module.get_default_origin() == "Denver, CO"
    finally:
        await ctx.__aexit__(None, None, None)


# ---------------------------------------------------------------------------
# C3 -- the service_api_key_empty warning fires once per construction, not
# once per call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_C3_service_api_key_empty_warning_logged_once_across_two_calls(monkeypatch):
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)
    monkeypatch.delenv("SERVICE_API_KEY", raising=False)
    _clear_cache_for_tests()

    warning_calls = []
    monkeypatch.setattr(
        admin_config_module.logger, "warning",
        lambda *args, **kwargs: warning_calls.append((args, kwargs)),
    )

    client = admin_config_module.AdminConfigClient(admin_url="http://admin-backend:8080", api_key="")
    await client.get_base_knowledge()
    await client.get_base_knowledge()

    empty_key_warnings = [c for c in warning_calls if c[0] and c[0][0] == "service_api_key_empty"]
    assert len(empty_key_warnings) == 1
