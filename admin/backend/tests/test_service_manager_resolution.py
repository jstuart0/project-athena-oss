"""ATHENA-118 Phase 1: T2 (manager resolution + grouping) and T3 (CA
inventory transport).

Plan: .mozart/plans/active/2026-09-27-deliver-athena-service-control-k8s.md
Test contract: same directory,
2026-09-27-deliver-athena-service-control-k8s.test-contract.md, T2/T3.

Mocking strategy: the Control Agent is a real HTTP boundary this repo
doesn't own. Faked at the transport level (httpx.MockTransport) injected
via service_managers._ca_transport -- the smallest real seam that still
exercises actual request construction (headers, path) and the resolver's
own matching/precedence logic. See the contract's "Mocking-strategy note".
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

from app.models import RagService
from app.services import service_managers as sm
from shared.config import _clear_cache_for_tests


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Clear the module cache and any injected transport between tests, and
    reset AthenaConfig's cache so CONTROL_AGENT_ENABLED env changes take
    effect (mirrors test_control_agent_caller_headers.py's pattern)."""
    sm._clear_inventory_cache()
    sm._ca_transport = None
    _clear_cache_for_tests()
    yield
    sm._clear_inventory_cache()
    sm._ca_transport = None
    _clear_cache_for_tests()


def _row(**overrides) -> RagService:
    defaults = dict(
        id=1,
        name="weather-rag",
        display_name="Weather RAG",
        host="192.168.10.108",
        port=8010,
        container_name=None,
        service_type="rag",
        enabled=True,
    )
    defaults.update(overrides)
    return RagService(**defaults)


def _inventory(
    *,
    enabled: bool = True,
    reachable: bool = True,
    processes=None,
    containers=None,
    docker_available: bool = True,
    note=None,
) -> sm.Inventory:
    return sm.Inventory(
        control_agent=sm.ControlAgentInventory(
            enabled=enabled,
            reachable=reachable,
            processes=processes or [],
            containers=containers or [],
            docker_available=docker_available,
            note=note,
        )
    )


CA_HOST = "localhost"  # matches CONTROL_AGENT_URL's default host (http://localhost:8099)


# ---------------------------------------------------------------------------
# T2 — CA + grouping resolution
# ---------------------------------------------------------------------------

def test_ca_host_match_process_port_resolves_process():
    row = _row(host=CA_HOST, port=8010)
    inv = _inventory(processes=[{"port": 8010, "running": True}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "control_agent"
    assert res.kind == "process"
    assert res.target == "8010"


def test_ca_host_match_container_name_resolves_docker():
    row = _row(host=CA_HOST, port=None, container_name="athena-piper-tts")
    inv = _inventory(containers=[{"name": "athena-piper-tts", "running": True, "ports": None}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "control_agent"
    assert res.kind == "docker"
    assert res.target == "athena-piper-tts"


def test_ca_host_match_published_port_ipv4_form_resolves_docker():
    row = _row(host=CA_HOST, port=10200, container_name=None)
    inv = _inventory(containers=[{"name": "athena-whisper", "running": True, "ports": "0.0.0.0:10200->10200/tcp"}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "control_agent"
    assert res.kind == "docker"
    assert res.target == "athena-whisper"


def test_ca_host_match_published_port_ipv6_form_resolves_docker():
    row = _row(host=CA_HOST, port=10200, container_name=None)
    inv = _inventory(containers=[{"name": "athena-whisper", "running": True, "ports": ":::10200->10200/tcp"}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "control_agent"
    assert res.kind == "docker"
    assert res.target == "athena-whisper"


def test_container_side_only_port_match_is_not_docker():
    """Negative: row.port=10200 only matches the CONTAINER-internal side of
    '0.0.0.0:19999->10200/tcp' -- must NOT resolve as docker (D4 L2)."""
    row = _row(host=CA_HOST, port=10200, container_name=None)
    inv = _inventory(containers=[{"name": "athena-other", "running": True, "ports": "0.0.0.0:19999->10200/tcp"}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "none"
    assert res.kind is None


def test_ca_host_mismatch_same_port_same_container_name_is_not_control_agent():
    """Negative: the retired-stack collision the plan names (weather-rag
    port/name collision) must not resolve to control_agent when hosts
    differ."""
    row = _row(host="athena-rag-weather", port=8010, container_name="athena-weather")
    inv = _inventory(
        processes=[{"port": 8010, "running": True}],
        containers=[{"name": "athena-weather", "running": True, "ports": None}],
    )
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "none"


def test_normalize_host_ipv6_bracket_forms_equal():
    assert sm.normalize_host("[::1]") == sm.normalize_host("::1")


def test_ca_disabled_resolves_none_zero_requests():
    row = _row(host=CA_HOST)
    inv = _inventory(enabled=False, reachable=False)
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "none"
    assert res.note in (None, "not_applicable")


def test_ca_unreachable_note():
    row = _row(host=CA_HOST)
    inv = _inventory(reachable=False)
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "none"
    assert res.note == "control_agent_unreachable"


def test_ca_docker_list_down_note_distinct_from_unreachable():
    """[D4 L2] A /docker/list 500 (docker daemon down, CA itself reachable)
    must yield a note DISTINCT from 'control_agent_unreachable'."""
    row = _row(host=CA_HOST, port=9999, container_name="athena-nonexistent")
    inv = _inventory(processes=[], docker_available=False, note="control_agent_docker_unavailable")
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "none"
    assert res.note == "control_agent_docker_unavailable"
    assert res.note != "control_agent_unreachable"


def test_ollama_row_ca_host_match_resolves_ollama_kind():
    row = _row(name="ollama", host=CA_HOST, port=None, container_name=None)
    inv = _inventory()
    res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert res.manager == "control_agent"
    assert res.kind == "ollama"


@pytest.mark.parametrize(
    "name,service_type,host,expected",
    [
        pytest.param("amtrak-rag", "api", "athena-amtrak", "rag", id="amtrak_rag_by_name_suffix"),
        pytest.param("weather", "api", "athena-rag-weather", "rag", id="weather_rag_by_host_prefix"),
        pytest.param("redis", "infrastructure", "redis", "infrastructure", id="redis_is_infrastructure"),
        pytest.param("orchestrator", "core", "athena-orchestrator", "core", id="orchestrator_is_core"),
    ],
)
def test_group_for_named_cases(name, service_type, host, expected):
    row = _row(name=name, service_type=service_type, host=host)
    assert sm.group_for(row) == expected


def test_native_state_container_up():
    row = _row(host=CA_HOST, container_name="athena-piper-tts")
    inv = _inventory(containers=[{"name": "athena-piper-tts", "running": True, "ports": None}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.native_state == "container up"


def test_native_state_container_stopped():
    row = _row(host=CA_HOST, container_name="athena-piper-tts")
    inv = _inventory(containers=[{"name": "athena-piper-tts", "running": False, "ports": None}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.native_state == "container stopped"


def test_actions_docker_running_is_stop_restart():
    row = _row(host=CA_HOST, container_name="athena-piper-tts")
    inv = _inventory(containers=[{"name": "athena-piper-tts", "running": True, "ports": None}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.actions == ["stop", "restart"]


def test_actions_docker_stopped_is_start():
    row = _row(host=CA_HOST, container_name="athena-piper-tts")
    inv = _inventory(containers=[{"name": "athena-piper-tts", "running": False, "ports": None}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.actions == ["start"]


def test_actions_none_manager_is_empty():
    row = _row(host="some-external-host")
    inv = _inventory()
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.actions == []


# ---------------------------------------------------------------------------
# T3 — CA inventory transport
# ---------------------------------------------------------------------------

class _RecordingTransport:
    def __init__(self, responses: dict):
        self.requests: list[httpx.Request] = []
        self._responses = responses

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for path, (status, body) in self._responses.items():
            if request.url.path == path:
                return httpx.Response(status, json=body)
        return httpx.Response(404, json=[])


@pytest.mark.asyncio
async def test_gather_inventory_sends_service_key_header(monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    monkeypatch.setenv("SERVICE_API_KEY", "test-svc-key-athena-118")
    _clear_cache_for_tests()

    transport = _RecordingTransport({
        "/process/list": (200, []),
        "/docker/list": (200, []),
    })
    sm._ca_transport = httpx.MockTransport(transport.handler)

    inv = await sm.gather_inventory(fresh=True)

    assert inv.control_agent.reachable is True
    assert len(transport.requests) == 2
    for req in transport.requests:
        assert req.headers.get("X-Service-Key") == "test-svc-key-athena-118"


@pytest.mark.asyncio
async def test_gather_inventory_timeout_does_not_raise(monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    _clear_cache_for_tests()

    def _raise_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    sm._ca_transport = httpx.MockTransport(_raise_timeout)

    inv = await sm.gather_inventory(fresh=True)

    assert inv.control_agent.reachable is False
    assert inv.control_agent.note == "control_agent_unreachable"


@pytest.mark.asyncio
async def test_gather_inventory_fresh_bypasses_cache_then_cache_hits(monkeypatch):
    monkeypatch.setenv("CONTROL_AGENT_ENABLED", "true")
    _clear_cache_for_tests()

    transport = _RecordingTransport({
        "/process/list": (200, []),
        "/docker/list": (200, []),
    })
    sm._ca_transport = httpx.MockTransport(transport.handler)

    await sm.gather_inventory(fresh=True)
    assert len(transport.requests) == 2

    await sm.gather_inventory(fresh=False)
    assert len(transport.requests) == 2  # cache hit -- no new calls

    await sm.gather_inventory(fresh=True)
    assert len(transport.requests) == 4  # fresh bypasses cache again


# ---------------------------------------------------------------------------
# T8 — Kubernetes resolution (extends T2). CA is disabled (default) in every
# case here so k8s resolution is exercised in isolation.
# ---------------------------------------------------------------------------

from app.services.k8s_control import DeploymentInfo  # noqa: E402


def _k8s_inv(deployments=None, available=True, reason=None, namespace='athena-prod'):
    return sm.Inventory(
        control_agent=sm.ControlAgentInventory(enabled=False, reachable=False),
        kubernetes=sm.KubernetesInventory(
            enabled=True, available=available, reason=reason,
            namespace=namespace, deployments=deployments or {},
        ),
    )


def _deploy(name, replicas, ready=None):
    return DeploymentInfo(name=name, replicas=replicas, ready_replicas=ready if ready is not None else replicas)


@pytest.mark.parametrize(
    "host,expect_manager",
    [
        pytest.param("athena-rag-tesla", "kubernetes", id="bare_label"),
        pytest.param("athena-rag-tesla.athena-prod.svc.cluster.local", "kubernetes", id="fqdn_form"),
        pytest.param("x.other-ns.svc", "none", id="other_namespace"),
        pytest.param("192.168.1.5", "none", id="bare_ip"),
    ],
)
def test_k8s_host_reduction_cases(host, expect_manager):
    row = _row(name="tesla-rag", host=host, port=None, container_name=None)
    inv = _k8s_inv(deployments={"athena-rag-tesla": _deploy("athena-rag-tesla", 1)})
    res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert res.manager == expect_manager


def test_k8s_protected_deployment_never_resolves():
    row = _row(name="admin-backend-row", host="athena-admin-backend", port=None, container_name=None)
    inv = _k8s_inv(deployments={"athena-admin-backend": _deploy("athena-admin-backend", 2)})
    res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert res.manager == "none"
    assert res.note == "protected"


def test_k8s_missing_deployment_gives_no_deployment_note():
    row = _row(name="ghost-row", host="athena-rag-ghost", port=None, container_name=None)
    inv = _k8s_inv(deployments={})
    res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert res.manager == "none"
    assert res.note == "no_deployment:athena-rag-ghost"


def test_k8s_replicas_zero_gives_stopped_shape():
    row = _row(name="tesla-rag", host="athena-rag-tesla", port=None, container_name=None)
    inv = _k8s_inv(deployments={"athena-rag-tesla": _deploy("athena-rag-tesla", 0, ready=0)})
    res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert res.manager == "kubernetes"
    assert res.native_actions == ["start"]
    assert res.native_state == "0/0 pods"
    assert res.k8s_replicas == 0


@pytest.mark.parametrize(
    "enabled,replicas,ready,expected_actions,expected_state",
    [
        pytest.param(False, 0, 0, ["start"], "0/0 pods", id="disabled_zero_replicas"),
        pytest.param(False, 1, 1, ["stop", "restart"], "1/1 pods", id="disabled_one_replica"),
    ],
)
def test_k8s_disabled_row_vs_scale_are_orthogonal(enabled, replicas, ready, expected_actions, expected_state):
    row = _row(name="tesla-rag", host="athena-rag-tesla", port=None, container_name=None, enabled=enabled)
    inv = _k8s_inv(deployments={"athena-rag-tesla": _deploy("athena-rag-tesla", replicas, ready=ready)})
    res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert res.native_actions == expected_actions
    assert res.native_state == expected_state


def test_k8s_owner_gate_orchestrator_actions_empty_for_operator():
    row = _row(name="orchestrator", host="athena-orchestrator", port=None, container_name=None, service_type="core")
    inv = _k8s_inv(deployments={"athena-orchestrator": _deploy("athena-orchestrator", 1)})
    operator_res = sm.resolve_manager(row, inv, {"read", "write"})
    assert operator_res.actions == []
    assert operator_res.note == "requires_owner"
    assert operator_res.confirm_required is True  # critical is still true; only actions are gated

    owner_res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert owner_res.actions == ["stop", "restart"]


def test_k8s_non_critical_rag_row_ungated_for_operator():
    row = _row(name="tesla-rag", host="athena-rag-tesla", port=None, container_name=None, service_type="rag")
    inv = _k8s_inv(deployments={"athena-rag-tesla": _deploy("athena-rag-tesla", 1)})
    operator_res = sm.resolve_manager(row, inv, {"read", "write"})
    assert operator_res.actions == ["stop", "restart"]
    assert operator_res.confirm_required is False


def test_k8s_fail_safe_critical_by_group_not_just_named_set():
    """[L3] A k8s row whose Deployment name ISN'T in the hardcoded
    CRITICAL_DEPLOYMENTS set is still critical if its group != 'rag' --
    proving the union is OR'd correctly, not backwards."""
    core_row = _row(name="my-core", host="my-core", port=None, container_name=None, service_type="core")
    inv = _k8s_inv(deployments={"my-core": _deploy("my-core", 1)})
    res = sm.resolve_manager(core_row, inv, {"read", "write"})
    assert res.confirm_required is True

    rag_row = _row(name="my-rag", host="my-rag", port=None, container_name=None, service_type="rag")
    inv2 = _k8s_inv(deployments={"my-rag": _deploy("my-rag", 1)})
    res2 = sm.resolve_manager(rag_row, inv2, {"read", "write"})
    assert res2.confirm_required is False


def test_k8s_confirm_name_is_the_deployment_label_not_row_name():
    """The alias row's OWN name ('obscure') must never satisfy the typed
    confirm -- only the resolved Deployment label does (D4.4 / mozart r3a)."""
    alias_row = _row(name="obscure", host="athena-orchestrator", port=None, container_name=None, service_type="core")
    inv = _k8s_inv(deployments={"athena-orchestrator": _deploy("athena-orchestrator", 1)})
    res = sm.resolve_manager(alias_row, inv, {"read", "write", "manage_infrastructure"})
    assert res.confirm_name == "athena-orchestrator"
    assert res.target == "athena-orchestrator"


def test_k8s_kubernetes_unavailable_note_carries_reason():
    row = _row(name="tesla-rag", host="athena-rag-tesla", port=None, container_name=None)
    inv = _k8s_inv(available=False, reason="forbidden")
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "none"
    assert res.note == "kubernetes_unavailable:forbidden"


# ---------------------------------------------------------------------------
# codex diff review r1 Critical #1: criticality follows the TARGET under
# every manager, not just Kubernetes. A CA-managed docker/process row whose
# group isn't 'rag' (the same fail-safe already applied to the k8s path)
# must resolve critical=True, with confirm_name naming the resolved target.
# ---------------------------------------------------------------------------

def test_ca_docker_non_rag_row_is_critical_fail_safe():
    row = _row(name="redis", host=CA_HOST, port=None, service_type="infrastructure", container_name="athena-redis")
    inv = _inventory(containers=[{"name": "athena-redis", "running": True, "ports": []}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "control_agent"
    assert res.kind == "docker"
    assert res.critical is True
    assert res.confirm_name == "athena-redis"
    assert res.actions == []  # no manage_infrastructure in the passed permission set


def test_ca_process_non_rag_row_is_critical_with_process_confirm_name():
    row = _row(name="redis", host=CA_HOST, port=8010, service_type="infrastructure")
    inv = _inventory(processes=[{"port": 8010, "running": True}])
    res = sm.resolve_manager(row, inv, {"read", "write", "manage_infrastructure"})
    assert res.manager == "control_agent"
    assert res.kind == "process"
    assert res.critical is True
    assert res.confirm_name == "process:8010"
    assert res.actions == ["stop", "restart"]  # gated actions ARE populated for an owner


def test_ca_rag_row_stays_non_critical():
    row = _row(name="athena-rag-weather", host=CA_HOST, port=None, container_name="athena-rag-weather", service_type="rag")
    inv = _inventory(containers=[{"name": "athena-rag-weather", "running": True, "ports": []}])
    res = sm.resolve_manager(row, inv, {"read", "write"})
    assert res.manager == "control_agent"
    assert res.critical is False
    assert res.actions == ["stop", "restart"]  # ungated -- no owner permission needed


# ---------------------------------------------------------------------------
# codex diff review r1 Critical #2: a CA process/container occupying
# Ollama's own port resolves kind='ollama' (critical=True) BEFORE the
# generic process/docker branches, even when the row isn't literally named
# 'ollama'.
# ---------------------------------------------------------------------------

def test_ca_process_on_ollama_port_resolves_ollama_kind_even_when_unnamed():
    row = _row(name="local-llm", host=CA_HOST, port=11434, service_type="infrastructure")
    inv = _inventory(processes=[{"port": 11434, "running": True}])
    res = sm.resolve_manager(row, inv, {"read", "write"}, ollama_port=11434)
    assert res.manager == "control_agent"
    assert res.kind == "ollama"
    assert res.critical is True
    assert res.confirm_name == "ollama"


def test_ca_container_on_ollama_port_resolves_ollama_kind_even_when_unnamed():
    # Registry rows for a docker-managed service carry `port` set to the
    # container's published port (matching every other docker-resolution
    # fixture in this file) -- this is the row shape the ollama_port
    # comparison is actually written against.
    row = _row(name="local-llm", host=CA_HOST, port=11434, container_name="some-other-name", service_type="infrastructure")
    inv = _inventory(containers=[{"name": "some-other-name", "running": True, "ports": ["11434:11434/tcp"]}])
    res = sm.resolve_manager(row, inv, {"read", "write"}, ollama_port=11434)
    assert res.manager == "control_agent"
    assert res.kind == "ollama"
    assert res.critical is True
    assert res.confirm_name == "ollama"


def test_ca_named_ollama_row_resolves_ollama_kind_without_port_hint():
    row = _row(name="ollama", host=CA_HOST, port=11434, service_type="infrastructure")
    inv = _inventory(processes=[{"port": 11434, "running": True}])
    res = sm.resolve_manager(row, inv, {"read", "write"})  # no ollama_port passed
    assert res.kind == "ollama"
    assert res.critical is True
