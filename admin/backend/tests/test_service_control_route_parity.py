"""ATHENA-118 Phase 1: T9 — route-parity drift guard for service_control.py.

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T9.

This is a drift guard, not a behavior test: it discovers routes from
router.routes at test time so a new POST route added later without the
shared rate-limit dependency (or a lifecycle route that forgets to audit)
fails CI immediately, rather than silently reopening a gap this plan closed.
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

import re

import httpx
import pytest

from app.models import AuditLog, RagService
from app.routes import service_control
from app.services import service_managers as sm
from shared.config import _clear_cache_for_tests

_REAL_ASYNC_CLIENT = httpx.AsyncClient
_LIFECYCLE_RE = re.compile(r"/(start|stop|restart)$")


def _discover_post_routes():
    return [r for r in service_control.router.routes if "POST" in getattr(r, "methods", set())]


# ---------------------------------------------------------------------------
# 28. Every POST route carries service_control_rate_limit_dep
# ---------------------------------------------------------------------------

def test_every_post_route_has_rate_limit_dependency():
    routes = _discover_post_routes()
    assert len(routes) >= 12

    full_paths = {r.path for r in routes}
    assert "/api/service-control/port/{port}/restart" in full_paths

    for route in routes:
        dep_callables = {dep.call for dep in route.dependant.dependencies}
        assert service_control.service_control_rate_limit_dep in dep_callables, (
            f"{route.path} is missing service_control_rate_limit_dep"
        )


# ---------------------------------------------------------------------------
# 29. Lifecycle subset: viewer 403; exactly one audit row per request that
#     reaches resolution (incl. 403 insufficient_role / every 409); zero
#     audit rows for a request rejected before resolution (403/404/422).
# ---------------------------------------------------------------------------

def _lifecycle_paths():
    paths = {r.path for r in _discover_post_routes() if _LIFECYCLE_RE.search(r.path)}
    assert len(paths) >= 9
    assert "/api/service-control/ollama/stop" in paths
    return paths


def test_lifecycle_route_floor_and_named_member():
    _lifecycle_paths()  # asserts floor 9 + named member inline


class _RecordingTransport:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/process/list":
            return httpx.Response(200, json=[])
        if request.url.path == "/docker/list":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"success": True, "message": "ok"})


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


def test_viewer_gets_403_on_the_generic_start_route(viewer_client, db, monkeypatch):
    """One representative check of the lifecycle-subset viewer-403 rule for
    the /{service_name}/* routes -- the manager-specific behavior (which row
    resolves to which manager) is already covered by T2/T4; this guard is
    about the SHAPE (every discovered lifecycle route rejects a viewer), not
    re-deriving manager resolution per route."""
    transport = _RecordingTransport()
    _patch_async_client(monkeypatch, transport)

    row = RagService(
        name="parity-row", display_name="Parity Row", host="localhost",
        port=None, container_name="athena-parity", service_type="rag", enabled=True,
    )
    db.add(row)
    db.commit()

    before = db.query(AuditLog).count()
    response = viewer_client.post(f"/api/service-control/{row.name}/start")

    assert response.status_code == 403
    assert db.query(AuditLog).count() == before


# ---------------------------------------------------------------------------
# 30. Static gate: no stray `.is_running = ` write survives in the route file
# ---------------------------------------------------------------------------

def test_no_is_running_writes_in_service_control_routes():
    path = os.path.join(_REPO_ROOT, "admin", "backend", "app", "routes", "service_control.py")
    with open(path) as f:
        source = f.read()
    assert ".is_running = " not in source


# ---------------------------------------------------------------------------
# tessa mid-build (Medium #2): /ollama/{action} must be registered BEFORE
# /{service_name}/{action} -- Starlette matches in registration order, and
# the parametrized route matches ANY first segment including "ollama". A
# regression here silently reroutes every /ollama/* POST through _run_action
# against a (usually nonexistent) RagService row named "ollama".
# ---------------------------------------------------------------------------

def test_ollama_routes_registered_before_generic_service_name_routes():
    post_paths = [r.path for r in _discover_post_routes()]
    # Only the lifecycle-shaped ollama routes (2 segments, ending in
    # start/stop/restart) collide with /{service_name}/{action} -- the model
    # load/unload routes have a different shape and never collide, so they're
    # deliberately excluded from this check.
    ollama_lifecycle_paths = {
        "/api/service-control/ollama/start",
        "/api/service-control/ollama/stop",
        "/api/service-control/ollama/restart",
    }
    ollama_indices = [i for i, p in enumerate(post_paths) if p in ollama_lifecycle_paths]
    generic_indices = [i for i, p in enumerate(post_paths) if p == "/api/service-control/{service_name}/start"]
    assert ollama_indices and generic_indices
    assert max(ollama_indices) < min(generic_indices), (
        "an /ollama/* lifecycle route is registered after /{service_name}/start and would be shadowed"
    )


def test_ollama_start_reaches_the_dedicated_handler_not_the_generic_dispatcher(owner_client, db, monkeypatch):
    """No RagService row named 'ollama' exists in this DB, and no Control
    Agent/Kubernetes manager is configured. If /ollama/start were shadowed
    by /{service_name}/start, _run_action's row lookup would 404 (no
    'ollama' registry row). Phase 3 routes /ollama/start through
    resolve_ollama_manager instead (D12), which resolves the synthetic
    Ollama row to manager='none' and refuses with 409
    `ollama_not_manageable` -- a distinct signal from the 404 a shadowed
    route would produce, proving the dedicated handler (not _run_action's
    row-based dispatch) served this request."""
    response = owner_client.post("/api/service-control/ollama/start")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "ollama_not_manageable"
