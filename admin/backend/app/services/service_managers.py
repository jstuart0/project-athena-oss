"""Service manager resolution (ATHENA-118, D4/D9/D10/D14/D17/D20).

For every row in the service registry, resolves which control-plane manager
(the Control Agent, Kubernetes, or none) can actually execute lifecycle
actions against it, and derives the manager-native state and available
actions.  Kubernetes resolution itself lands in Phase 2
(``app/services/k8s_control.py``); this module already carries the
``kind='kubernetes'`` shape and the ``manage_infrastructure`` owner gate so
Phase 2 only has to plug in real inventory, not change this contract.

Both real boundaries this module reaches (the Control Agent, and — once
Phase 2 lands — the Kubernetes API) are HTTP over ``httpx``.  Tests inject a
fake transport via the module-level ``_ca_transport`` / ``_k8s_transport``
hooks rather than mocking this module's own functions, so the real
request-construction and status-mapping code is exercised.
"""
import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx
import structlog

from app.models import RagService
from app.utils.service_auth import control_agent_headers
from app.services.k8s_control import DEPLOYMENT_NAME_RE, K8sControlError, get_k8s_client
from shared.config import get_config

logger = structlog.get_logger()

# Moved here from service_control.py (D4 / plan step 4). service_control.py
# re-exports this name so test_control_agent_caller_headers.py's
# monkeypatches keep binding to a single symbol.
CONTROL_AGENT_URL = os.getenv("CONTROL_AGENT_URL", "http://localhost:8099")

# Deployments that can never be scaled/restarted through Service Control,
# under any manager (D9).
PROTECTED_DEPLOYMENTS = {"athena-admin-backend", "athena-admin-frontend"}

# Fail-safe critical set (D9): a k8s-resolved row whose group != 'rag' is
# ALSO critical even if its Deployment name isn't listed here (L3) — that
# union is applied in Phase 2 once k8s resolution exists. Ollama is
# critical under any manager (bob r2 M3), enforced in _finish below.
CRITICAL_DEPLOYMENTS = {
    "athena-gateway",
    "athena-orchestrator",
    "athena-mode-service",
    "athena-jarvis-web",
    "redis",
    "qdrant",
    "ollama",
}

# Test-injection hooks (plan step 4). None in production; gather_inventory
# constructs a real httpx.AsyncClient when unset.
_ca_transport: Optional[httpx.AsyncBaseTransport] = None
_k8s_transport: Optional[httpx.AsyncBaseTransport] = None  # consumed by k8s_control.py (Phase 2)

_CACHE_TTL_SECONDS = 10.0

# Extracts the HOST-published port from a Docker `Ports` string, e.g.
# "0.0.0.0:10200->10200/tcp", ":::10200->10200/tcp", "[::]:10200->10200/tcp".
# Only the digits immediately before "->" are host-published; the digits
# between "->" and "/" are the container-internal port and must NOT match
# (D4 L2 negative case: a container-side-only match is not a docker hit).
_HOST_PUBLISHED_PORT_RE = re.compile(r'(\d+)->(\d+)/\w+')


def normalize_host(host: Optional[str]) -> str:
    """Lowercase and strip IPv6 brackets so '[::1]' and '::1' compare equal (D4)."""
    if not host:
        return ""
    h = host.strip().lower()
    if h.startswith('[') and h.endswith(']'):
        h = h[1:-1]
    return h


def group_for(row: RagService) -> str:
    """Server-side grouping (D17) — the frontend only reads ``row.group``."""
    name = (row.name or '').lower()
    host = (row.host or '').lower()
    service_type = (row.service_type or '').lower()

    if service_type == 'rag' or name.endswith('-rag') or host.startswith('athena-rag-'):
        return 'rag'
    if service_type == 'infrastructure' or name in {'redis', 'qdrant', 'postgres', 'searxng', 'control-agent'}:
        return 'infrastructure'
    return 'core'


def _docker_host_published_port(ports: Optional[str]) -> List[int]:
    """Return every host-published port found in a Docker `Ports` string."""
    if not ports:
        return []
    return [int(m.group(1)) for m in _HOST_PUBLISHED_PORT_RE.finditer(ports)]


def _k8s_label_from_host(host: Optional[str], namespace: Optional[str]) -> Optional[str]:
    """Reduce a row's host to a Deployment label (D4 rule 2):
    a bare RFC1123 label is used as-is; `<label>.<namespace>[.svc[.cluster.local]]`
    gives `<label>`; IPs, other namespaces, and external FQDNs give nothing."""
    if not host:
        return None
    h = host.strip().lower()
    try:
        ipaddress.ip_address(h)
        return None
    except ValueError:
        pass
    if DEPLOYMENT_NAME_RE.match(h):
        return h
    if namespace:
        for suffix in (f".{namespace}.svc.cluster.local", f".{namespace}.svc", f".{namespace}"):
            if h.endswith(suffix):
                label = h[: -len(suffix)]
                if label and DEPLOYMENT_NAME_RE.match(label):
                    return label
    return None


@dataclass
class ControlAgentInventory:
    enabled: bool
    reachable: bool = False
    processes: List[dict] = field(default_factory=list)
    containers: List[dict] = field(default_factory=list)
    docker_available: bool = True
    note: Optional[str] = None


@dataclass
class KubernetesInventory:
    enabled: bool
    available: bool
    reason: Optional[str] = None
    namespace: Optional[str] = None
    deployments: Dict[str, object] = field(default_factory=dict)  # name -> DeploymentInfo


@dataclass
class Inventory:
    control_agent: ControlAgentInventory
    kubernetes: Optional[KubernetesInventory] = None


_inventory_cache: Dict[str, Tuple[float, Inventory]] = {}


def _clear_inventory_cache() -> None:
    """Test-only helper — the module cache is process/global, so tests that
    inject a fresh transport must clear it first."""
    _inventory_cache.clear()


def _build_ca_client(timeout: float = 3.0) -> httpx.AsyncClient:
    kwargs: dict = {"timeout": timeout, "headers": control_agent_headers()}
    if _ca_transport is not None:
        kwargs["transport"] = _ca_transport
    return httpx.AsyncClient(**kwargs)


async def _gather_control_agent_inventory() -> ControlAgentInventory:
    cfg = get_config()
    if not cfg.control_agent_enabled:
        return ControlAgentInventory(enabled=False, reachable=False, docker_available=False)

    processes: List[dict] = []
    containers: List[dict] = []
    docker_available = True
    note: Optional[str] = None
    reachable = True

    async with _build_ca_client() as client:
        try:
            proc_resp = await client.get(f"{CONTROL_AGENT_URL}/process/list")
            if proc_resp.status_code == 200:
                processes = proc_resp.json()
            else:
                reachable = False
        except httpx.HTTPError:
            reachable = False

        if reachable:
            try:
                docker_resp = await client.get(f"{CONTROL_AGENT_URL}/docker/list")
                if docker_resp.status_code == 200:
                    containers = docker_resp.json()
                else:
                    docker_available = False
                    note = 'control_agent_docker_unavailable'
            except httpx.HTTPError:
                docker_available = False
                note = 'control_agent_docker_unavailable'

    if not reachable:
        note = 'control_agent_unreachable'

    return ControlAgentInventory(
        enabled=True,
        reachable=reachable,
        processes=processes,
        containers=containers,
        docker_available=docker_available,
        note=note,
    )


async def _gather_kubernetes_inventory() -> KubernetesInventory:
    default_namespace = os.getenv("ATHENA_NAMESPACE", "athena-prod")
    client, reason = get_k8s_client()
    if client is None:
        return KubernetesInventory(
            enabled=get_config().service_control_k8s_enabled,
            available=False,
            reason=reason,
            namespace=default_namespace,
        )
    try:
        deployments = await client.list_deployments()
    except K8sControlError as exc:
        return KubernetesInventory(enabled=True, available=False, reason=exc.kind, namespace=client.namespace)
    except Exception as exc:  # noqa: BLE001 -- inventory gathering must never
        # crash the page (D14's "never raise" principle, extended past
        # K8sControlError): a token-file read failure or any other
        # unexpected error degrades to unavailable, not a 500.
        logger.error("k8s_inventory_gather_failed", error_kind=type(exc).__name__)
        return KubernetesInventory(enabled=True, available=False, reason="unavailable", namespace=client.namespace)
    return KubernetesInventory(
        enabled=True,
        available=True,
        reason=None,
        namespace=client.namespace,
        deployments={d.name: d for d in deployments},
    )


async def gather_inventory(fresh: bool = False) -> Inventory:
    """Fetch (or reuse a <=10s-old cached) snapshot of every manager's
    inventory (D14). Action routes always pass fresh=True so a
    client-supplied target can never ride a stale cache."""
    cache_key = "default"
    now = time.monotonic()
    if not fresh:
        cached = _inventory_cache.get(cache_key)
        if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

    ca_inventory = await _gather_control_agent_inventory()
    k8s_inventory = await _gather_kubernetes_inventory()
    inv = Inventory(control_agent=ca_inventory, kubernetes=k8s_inventory)
    _inventory_cache[cache_key] = (now, inv)
    return inv


@dataclass
class ManagerResolution:
    manager: str  # 'control_agent' | 'kubernetes' | 'none'
    kind: Optional[str] = None  # 'process' | 'docker' | 'ollama' | 'kubernetes' | None
    target: Optional[str] = None
    note: Optional[str] = None
    native_state: Optional[str] = None
    native_actions: List[str] = field(default_factory=list)
    actions: List[str] = field(default_factory=list)  # per-user gated (D20)
    confirm_required: bool = False
    confirm_name: Optional[str] = None
    critical: bool = False
    k8s_replicas: Optional[int] = None
    k8s_ready_replicas: Optional[int] = None


def _gate_for_user(
    manager: str,
    kind: Optional[str],
    target: Optional[str],
    native_state: Optional[str],
    native_actions: List[str],
    permissions: Set[str],
    critical: bool,
    confirm_name: Optional[str],
    note: Optional[str] = None,
) -> ManagerResolution:
    """Apply the D20 owner gate: a critical resolution is only actionable
    (has non-empty `actions`) for a caller with `manage_infrastructure`.
    `native_actions` always reflects the manager's real state and is what
    the action-availability check (409 action_not_available) is measured
    against — never the per-user-gated list (mozart r3a amendment)."""
    if critical and 'manage_infrastructure' not in permissions:
        actions: List[str] = []
        result_note = 'requires_owner'
    else:
        actions = list(native_actions)
        result_note = note
    return ManagerResolution(
        manager=manager,
        kind=kind,
        target=target,
        note=result_note,
        native_state=native_state,
        native_actions=native_actions,
        actions=actions,
        confirm_required=critical,
        confirm_name=confirm_name,
        critical=critical,
        k8s_replicas=None,
        k8s_ready_replicas=None,
    )


def _try_kubernetes(row: RagService, inv: Inventory, permissions: Set[str]) -> Optional[ManagerResolution]:
    """Kubernetes resolution (D4 rule 2, Phase 2). Returns None when the
    row's host doesn't reduce to a Deployment label at all -- that's "this
    row isn't a k8s row", distinct from every other branch's terminal
    'none' resolutions (which mean "it IS a k8s row, but ...")."""
    k8s = inv.kubernetes
    if k8s is None:
        return None

    label = _k8s_label_from_host(row.host, k8s.namespace)
    if not label:
        return None

    if not k8s.available:
        return ManagerResolution(manager='none', note=f'kubernetes_unavailable:{k8s.reason}')

    if label in PROTECTED_DEPLOYMENTS:
        return ManagerResolution(manager='none', note='protected')

    deployment = k8s.deployments.get(label)
    if deployment is None:
        return ManagerResolution(manager='none', note=f'no_deployment:{label}')

    native_state = f"{deployment.ready_replicas}/{deployment.replicas} pods"
    native_actions = ['stop', 'restart'] if deployment.replicas > 0 else ['start']
    # Fail-safe critical (D9/L3): the named set, UNION any k8s-resolved row
    # whose group isn't 'rag' -- so a renamed/unlisted core Deployment still
    # gets the typed confirm instead of silently losing it.
    critical = label in CRITICAL_DEPLOYMENTS or group_for(row) != 'rag'
    resolution = _gate_for_user(
        'kubernetes', 'kubernetes', label, native_state, native_actions,
        permissions, critical=critical, confirm_name=label,
    )
    resolution.k8s_replicas = deployment.replicas
    resolution.k8s_ready_replicas = deployment.ready_replicas
    return resolution


def _get_ollama_port(db) -> Optional[int]:
    """The configured Ollama URL's port, or None if it can't be read (e.g.
    no db session available). Local import mirrors resolve_ollama_manager's
    own trick to avoid a module-load cycle with app.routes.service_control."""
    if db is None:
        return None
    from app.routes.service_control import get_ollama_url  # local import: avoid a module-load cycle
    try:
        return urlparse(get_ollama_url(db)).port
    except Exception:  # noqa: BLE001 -- never let a port lookup break resolution
        return None


def resolve_manager(
    row: RagService,
    inv: Inventory,
    permissions: Optional[Set[str]] = None,
    ollama_port: Optional[int] = None,
) -> ManagerResolution:
    """Resolve the manager for a single registry row (D4).

    Precedence: Control Agent (host-gated) -> Kubernetes -> none. CA's own
    unreachability/docker-unavailability notes apply ONLY to rows whose
    host actually equals the CA host -- a non-CA row must still get a
    chance at Kubernetes resolution even when the CA happens to be down.

    codex diff review r1 (Critical #1/#2): criticality must follow the
    TARGET under every manager, not just Kubernetes -- a CA-managed
    process/container resolving to a named-critical or non-`rag` row is
    exactly as dangerous to stop/restart as a Kubernetes Deployment, and
    Ollama specifically must be recognised (and forced critical) BEFORE the
    generic process/docker branches, so a CA process/container that
    happens to occupy Ollama's own port is never resolved as a
    non-critical generic process/container.
    """
    permissions = permissions or set()
    ca = inv.control_agent
    row_host = normalize_host(row.host)

    if ca.enabled:
        ca_host = normalize_host(urlparse(CONTROL_AGENT_URL).hostname)
        if row_host and row_host == ca_host:
            if not ca.reachable:
                return ManagerResolution(manager='none', note='control_agent_unreachable')

            is_ollama_row = bool(row.name and row.name.lower() == 'ollama') or (
                ollama_port is not None and row.port == ollama_port
            )
            if is_ollama_row:
                for proc in ca.processes:
                    if proc.get('port') == row.port:
                        running = bool(proc.get('running'))
                        native_state = 'process running' if running else 'process stopped'
                        native_actions = ['stop', 'restart'] if running else ['start']
                        return _gate_for_user(
                            'control_agent', 'ollama', str(row.port), native_state,
                            native_actions, permissions, critical=True, confirm_name='ollama',
                        )
                for container in ca.containers:
                    if (row.container_name and container.get('name') == row.container_name) or (
                        row.port in _docker_host_published_port(container.get('ports'))
                    ):
                        running = bool(container.get('running'))
                        native_state = 'container up' if running else 'container stopped'
                        native_actions = ['stop', 'restart'] if running else ['start']
                        return _gate_for_user(
                            'control_agent', 'ollama', container.get('name'), native_state,
                            native_actions, permissions, critical=True, confirm_name='ollama',
                        )
                return _gate_for_user(
                    'control_agent', 'ollama', 'ollama', None,
                    ['start', 'stop', 'restart'], permissions,
                    critical=True, confirm_name='ollama',
                )

            critical = bool(row.name) and (
                row.name.lower() in CRITICAL_DEPLOYMENTS or group_for(row) != 'rag'
            )

            for proc in ca.processes:
                if proc.get('port') == row.port:
                    running = bool(proc.get('running'))
                    native_state = 'process running' if running else 'process stopped'
                    native_actions = ['stop', 'restart'] if running else ['start']
                    return _gate_for_user(
                        'control_agent', 'process', str(row.port), native_state,
                        native_actions, permissions, critical=critical,
                        confirm_name=f'process:{row.port}',
                    )

            if not ca.docker_available:
                return ManagerResolution(manager='none', note='control_agent_docker_unavailable')

            for container in ca.containers:
                if row.container_name and container.get('name') == row.container_name:
                    running = bool(container.get('running'))
                    native_state = 'container up' if running else 'container stopped'
                    native_actions = ['stop', 'restart'] if running else ['start']
                    return _gate_for_user(
                        'control_agent', 'docker', container.get('name'), native_state,
                        native_actions, permissions, critical=critical,
                        confirm_name=container.get('name'),
                    )

            for container in ca.containers:
                if row.port in _docker_host_published_port(container.get('ports')):
                    running = bool(container.get('running'))
                    native_state = 'container up' if running else 'container stopped'
                    native_actions = ['stop', 'restart'] if running else ['start']
                    return _gate_for_user(
                        'control_agent', 'docker', container.get('name'), native_state,
                        native_actions, permissions, critical=critical,
                        confirm_name=container.get('name'),
                    )

            # CA host matched but nothing resolved inside -- deliberately
            # falls through to the generic terminal return below, not to
            # Kubernetes: a row whose host IS the CA's own host is never a
            # cluster-internal Deployment.
            return ManagerResolution(manager='none', note='managed_externally')

    k8s_resolution = _try_kubernetes(row, inv, permissions)
    if k8s_resolution is not None:
        return k8s_resolution

    if not ca.enabled and inv.kubernetes is None:
        return ManagerResolution(manager='none')
    return ManagerResolution(manager='none', note='managed_externally')


def resolve_ollama_manager(
    db, inv: Inventory, permissions: Optional[Set[str]] = None,
) -> Tuple[ManagerResolution, Optional[str]]:
    """Resolve the manager for the Ollama card (D12).

    Picks the registry row whose (normalize_host(host), port) equals the
    Ollama URL's, else a row named 'ollama', else a synthetic row -- and
    runs the same resolve_manager. Ollama is critical under any manager
    (bob r2 M3), enforced inside resolve_manager's 'ollama' kind branch and
    by the k8s fail-safe critical set (D9) for the in-cluster case.

    Returns (resolution, row_name) -- row_name is None for the synthetic
    row (no registry row backs it), so callers never try to look up or
    audit against a nonexistent RagService id.
    """
    from app.routes.service_control import get_ollama_url  # local import: avoid a module-load cycle

    ollama_url = get_ollama_url(db)
    parsed = urlparse(ollama_url)
    ollama_host = normalize_host(parsed.hostname)
    ollama_port = parsed.port

    candidates = (
        db.query(RagService)
        .filter(RagService.host.isnot(None))
        .all()
    )
    matched = None
    for candidate in candidates:
        if normalize_host(candidate.host) == ollama_host and candidate.port == ollama_port:
            matched = candidate
            break
    if matched is None:
        matched = next((c for c in candidates if (c.name or '').lower() == 'ollama'), None)

    row_name = matched.name if matched is not None else None
    if matched is None:
        matched = RagService(name='ollama', host=parsed.hostname, port=ollama_port, enabled=True)

    # Ollama is critical under every manager (bob r2 M3): the Control Agent
    # branch above forces critical=True via the is_ollama_row check, and
    # the Kubernetes branch's own fail-safe (label in CRITICAL_DEPLOYMENTS
    # or group_for(row) != 'rag') is always true for an 'ollama'-named row
    # (group_for never classifies it 'rag'), so no separate assertion is
    # needed here -- asserted instead by test_service_control_ollama.py.
    resolution = resolve_manager(matched, inv, permissions=permissions, ollama_port=ollama_port)
    return resolution, row_name
