"""ATHENA-113b — per-service RAG URL resolution (app.utils.rag_urls) and the
Mission Control dashboard voice-health card.

dashboard.py used to probe every RAG through one shared RAG_HOST + a
hardcoded port, an OSS-First single-host assumption that breaks whenever a
RAG runs behind its own Kubernetes Service (the normal case) -- the card
then reported every RAG "unreachable" instead of "not configured". These
tests cover the resolution order (registry -> RAG_<NAME>_URL -> legacy
RAG_HOST/RAG_SERVICE_HOST -> unconfigured) and the dashboard route's
"not configured" (not "unreachable") card behavior when nothing resolves.
"""
import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from app.auth.oidc import get_current_user
from app.models import RagService
from app.routes import dashboard as dashboard_module
from app.services import health_poller as health_poller_module
from app.utils import rag_urls
from main import app


@pytest.fixture(autouse=True)
def _clean_rag_env(monkeypatch):
    for name in (
        "RAG_HOST", "RAG_SERVICE_HOST", "RAG_WEATHER_URL", "RAG_SPORTS_URL",
        "RAG_DINING_URL", "RAG_AIRPORTS_URL", "RAG_FLIGHTS_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    rag_urls._reset_legacy_warning_cache()
    yield
    rag_urls._reset_legacy_warning_cache()


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


# ---------------------------------------------------------------------------
# resolve_rag_base_url / resolve_rag_url — resolution order
# ---------------------------------------------------------------------------


def test_registry_row_wins_over_env_and_legacy(db, monkeypatch):
    monkeypatch.setenv("RAG_WEATHER_URL", "http://env-weather:9999")
    monkeypatch.setenv("RAG_HOST", "http://legacy-host")
    db.add(RagService(
        name="weather", display_name="Weather", host="registry-weather",
        port=8010, protocol="http", enabled=True,
    ))
    db.commit()

    base, source = rag_urls.resolve_rag_base_url("weather", 8010, db)
    assert source == "registry"
    assert base == "http://registry-weather:8010"


def test_registry_row_disabled_falls_through_to_env(db, monkeypatch):
    monkeypatch.setenv("RAG_WEATHER_URL", "http://env-weather:9999")
    db.add(RagService(
        name="weather", display_name="Weather", host="registry-weather",
        port=8010, protocol="http", enabled=False,
    ))
    db.commit()

    base, source = rag_urls.resolve_rag_base_url("weather", 8010, db)
    assert (base, source) == ("http://env-weather:9999", "env")


def test_env_wins_over_legacy_when_no_registry_row(db, monkeypatch):
    monkeypatch.setenv("RAG_WEATHER_URL", "http://env-weather:9999")
    monkeypatch.setenv("RAG_HOST", "http://legacy-host")

    base, source = rag_urls.resolve_rag_base_url("weather", 8010, db)
    assert (base, source) == ("http://env-weather:9999", "env")


def test_legacy_rag_host_used_when_no_registry_or_env(db, monkeypatch):
    monkeypatch.setenv("RAG_HOST", "http://legacy-host")

    base, source = rag_urls.resolve_rag_base_url("weather", 8010, db)
    assert (base, source) == ("http://legacy-host:8010", "legacy")


def test_legacy_rag_service_host_used_as_fallback(db, monkeypatch):
    monkeypatch.setenv("RAG_SERVICE_HOST", "legacy-host-no-scheme")

    base, source = rag_urls.resolve_rag_base_url("weather", 8010, db)
    assert (base, source) == ("http://legacy-host-no-scheme:8010", "legacy")


def test_legacy_fallback_warns_once_per_service(db, monkeypatch):
    import structlog

    monkeypatch.setenv("RAG_HOST", "http://legacy-host")
    with structlog.testing.capture_logs() as cap:
        rag_urls.resolve_rag_base_url("weather", 8010, db)
        rag_urls.resolve_rag_base_url("weather", 8010, db)
    warnings = [e for e in cap if e.get("event") == "rag_url_legacy_host_fallback"]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}: {warnings}"


def test_unconfigured_when_nothing_resolves(db):
    base, source = rag_urls.resolve_rag_base_url("weather", 8010, db)
    assert (base, source) == (None, "unconfigured")


def test_resolve_rag_url_appends_path():
    with patch.object(rag_urls, "resolve_rag_base_url", return_value=("http://host:8010", "env")):
        url, source = rag_urls.resolve_rag_url("weather", 8010, path="/health")
        assert (url, source) == ("http://host:8010/health", "env")


def test_resolve_rag_url_unconfigured_returns_none():
    with patch.object(rag_urls, "resolve_rag_base_url", return_value=(None, "unconfigured")):
        url, source = rag_urls.resolve_rag_url("weather", 8010, path="/health")
        assert (url, source) == (None, "unconfigured")


# ---------------------------------------------------------------------------
# check_ssrf_safe -- validates the FULL url (host, port, path, query), not
# just the host with an empty path (codex r2 delta). The path/CRLF/NUL/
# traversal check runs before any DNS resolution or host allowlist check in
# _validate_service_url, so these are host-independent: any host proves it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_ssrf_safe_blocks_traversal_in_path():
    """Real, unmocked _validate_service_url: the path/traversal check runs
    before any DNS resolution, so this is host-independent and needs no
    network access or allowlist setup to demonstrate."""
    allowed, reason = await rag_urls.check_ssrf_safe("http://weather-svc:8010/../../etc/passwd")
    assert allowed is False
    assert "traversal" in reason.lower()


@pytest.mark.asyncio
async def test_check_ssrf_safe_passes_full_path_and_query_to_validator(monkeypatch):
    """codex r2 delta: check_ssrf_safe previously always passed path="" to
    _validate_service_url regardless of what was actually in the URL, so
    its CRLF/NUL/traversal path check never saw a real request path or
    query string. A spy on the real validator proves the fix: the exact
    path AND query string of the given URL are what gets checked, not an
    empty placeholder -- urlparse itself neutralizes raw control characters
    before this point (a stdlib hardening this test doesn't need to
    re-prove), so a spy on the actual argument is the precise way to show
    check_ssrf_safe no longer discards the path."""
    captured = {}

    async def _spy_validator(host, port, path):
        captured["host"], captured["port"], captured["path"] = host, port, path
        return True, ""
    monkeypatch.setattr(health_poller_module, "_validate_service_url", _spy_validator)

    allowed, reason = await rag_urls.check_ssrf_safe("http://weather-svc:8010/weather/current?location=Denver,CO")

    assert (allowed, reason) == (True, "")
    assert captured == {"host": "weather-svc", "port": 8010, "path": "/weather/current?location=Denver,CO"}


# ---------------------------------------------------------------------------
# get_dashboard_data -- RAG rows read cached registry health_status (no live
# probe, no SSRF surface -- codex BLOCK, 2026-09-27-diagnose-athena-mission-
# control review). Gateway/Orchestrator still live-probe, now behind
# check_ssrf_safe; mocked here to isolate the RAG-cache behavior under test
# (SSRF-blocked coverage lives in test_voice_tests_ssrf_guard.py, scoped to
# the endpoints that actually issue an operator/registry-resolved live probe).
# ---------------------------------------------------------------------------


def _mock_ok_client():
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.get = AsyncMock(return_value=mock_response)
    return mock_client


@pytest.fixture(autouse=True)
def _allow_gateway_orchestrator_probe(monkeypatch):
    """Gateway/Orchestrator's SSRF gate is not what these tests exercise --
    default GATEWAY_URL/ORCHESTRATOR_URL resolve to localhost in test env,
    which the real allowlist correctly blocks. Bypass it here so these tests
    isolate the RAG-cache-read behavior; the gate itself is covered directly
    in test_voice_tests_ssrf_guard.py."""
    monkeypatch.setattr(dashboard_module, "check_ssrf_safe", AsyncMock(return_value=(True, "")))


def test_dashboard_shows_only_core_when_no_rag_rows_exist(owner_client, db):
    """No service_type='rag' rows registered at all -- the card is fully
    data-driven now (no hardcoded Weather/Sports/Dining placeholders): only
    the 2 core services appear."""
    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/dashboard")

    assert response.status_code == 200
    data = response.json()
    names = {s["name"] for s in data["services"]}
    assert names == {"Gateway", "Orchestrator"}
    assert data["voice_health"]["total"] == 2
    assert data["voice_health"]["healthy"] == 2


def test_dashboard_reads_healthy_rag_registry_row_from_cache_no_probe(owner_client, db):
    db.add(RagService(
        name="weather", display_name="Weather", host="weather-svc",
        port=8010, protocol="http", service_type="rag", enabled=True, health_status="healthy",
    ))
    db.commit()

    mock_client = _mock_ok_client()
    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=mock_client):
        response = owner_client.get("/api/dashboard")

    assert response.status_code == 200
    data = response.json()
    statuses = {s["name"]: s["status"] for s in data["voice_health"]["critical_services"]}
    assert "Weather" not in statuses  # healthy -> not in the problem list

    # No live probe for weather -- only Gateway/Orchestrator hit the client.
    called_urls = [c.args[0] if c.args else c.kwargs.get("url") for c in mock_client.get.call_args_list]
    assert not any("weather-svc" in u for u in called_urls), called_urls
    assert len(called_urls) == 2


def test_dashboard_reads_unhealthy_rag_registry_row_from_cache(owner_client, db):
    db.add(RagService(
        name="sports", display_name="Sports", host="sports-svc",
        port=8017, protocol="http", service_type="rag", enabled=True, health_status="unhealthy",
        last_error="connection_refused",
    ))
    db.commit()

    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/dashboard")

    data = response.json()
    entries = {s["name"]: s for s in data["voice_health"]["critical_services"]}
    assert entries["Sports"]["status"] == "unhealthy"
    assert entries["Sports"]["last_error"] == "connection_refused"


def test_dashboard_excludes_disabled_rag_row_entirely(owner_client, db):
    """codex follow-up: disabled RAG rows are excluded from the card
    entirely (not shown, not counted) -- distinct from ATHENA-112/113c's
    'show it labeled disabled' convention. The query itself filters
    enabled=True, so a disabled row simply never reaches the response."""
    db.add(RagService(
        name="dining", display_name="Dining", host="dining-svc",
        port=8019, protocol="http", service_type="rag", enabled=False, health_status="unhealthy",
    ))
    db.commit()

    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/dashboard")

    data = response.json()
    names = {s["name"] for s in data["services"]}
    assert "Dining" not in names
    assert data["voice_health"]["total"] == 2  # core only -- dining never counted


def test_dashboard_reads_pending_for_null_health_status_rag_row(owner_client, db):
    db.add(RagService(
        name="weather", display_name="Weather", host="weather-svc",
        port=8010, protocol="http", service_type="rag", enabled=True, health_status=None,
    ))
    db.commit()

    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/dashboard")

    data = response.json()
    statuses = {s["name"]: s["status"] for s in data["voice_health"]["critical_services"]}
    assert statuses.get("Weather") == "pending"


def test_dashboard_core_service_reads_registry_row_no_probe(owner_client, db):
    """A registered 'gateway' row is used as-is (cached health), same as a
    RAG row -- the live-probe fallback only fires when no row exists."""
    db.add(RagService(
        name="gateway", display_name="Gateway", host="athena-gateway",
        port=8000, protocol="http", enabled=True, health_status="healthy",
    ))
    db.commit()

    mock_client = _mock_ok_client()
    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=mock_client):
        response = owner_client.get("/api/dashboard")

    assert response.status_code == 200
    called_urls = [c.args[0] if c.args else c.kwargs.get("url") for c in mock_client.get.call_args_list]
    # Only Orchestrator's fallback probe fires -- Gateway has a registry row.
    assert len(called_urls) == 1
    assert "orchestrator" in called_urls[0].lower() or "8001" in called_urls[0]


def test_dashboard_registry_driven_scenario_matches_mission_control_spec(owner_client, db):
    """The exact scenario from the redesign spec: 2 core + 6 RAG rows (1
    disabled, 1 unhealthy) -> total 7 enabled (2 core + 5 enabled RAG),
    healthy 6, attention (critical_services) 1, disabled fully excluded."""
    db.add(RagService(name="gateway", display_name="Gateway", host="h", port=8000,
                       protocol="http", enabled=True, health_status="healthy"))
    db.add(RagService(name="orchestrator", display_name="Orchestrator", host="h", port=8001,
                       protocol="http", enabled=True, health_status="healthy"))
    rag_names = ["weather", "sports", "dining", "news", "stocks", "flights"]
    for i, name in enumerate(rag_names):
        is_disabled = name == "flights"
        is_unhealthy = name == "stocks"
        db.add(RagService(
            name=name, display_name=name.capitalize(), host=f"{name}-svc", port=8010 + i,
            protocol="http", service_type="rag",
            enabled=not is_disabled,
            health_status="unhealthy" if is_unhealthy else "healthy",
        ))
    db.commit()

    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=_mock_ok_client()):
        response = owner_client.get("/api/dashboard")

    assert response.status_code == 200
    data = response.json()
    assert data["voice_health"]["total"] == 7
    assert data["voice_health"]["healthy"] == 6
    assert len(data["voice_health"]["critical_services"]) == 1
    assert data["voice_health"]["critical_services"][0]["name"] == "Stocks"
    names = {s["name"] for s in data["services"]}
    assert "Flights" not in names  # disabled -- excluded entirely
    assert len(data["services"]) == 7


# ---------------------------------------------------------------------------
# get_quick_stats -- codex r2 delta High: this endpoint's Gateway/Orchestrator
# probes had no SSRF gate at all (unlike get_dashboard_data above). Uses the
# REAL check_ssrf_safe (overriding this file's autouse bypass fixture) since
# the default test-env GATEWAY_URL/ORCHESTRATOR_URL (localhost, unallowlisted)
# is itself already the "blocked" case -- no extra env setup needed.
# ---------------------------------------------------------------------------


def test_quick_stats_blocks_ssrf_unsafe_gateway_with_no_network_call(owner_client, db, monkeypatch):
    monkeypatch.setattr(dashboard_module, "check_ssrf_safe", rag_urls.check_ssrf_safe)

    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=AssertionError("must not be called when SSRF-blocked"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    import structlog
    with structlog.testing.capture_logs() as cap:
        with patch("app.routes.dashboard.httpx.AsyncClient", return_value=mock_client):
            response = owner_client.get("/api/dashboard/quick-stats")

    assert response.status_code == 200
    body = response.json()
    assert body["healthy_services"] == 0

    blocked_events = [e for e in cap if e.get("event") == "dashboard_quick_stats_ssrf_blocked"]
    assert len(blocked_events) == 2, blocked_events  # Gateway + Orchestrator
    assert all(e.get("url_status") == "ssrf_blocked" for e in blocked_events)
