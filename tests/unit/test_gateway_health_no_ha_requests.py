"""The gateway's health routes send nothing to Home Assistant.

Fail-on-call transports sit under the gateway's shared Home Assistant client
and in place of its area registry fetcher; the orchestrator client answers 200
over a MockTransport. A positive control proves the fixture can fail. The gateway
reaches the orchestrator only over HTTP, so the full chain is this test
composed with tests/unit/test_health_probes.py (which covers the orchestrator
side). Runs in its own process: it stubs prometheus_client in sys.modules.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from pathlib import Path

import httpx
import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

sys.modules.setdefault("prometheus_client", mock.MagicMock())

import gateway.main as gw  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from shared.ha_registry import HARegistry  # noqa: E402

HA_URL = "https://ha.test"
HEALTH_ROUTES = ["/health", "/health/live", "/health/ready", "/health/startup"]


@pytest.fixture
def traffic(monkeypatch):
    seen: list = []

    def ha_handler(req):
        seen.append(f"{req.method} {req.url}")
        raise AssertionError(f"unexpected request to Home Assistant: {req.method} {req.url}")

    async def registry_fetcher():
        seen.append("registry fetch")
        raise AssertionError("unexpected area registry fetch")

    def orchestrator_handler(req):
        return httpx.Response(200, json={"status": "healthy"})

    monkeypatch.setattr(gw, "ha_client", httpx.AsyncClient(transport=httpx.MockTransport(ha_handler)))
    monkeypatch.setattr(gw, "ha_registry", HARegistry(registry_fetcher))
    monkeypatch.setattr(
        gw, "orchestrator_client",
        httpx.AsyncClient(transport=httpx.MockTransport(orchestrator_handler), base_url="http://orchestrator.test"),
    )
    monkeypatch.setattr(gw, "ollama_client", mock.MagicMock(list_models=mock.AsyncMock(return_value={"models": [{}]})))
    monkeypatch.setattr(gw, "orchestrator_circuit_breaker", None)
    monkeypatch.setattr(gw, "global_rate_limiter", None)
    return seen


def test_the_fixture_can_fail_a_request_through_the_gateways_ha_client_is_counted(traffic):
    """Positive control: an empty list below means no request, not a blind fixture."""
    with pytest.raises(AssertionError):
        asyncio.run(gw.ha_client.get(f"{HA_URL}/api/states"))
    assert traffic == [f"GET {HA_URL}/api/states"]


def test_the_fixture_also_catches_a_registry_fetch(traffic):
    with pytest.raises(AssertionError):
        asyncio.run(gw.ha_registry._fetcher())
    assert traffic == ["registry fetch"]


def test_no_gateway_health_route_sends_anything_to_home_assistant(traffic):
    client = TestClient(gw.app)
    assert len(HEALTH_ROUTES) >= 4
    for route in HEALTH_ROUTES:
        response = client.get(route)
        assert response.status_code == 200, f"{route} -> {response.status_code}"
        assert traffic == [], f"{route} sent a request to Home Assistant: {traffic}"
    body = client.get("/health").json()
    assert body["orchestrator"] is True
