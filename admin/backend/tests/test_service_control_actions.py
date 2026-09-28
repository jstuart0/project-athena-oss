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
from fastapi import HTTPException

from app.models import AuditLog, RagService
from app.routes import service_control
from app.services import service_managers as sm
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
    # piper_row is service_type="infrastructure" -- codex diff review r1
    # Critical #1 makes any non-rag CA-managed row critical (same fail-safe
    # as the Kubernetes path), so this now requires the typed confirmation.
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post(
        f"/api/service-control/{piper_row.name}/stop",
        json={"confirm_name": "athena-piper-tts"},
    )

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
        json={"target": "athena-admin-backend", "confirm_name": "athena-piper-tts"},
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
    response = owner_client.post(
        f"/api/service-control/{piper_row.name}/stop",
        json={"confirm_name": "athena-piper-tts"},
    )
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
        response = owner_client.post(
            f"/api/service-control/{piper_row.name}/stop",
            json={"confirm_name": "athena-piper-tts"},
        )

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

    response = owner_client.post(
        f"/api/service-control/{piper_row.name}/stop",
        json={"confirm_name": "athena-piper-tts"},
    )
    assert response.status_code == 200

    audit = db.query(AuditLog).filter(AuditLog.resource_id == piper_row.id).one()
    assert audit.old_value["run_state"] == "running"
    assert "replicas_after" in audit.new_value
    assert audit.new_value["replicas_after"] is None  # no k8s manager in Phase 1
    assert audit.old_value != audit.new_value


# ---------------------------------------------------------------------------
# tessa mid-build (High #1): mutation-tested findings -- the owner-gate/
# availability order, and accepting the row's own name as a valid confirm,
# both left every T4 test green. These drive a genuinely CRITICAL
# resolution (kind='ollama', critical under any manager per D9/D12) through
# _run_action directly.
#
# Why _run_action directly and not TestClient.post("/api/service-control/
# ollama/stop"): that URL is currently served by the DEDICATED start_ollama/
# stop_ollama/restart_ollama handlers (tessa mid-build #2 -- /ollama/* is
# registered before /{service_name}/* specifically so it ISN'T shadowed),
# and those handlers don't call _run_action until Phase 3 rewrites them
# (D12, plan step 20). _run_action is exactly the function those routes
# will call then; a row named 'ollama' is the only row shape that resolves
# to kind='ollama' via the CA host-match fallback today, so this is the
# real function under real conditions, just not reachable via that URL yet.
# The k8s-alias case below (once Phase 2 lands later in this same file)
# covers the same two mutations end-to-end over TestClient/HTTP.
# ---------------------------------------------------------------------------

@pytest.fixture
def ollama_row(db) -> RagService:
    row = RagService(
        name="ollama", display_name="Ollama Server", host="localhost", port=None,
        container_name=None, service_type="infrastructure", enabled=True,
        health_status="healthy",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_ollama_kind_operator_without_manage_infrastructure_gets_403_not_409(
    db, operator_user, ollama_row, monkeypatch
):
    """Mutation 1 (order swap): an operator has 'write' but not
    'manage_infrastructure'. If the availability check ran before the
    owner gate, a valid action (stop IS in native_actions for kind=
    'ollama') would sail through to the confirm check instead of being
    refused at the gate. The correct order refuses here regardless of
    action validity."""
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    body = service_control.ServiceActionRequest(confirm_name="ollama")
    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("ollama", "stop", body, None, db, operator_user)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"error": "insufficient_role"}

    ollama_calls = [r for r in transport.requests if r.url.path.startswith("/ollama/")]
    assert ollama_calls == []

    rows = db.query(AuditLog).filter(AuditLog.resource_id == ollama_row.id).all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_message == "insufficient_role"


@pytest.mark.asyncio
async def test_ollama_kind_owner_wrong_confirm_value_rejected(db, test_user, ollama_row, monkeypatch):
    """Mutation 2 (accept row name instead of resolved target): the row's
    own `display_name` ('Ollama Server') must NOT satisfy the typed
    confirm -- only the resolved target's identity ('ollama') may."""
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    wrong_body = service_control.ServiceActionRequest(confirm_name="Ollama Server")
    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("ollama", "stop", wrong_body, None, db, test_user)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"error": "confirmation_required"}
    ollama_calls = [r for r in transport.requests if r.url.path.startswith("/ollama/")]
    assert ollama_calls == []


@pytest.mark.asyncio
async def test_ollama_kind_owner_no_confirm_gets_409(db, test_user, ollama_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    empty_body = service_control.ServiceActionRequest()
    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("ollama", "stop", empty_body, None, db, test_user)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"error": "confirmation_required"}


@pytest.mark.asyncio
async def test_ollama_kind_owner_correct_confirm_value_proceeds(db, test_user, ollama_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    transport._responses["/ollama/stop"] = (200, {"success": True, "message": "stopped"})
    _patch_async_client(monkeypatch, transport)

    correct_body = service_control.ServiceActionRequest(confirm_name="ollama")
    result = await service_control._run_action("ollama", "stop", correct_body, None, db, test_user)

    assert result.success is True
    ollama_calls = [r for r in transport.requests if r.url.path == "/ollama/stop"]
    assert len(ollama_calls) == 1


# ---------------------------------------------------------------------------
# T8 (Phase 2) route-level: k8s dispatch through _run_action, end to end
# over the real HTTP surface. This is the "once P2 lands" half of tessa's
# mid-build High #1 -- and unlike the ollama-kind case above, native_actions
# for a k8s row IS state-dependent, so this genuinely discriminates the
# owner-gate/availability ORDER mutation (an operator hitting 'stop' on a
# Deployment already at 0 replicas must get 403, never 409, even though
# 'stop' is also unavailable there).
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402
from datetime import datetime as _datetime, timedelta as _timedelta, timezone as _timezone  # noqa: E402

from app.services import k8s_control as kc  # noqa: E402
from app.services.service_control_settings import acquire_lease, release_lease, remember_replicas  # noqa: E402
from tests.conftest import TestingSessionLocal  # noqa: E402


class _K8sFakeTransport:
    def __init__(self, deployments):
        self.deployments = deployments  # name -> {"replicas": n, "ready": n}
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/deployments"):
            items = [
                {
                    "metadata": {"name": name},
                    "spec": {"replicas": d["replicas"]},
                    "status": {"readyReplicas": d["ready"]},
                }
                for name, d in self.deployments.items()
            ]
            return httpx.Response(200, json={"items": items})

        name = path.split("/deployments/")[1].split("/scale")[0]
        if request.method == "GET":
            d = self.deployments[name]
            return httpx.Response(200, json={
                "spec": {"replicas": d["replicas"]},
                "status": {"replicas": d["replicas"]},
                "metadata": {"resourceVersion": f"rv-{name}"},
            })
        if request.method == "PATCH":
            body = _json.loads(request.content)
            n = body["spec"]["replicas"]
            self.deployments[name]["replicas"] = n
            self.deployments[name]["ready"] = n
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})


@pytest.fixture
def k8s_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_CONTROL_K8S_ENABLED", "true")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _clear_cache_for_tests()
    token_path = tmp_path / "token"
    token_path.write_text("tok-k8s")
    monkeypatch.setattr(kc, "TOKEN_PATH_DEFAULT", str(token_path))
    monkeypatch.setattr(kc, "NAMESPACE_PATH_DEFAULT", str(tmp_path / "namespace"))
    monkeypatch.setattr(service_control, "LEASE_SESSION_FACTORY", TestingSessionLocal)

    def _apply(deployments):
        transport = _K8sFakeTransport(deployments)
        monkeypatch.setattr(kc, "_test_transport", httpx.MockTransport(transport.handler))
        kc._clear_client_cache()
        return transport

    yield _apply
    kc._clear_client_cache()
    _clear_cache_for_tests()


@pytest.fixture
def orchestrator_row(db) -> RagService:
    row = RagService(
        name="orchestrator", display_name="Orchestrator", host="athena-orchestrator",
        port=None, container_name=None, service_type="core", enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.fixture
def tesla_row(db) -> RagService:
    row = RagService(
        name="tesla-rag", display_name="Tesla RAG", host="athena-rag-tesla",
        port=None, container_name=None, service_type="rag", enabled=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@pytest.mark.asyncio
async def test_k8s_critical_owner_no_confirm_gets_409_zero_patch(db, test_user, orchestrator_row, k8s_env):
    transport = k8s_env({"athena-orchestrator": {"replicas": 2, "ready": 2}})
    body = service_control.ServiceActionRequest()

    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("orchestrator", "stop", body, None, db, test_user)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"error": "confirmation_required"}
    patch_calls = [r for r in transport.requests if r.method == "PATCH"]
    assert patch_calls == []


@pytest.mark.asyncio
async def test_k8s_critical_owner_correct_confirm_dispatches(db, test_user, orchestrator_row, k8s_env):
    transport = k8s_env({"athena-orchestrator": {"replicas": 2, "ready": 2}})
    body = service_control.ServiceActionRequest(confirm_name="athena-orchestrator")

    result = await service_control._run_action("orchestrator", "stop", body, None, db, test_user)

    assert result.success is True
    patch_calls = [r for r in transport.requests if r.method == "PATCH"]
    assert len(patch_calls) == 1
    assert _json.loads(patch_calls[0].content)["spec"]["replicas"] == 0


@pytest.mark.asyncio
async def test_k8s_alias_row_confirm_must_be_resolved_target_not_alias_name(db, test_user, k8s_env):
    """D4.4 / mozart r3a: an alias row ('obscure') whose host resolves to
    the critical athena-orchestrator Deployment must require typing the
    TARGET's name, not the alias row's own name."""
    alias_row = RagService(
        name="obscure", display_name="Obscure Alias", host="athena-orchestrator",
        port=None, container_name=None, service_type="core", enabled=True,
    )
    db.add(alias_row)
    db.commit()
    db.refresh(alias_row)

    transport = k8s_env({"athena-orchestrator": {"replicas": 2, "ready": 2}})

    wrong_body = service_control.ServiceActionRequest(confirm_name="obscure")
    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("obscure", "stop", wrong_body, None, db, test_user)
    assert exc_info.value.status_code == 409
    assert [r for r in transport.requests if r.method == "PATCH"] == []

    correct_body = service_control.ServiceActionRequest(confirm_name="athena-orchestrator")
    result = await service_control._run_action("obscure", "stop", correct_body, None, db, test_user)
    assert result.success is True


@pytest.mark.asyncio
async def test_k8s_operator_without_manage_infrastructure_gets_403_even_when_action_unavailable(
    db, operator_user, orchestrator_row, k8s_env
):
    """Mutation 1, now genuinely discriminating: 'stop' on a Deployment
    ALREADY at 0 replicas is not in native_actions (native_actions ==
    ['start'] only) -- if the availability check ran before the owner gate,
    this would 409 action_not_available instead of 403 insufficient_role."""
    transport = k8s_env({"athena-orchestrator": {"replicas": 0, "ready": 0}})
    body = service_control.ServiceActionRequest(confirm_name="athena-orchestrator")

    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("orchestrator", "stop", body, None, db, operator_user)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"error": "insufficient_role"}
    assert [r for r in transport.requests if r.method == "PATCH"] == []


@pytest.mark.asyncio
async def test_k8s_operator_can_control_non_critical_rag_row(db, operator_user, tesla_row, k8s_env):
    transport = k8s_env({"athena-rag-tesla": {"replicas": 1, "ready": 1}})
    body = service_control.ServiceActionRequest()

    result = await service_control._run_action("tesla-rag", "stop", body, None, db, operator_user)

    assert result.success is True
    assert len([r for r in transport.requests if r.method == "PATCH"]) == 1


@pytest.mark.asyncio
async def test_k8s_busy_lease_gives_409_action_in_progress_zero_patch_audited(
    db, test_user, tesla_row, k8s_env
):
    transport = k8s_env({"athena-rag-tesla": {"replicas": 1, "ready": 1}})

    held_lease = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "restart", target_replicas=0)

    body = service_control.ServiceActionRequest()
    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("tesla-rag", "stop", body, None, db, test_user)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"error": "action_in_progress"}
    assert [r for r in transport.requests if r.method == "PATCH"] == []

    rows = db.query(AuditLog).filter(AuditLog.resource_id == tesla_row.id).all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_message == "action_in_progress"

    release_lease(TestingSessionLocal, held_lease)


@pytest.mark.asyncio
async def test_k8s_target_collision_blocks_both_rows_in_envelope(db, test_user, k8s_env):
    row1 = RagService(
        name="orchestrator-a", display_name="Orchestrator A", host="athena-orchestrator",
        port=None, service_type="core", enabled=True,
    )
    row2 = RagService(
        name="orchestrator-b", display_name="Orchestrator B", host="athena-orchestrator.athena-prod.svc",
        port=None, service_type="core", enabled=True,
    )
    db.add_all([row1, row2])
    db.commit()

    k8s_env({"athena-orchestrator": {"replicas": 2, "ready": 2}})

    # codex diff review r1 Low #8: this test previously stopped at the
    # precondition (both rows resolve to the same target) without ever
    # calling the envelope the collision guard actually lives in --
    # exercise GET /api/service-control (list_services) directly.
    envelope = await service_control.list_services(None, db, test_user)
    row1_out = next(r for r in envelope.services if r.name == "orchestrator-a")
    row2_out = next(r for r in envelope.services if r.name == "orchestrator-b")

    for row_out in (row1_out, row2_out):
        assert row_out.manager_note is not None
        assert row_out.manager_note.startswith("target_collision:")
        assert row_out.actions == []


# ---------------------------------------------------------------------------
# xander P2 review, Medium #1: the D4.4 collision guard was envelope-only --
# _run_action resolved a single row fresh and would have dispatched anyway.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_k8s_target_collision_blocks_dispatch_not_just_the_envelope(db, test_user, k8s_env):
    row1 = RagService(
        name="tesla-a", display_name="Tesla A", host="athena-rag-tesla",
        port=None, service_type="rag", enabled=True,
    )
    row2 = RagService(
        name="tesla-b", display_name="Tesla B", host="athena-rag-tesla.athena-prod.svc",
        port=None, service_type="rag", enabled=True,
    )
    db.add_all([row1, row2])
    db.commit()

    transport = k8s_env({"athena-rag-tesla": {"replicas": 1, "ready": 1}})
    body = service_control.ServiceActionRequest()

    for row_name in ("tesla-a", "tesla-b"):
        with pytest.raises(HTTPException) as exc_info:
            await service_control._run_action(row_name, "stop", body, None, db, test_user)
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error"] == "target_collision:athena-rag-tesla"

    assert [r for r in transport.requests if r.method == "PATCH"] == []
    rows = db.query(AuditLog).all()
    assert len(rows) == 2
    assert all(r.success is False and r.error_message == "target_collision:athena-rag-tesla" for r in rows)


# ---------------------------------------------------------------------------
# xander P2 review, Medium #2: a non-K8sControlError exception escaping
# dispatch (token file I/O, a remember/recall DB error, a lease OperationalError)
# must still audit and return a structured response, never a bare 500.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_exception_is_audited_and_returns_structured_500(db, test_user, tesla_row, k8s_env, monkeypatch):
    k8s_env({"athena-rag-tesla": {"replicas": 1, "ready": 1}})

    # Succeeds for the FIRST call (gather_inventory's list_deployments, used
    # for resolution) so the row still resolves manager='kubernetes'; fails
    # from the second call onward (inside dispatch itself) -- isolating the
    # exception to the dispatch phase this test targets, not resolution.
    call_count = {"n": 0}

    def _flaky_read_token(self):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "tok-k8s"
        raise FileNotFoundError("token file vanished mid-request")

    monkeypatch.setattr(kc.K8sDeploymentClient, "_read_token", _flaky_read_token)

    body = service_control.ServiceActionRequest()
    with pytest.raises(HTTPException) as exc_info:
        await service_control._run_action("tesla-rag", "stop", body, None, db, test_user)

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == {"error": "dispatch_failed", "kind": "FileNotFoundError"}

    rows = db.query(AuditLog).filter(AuditLog.resource_id == tesla_row.id).all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_message == "FileNotFoundError"
    assert "token file vanished" not in (rows[0].error_message or "")  # never str(exc), only the type name

    # The lease was released despite the exception (its own finally ran).
    from app.services.service_control_settings import read_lease
    assert read_lease(db, "athena-rag-tesla") is None


# ---------------------------------------------------------------------------
# xander P2 review, Low #4: the documented forbidden-RBAC message.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_k8s_forbidden_error_has_the_documented_message(db, test_user, tesla_row, k8s_env, monkeypatch):
    transport = k8s_env({"athena-rag-tesla": {"replicas": 1, "ready": 1}})

    real_handler = transport.handler

    def _forbidden_handler(request):
        if request.method == "PATCH":
            return httpx.Response(403, json={})
        return real_handler(request)

    transport.handler = _forbidden_handler
    monkeypatch.setattr(kc, "_test_transport", httpx.MockTransport(transport.handler))
    kc._clear_client_cache()

    body = service_control.ServiceActionRequest()
    result = await service_control._run_action("tesla-rag", "stop", body, None, db, test_user)

    assert result.success is False
    assert "Not permitted by the cluster Role for deployment 'athena-rag-tesla'" in result.message
    assert "docs/CONFIGURATION.md" in result.message


# ---------------------------------------------------------------------------
# tessa P2 mid-build Medium: restart_interrupted had zero coverage.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_interrupted_marker_shows_and_clears_after_start(db, test_user, tesla_row, k8s_env):
    k8s_env({"athena-rag-tesla": {"replicas": 0, "ready": 0}})

    remember_replicas(db, "athena-rag-tesla", 3)
    past = _datetime.now(_timezone.utc) - _timedelta(seconds=200)
    acquire_lease(
        TestingSessionLocal, "athena-rag-tesla", "restart", target_replicas=0,
        ttl=1, now=lambda: past,
    )  # already expired relative to real now()

    envelope = await service_control.list_services(None, db, test_user)
    row = next(r for r in envelope.services if r.name == "tesla-rag")
    assert row.manager_note == "restart_interrupted"
    assert row.actions == ["start"]

    body = service_control.ServiceActionRequest()
    result = await service_control._run_action("tesla-rag", "start", body, None, db, test_user)
    assert result.success is True

    sm._clear_inventory_cache()  # list_services' fresh=False cache would otherwise mask the update
    envelope2 = await service_control.list_services(None, db, test_user)
    row2 = next(r for r in envelope2.services if r.name == "tesla-rag")
    assert row2.manager_note != "restart_interrupted"
    assert row2.run_state == "running" or row2.k8s_replicas == 3


# ---------------------------------------------------------------------------
# tessa P2 mid-build Low: T8 #26 restated at the route layer.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_k8s_stop_already_at_zero_is_idempotent_at_route_layer(db, test_user, tesla_row, k8s_env):
    """T8 #26, restated at the route layer (not just the bare adapter, T5):
    a stop that reaches _dispatch_kubernetes_action -- the exact function
    _run_action calls -- with the Deployment already at 0 replicas (e.g. an
    out-of-band scale-down between resolution and dispatch) is a clean
    idempotent 200, not a spurious failure, and never sends a PATCH."""
    transport = k8s_env({"athena-rag-tesla": {"replicas": 0, "ready": 0}})

    success, message, replicas_after = await service_control._dispatch_kubernetes_action(
        "athena-rag-tesla", "stop", db, test_user, None, tesla_row, {},
    )
    assert success is True
    assert message == "already stopped"
    assert replicas_after == 0
    assert [r for r in transport.requests if r.method == "PATCH"] == []


# ---------------------------------------------------------------------------
# codex diff review r1 Critical #1 (route-level): a CA-managed row whose
# group isn't 'rag' (piper_row is service_type="infrastructure") is now
# critical -- an operator (write, no manage_infrastructure) gets 403
# regardless of confirm_name; an owner without confirm_name gets 409
# confirmation_required, never a bare 200.
# ---------------------------------------------------------------------------

def test_ca_docker_non_rag_row_operator_gets_403_insufficient_role(operator_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    response = operator_client.post(
        f"/api/service-control/{piper_row.name}/stop",
        json={"confirm_name": "athena-piper-tts"},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "insufficient_role"
    # Inventory listing (process/list, docker/list) happens during
    # resolution regardless -- the assertion that matters is that the
    # actual MUTATING dispatch call never fires.
    assert not any(r.url.path == "/docker/stop/athena-piper-tts" for r in transport.requests)


def test_ca_docker_non_rag_row_owner_without_confirm_gets_409(owner_client, db, piper_row, monkeypatch):
    transport = _ca_transport_for_piper(running=True)
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post(f"/api/service-control/{piper_row.name}/stop")

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "confirmation_required"
    assert not any(r.url.path == "/docker/stop/athena-piper-tts" for r in transport.requests)


# ---------------------------------------------------------------------------
# codex diff review r1 Critical #3: lease-aware scale-back, route-layer
# wiring (unit-level coverage of restart()'s own still_owner behavior lives
# in test_k8s_control.py).
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_superseded_audits_distinctly_and_skips_scaleback(
    db, test_user, tesla_row, k8s_env, monkeypatch
):
    transport = k8s_env({"athena-rag-tesla": {"replicas": 1, "ready": 1}})
    monkeypatch.setattr(service_control, "still_holds_lease", lambda *a, **k: False)

    success, message, replicas_after = await service_control._dispatch_kubernetes_action(
        "athena-rag-tesla", "restart", db, test_user, None, tesla_row, {},
    )

    assert success is False
    assert "superseded" in message
    patch_calls = [r for r in transport.requests if r.method == "PATCH"]
    assert len(patch_calls) == 1  # only the initial scale-to-0 -- no scale-back raced

    rows = db.query(AuditLog).filter(
        AuditLog.resource_id == tesla_row.id, AuditLog.error_message == "restart_superseded"
    ).all()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# codex diff review r1 Critical #4: an explicit interrupted marker, since
# releasing the lease in _dispatch_kubernetes_action's own finally means an
# expired-lease check alone would never catch a scale-back PATCH failure.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_scaleback_patch_failure_marks_interrupted_and_start_clears_it(
    db, test_user, tesla_row, k8s_env, monkeypatch
):
    transport = k8s_env({"athena-rag-tesla": {"replicas": 1, "ready": 1}})
    real_handler = transport.handler
    patch_count = {"n": 0}

    def _fail_second_patch(request):
        if request.method == "PATCH":
            patch_count["n"] += 1
            if patch_count["n"] == 2:
                return httpx.Response(500, json={})
        return real_handler(request)

    monkeypatch.setattr(kc, "_test_transport", httpx.MockTransport(_fail_second_patch))
    kc._clear_client_cache()

    success, message, replicas_after = await service_control._dispatch_kubernetes_action(
        "athena-rag-tesla", "restart", db, test_user, None, tesla_row, {},
    )
    assert success is False
    assert "scale-back" in message

    from app.services.service_control_settings import read_interrupted

    assert read_interrupted(db, "athena-rag-tesla") is True

    sm._clear_inventory_cache()
    envelope = await service_control.list_services(None, db, test_user)
    row = next(r for r in envelope.services if r.name == "tesla-rag")
    assert row.manager_note == "restart_interrupted"

    # A successful start clears the marker (real handler, no more injected failure).
    kc._clear_client_cache()
    monkeypatch.setattr(kc, "_test_transport", httpx.MockTransport(real_handler))
    body = service_control.ServiceActionRequest()
    result = await service_control._run_action("tesla-rag", "start", body, None, db, test_user)
    assert result.success is True

    assert read_interrupted(db, "athena-rag-tesla") is False

    sm._clear_inventory_cache()
    envelope2 = await service_control.list_services(None, db, test_user)
    row2 = next(r for r in envelope2.services if r.name == "tesla-rag")
    assert row2.manager_note != "restart_interrupted"
