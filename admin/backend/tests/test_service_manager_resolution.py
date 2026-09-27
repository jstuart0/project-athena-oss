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
