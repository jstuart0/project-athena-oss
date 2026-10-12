"""GET /admin/ha-status: the orchestrator's Home Assistant link view for admin.

Runs in its own process: it imports orchestrator.main behind a stubbed
config_loader (see test_health_probes.py for why). The response is enums,
counts and timestamps only; the golden file tests/fixtures/ha_link/
admin_ha_status_v2.json is what admin-backend's tests parse, so the two sides
can't drift. Regenerate it with UPDATE_GOLDEN=1.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import unittest.mock as mock
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

os.environ.setdefault("SERVICE_API_KEY", "test-key-ha-status-route")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

SRC = Path(__file__).resolve().parents[2] / "src"
sys.path.insert(0, str(SRC))
from shared.config import _clear_cache_for_tests, get_config as _sync_get_config  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _sync_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules["orchestrator.config_loader"] = _config_loader_mock

import orchestrator.main as _main_module  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from shared import ha_link  # noqa: E402
from shared.ha_registry import HARegistry, RegistrySnapshot  # noqa: E402

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
GOLDEN = FIXTURES / "ha_link" / "admin_ha_status_v2.json"
HOUSE = json.loads((FIXTURES / "ha_registry" / "house_a.json").read_text(encoding="utf-8"))
AREA_NAMES = [
    a["name"] for a in HOUSE["areas"]
    if isinstance(a, dict) and isinstance(a.get("name"), str) and len(a["name"]) >= 4  # digit-only names would match timestamps
]
TOKEN = "sekret-token-123"
HOST = "ha.test"
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

TOP_KEYS = {"configured", "rest", "registry", "auth", "coverage", "coverage_reason", "replicas_reporting", "observed_at"}
REST_KEYS = {"last_outcome", "last_outcome_at", "last_success_at", "consecutive_failures", "ban_suspected"}
REGISTRY_KEYS = {"state", "areas", "last_success_at", "last_failure_at", "last_failure_class", "ban_suspected"}
AUTH_KEYS = {"latched", "latched_at", "next_probe_at", "banned_at", "failures", "budget_spent", "budget_spent_at"}


class FakeRedis:
    def __init__(self):
        self.h = {}

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def expire(self, key, seconds):
        pass

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

    async def hscan(self, key, cursor=0, match=None, count=None):
        return 0, dict(self.h.get(key, {}))

    async def hdel(self, key, *fields):
        pass


def _ok_registry():
    registry = HARegistry(None, wall=lambda: T0 - timedelta(seconds=120))
    registry._snapshot = RegistrySnapshot.from_raw(HOUSE, fetched_at=0.0)
    registry._state = "ok"
    registry._last_success_at = T0 - timedelta(seconds=120)
    return registry


def _connected_publisher(redis=None, *, registry=None, configured=True):
    clock = [T0 - timedelta(seconds=30)]
    monitor = ha_link.HALinkMonitor(wall=lambda: clock[0])
    monitor.record_response("GET", "/api/states", 200)
    clock[0] = T0
    publisher = ha_link.HALinkPublisher(
        monitor, registry if registry is not None else _ok_registry(), redis, "pod-test",
        configured=configured, wall=lambda: T0,
    )
    if redis is not None:
        asyncio.run(publisher.publish_once())
    return publisher


@pytest.fixture(autouse=True)
def _runtime_and_config():
    _runtime.reset_for_test()
    _clear_cache_for_tests()
    yield
    _runtime.reset_for_test()
    _clear_cache_for_tests()


@pytest.fixture
def client():
    return TestClient(_main_module.app)


def _key():
    return {"X-Service-Key": _sync_get_config().service_api_key}


def _mode(monkeypatch, mode):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", mode)
    monkeypatch.setenv("SERVICE_API_KEY", "test-key-ha-status-route")
    monkeypatch.delenv("DEV_MODE", raising=False)
    _clear_cache_for_tests()


# -- auth --------------------------------------------------------------------

def test_no_key_is_401_under_enforce(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    assert client.get("/admin/ha-status").status_code == 401


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_a_wrong_key_is_401_in_every_mode(monkeypatch, client, mode):
    _mode(monkeypatch, mode)
    assert client.get("/admin/ha-status", headers={"X-Service-Key": "wrong"}).status_code == 401


def test_the_right_key_is_200_under_enforce(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    _runtime.set_ha_link_publisher(_connected_publisher())
    response = client.get("/admin/ha-status", headers=_key())
    assert response.status_code == 200


# -- shape -------------------------------------------------------------------

def _body(client, publisher):
    _runtime.set_ha_link_publisher(publisher)
    return client.get("/admin/ha-status", headers=_key()).json()


def test_exact_key_sets_at_every_level(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    body = _body(client, _connected_publisher())
    assert set(body) == TOP_KEYS
    assert set(body["rest"]) == REST_KEYS
    assert set(body["registry"]) == REGISTRY_KEYS
    assert set(body["auth"]) == AUTH_KEYS


def test_not_configured_and_registry_missing_shapes(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    publisher = ha_link.HALinkPublisher(
        ha_link.HALinkMonitor(), None, None, "pod-test", configured=False, wall=lambda: T0
    )
    body = _body(client, publisher)
    assert body["configured"] is False
    assert body["registry"] == {
        "state": "disabled", "areas": None, "last_success_at": None,
        "last_failure_at": None, "last_failure_class": None, "ban_suspected": False,
    }
    assert body["rest"] == {
        "last_outcome": None, "last_outcome_at": None, "last_success_at": None, "consecutive_failures": 0,
        "ban_suspected": False,
    }
    assert body["auth"] == ha_link.empty_auth()
    assert set(body) == TOP_KEYS


def test_without_any_publisher_the_route_answers_not_configured_not_500(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    body = client.get("/admin/ha-status", headers=_key()).json()
    assert set(body) == TOP_KEYS and body["configured"] is False


def test_cluster_coverage_reports_how_many_replicas(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    body = _body(client, _connected_publisher(FakeRedis()))
    assert (body["coverage"], body["coverage_reason"], body["replicas_reporting"]) == ("cluster", None, 1)


def test_a_replica_only_view_says_why(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    body = _body(client, _connected_publisher(None))
    assert (body["coverage"], body["coverage_reason"]) == ("replica", "redis_unconfigured")


# -- secrecy -----------------------------------------------------------------

def test_no_value_carries_a_url_a_host_an_area_name_or_the_token_and_neither_do_logs(monkeypatch, client, caplog, capsys):
    _mode(monkeypatch, "enforce")
    publisher = _connected_publisher(FakeRedis())
    with structlog.testing.capture_logs() as logs, caplog.at_level(logging.DEBUG):
        publisher._monitor.record_exception(
            __import__("httpx").ConnectError(f"cannot reach https://{HOST}/api/states Bearer {TOKEN}")
        )
        asyncio.run(publisher.publish_once())
        body = _body(client, publisher)
    app_log_text = "".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    blob = json.dumps(body) + json.dumps(logs, default=str) + app_log_text + capsys.readouterr().out
    assert AREA_NAMES, "the fixture must have named areas for this check to mean anything"
    assert "ha_link_state_changed" in [e["event"] for e in logs], "the exception must have been logged for this to mean anything"
    for forbidden in ["://", HOST, TOKEN, "cannot reach", *AREA_NAMES]:
        assert forbidden not in blob, forbidden
    assert body["rest"]["last_outcome"] == "unreachable"


# -- the shared sign-in state rides under "auth" -------------------------------------

class _Latch:
    def __init__(self, summary):
        self._summary = summary

    async def sync(self, snapshot):
        return None

    def summary(self, snapshot):
        return dict(self._summary)


class _Credentials:
    def current(self):
        return SimpleNamespace(digest="d1")


def _publisher_with_auth(**auth):
    clock = [T0 - timedelta(seconds=60)]
    monitor = ha_link.HALinkMonitor(wall=lambda: clock[0])
    monitor.record_response("GET", "/api/states", 200)
    return ha_link.HALinkPublisher(
        monitor, _ok_registry(), None, "pod-test", configured=True, wall=lambda: T0,
        latch=_Latch({**ha_link.empty_auth(), **auth}), credentials=_Credentials(),
    )


def test_a_ban_that_only_another_service_saw_reaches_the_response_and_reads_ip_banned(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    banned_at = (T0 - timedelta(seconds=10)).isoformat()
    body = _body(client, _publisher_with_auth(banned_at=banned_at, failures={"gateway_rest": {"count": 2, "last_at": banned_at}}))
    assert body["auth"]["banned_at"] == banned_at and body["auth"]["failures"]["gateway_rest"]["count"] == 2
    assert ha_link.link_state(body) == ("failing", "ip_banned")


def test_a_spent_budget_reaches_the_response_and_reads_token_rejected(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    spent = (T0 - timedelta(seconds=10)).isoformat()
    body = _body(client, _publisher_with_auth(budget_spent=True, budget_spent_at=spent))
    assert body["auth"]["budget_spent"] is True
    assert ha_link.link_state(body) == ("failing", "token_rejected")


# -- the golden file admin-backend consumes ----------------------------------

def test_the_response_matches_the_golden_file(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    body = _body(client, _connected_publisher(FakeRedis()))
    rendered = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(rendered, encoding="utf-8")
    assert GOLDEN.read_text(encoding="utf-8") == rendered
    assert ha_link.link_state(body) == ("connected", "ok")


# -- a token with no URL must boot and read "not set up" -----------------------------------------

def _credentials(**env):
    from shared.ha_credentials import KeyLookup, StaticResolver, resolve

    return StaticResolver(resolve(KeyLookup.absent(), env, previous=None, service_api_key="k"))


def _latch():
    from shared import ha_auth

    return ha_auth.HAAuthLatch(None, instance="t", service_api_key="k")


def test_a_token_without_a_url_builds_no_home_assistant_client_and_no_entity_manager():
    monitor = ha_link.HALinkMonitor()
    for env in ({"HA_TOKEN": "tok"}, {"HA_URL": "https://ha.test"}, {}):
        assert _main_module._build_ha_channels(_credentials(**env), _latch(), monitor) == (None, None), env


def test_a_url_and_a_token_build_both_holders_over_one_channel_recording_into_the_monitor():
    monitor = ha_link.HALinkMonitor()
    client, entity_manager = _main_module._build_ha_channels(
        _credentials(HA_URL="https://ha.test", HA_TOKEN="tok"), _latch(), monitor,
    )
    assert client is not None and client.is_configured
    assert entity_manager is not None and entity_manager._ha._inner is client, "one client, behind the permission guard"
    assert client._channel._transport._monitor is monitor


def test_a_token_only_configuration_reports_not_configured_end_to_end(monkeypatch, client):
    """The lifespan's own wiring for a token with no URL: the holders are None, the
    publisher built from them says not configured, and /health and /admin/ha-status
    answer instead of the process failing to start."""
    _mode(monkeypatch, "enforce")
    raw, entity_manager = _main_module._build_ha_channels(_credentials(HA_TOKEN="tok"), _latch(), ha_link.HALinkMonitor())
    publisher = ha_link.create_publisher(ha_link.HALinkMonitor(), HARegistry(None), None, raw)
    assert (raw, entity_manager) == (None, None)
    _runtime.set_ha_link_publisher(publisher)
    _runtime.set_llm_router(mock.MagicMock())
    body = client.get("/admin/ha-status", headers=_key()).json()
    assert body["configured"] is False
