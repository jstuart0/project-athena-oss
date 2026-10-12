"""Every status-serving read of a registry row says the same thing.

A row that is switched off must never read healthy, and a row the checker has
not reached recently must read `stale`, on every payload the admin pages use:
the registry list and detail, Service Control's envelope and Mission Control's
dashboard. The raw cached value stays available as `last_observed_health`
(None when the row is off), and `run_state` still comes from the raw column.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.auth.oidc import get_current_user
from app.models import RagService
from app.routes import dashboard as dashboard_module
from main import app

LONG_AGO = datetime.now(timezone.utc) - timedelta(days=16)


def _recent():
    return datetime.now(timezone.utc) - timedelta(seconds=10)


@pytest.fixture
def owner_client(client, test_user):
    async def _get_user():
        return test_user

    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture(autouse=True)
def _no_ssrf_block(monkeypatch):
    monkeypatch.setattr(dashboard_module, "check_ssrf_safe", AsyncMock(return_value=(True, "")))


def _mock_http():
    response = MagicMock()
    response.status_code = 200
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=response)
    return client


def _add(db, name, *, enabled=True, health="healthy", checked=None, service_type="rag", error=None):
    db.add(RagService(
        name=name, display_name=name.title(), host=f"{name}-svc", port=8000, protocol="http",
        service_type=service_type, enabled=enabled, health_status=health, last_health_check=checked,
        last_error=error,
    ))
    db.commit()


def _service_rows(owner_client, path, key="services"):
    body = owner_client.get(path).json()
    return {row["name"]: row for row in body[key]}


# -- one disabled row, four payloads -------------------------------------------------

DISABLED_PAYLOADS = [
    "/api/service-registry/services",
    "/api/service-registry/services/{name}",
    "/api/service-control",
]


@pytest.mark.parametrize("path", DISABLED_PAYLOADS)
def test_a_switched_off_row_never_reads_healthy(owner_client, db, path):
    _add(db, "ha-api", enabled=False, health="healthy", checked=LONG_AGO, error="timeout:old failure")
    if "{name}" in path:
        row = owner_client.get(path.format(name="ha-api")).json()
    else:
        row = _service_rows(owner_client, path)["ha-api"]
    assert row["health_status"] == "disabled"
    assert row["monitoring"] == "off"
    assert row["last_observed_health"] is None
    assert "healthy" not in {row["health_status"], row["monitoring"]}


def test_the_dashboard_carries_a_disabled_row_as_disabled_and_leaves_it_out_of_the_counts(owner_client, db):
    _add(db, "gateway", enabled=False, health="healthy", checked=LONG_AGO, service_type="core")
    _add(db, "orchestrator", health="healthy", checked=_recent(), service_type="core")
    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=_mock_http()):
        body = owner_client.get("/api/dashboard").json()
    statuses = {s["name"]: s["status"] for s in body["services"]}
    assert statuses["Gateway"] == "disabled"
    assert body["voice_health"]["total"] == 1 and body["voice_health"]["healthy"] == 1
    assert body["voice_health"]["critical_services"] == []


# -- run_state and the audit still read the raw column ---------------------------------

def test_a_stale_row_keeps_its_run_state_from_the_raw_column(owner_client, db):
    _add(db, "weather", health="healthy", checked=LONG_AGO)
    row = _service_rows(owner_client, "/api/service-control")["weather"]
    assert row["health_status"] == "stale"
    assert row["monitoring"] == "stale"
    assert row["last_observed_health"] == "healthy"
    assert row["run_state"] == "running"


def test_a_live_row_reads_its_cached_status(owner_client, db):
    _add(db, "weather", health="healthy", checked=_recent())
    _add(db, "sports", health="unhealthy", checked=_recent(), error="timeout:slow")
    rows = _service_rows(owner_client, "/api/service-registry/services")
    assert (rows["weather"]["health_status"], rows["weather"]["monitoring"]) == ("healthy", "live")
    assert (rows["sports"]["health_status"], rows["sports"]["monitoring"]) == ("unhealthy", "live")
    assert rows["weather"]["last_observed_health"] == "healthy"


def test_a_never_checked_row_reads_pending(owner_client, db):
    _add(db, "weather", health="healthy", checked=None)
    row = _service_rows(owner_client, "/api/service-registry/services")["weather"]
    assert (row["health_status"], row["monitoring"]) == ("pending", "pending")


# -- the roll-up ------------------------------------------------------------------------

def test_overall_health_is_unknown_when_every_enabled_row_is_stale(owner_client, db):
    _add(db, "weather", health="healthy", checked=LONG_AGO)
    _add(db, "sports", health="healthy", checked=LONG_AGO)
    _add(db, "off", enabled=False, health="healthy", checked=_recent())
    body = owner_client.get("/api/service-registry/services").json()
    assert body["overall_health"] == "unknown"
    assert body["healthy_services"] == 0


def test_one_live_healthy_row_beside_a_stale_one_is_degraded(owner_client, db):
    _add(db, "weather", health="healthy", checked=_recent())
    _add(db, "sports", health="healthy", checked=LONG_AGO)
    body = owner_client.get("/api/service-registry/services").json()
    assert body["overall_health"] == "degraded"
    assert body["healthy_services"] == 1


def test_all_live_healthy_is_healthy(owner_client, db):
    _add(db, "weather", health="healthy", checked=_recent())
    assert owner_client.get("/api/service-registry/services").json()["overall_health"] == "healthy"


# -- Mission Control (dashboard) and the pending actions ---------------------------------

def _dashboard(owner_client):
    with patch("app.routes.dashboard.httpx.AsyncClient", return_value=_mock_http()):
        return owner_client.get("/api/dashboard").json()


def _core_rows(db):
    _add(db, "gateway", health="healthy", checked=_recent(), service_type="core")
    _add(db, "orchestrator", health="healthy", checked=_recent(), service_type="core")


def test_stale_rows_leave_the_health_count_and_are_reported_on_their_own(owner_client, db):
    _core_rows(db)
    _add(db, "weather", health="healthy", checked=_recent())
    _add(db, "sports", health="healthy", checked=LONG_AGO)
    _add(db, "news", health="unhealthy", checked=LONG_AGO)
    voice = _dashboard(owner_client)["voice_health"]
    assert (voice["healthy"], voice["total"], voice["stale_count"]) == (3, 3, 2)
    assert voice["critical_services"] == []
    statuses = {s["name"]: s["status"] for s in _dashboard(owner_client)["services"]}
    assert statuses["Sports"] == "stale" and statuses["News"] == "stale"


def test_a_stale_row_and_a_disabled_row_produce_no_pending_action(owner_client, db):
    _core_rows(db)
    _add(db, "sports", health="unhealthy", checked=LONG_AGO)
    _add(db, "tesla", enabled=False, health="unhealthy", checked=LONG_AGO)
    _add(db, "fresh", health="healthy", checked=None)  # pending
    body = _dashboard(owner_client)
    assert [a for a in body["pending_actions"] if a["type"] == "unhealthy_services"] == []
    assert body["voice_health"]["critical_services"] == []


def test_a_live_unhealthy_row_does_produce_a_pending_action(owner_client, db):
    _core_rows(db)
    _add(db, "sports", health="unhealthy", checked=_recent(), error="timeout:slow")
    body = _dashboard(owner_client)
    actions = [a for a in body["pending_actions"] if a["type"] == "unhealthy_services"]
    assert len(actions) == 1 and actions[0]["count"] == 1
    assert [c["name"] for c in body["voice_health"]["critical_services"]] == ["Sports"]


def test_a_live_unconfigured_row_still_needs_attention(owner_client, db):
    _core_rows(db)
    _add(db, "tesla", health="unconfigured", checked=_recent())
    names = [c["name"] for c in _dashboard(owner_client)["voice_health"]["critical_services"]]
    assert names == ["Tesla"]


def test_with_no_live_row_the_voice_health_has_no_percentage_so_the_page_can_show_it_neutral(owner_client, db):
    _add(db, "gateway", health="healthy", checked=LONG_AGO, service_type="core")
    _add(db, "orchestrator", health="healthy", checked=LONG_AGO, service_type="core")
    _add(db, "weather", health="healthy", checked=LONG_AGO)
    voice = _dashboard(owner_client)["voice_health"]
    assert (voice["total"], voice["healthy"], voice["stale_count"]) == (0, 0, 3)
    assert voice["percentage"] is None
    assert voice["critical_services"] == []
    assert set(voice["history"]) == {0}


def test_with_a_live_row_the_percentage_is_a_number(owner_client, db):
    _core_rows(db)
    assert _dashboard(owner_client)["voice_health"]["percentage"] == 100
