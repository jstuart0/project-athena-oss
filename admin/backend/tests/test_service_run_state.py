"""ATHENA-118 (campaign 2026-09-27-deliver-athena-service-control-k8s, Phase 1): T1.

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T1.

Mocking strategy: derive_run_state / normalized_health_status are pure
functions, called directly. The envelope + to_dict() cases use a real
SQLite-in-memory DB via the existing db/client fixture pattern
(conftest.py:44-102) — no mock hides the ORM round trip.
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

import pytest

from app.models import RagService
from app.utils.service_state import derive_run_state, normalized_health_status


# ---------------------------------------------------------------------------
# 1. derive_run_state — the full D2 table, >= 14 named cases
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "enabled,health_status,k8s_replicas,expected",
    [
        pytest.param(False, "healthy", None, "disabled", id="disabled_row_healthy_stays_disabled"),
        pytest.param(False, "unhealthy", None, "disabled", id="disabled_row_unhealthy_stays_disabled"),
        pytest.param(False, None, 5, "disabled", id="disabled_row_with_replicas_stays_disabled"),
        pytest.param(False, "bogus", 0, "disabled", id="disabled_row_zero_replicas_stays_disabled"),
        pytest.param(True, "healthy", 0, "stopped", id="k8s_zero_replicas_overrides_stale_healthy"),
        pytest.param(True, "unconfigured", 0, "stopped", id="k8s_zero_replicas_overrides_unconfigured"),
        pytest.param(True, "healthy", None, "running", id="healthy_no_k8s_is_running"),
        pytest.param(True, "healthy", 1, "running", id="healthy_one_replica_is_running"),
        pytest.param(True, "unconfigured", None, "running", id="unconfigured_no_health_endpoint_is_running"),
        pytest.param(True, "unconfigured", 3, "running", id="unconfigured_with_replicas_is_running"),
        pytest.param(True, "unhealthy", None, "stopped", id="unhealthy_is_stopped"),
        pytest.param(True, None, None, "stopped", id="never_polled_is_stopped"),
        pytest.param(True, "bogus-unknown-string", None, "stopped", id="unrecognized_poller_string_fails_closed"),
        pytest.param(True, "unhealthy", 2, "stopped", id="unhealthy_with_replicas_is_stopped"),
    ],
)
def test_derive_run_state_full_table(enabled, health_status, k8s_replicas, expected):
    assert derive_run_state(enabled, health_status, k8s_replicas) == expected


# ---------------------------------------------------------------------------
# 2. RagService.to_dict()['is_running'] is derived, not the column
# ---------------------------------------------------------------------------

def test_to_dict_is_running_derived_not_column(db):
    unhealthy_but_flagged_running = RagService(
        name="unhealthy-flagged-running",
        display_name="Unhealthy Flagged Running",
        host="localhost",
        port=9001,
        enabled=True,
        is_running=True,  # stale column value — must be ignored
        health_status="unhealthy",
    )
    healthy_but_flagged_stopped = RagService(
        name="healthy-flagged-stopped",
        display_name="Healthy Flagged Stopped",
        host="localhost",
        port=9002,
        enabled=True,
        is_running=False,  # stale column value — must be ignored
        health_status="healthy",
    )
    db.add_all([unhealthy_but_flagged_running, healthy_but_flagged_stopped])
    db.commit()

    assert unhealthy_but_flagged_running.to_dict()["is_running"] is False
    assert healthy_but_flagged_stopped.to_dict()["is_running"] is True


# ---------------------------------------------------------------------------
# 3. Envelope counts sum correctly AND each named row is individually correct
# ---------------------------------------------------------------------------

def test_envelope_counts_and_per_row_run_state(client, db):
    disabled_row = RagService(
        name="disabled-row", display_name="Disabled Row", host="localhost",
        port=9101, enabled=False, health_status="healthy",
    )
    stopped_row = RagService(
        name="stopped-row", display_name="Stopped Row", host="localhost",
        port=9102, enabled=True, health_status="unhealthy",
    )
    healthy_row = RagService(
        name="healthy-row", display_name="Healthy Row", host="localhost",
        port=9103, enabled=True, health_status="healthy",
    )
    db.add_all([disabled_row, stopped_row, healthy_row])
    db.commit()

    response = client.get("/api/service-control")
    assert response.status_code == 200
    data = response.json()

    counts = data["counts"]
    assert counts["running"] + counts["stopped"] + counts["disabled"] == len(data["services"])
    assert counts == {"running": 1, "stopped": 1, "disabled": 1}

    by_name = {row["name"]: row for row in data["services"]}
    assert by_name["disabled-row"]["run_state"] == "disabled"
    assert by_name["stopped-row"]["run_state"] == "stopped"
    assert by_name["healthy-row"]["run_state"] == "running"


def test_envelope_zero_rows_no_divide_by_zero(client):
    """[tessa-added boundary case] Zero registry rows: counts are all zero,
    and the envelope must not ship a NaN/undefined ratio anywhere."""
    response = client.get("/api/service-control")
    assert response.status_code == 200
    data = response.json()
    assert data["services"] == []
    assert data["counts"] == {"running": 0, "stopped": 0, "disabled": 0}
    assert "NaN" not in response.text


# ---------------------------------------------------------------------------
# normalized_health_status — shared with service_registry.py (D3)
# ---------------------------------------------------------------------------

def test_normalized_health_status_disabled_overrides_stale_cache():
    assert normalized_health_status(False, "healthy") == "disabled"


def test_normalized_health_status_never_polled_is_pending():
    assert normalized_health_status(True, None) == "pending"


def test_normalized_health_status_passes_through_real_value():
    assert normalized_health_status(True, "healthy") == "healthy"


# ---------------------------------------------------------------------------
# codex diff review r1 Medium #5: ServiceResponse was silently dropping
# protocol/endpoint_url/health_status/health_message/last_response_time_ms
# via `extra="ignore"` even though RagService.to_dict() already produces
# them -- service-control.js's TCP-vs-HTTP row editor and health-status
# rendering both read these fields from the envelope directly.
# ---------------------------------------------------------------------------

def test_envelope_carries_protocol_and_health_fields_for_a_tcp_row(client, db):
    tcp_row = RagService(
        name="redis-tcp", display_name="Redis", host="redis.athena-prod.svc",
        port=6379, protocol="tcp", enabled=True, health_status="healthy",
        health_message="tcp connect ok", last_response_time_ms=12,
    )
    db.add(tcp_row)
    db.commit()

    response = client.get("/api/service-control")
    assert response.status_code == 200
    row = next(r for r in response.json()["services"] if r["name"] == "redis-tcp")

    assert row["protocol"] == "tcp"
    assert row["health_status"] == "healthy"
    assert row["health_message"] == "tcp connect ok"
    assert row["last_response_time_ms"] == 12
    assert row["endpoint_url"] is None
