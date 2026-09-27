"""ATHENA-113c — GET /api/status derives service health from the service
registry instead of probing a hardcoded "Mac Studio"/"Mac Mini" topology.

dick's investigation found the System Configuration page's Gateway/
Orchestrator/Ollama cards permanently "Offline": get_system_status probed
MAC_STUDIO_IP/MAC_MINI_IP directly for a pre-Kubernetes deployment topology
that no longer exists. The registry (athena_service_registry, same cached
health_status GET /api/service-registry/services reads) is now the source
of truth; Gateway/Orchestrator/Ollama/SearXNG (no registry row by default)
are checked directly only when the registry has no row for them.

healthy_services/overall_health reuse service_registry._overall_health
(ATHENA-112's enabled-only rollup) rather than a second implementation.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth.oidc import get_current_user
from app.models import RagService
from main import app


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


def _mock_ok_client():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json = MagicMock(return_value={})
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


def test_registry_rows_populate_services_with_no_direct_probe(owner_client, db):
    db.add(RagService(
        name="weather", display_name="Weather", host="weather-svc",
        port=8010, protocol="http", enabled=True, health_status="healthy",
    ))
    db.add(RagService(
        name="sports", display_name="Sports", host="sports-svc",
        port=8017, protocol="http", enabled=True, health_status="unhealthy",
        last_error="connection_refused",
    ))
    db.commit()

    with patch("main.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/status")

    assert response.status_code == 200
    data = response.json()
    by_name = {s["name"]: s for s in data["services"]}
    assert by_name["weather"]["healthy"] is True
    assert by_name["weather"]["status"] == "healthy"
    assert by_name["sports"]["healthy"] is False
    assert by_name["sports"]["status"] == "unhealthy"
    assert by_name["sports"]["error"] == "connection_refused"


def test_disabled_row_reads_disabled_and_is_excluded_from_counts(owner_client, db):
    db.add(RagService(
        name="weather", display_name="Weather", host="weather-svc",
        port=8010, protocol="http", enabled=True, health_status="healthy",
    ))
    db.add(RagService(
        name="tesla", display_name="Tesla", host="tesla-svc",
        port=8028, protocol="http", enabled=False, health_status="unhealthy",
    ))
    db.commit()

    with patch("main.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/status")

    data = response.json()
    by_name = {s["name"]: s for s in data["services"]}
    assert by_name["tesla"]["status"] == "disabled"
    assert by_name["tesla"]["healthy"] is False

    # tesla is excluded from the rollup entirely -- its stale 'unhealthy'
    # must not appear. weather + the direct gateway/orchestrator/ollama
    # checks (mocked healthy) plus searxng (unconfigured in tests, so
    # unhealthy-but-not-network-probed) are the only rollup members: 4/5
    # healthy, never counting tesla as a 6th (disabled) entry.
    assert data["total_services"] == 5
    assert data["healthy_services"] == 4
    assert data["overall_health"] == "degraded"
    assert not any(s["name"] == "tesla" for s in data["services"] if s["status"] != "disabled")


def test_null_health_status_reads_pending(owner_client, db):
    db.add(RagService(
        name="weather", display_name="Weather", host="weather-svc",
        port=8010, protocol="http", enabled=True, health_status=None,
    ))
    db.commit()

    with patch("main.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/status")

    by_name = {s["name"]: s for s in response.json()["services"]}
    assert by_name["weather"]["status"] == "pending"
    assert by_name["weather"]["healthy"] is False


def test_registry_row_for_gateway_skips_the_direct_probe(owner_client, db):
    db.add(RagService(
        name="gateway", display_name="Gateway", host="athena-gateway",
        port=8000, protocol="http", enabled=True, health_status="healthy",
    ))
    db.commit()

    mock_client = _mock_ok_client()
    with patch("main.httpx.AsyncClient", return_value=mock_client):
        response = owner_client.get("/api/status")

    assert response.status_code == 200
    by_name = {s["name"]: s for s in response.json()["services"]}
    assert by_name["gateway"]["status"] == "healthy"
    # Only ollama/searxng/orchestrator direct checks fire (gateway's registry
    # row satisfies it) -- searxng short-circuits before any network call
    # (get_config().searxng_base_url unset in tests), so at most 2 HTTP GETs.
    assert mock_client.get.call_count <= 2


def test_no_registry_rows_falls_back_to_direct_checks_for_all_four(owner_client, db):
    with patch("main.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/status")

    assert response.status_code == 200
    data = response.json()
    names = {s["name"] for s in data["services"]}
    assert {"gateway", "orchestrator", "ollama"}.issubset(names)
    assert data["total_services"] >= 3


def test_no_healthy_services_reads_unhealthy_not_a_stale_critical_label(owner_client, db):
    db.add(RagService(
        name="weather", display_name="Weather", host="weather-svc",
        port=8010, protocol="http", enabled=True, health_status="unhealthy",
    ))
    db.commit()

    mock_response = MagicMock()
    mock_response.status_code = 503
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)

    with patch("main.httpx.AsyncClient", return_value=mock_client):
        response = owner_client.get("/api/status")

    data = response.json()
    assert data["healthy_services"] == 0
    assert data["overall_health"] == "unhealthy"
