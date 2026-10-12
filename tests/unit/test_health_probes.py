"""Unit tests for health probe endpoints (Phase 1.5 — bare-except cleanup).

Verifies:
  - No health or probe handler (and not /admin/ha-status) sends anything to
    Home Assistant: fail-on-call transports sit under both HA REST clients
    and in place of the registry fetcher, with a positive control proving the
    fixture can fail.
  - components.home_assistant is derived from the merged link view, never
    from a request.
  - /health/ready emits WARNING log when the HA or Redis probe raises, and the
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
import json
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


class _BrokenLinkPublisher:
    """A link publisher whose view raises, to exercise the probe's warning path."""

    def view(self):
        raise ConnectionError("simulated link view failure")


class _BrokenCache:
    """Simulates a Redis cache client whose ping always raises."""

    async def ping(self) -> bool:
        raise ConnectionError("simulated redis outage")


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


def _monitor_with(*statuses: int):
    from shared import ha_link

    monitor = ha_link.HALinkMonitor()
    for status in statuses:
        monitor.record_response("GET", "/api/states", status)
    return monitor


def _link_publisher(monitor, *, configured: bool = True, registry=None):
    from shared import ha_link
    from shared.ha_registry import HARegistry

    publisher = ha_link.HALinkPublisher(
        monitor, registry if registry is not None else HARegistry(None), None, "pod-test", configured=configured
    )
    return monitor, publisher


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
    When the Home Assistant link view raises, /health/ready must:
      (a) set components["home_assistant"] = False in the response, and
      (b) call logger.warning with a message that identifies the probe site
          and passes exc_info so the exception is captured.

    Note: orchestrator.main uses structlog, which does not propagate to the
    standard logging subsystem that caplog captures.  We patch
    orchestrator.main.logger directly instead.
    """
    _runtime.set_llm_router(_make_fake_llm())
    _runtime.set_ha_link_publisher(_BrokenLinkPublisher())
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
    # /health uses module-level globals (llm_router, cache_client)
    _main_module.llm_router = _make_fake_llm()
    _main_module.cache_client = mock.MagicMock(
        ping=mock.AsyncMock(return_value=True)
    )

    # /health/ready uses _runtime singletons
    _runtime.set_llm_router(_make_fake_llm())
    _runtime.set_ha_link_publisher(_link_publisher(_monitor_with(200))[1])
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


# ---------------------------------------------------------------------------
# Area registry state on /health
# ---------------------------------------------------------------------------

REGISTRY_STATES = {"disabled", "refused", "unavailable", "ok"}


def _registry_in_state(state: str):
    from shared import ha_registry

    class _Fetcher:
        async def __call__(self):
            raise AssertionError("a health probe must not fetch")

    registry = ha_registry.HARegistry(_Fetcher(), configured=lambda: state != "disabled")
    registry._state = state
    return registry


def test_orchestrator_health_reports_ha_registry_under_components_only():
    _runtime.set_ha_registry(_registry_in_state("refused"))
    body = asyncio.run(_get("/health")).json()
    assert body["components"]["ha_registry"] == "refused"
    assert "ha_registry" not in body


def test_orchestrator_health_reports_disabled_when_no_registry_is_installed():
    body = asyncio.run(_get("/health")).json()
    assert body["components"]["ha_registry"] in REGISTRY_STATES


def test_orchestrator_health_ha_registry_carries_only_a_state_word():
    _runtime.set_ha_registry(_registry_in_state("ok"))
    body = asyncio.run(_get("/health")).json()
    assert isinstance(body["components"]["ha_registry"], str)
    assert body["components"]["ha_registry"] in REGISTRY_STATES


@pytest.mark.parametrize("state", ["ok", "unavailable", "refused", "disabled"])
def test_gateway_health_reports_ha_registry_at_the_top_level(state, monkeypatch):
    sys.modules.setdefault("prometheus_client", mock.MagicMock())
    import gateway.main as gw
    from fastapi.testclient import TestClient

    monkeypatch.setattr(gw, "ha_registry", _registry_in_state(state))
    monkeypatch.setattr(gw, "orchestrator_client", mock.MagicMock(get=mock.AsyncMock(side_effect=OSError)))
    monkeypatch.setattr(gw, "ollama_client", None)
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    monkeypatch.setattr(gw, "global_rate_limiter", None)
    body = TestClient(gw.app).get("/health").json()
    assert body["ha_registry"] == state
    assert "ha_registry" not in body.get("components", {})


# ---------------------------------------------------------------------------
# No probe sends anything to Home Assistant
# ---------------------------------------------------------------------------

HA_TEST_URL = "https://ha.test"
HA_TEST_TOKEN = "sekret-token-123"


@pytest.fixture
def ha_traffic(monkeypatch):
    """Fail-on-call transports under the orchestrator's real HA channel (REST
    and the WebSocket proof) and in place of the registry fetcher. Anything that
    reaches Home Assistant raises AssertionError AND is appended to ``traffic``,
    so a handler that swallows the exception still fails the test."""
    import httpx as _httpx
    from orchestrator.ha_entity_manager import HAEntityManager
    from orchestrator.mode_permission import ensure_permission_enforcing
    from shared import ha_auth, ha_link
    from shared.ha_client import HomeAssistantClient
    from shared.ha_credentials import KeyLookup, StaticResolver, resolve
    from shared.ha_http import HAChannel
    from shared.ha_registry import HARegistry

    traffic: list = []

    def handler(req):
        traffic.append(f"{req.method} {req.url}")
        raise AssertionError(f"unexpected request to Home Assistant: {req.method} {req.url}")

    def connect(url, **kwargs):
        traffic.append("websocket connect")
        raise AssertionError("unexpected WebSocket connection to Home Assistant")

    async def fetcher():
        traffic.append("registry fetch")
        raise AssertionError("unexpected area registry fetch")

    monitor = ha_link.HALinkMonitor()
    credentials = resolve(
        KeyLookup.absent(), {"HA_URL": HA_TEST_URL, "HA_TOKEN": HA_TEST_TOKEN}, previous=None, service_api_key="k",
    )
    resolver = StaticResolver(credentials)
    latch = ha_auth.HAAuthLatch(None, instance="t", service_api_key="k", gate_wait=0.05)
    channel = HAChannel(
        resolver, latch, caller="orchestrator_rest", monitor=monitor,
        inner=_httpx.MockTransport(handler), ws_connect=connect,
    )
    raw_client = HomeAssistantClient(channel)
    registry = HARegistry(fetcher)
    _monitor, link_publisher = _link_publisher(monitor, registry=registry)

    _runtime.set_ha_link(monitor)
    _runtime.set_ha_client(ensure_permission_enforcing(raw_client))
    _runtime.set_entity_manager(HAEntityManager(ensure_permission_enforcing(raw_client)))
    _runtime.set_ha_registry(registry)
    _runtime.set_ha_credentials(resolver)
    _runtime.set_ha_auth_latch(latch)
    _runtime.set_ha_link_publisher(link_publisher)
    _runtime.set_llm_router(_make_fake_llm())
    _runtime.set_cache_client(_HealthyCache())
    return mock.MagicMock(
        traffic=traffic, raw_client=raw_client, channel=channel, monitor=monitor, publisher=link_publisher,
    )


PROBE_ROUTES = ["/health", "/health/ready", "/health/live", "/health/startup", "/admin/ha-status"]


def _service_key_headers():
    from shared.config import get_config

    return {"X-Service-Key": get_config().service_api_key}


def test_the_fixture_can_fail_a_request_to_home_assistant_is_counted(ha_traffic):
    """Positive control: a request through the same fixture IS recorded (the
    channel's token proof tries the WebSocket, then a GET /api), so an empty
    list below means no request, not a blind fixture."""
    asyncio.run(ha_traffic.channel.request("GET", "/api/states"))
    assert ha_traffic.traffic == ["websocket connect", f"GET {HA_TEST_URL}/api"]


def test_the_fixture_also_catches_the_entity_manager_and_the_registry(ha_traffic):
    entity_manager = _runtime.get_entity_manager()
    with pytest.raises(Exception):
        asyncio.run(entity_manager.refresh_entities())  # a withheld 401 raises like a real one
    with pytest.raises(AssertionError):
        asyncio.run(_runtime.get_ha_registry()._fetcher())
    assert ha_traffic.traffic == ["websocket connect", f"GET {HA_TEST_URL}/api", "registry fetch"]


def test_no_probe_route_sends_anything_to_home_assistant(ha_traffic):
    assert len(PROBE_ROUTES) >= 5
    for route in PROBE_ROUTES:
        response = asyncio.run(_get_with_key(route))
        assert response.status_code in (200, 503), f"{route} -> {response.status_code}"
        assert ha_traffic.traffic == [], f"{route} sent a request to Home Assistant: {ha_traffic.traffic}"


async def _get_with_key(path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path, headers=_service_key_headers())


def test_no_orchestrator_code_calls_a_health_check_method_on_the_ha_client():
    import ast

    source = open(os.path.join(os.path.dirname(_main_module.__file__), "main.py"), encoding="utf-8").read()
    calls = [
        n for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "health_check"
    ]
    assert calls == []


def test_home_assistant_is_true_after_a_successful_contact_and_false_after_a_rejected_one(ha_traffic):
    ha_traffic.monitor.record_response("GET", "/api/states", 200)
    for route, key in (("/health", "components"), ("/health/ready", "components")):
        assert asyncio.run(_get(route)).json()[key]["home_assistant"] is True
    ha_traffic.monitor.record_response("GET", "/api/states", 401)
    for route, key in (("/health", "components"), ("/health/ready", "components")):
        assert asyncio.run(_get(route)).json()[key]["home_assistant"] is False
    assert ha_traffic.traffic == []


def test_home_assistant_is_false_before_any_contact_and_when_not_configured():
    _runtime.set_llm_router(_make_fake_llm())
    _runtime.set_ha_link_publisher(_link_publisher(_monitor_with())[1])
    assert asyncio.run(_get("/health")).json()["components"]["home_assistant"] is False
    _runtime.set_ha_link_publisher(_link_publisher(_monitor_with(200), configured=False)[1])
    assert asyncio.run(_get("/health")).json()["components"]["home_assistant"] is False


def test_an_unrelated_forbidden_response_does_not_fail_the_link_until_three_in_a_row():
    _runtime.set_llm_router(_make_fake_llm())
    from shared import ha_link

    monitor = ha_link.HALinkMonitor()
    monitor.record_response("GET", "/api/states", 200)
    forbidden = ("POST", "/api/services/light/turn_on", 403)  # a service call, not a read any token may make
    monitor.record_response(*forbidden)
    _runtime.set_ha_link_publisher(_link_publisher(monitor)[1])
    assert asyncio.run(_get("/health")).json()["components"]["home_assistant"] is True
    monitor.record_response(*forbidden)
    monitor.record_response(*forbidden)
    assert asyncio.run(_get("/health")).json()["components"]["home_assistant"] is False


def test_readiness_is_not_gated_on_home_assistant(ha_traffic):
    _runtime.set_session_manager(mock.MagicMock())
    _runtime.set_rag_client(mock.MagicMock())
    _runtime.set_mode_client(mock.MagicMock())
    _runtime.set_intent_classifier(mock.MagicMock())
    ha_traffic.monitor.record_response("GET", "/api/states", 401)
    response = asyncio.run(_get("/health/ready"))
    assert response.status_code == 200
    assert response.json()["components"]["home_assistant"] is False


def test_health_never_carries_an_areas_key_or_an_area_name(ha_traffic):
    import json as _json
    from shared.ha_registry import RegistrySnapshot

    raw = _json.loads(
        (__import__("pathlib").Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json").read_text()
    )
    registry = _runtime.get_ha_registry()
    registry._snapshot = RegistrySnapshot.from_raw(raw, fetched_at=0.0)
    registry._state = "ok"
    ha_traffic.monitor.record_response("GET", "/api/states", 200)
    body = asyncio.run(_get("/health")).json()

    def keys(node):
        if isinstance(node, dict):
            for k, v in node.items():
                yield k
                yield from keys(v)
        elif isinstance(node, list):
            for item in node:
                yield from keys(item)

    assert "areas" not in set(keys(body))
    assert body["components"]["ha_registry"] == "ok"
    names = [a["name"] for a in raw["areas"] if isinstance(a, dict) and isinstance(a.get("name"), str)]
    assert names and not any(name in json.dumps(body) for name in names)
