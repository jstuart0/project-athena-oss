"""GET /admin/ha-status: the orchestrator's Home Assistant link view for admin.

Runs in its own process: it imports orchestrator.main behind a stubbed
config_loader (see test_health_probes.py for why). The response is enums,
counts and timestamps only; the golden file tests/fixtures/ha_link/
admin_ha_status_v1.json is what admin-backend's tests parse, so the two sides
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
GOLDEN = FIXTURES / "ha_link" / "admin_ha_status_v1.json"
HOUSE = json.loads((FIXTURES / "ha_registry" / "house_a.json").read_text(encoding="utf-8"))
AREA_NAMES = [
    a["name"] for a in HOUSE["areas"]
    if isinstance(a, dict) and isinstance(a.get("name"), str) and len(a["name"]) >= 4  # digit-only names would match timestamps
]
TOKEN = "sekret-token-123"
HOST = "ha.test"
T0 = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)

TOP_KEYS = {"configured", "rest", "registry", "coverage", "coverage_reason", "replicas_reporting", "observed_at"}
REST_KEYS = {"last_outcome", "last_outcome_at", "last_success_at", "consecutive_failures"}
REGISTRY_KEYS = {"state", "areas", "last_success_at", "last_failure_at", "last_failure_class"}


class FakeRedis:
    def __init__(self):
        self.h = {}

    async def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    async def expire(self, key, seconds):
        pass

    async def hgetall(self, key):
        return dict(self.h.get(key, {}))

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


def test_not_configured_and_registry_missing_shapes(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    publisher = ha_link.HALinkPublisher(
        ha_link.HALinkMonitor(), None, None, "pod-test", configured=False, wall=lambda: T0
    )
    body = _body(client, publisher)
    assert body["configured"] is False
    assert body["registry"] == {
        "state": "disabled", "areas": None, "last_success_at": None,
        "last_failure_at": None, "last_failure_class": None,
    }
    assert body["rest"] == {
        "last_outcome": None, "last_outcome_at": None, "last_success_at": None, "consecutive_failures": 0,
    }
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


# -- the golden file admin-backend consumes ----------------------------------

def test_the_response_matches_the_golden_file(monkeypatch, client):
    _mode(monkeypatch, "enforce")
    body = _body(client, _connected_publisher(FakeRedis()))
    rendered = json.dumps(body, indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(rendered, encoding="utf-8")
    assert GOLDEN.read_text(encoding="utf-8") == rendered
    assert ha_link.link_state(body) == ("connected", "ok")
