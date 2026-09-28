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
    # ATHENA-108 follow-up: the toggle target is the "-rag"-suffixed
    # registry name (register_service()'s own convention), not the bare
    # connector name -- a bare-named path never matches a "<name>-rag" row,
    # so shutdown silently toggled nothing.
    assert transport.requests[0].url.path == "/api/service-registry/services/weather-rag/toggle"
    assert dict(transport.requests[0].url.params)["host_label"] == "athena-rag-weather"
    assert transport.requests[0].headers.get("X-Service-Key") == "test-service-key-athena-108"


@pytest.mark.asyncio
async def test_unregister_service_already_rag_suffixed_name_is_idempotent(monkeypatch):
    """to_rag_registry_name() is idempotent -- a caller that already passes
    the "-rag"-suffixed name must not get "-rag-rag" appended."""
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=200)
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.unregister_service("weather-rag")

    assert ok is True
    assert transport.requests[0].url.path == "/api/service-registry/services/weather-rag/toggle"
    assert dict(transport.requests[0].url.params)["host_label"] == "athena-rag-weather"


@pytest.mark.asyncio
async def test_unregister_service_clears_cache_keyed_by_bare_name(monkeypatch):
    """_url_cache/_cache_time are keyed by the bare name get_service_url()
    callers use -- unregister_service() must clear that key, not the
    "-rag"-derived name it now posts to the toggle route (regression pin:
    the rebind that derives the registry name must not also break cache
    invalidation)."""
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    _clear_cache_for_tests()
    service_registry_module._url_cache["weather"] = "http://athena-rag-weather:8010"
    service_registry_module._cache_time["weather"] = 0.0
    transport = _RecordingTransport(status_code=200)
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.unregister_service("weather")

    assert ok is True
    assert "weather" not in service_registry_module._url_cache
    assert "weather" not in service_registry_module._cache_time


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


# ---------------------------------------------------------------------------
# xander diff-review Critical (2026-09-28): register_service()'s payload
# must never claim ownership of network location it doesn't have --
# service_type is always 'rag' (never 'api'), and endpoint_url is included
# ONLY when SERVICE_REGISTRY_ENDPOINT_URL is explicitly set.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_register_service_payload_omits_endpoint_url_by_default(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    monkeypatch.delenv("SERVICE_REGISTRY_ENDPOINT_URL", raising=False)
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=200)
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.register_service("weather", 8010, "Weather Service")

    assert ok is True
    assert len(transport.requests) == 1
    query = dict(transport.requests[0].url.params)
    assert "endpoint_url" not in query
    assert query["service_type"] == "rag"
    # ATHENA-108 follow-up: the registry row is named "<name>-rag", not the
    # bare connector name -- posting "weather" against a "weather-rag" row
    # found no match and 422'd (registration silently never landed).
    assert query["name"] == "weather-rag"
    assert query["host_label"] == "athena-rag-weather"


@pytest.mark.asyncio
async def test_register_service_payload_includes_endpoint_url_when_env_set(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    monkeypatch.setenv("SERVICE_REGISTRY_ENDPOINT_URL", "http://weather-dev-box:8010")
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=200)
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.register_service("weather", 8010, "Weather Service")

    assert ok is True
    query = dict(transport.requests[0].url.params)
    assert query["endpoint_url"] == "http://weather-dev-box:8010"


@pytest.mark.asyncio
async def test_register_service_never_sends_service_type_api(monkeypatch):
    """Regression pin for the xander Critical finding: this payload must
    never again ship the hardcoded, wrong 'api' service_type."""
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=200)
    _patch_async_client(monkeypatch, transport)

    for name in ("weather", "amtrak", "tesla", "site-scraper", "price-compare"):
        transport.requests.clear()
        await service_registry_module.register_service(name, 8010, "x")
        query = dict(transport.requests[0].url.params)
        assert query["service_type"] == "rag", f"{name!r}: {query}"
        assert query["service_type"] != "api"


# ---------------------------------------------------------------------------
# ATHENA-108 follow-up: registry rows are named "<name>-rag" (the same
# "-rag" suffix convention admin/backend/app/database.py::
# _infer_oss_service_type and the Control Agent's registry-sync apply), not
# the bare connector name register_service() used to post -- that mismatch
# 422'd the admin upsert and registration never landed for any RAG.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bare,expected", [
    ("weather", "weather-rag"),
    ("weather-rag", "weather-rag"),
    # ATHENA-108 follow-up 2: a connector name with a hyphen the seeded
    # row's name/host never had (e.g. "price-compare" vs seeded
    # "pricecompare") must normalise to the seeded base, not carry the
    # hyphen through.
    ("site-scraper", "sitescraper-rag"),
    ("price-compare", "pricecompare-rag"),
    ("price-compare-rag", "pricecompare-rag"),
    ("price_compare", "pricecompare-rag"),
    ("Price-Compare", "pricecompare-rag"),
    ("SITE_SCRAPER", "sitescraper-rag"),
])
def test_to_rag_registry_name_derivation(bare, expected):
    assert service_registry_module.to_rag_registry_name(bare) == expected


@pytest.mark.parametrize("bare,expected", [
    ("weather", "athena-rag-weather"),
    ("weather-rag", "athena-rag-weather"),
    ("site-scraper", "athena-rag-sitescraper"),
    ("price-compare", "athena-rag-pricecompare"),
    ("price_compare", "athena-rag-pricecompare"),
    ("Price-Compare-RAG", "athena-rag-pricecompare"),
])
def test_to_rag_host_label_derivation(bare, expected):
    assert service_registry_module.to_rag_host_label(bare) == expected


def test_service_registry_name_override_bypasses_normalization(monkeypatch):
    """SERVICE_REGISTRY_NAME lets a deployer whose seeded row doesn't
    follow the lower-case/strip-separators convention pin the exact base,
    skipping _normalize_rag_base() entirely."""
    monkeypatch.setenv("SERVICE_REGISTRY_NAME", "Custom_Row")
    from shared.config import _clear_cache_for_tests as _cfg_clear
    _cfg_clear()
    try:
        assert service_registry_module.to_rag_registry_name("price-compare") == "Custom_Row-rag"
        assert service_registry_module.to_rag_host_label("price-compare") == "athena-rag-Custom_Row"
    finally:
        monkeypatch.delenv("SERVICE_REGISTRY_NAME", raising=False)
        _cfg_clear()


# ---------------------------------------------------------------------------
# codex follow-up (2026-09-28, Low): to_rag_registry_name()/to_rag_host_label()
# must bound the derived name/host_label with the same regex the admin
# route's own validation uses (_SERVICE_NAME_RE/_HOST_LABEL_RE), failing
# early with a clear error rather than sending a value the route would 422
# on anyway -- most reachable via a misconfigured SERVICE_REGISTRY_NAME.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_override", [
    "has spaces",
    "has/slash",
    "has.dot",  # invalid for `name` even though host_label allows dots
    "x" * 61,  # -> "<61 x's>-rag" is 65 chars, over _SERVICE_NAME_RE's 64 cap
])
def test_to_rag_registry_name_rejects_invalid_override(monkeypatch, bad_override):
    monkeypatch.setenv("SERVICE_REGISTRY_NAME", bad_override)
    from shared.config import _clear_cache_for_tests as _cfg_clear
    _cfg_clear()
    try:
        with pytest.raises(ValueError, match="does not match"):
            service_registry_module.to_rag_registry_name("weather")
    finally:
        monkeypatch.delenv("SERVICE_REGISTRY_NAME", raising=False)
        _cfg_clear()


@pytest.mark.parametrize("bad_override", [
    "has spaces",
    "has/slash",
    "x" * 245,  # -> "athena-rag-<245 x's>" is 256 chars, over _HOST_LABEL_RE's 255 cap
])
def test_to_rag_host_label_rejects_invalid_override(monkeypatch, bad_override):
    monkeypatch.setenv("SERVICE_REGISTRY_NAME", bad_override)
    from shared.config import _clear_cache_for_tests as _cfg_clear
    _cfg_clear()
    try:
        with pytest.raises(ValueError, match="does not match"):
            service_registry_module.to_rag_host_label("weather")
    finally:
        monkeypatch.delenv("SERVICE_REGISTRY_NAME", raising=False)
        _cfg_clear()


def test_service_registry_name_override_boundary_length(monkeypatch):
    """Positive control: a 60-char override (name becomes exactly 64 chars,
    _SERVICE_NAME_RE's cap) is accepted; 61 chars is rejected -- proves the
    bound is the same 64-char cap the admin route enforces, not an
    arbitrarily looser one."""
    from shared.config import _clear_cache_for_tests as _cfg_clear

    monkeypatch.setenv("SERVICE_REGISTRY_NAME", "x" * 60)
    _cfg_clear()
    try:
        assert service_registry_module.to_rag_registry_name("weather") == ("x" * 60) + "-rag"
    finally:
        monkeypatch.delenv("SERVICE_REGISTRY_NAME", raising=False)
        _cfg_clear()

    monkeypatch.setenv("SERVICE_REGISTRY_NAME", "x" * 61)
    _cfg_clear()
    try:
        with pytest.raises(ValueError, match="does not match"):
            service_registry_module.to_rag_registry_name("weather")
    finally:
        monkeypatch.delenv("SERVICE_REGISTRY_NAME", raising=False)
        _cfg_clear()


@pytest.mark.asyncio
async def test_register_service_payload_sends_derived_name_and_host_label(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-service-key-athena-108")
    _clear_cache_for_tests()
    transport = _RecordingTransport(status_code=200)
    _patch_async_client(monkeypatch, transport)

    ok = await service_registry_module.register_service("weather", 8010, "Weather Service")

    assert ok is True
    assert len(transport.requests) == 1
    query = dict(transport.requests[0].url.params)
    assert query["name"] == "weather-rag"
    assert query["host_label"] == "athena-rag-weather"
    assert query["display_name"] == "Weather Service"
    assert query["service_type"] == "rag"
