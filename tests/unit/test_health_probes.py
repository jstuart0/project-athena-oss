"""Unit tests for health probe endpoints (Phase 1.5 — bare-except cleanup).

Verifies:
  - /health/ready emits WARNING log when HA or Redis probe raises, and the
    response payload still reflects the component as False.
  - /health returns 200 with the documented JSON shape on the happy path.
  - /health/ready returns the documented JSON shape on the happy path.

Design notes:
  - orchestrator.main cannot be imported without heavy deps (langgraph, etc.).
    We mock those at sys.modules level before import, and swap the
    orchestrator.config_loader symbol to prevent its async get_config from
    overwriting shared.config.get_config at module-load time.
  - Module-level state (_ha_client, _llm_router, _cache_client globals in main,
    plus _runtime singletons) is installed per-test and reset in teardown.
  - Tests drive the in-process FastAPI app via
    httpx.AsyncClient(transport=ASGITransport(app=app)).
  - Async tests use asyncio.run() directly (no pytest-asyncio dependency),
    matching the pattern established in tests/unit/test_runtime.py.
"""
from __future__ import annotations

import asyncio
import os
import sys
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Heavy-dependency mocking — must happen before any orchestrator import
# ---------------------------------------------------------------------------

# Stub out deps that are not installed in the unit-test environment.
for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

# Ensure SERVICE_API_KEY and ADMIN_API_URL are present so main.py module-level
# code doesn't raise on first load.
os.environ.setdefault("SERVICE_API_KEY", "test-key-health-probes")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

# Pre-import shared.config so its lru_cache is warmed up with the SYNC
# get_config before orchestrator.config_loader (which exports an ASYNC
# get_config) is imported.  main.py line 50 imports shared.config.get_config,
# then line 63 re-imports orchestrator.config_loader.get_config under the
# same name — the second import wins at module load.  We install a mock for
# orchestrator.config_loader that hands back the sync version to keep line 182
# (`_SERVICE_API_KEY = get_config().service_api_key`) working.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))
from shared.config import get_config as _sync_get_config  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _sync_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules["orchestrator.config_loader"] = _config_loader_mock

# Now it is safe to import from orchestrator.main.
import orchestrator.main as _main_module  # noqa: E402
from orchestrator.main import app  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402

import httpx  # noqa: E402
from httpx import ASGITransport  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _BrokenHA:
    """Simulates a Home Assistant client whose health_check always raises."""

    async def health_check(self) -> bool:
        raise ConnectionError("simulated HA outage")


class _BrokenCache:
    """Simulates a Redis cache client whose ping always raises."""

    async def ping(self) -> bool:
        raise ConnectionError("simulated redis outage")


class _HealthyHA:
    async def health_check(self) -> bool:
        return True


class _HealthyCache:
    async def ping(self) -> bool:
        return True


def _make_fake_llm() -> mock.MagicMock:
    return mock.MagicMock(name="fake_llm_router")


async def _get(path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_runtime():
    """Isolate each test: clear _runtime and module-level globals before/after."""
    _runtime.reset_for_test()
    _main_module.ha_client = None
    _main_module.llm_router = None
    _main_module.cache_client = None
    yield
    _runtime.reset_for_test()
    _main_module.ha_client = None
    _main_module.llm_router = None
    _main_module.cache_client = None


# ---------------------------------------------------------------------------
# Test 1: readiness probe emits WARNING when HA health raises
# ---------------------------------------------------------------------------


def test_readiness_probe_logs_warning_on_ha_health_failure():
    """
    When _runtime.ha_client.health_check() raises, /health/ready must:
      (a) set components["home_assistant"] = False in the response, and
      (b) call logger.warning with a message that identifies the probe site
          and passes exc_info so the exception is captured.

    Note: orchestrator.main uses structlog, which does not propagate to the
    standard logging subsystem that caplog captures.  We patch
    orchestrator.main.logger directly instead.
    """
    _runtime.set_llm_router(_make_fake_llm())
    _runtime.set_ha_client(_BrokenHA())
    fake_cache = mock.MagicMock()
    fake_cache.ping = mock.AsyncMock(return_value=True)
    _runtime.set_cache_client(fake_cache)

    with mock.patch.object(_main_module, "logger") as mock_logger:
        resp = asyncio.run(_get("/health/ready"))

    data = resp.json()

    # (a) Response shape: home_assistant must be reported as False.
    assert data["components"]["home_assistant"] is False, (
        f"expected home_assistant=False, got: {data['components']}"
    )

    # (b) logger.warning must have been called with a message that identifies
    #     the probe site.  The exc_info kwarg carries the exception instance.
    warning_calls = mock_logger.warning.call_args_list
    assert warning_calls, "Expected at least one logger.warning call, got none"
    messages = [str(call.args[0]) for call in warning_calls if call.args]
    assert any(
        "home_assistant" in msg and "readiness_probe" in msg
        for msg in messages
    ), f"Expected warning mentioning 'home_assistant' and 'readiness_probe'. calls={warning_calls}"
    # exc_info should be the ConnectionError instance
    exc_info_values = [call.kwargs.get("exc_info") for call in warning_calls]
    assert any(isinstance(ei, ConnectionError) for ei in exc_info_values), (
        f"Expected exc_info=ConnectionError in one warning call. exc_info values={exc_info_values}"
    )


# ---------------------------------------------------------------------------
# Test 2: readiness probe emits WARNING when Redis ping raises
# ---------------------------------------------------------------------------


def test_readiness_probe_logs_warning_on_redis_failure():
    """
    When _runtime.cache_client.ping() raises, /health/ready must:
      (a) set components["redis"] = False in the response, and
      (b) call logger.warning identifying the redis probe site with exc_info.
    """
    _runtime.set_llm_router(_make_fake_llm())
    _runtime.set_cache_client(_BrokenCache())
    # HA not set — probe sees None and returns False without raising.

    with mock.patch.object(_main_module, "logger") as mock_logger:
        resp = asyncio.run(_get("/health/ready"))

    data = resp.json()

    # (a) Response shape: redis must be reported as False.
    assert data["components"]["redis"] is False, (
        f"expected redis=False, got: {data['components']}"
    )

    # (b) logger.warning must mention the redis probe site.
    warning_calls = mock_logger.warning.call_args_list
    assert warning_calls, "Expected at least one logger.warning call, got none"
    messages = [str(call.args[0]) for call in warning_calls if call.args]
    assert any(
        "redis" in msg and "readiness_probe" in msg
        for msg in messages
    ), f"Expected warning mentioning 'redis' and 'readiness_probe'. calls={warning_calls}"
    exc_info_values = [call.kwargs.get("exc_info") for call in warning_calls]
    assert any(isinstance(ei, ConnectionError) for ei in exc_info_values), (
        f"Expected exc_info=ConnectionError in one warning call. exc_info values={exc_info_values}"
    )


# ---------------------------------------------------------------------------
# Test 3: happy path — response shapes unchanged
# ---------------------------------------------------------------------------


def test_health_probes_return_unchanged_shape():
    """
    On the happy path, /health returns 200 and /health/ready returns 200
    with the documented JSON shapes.  This is the regression guard that no
    HTTP behavior changed as part of Phase 1.5.
    """
    # /health uses module-level globals (ha_client, llm_router, cache_client)
    _main_module.ha_client = mock.MagicMock(
        health_check=mock.AsyncMock(return_value=True)
    )
    _main_module.llm_router = _make_fake_llm()
    _main_module.cache_client = mock.MagicMock(
        ping=mock.AsyncMock(return_value=True)
    )

    # /health/ready uses _runtime singletons
    _runtime.set_llm_router(_make_fake_llm())
    _runtime.set_ha_client(_HealthyHA())
    _runtime.set_cache_client(_HealthyCache())
    # Satisfy the remaining required singletons so is_ready() returns True.
    _runtime.set_session_manager(mock.MagicMock())
    _runtime.set_rag_client(mock.MagicMock())
    _runtime.set_mode_client(mock.MagicMock())
    _runtime.set_intent_classifier(mock.MagicMock())

    health_resp = asyncio.run(_get("/health"))
    ready_resp = asyncio.run(_get("/health/ready"))

    # /health shape
    assert health_resp.status_code == 200
    health_data = health_resp.json()
    assert "status" in health_data
    assert "components" in health_data
    assert "service" in health_data
    assert health_data["service"] == "orchestrator"

    # /health/ready shape
    assert ready_resp.status_code == 200
    ready_data = ready_resp.json()
    assert "status" in ready_data
    assert "ready" in ready_data
    assert "components" in ready_data
    assert ready_data["ready"] is True
    assert ready_data["status"] == "ready"
