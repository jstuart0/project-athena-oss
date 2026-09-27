"""ATHENA-118 Phase 1: T4 — /api/service-control/{name}/{start|stop|restart}
action routes (_run_action core).

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T4.

Mocking strategy: real DB (conftest.py's db/client fixtures), real
User.has_permission()/get_permissions() against real role values (mocking
permission checks would defeat the viewer-403 assertions), and the Control
Agent faked at the httpx transport level -- both via service_managers's
module-level `_ca_transport` hook (used by gather_inventory) and via a
global httpx.AsyncClient monkeypatch (used by the dispatch helpers
docker_service_action/process_service_action, same technique as
test_control_agent_caller_headers.py) so one fake transport answers every
outbound call in a single request.
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

import httpx
import pytest
import structlog

from app.auth.oidc import get_current_user
from app.models import AuditLog, RagService, User
from app.services import service_managers as sm
from main import app
from shared.config import _clear_cache_for_tests

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _RecordingTransport:
    def __init__(self, responses: dict):
        self.requests: list[httpx.Request] = []
        self._responses = responses

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = request.url.path
        if key in self._responses:
            status, body = self._responses[key]
            return httpx.Response(status, json=body)
        return httpx.Response(404, json={})


def _patch_async_client(monkeypatch, transport: _RecordingTransport) -> None:
    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(transport.handler))
    monkeypatch.setattr(httpx, "AsyncClient", factory)


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    monkeypatch.setenv("SERVICE_API_KEY", "test-svc-key-athena-118")
    _clear_cache_for_tests()
    sm._clear_inventory_cache()
    sm._ca_transport = None
    yield
    sm._clear_inventory_cache()
    sm._ca_transport = None
    _clear_cache_for_tests()


@pytest.fixture
def owner_user(db):
    user = User(
        authentik_id="owner-athena-118",
        username="owner-athena-118",
        email="owner-athena-118@example.com",
        full_name="Owner",
        role="owner",
        active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@pytest.fixture
def owner_client(client, owner_user):
    async def _get_user():
        return owner_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def viewer_client(client, viewer_user):
    async def _get_user():
        return viewer_user
    app.dependency_overrides[get_current_user] = _get_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


@pytest.fixture
def piper_row(db) -> RagService:
    row = RagService(
        name="piper-tts",
        display_name="Piper TTS",
        host="localhost",
        port=None,
        container_name="athena-piper-tts",
        service_type="infrastructure",
        enabled=True,
        health_status="healthy",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def unmanaged_row(db) -> RagService:
    """A row whose host doesn't match the Control Agent -- resolves to
    manager='none' (no k8s in Phase 1 either)."""
    row = RagService(
        name="external-row",
        display_name="External Row",
        host="some-external-host",
        port=1234,
        service_type="rag",
        enabled=True,
        health_status="healthy",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ca_transport_for_piper(running: bool = True) -> _RecordingTransport:
    return _RecordingTransport({
        "/process/list": (200, []),
        "/docker/list": (200, [{"name": "athena-piper-tts", "status": "Up 1 day" if running else "Exited", "running": running, "ports": None}]),
        "/docker/stop/athena-piper-tts": (200, {"success": True, "message": "stopped"}),
        "/docker/start/athena-piper-tts": (200, {"success": True, "message": "started"}),
        "/docker/restart/athena-piper-tts": (200, {"success": True, "message": "restarted"}),
    })


# ---------------------------------------------------------------------------
# 20. viewer -> 403, zero transport calls, zero audit rows
# ---------------------------------------------------------------------------

def test_viewer_gets_403_zero_calls_zero_audit(viewer_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper()
    _patch_async_client(monkeypatch, transport)

    before = db.query(AuditLog).count()
    response = viewer_client.post(f"/api/service-control/{piper_row.name}/stop")

    assert response.status_code == 403
    assert transport.requests == []
    assert db.query(AuditLog).count() == before


# ---------------------------------------------------------------------------
# 21. Malformed service name -> 422, before any resolution
# ---------------------------------------------------------------------------

def test_malformed_service_name_422(owner_client, db, monkeypatch):
    transport = _ca_transport_for_piper()
    _patch_async_client(monkeypatch, transport)

    before = db.query(AuditLog).count()
    response = owner_client.post("/api/service-control/x'y/stop")

    assert response.status_code == 422
    assert transport.requests == []
    assert db.query(AuditLog).count() == before


# ---------------------------------------------------------------------------
# 22. manager='none' -> 409 action_not_available, exactly one audit row
# ---------------------------------------------------------------------------

def test_unmanaged_row_returns_409_action_not_available(owner_client, db, unmanaged_row, monkeypatch):
    transport = _ca_transport_for_piper()
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post(f"/api/service-control/{unmanaged_row.name}/stop")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "action_not_available"

    rows = db.query(AuditLog).filter(AuditLog.resource_id == unmanaged_row.id).all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_message == "action_not_available"


# ---------------------------------------------------------------------------
# 23. CA docker stop -> exactly one POST /docker/stop/athena-piper-tts,
#     one audit row action='service_stop'
# ---------------------------------------------------------------------------

def test_ca_docker_stop_dispatches_exactly_once_and_audits(owner_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post(f"/api/service-control/{piper_row.name}/stop")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    stop_calls = [r for r in transport.requests if r.url.path == "/docker/stop/athena-piper-tts"]
    assert len(stop_calls) == 1

    rows = db.query(AuditLog).filter(AuditLog.resource_id == piper_row.id).all()
    assert len(rows) == 1
    assert rows[0].action == "service_stop"
    assert rows[0].success is True


# ---------------------------------------------------------------------------
# 24. Client-supplied "target" override is ignored; dispatch resolves from DB
# ---------------------------------------------------------------------------

def test_client_supplied_target_override_ignored(owner_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post(
        f"/api/service-control/{piper_row.name}/stop",
        json={"target": "athena-admin-backend"},
    )

    assert response.status_code == 200
    stop_calls = [r for r in transport.requests if r.url.path == "/docker/stop/athena-piper-tts"]
    assert len(stop_calls) == 1
    # The (nonexistent, protected) supplied target was never touched.
    assert not any("admin-backend" in r.url.path for r in transport.requests)


# ---------------------------------------------------------------------------
# 25. is_running column is bit-for-bit unchanged after start/stop
# ---------------------------------------------------------------------------

def test_is_running_column_unchanged_after_stop(owner_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    before = piper_row.is_running
    response = owner_client.post(f"/api/service-control/{piper_row.name}/stop")
    assert response.status_code == 200

    db.refresh(piper_row)
    assert piper_row.is_running == before


# ---------------------------------------------------------------------------
# 26. Audit-write DB failure is tolerated: response stays 200, event logged
# ---------------------------------------------------------------------------

def test_audit_write_failure_does_not_break_the_response(owner_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    def _raise_commit():
        raise RuntimeError("simulated db failure on audit write")

    monkeypatch.setattr(db, "commit", _raise_commit)

    with structlog.testing.capture_logs() as logs:
        response = owner_client.post(f"/api/service-control/{piper_row.name}/stop")

    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    assert any(entry.get("event") == "service_control_audit_failed" for entry in logs)


# ---------------------------------------------------------------------------
# 27. old_value captured pre-action; new_value carries replicas_after key
# ---------------------------------------------------------------------------

def test_audit_old_value_is_pre_action_state(owner_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post(f"/api/service-control/{piper_row.name}/stop")
    assert response.status_code == 200

    audit = db.query(AuditLog).filter(AuditLog.resource_id == piper_row.id).one()
    assert audit.old_value["run_state"] == "running"
    assert "replicas_after" in audit.new_value
    assert audit.new_value["replicas_after"] is None  # no k8s manager in Phase 1
    assert audit.old_value != audit.new_value
