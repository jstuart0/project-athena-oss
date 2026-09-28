"""ATHENA-118 Phase 3: T11 -- Ollama panel backend (D12/D21).

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T11.

Mocking strategy: Ollama and the Control Agent are both real HTTP
boundaries this repo doesn't own -- faked at the httpx transport level via
a global httpx.AsyncClient monkeypatch (same technique as
test_control_agent_caller_headers.py), keyed by request path so one fake
answers both Ollama's and the CA's calls in a single test. check_ssrf_safe
is never mocked (D21's own gate is exactly what's under test in the SSRF
cases).
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

from app.models import AuditLog, RagService, SystemSetting
from app.services import service_managers as sm
from app.services import k8s_control as kc
from shared.config import _clear_cache_for_tests


def _clear_all_config_caches() -> None:
    """Belt-and-suspenders cache clear (ATHENA-118 test-isolation note):
    test_rate_limit_active.py evicts and re-imports every app./shared.*
    module mid-suite to reproduce a real FastAPI-version regression.
    check_ssrf_safe (app/utils/rag_urls.py, pre-existing ATHENA-113 code)
    does a LAZY `from app.services.health_poller import
    _validate_service_url` inside its own body -- if health_poller.py was
    reloaded after this file's own top-level `_clear_cache_for_tests`
    import was captured, that lazy import resolves against a DIFFERENT
    (reloaded) shared.config module whose lru_cache this file's own
    reference never touches. Clearing via sys.modules ensures whichever
    copy is currently live gets cleared, regardless of reload timing."""
    _clear_cache_for_tests()
    live = sys.modules.get('shared.config')
    if live is not None and hasattr(live, '_clear_cache_for_tests'):
        live._clear_cache_for_tests()

_REAL_ASYNC_CLIENT = httpx.AsyncClient


class _MultiHostTransport:
    """Routes by (host, path) so one fake answers both the Ollama host and
    the Control Agent host in a single request cycle."""

    def __init__(self, responses: dict):
        # responses: {(host, path): (status, json)} ; host is None to match any
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host = request.url.host
        path = request.url.path
        for (r_host, r_path), (status, body) in self.responses.items():
            if (r_host is None or r_host == host) and r_path == path:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={})


def _patch_async_client(monkeypatch, transport: _MultiHostTransport) -> None:
    """Only injects the fake transport when the CALLER didn't already pass
    one explicitly -- the k8s adapter (k8s_control.py's K8sDeploymentClient)
    sets its own `transport=` kwarg from `kc._test_transport` and must keep
    it; blindly overriding every httpx.AsyncClient() call here would silently
    swallow the k8s adapter's own fake transport in the in-cluster-Ollama
    test, which needs BOTH an Ollama-host fake and a k8s-API fake active at
    once."""
    def factory(*args, **kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = httpx.MockTransport(transport.handler)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _allow_host(monkeypatch, host: str) -> None:
    """RFC1918/loopback hosts are blocked by check_ssrf_safe by default
    (fail-closed for OSS deployers) -- explicitly allowlist the test host,
    same as a real deployer would for their own Ollama host."""
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", host)
    _clear_all_config_caches()


def _set_ollama_url(db, url: str) -> None:
    row = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
    if row:
        row.value = url
    else:
        db.add(SystemSetting(key="ollama_url", value=url, category="llm"))
    db.commit()


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-svc-key-athena-118")
    _clear_all_config_caches()
    sm._clear_inventory_cache()
    sm._ca_transport = None
    kc._clear_client_cache()
    yield
    sm._clear_inventory_cache()
    sm._ca_transport = None
    kc._clear_client_cache()
    _clear_all_config_caches()


# ---------------------------------------------------------------------------
# 1-3. Direct probe, zero CA calls; offline; 502 on /api/tags failure
# ---------------------------------------------------------------------------

def test_healthy_with_two_loaded_models_zero_ca_calls(owner_client, db, monkeypatch):
    _allow_host(monkeypatch, "10.0.0.108")
    _set_ollama_url(db, "http://10.0.0.108:11434")
    transport = _MultiHostTransport({
        ("10.0.0.108", "/api/version"): (200, {"version": "0.5.1"}),
        ("10.0.0.108", "/api/ps"): (200, {"models": [{"name": "a"}, {"name": "b"}]}),
    })
    _patch_async_client(monkeypatch, transport)

    response = owner_client.get("/api/service-control/ollama/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert data["models_loaded"] == 2

    ca_calls = [r for r in transport.requests if "8099" in str(r.url)]
    assert ca_calls == []


def test_version_refused_is_offline(owner_client, db, monkeypatch):
    _allow_host(monkeypatch, "10.0.0.108")
    _set_ollama_url(db, "http://10.0.0.108:11434")

    def _raise_connect(request):
        raise httpx.ConnectError("nope", request=request)

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(_raise_connect))
    monkeypatch.setattr(httpx, "AsyncClient", factory)

    response = owner_client.get("/api/service-control/ollama/health")
    assert response.status_code == 200
    assert response.json()["status"] == "offline"


def test_tags_failure_is_502(owner_client, db, monkeypatch):
    _allow_host(monkeypatch, "10.0.0.108")
    _set_ollama_url(db, "http://10.0.0.108:11434")
    transport = _MultiHostTransport({
        ("10.0.0.108", "/api/tags"): (500, {}),
    })
    _patch_async_client(monkeypatch, transport)

    response = owner_client.get("/api/service-control/ollama/models")
    assert response.status_code == 502
    assert response.json()["detail"]["error"] == "models_endpoint_unreachable"


# ---------------------------------------------------------------------------
# 4-5. CA-host-match dispatch; host mismatch -> ollama_not_manageable
# ---------------------------------------------------------------------------

def test_ca_host_match_dispatches_through_control_agent(owner_client, db, monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    _clear_all_config_caches()  # owner_client's app startup already cached get_config()
    # CONTROL_AGENT_URL defaults to http://localhost:8099 -- match the
    # Ollama URL's host to it so resolve_manager's CA host gate passes.
    _set_ollama_url(db, "http://localhost:11434")

    transport = _MultiHostTransport({
        ("localhost", "/process/list"): (200, []),
        ("localhost", "/docker/list"): (200, []),
        ("localhost", "/ollama/restart"): (200, {"success": True, "message": "restarted"}),
    })
    _patch_async_client(monkeypatch, transport)

    response = owner_client.post("/api/service-control/ollama/restart", json={"confirm_name": "ollama"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True

    restart_calls = [r for r in transport.requests if r.url.path == "/ollama/restart"]
    assert len(restart_calls) == 1

    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).all()
    assert rows[0].action == "service_restart"
    assert rows[0].success is True


def test_host_mismatch_resolves_none_and_refuses(owner_client, db, monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    _clear_all_config_caches()
    _allow_host(monkeypatch, "10.0.0.108")
    # CONTROL_AGENT_URL default host is 'localhost'; put Ollama somewhere else.
    _set_ollama_url(db, "http://10.0.0.108:11434")

    transport = _MultiHostTransport({
        ("localhost", "/process/list"): (200, []),
        ("localhost", "/docker/list"): (200, []),
    })
    _patch_async_client(monkeypatch, transport)

    before = db.query(AuditLog).count()
    response = owner_client.post("/api/service-control/ollama/stop")
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "ollama_not_manageable"

    ca_calls = [r for r in transport.requests if "/ollama/" in r.url.path]
    assert ca_calls == []

    rows = db.query(AuditLog).all()
    assert len(rows) == before + 1
    assert rows[-1].success is False


# ---------------------------------------------------------------------------
# 6-7. SSRF runtime gate across all four call sites; RFC1918 allowlisted
# host probed normally
# ---------------------------------------------------------------------------

def test_ssrf_blocked_host_zero_requests_across_all_call_sites(owner_client, db, monkeypatch):
    _set_ollama_url(db, "http://169.254.169.254:11434")
    transport = _MultiHostTransport({})
    _patch_async_client(monkeypatch, transport)

    health = owner_client.get("/api/service-control/ollama/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ssrf_blocked"

    models = owner_client.get("/api/service-control/ollama/models")
    assert models.status_code == 403
    assert models.json()["detail"]["error"] == "ssrf_blocked"

    load = owner_client.post("/api/service-control/ollama/models/llama3/load")
    assert load.status_code == 200  # ModelActionResponse, success=false
    assert load.json()["success"] is False

    unload = owner_client.post("/api/service-control/ollama/models/llama3/unload")
    assert unload.json()["success"] is False

    assert transport.requests == []


def test_rfc1918_allowlisted_host_is_probed_normally(owner_client, db, monkeypatch):
    monkeypatch.setenv("HEALTH_POLL_ALLOWED_PRIVATE_HOSTS", "192.168.10.108")
    _clear_all_config_caches()
    _set_ollama_url(db, "http://192.168.10.108:11434")

    transport = _MultiHostTransport({
        ("192.168.10.108", "/api/version"): (200, {"version": "0.5.1"}),
        ("192.168.10.108", "/api/ps"): (200, {"models": []}),
    })
    _patch_async_client(monkeypatch, transport)

    response = owner_client.get("/api/service-control/ollama/health")
    assert response.status_code == 200
    assert response.json()["status"] == "idle"
    assert len(transport.requests) == 2


# ---------------------------------------------------------------------------
# 8. In-cluster Ollama -> Kubernetes manager, synthetic row (row_name=None)
# ---------------------------------------------------------------------------

def test_in_cluster_ollama_resolves_kubernetes_synthetic_row(owner_client, db, monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_CONTROL_K8S_ENABLED", "true")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    _clear_all_config_caches()
    token_path = tmp_path / "token"
    token_path.write_text("tok-k8s")
    monkeypatch.setattr(kc, "TOKEN_PATH_DEFAULT", str(token_path))
    monkeypatch.setattr(kc, "NAMESPACE_PATH_DEFAULT", str(tmp_path / "namespace"))

    _set_ollama_url(db, "http://ollama:11434")

    class _K8sTransport:
        def __init__(self):
            self.requests: list[httpx.Request] = []
            self.replicas = 1

        def handler(self, request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path.endswith("/deployments"):
                return httpx.Response(200, json={"items": [
                    {"metadata": {"name": "ollama"}, "spec": {"replicas": self.replicas}, "status": {"readyReplicas": self.replicas}},
                ]})
            if request.method == "GET":
                return httpx.Response(200, json={
                    "spec": {"replicas": self.replicas}, "status": {"replicas": self.replicas},
                    "metadata": {"resourceVersion": "rv"},
                })
            if request.method == "PATCH":
                import json as _json
                self.replicas = _json.loads(request.content)["spec"]["replicas"]
                return httpx.Response(200, json={})
            return httpx.Response(404, json={})

    k8s_transport = _K8sTransport()
    monkeypatch.setattr(kc, "_test_transport", httpx.MockTransport(k8s_transport.handler))
    kc._clear_client_cache()

    ollama_transport = _MultiHostTransport({("ollama", "/api/version"): (200, {"version": "0.5.1"})})
    _patch_async_client(monkeypatch, ollama_transport)
    sm._clear_inventory_cache()

    health = owner_client.get("/api/service-control/ollama/health")
    assert health.status_code == 200
    data = health.json()
    assert data["manager"] == "kubernetes"
    assert data["confirm_required"] is True
    assert data["row_name"] is None

    no_confirm = owner_client.post("/api/service-control/ollama/stop")
    assert no_confirm.status_code == 409
    assert no_confirm.json()["detail"]["error"] == "confirmation_required"
    assert [r for r in k8s_transport.requests if r.method == "PATCH"] == []

    with_confirm = owner_client.post("/api/service-control/ollama/stop", json={"confirm_name": "ollama"})
    assert with_confirm.status_code == 200
    assert with_confirm.json()["success"] is True
    patches = [r for r in k8s_transport.requests if r.method == "PATCH"]
    assert len(patches) == 1
