"""
Service Control Routes

Start, stop, restart Athena services and Ollama models.
Uses Control Agent pattern for secure service management.

ATHENA-118 (Phase 1): run state is derived from health (D2), never from the
deprecated `is_running` column; `GET /api/service-control` returns a single
envelope (D3) with server-resolved per-row managers (D4); lifecycle actions
route through one audited `_run_action` core (D9/D10/D20).
"""

from datetime import datetime
from typing import List, Optional, Tuple
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
import structlog
import httpx
import asyncio

from app.database import get_db, SessionLocal
from app.models import RagService, User, LLMBackend, SystemSetting
from app.auth.oidc import get_current_user
from app.utils.service_auth import control_agent_headers
from app.utils.rate_limit import service_control_rate_limit_dep
from app.utils.service_state import derive_run_state
from app.routes.service_registry import _SERVICE_NAME_RE
from app.routes.services import create_audit_log
from app.services.service_managers import (
    CONTROL_AGENT_URL,
    ManagerResolution,
    gather_inventory,
    group_for,
    resolve_manager,
)
from app.services.k8s_control import K8sControlError, get_k8s_client
from app.services.service_control_settings import (
    LeaseBusy,
    acquire_lease,
    read_lease,
    lease_is_expired,
    recall_replicas,
    release_lease,
    remember_replicas,
)
from shared.config import get_config

logger = structlog.get_logger()
router = APIRouter(prefix="/api/service-control", tags=["service-control"])

# The cross-replica lease (D11) needs its OWN short-lived DB session, visible
# to another admin-backend replica immediately and independent of the
# request's own transaction -- never the request-scoped `db: Session =
# Depends(get_db)`. Production uses the app's real SessionLocal; tests
# monkeypatch this module attribute to a sessionmaker bound to the same
# SQLite engine as the test's `db` fixture (ATHENA-118 test hook, same
# pattern as service_managers._ca_transport).
LEASE_SESSION_FACTORY = SessionLocal


def get_ollama_url(db: Session) -> str:
    """
    Get centralized Ollama URL from system_settings.

    The system_settings table is the single source of truth.
    Falls back to OLLAMA_URL environment variable if not set.
    """
    setting = db.query(SystemSetting).filter(SystemSetting.key == "ollama_url").first()
    if setting and setting.value:
        return setting.value
    return get_config().ollama_url


# Pydantic Models
class ServiceResponse(BaseModel):
    id: int
    name: str
    service_name: str  # back-compat alias — mirrors RagService.service_name property
    display_name: str
    description: Optional[str] = None
    service_type: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    health_endpoint: Optional[str] = None
    control_method: Optional[str] = None
    container_name: Optional[str] = None
    is_running: bool = False  # DEPRECATED (D1): derived from run_state, not the column
    run_state: str = "stopped"
    last_health_check: Optional[str] = None
    last_error: Optional[str] = None
    auto_start: bool = True
    enabled: bool = True

    class Config:
        from_attributes = True
        extra = "ignore"


class ServiceControlRow(ServiceResponse):
    """A registry row plus the server-resolved manager fields (D3)."""
    group: str = "core"
    manager: str = "none"
    manager_target: Optional[str] = None
    manager_note: Optional[str] = None
    native_state: Optional[str] = None
    actions: List[str] = []
    confirm_required: bool = False
    k8s_replicas: Optional[int] = None
    k8s_ready_replicas: Optional[int] = None


class ServiceControlCounts(BaseModel):
    running: int
    stopped: int
    disabled: int


class ControlAgentStatus(BaseModel):
    enabled: bool
    reachable: bool
    note: Optional[str] = None


class KubernetesStatus(BaseModel):
    """Kubernetes manager status (D3). Phase 1 always reports the feature as
    disabled — the adapter and its config flag land in Phase 2."""
    enabled: bool = False
    available: bool = False
    reason: Optional[str] = "disabled"


class ServiceControlListResponse(BaseModel):
    services: List[ServiceControlRow]
    counts: ServiceControlCounts
    control_agent: ControlAgentStatus
    kubernetes: KubernetesStatus


class ServiceActionRequest(BaseModel):
    """Body for lifecycle actions.

    `confirm_name` must equal the RESOLVED target's name for a critical
    action — the Deployment (or Ollama's conceptual identity), never the
    row's own display name when the row is an alias onto that target
    (D4.4 / mozart r3a). Unknown/legacy fields (e.g. a stale `target`) are
    ignored, never trusted for dispatch (T4.24).
    """
    confirm_name: Optional[str] = None

    class Config:
        extra = "ignore"


class ServiceActionResponse(BaseModel):
    service_name: str
    action: str
    success: bool
    message: str


class OllamaModelResponse(BaseModel):
    name: str
    size: int
    loaded: bool
    modified_at: str


class ModelActionResponse(BaseModel):
    model_name: str
    action: str
    success: bool
    message: str


def _audit_lifecycle(
    db: Session,
    user: User,
    request: Optional[Request],
    action: str,
    service: Optional[RagService],
    old_value: dict,
    new_value: dict,
    success: bool,
    error_message: Optional[str] = None,
) -> None:
    """Audit-write wrapper that never raises (M4).

    A DB hiccup on the audit write must not turn a successful (or already-
    refused) mutation into a 500 that invites a client retry — retries on a
    non-idempotent action are exactly what D11/M-4 exist to prevent
    upstream of this helper. Rolls back only the audit statement's own
    failed state; the caller's own commit (if any) already happened.
    """
    try:
        create_audit_log(
            db=db,
            user=user,
            action=action,
            service=service,
            old_value=old_value,
            new_value=new_value,
            request=request,
            success=success,
            error_message=error_message,
        )
    except Exception as exc:  # noqa: BLE001 — audit failures must never propagate
        db.rollback()
        logger.error("service_control_audit_failed", action=action, error=str(exc))


async def _run_action(
    service_name: str,
    action: str,
    body: ServiceActionRequest,
    request: Optional[Request],
    db: Session,
    current_user: User,
) -> ServiceActionResponse:
    """Shared lifecycle-action core for start/stop/restart (D9/D10/D20).

    Order (mozart r3a amendment): permission -> name validation -> row
    lookup -> resolution -> the manage_infrastructure gate for a critical
    target (403, evaluated BEFORE action availability) -> action-not-
    available (409, native/un-gated actions) -> typed confirm (409) ->
    dispatch -> audit. Only steps at or after resolution write an audit row.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not _SERVICE_NAME_RE.match(service_name):
        raise HTTPException(status_code=422, detail="Invalid service name")

    service = db.query(RagService).filter(RagService.name == service_name).first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")

    inv = await gather_inventory(fresh=True)
    permissions = current_user.get_permissions()
    resolution = resolve_manager(service, inv, permissions)

    old_value = {
        "run_state": derive_run_state(service.enabled, service.health_status, resolution.k8s_replicas),
        "health_status": service.health_status,
        "native_state": resolution.native_state,
        "k8s_replicas": resolution.k8s_replicas,
    }

    if resolution.critical and 'manage_infrastructure' not in permissions:
        _audit_lifecycle(
            db, current_user, request, f"service_{action}", service,
            old_value, {}, success=False, error_message='insufficient_role',
        )
        raise HTTPException(status_code=403, detail={"error": "insufficient_role"})

    if action not in resolution.native_actions:
        _audit_lifecycle(
            db, current_user, request, f"service_{action}", service,
            old_value, {}, success=False, error_message='action_not_available',
        )
        raise HTTPException(
            status_code=409,
            detail={"error": "action_not_available", "manager_note": resolution.note},
        )

    if resolution.critical and body.confirm_name != resolution.confirm_name:
        _audit_lifecycle(
            db, current_user, request, f"service_{action}", service,
            old_value, {}, success=False, error_message='confirmation_required',
        )
        raise HTTPException(status_code=409, detail={"error": "confirmation_required"})

    replicas_after = resolution.k8s_replicas

    if resolution.kind == 'process':
        success, message = await process_service_action(service.port, action)
    elif resolution.kind == 'docker':
        success, message = await docker_service_action(service.container_name, action)
    elif resolution.kind == 'ollama':
        success, message = await launchd_service_action("ollama", action)
    elif resolution.kind == 'kubernetes':
        success, message, replicas_after = await _dispatch_kubernetes_action(
            resolution.target, action, db, current_user, request, service, old_value,
        )
    else:
        success, message = False, f"Service '{service.name}' is not managed by any control plane"

    new_value = {
        "manager": resolution.manager,
        "kind": resolution.kind,
        "target": resolution.target,
        "message": message,
        "replicas_after": replicas_after,
    }
    _audit_lifecycle(db, current_user, request, f"service_{action}", service, old_value, new_value, success=success)

    logger.info(f"service_{action}", service=service_name, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=service_name,
        action=action,
        success=success,
        message=message,
    )


async def _dispatch_kubernetes_action(
    deployment_name: str,
    action: str,
    db: Session,
    current_user: User,
    request: Optional[Request],
    service: RagService,
    old_value: dict,
) -> Tuple[bool, str, Optional[int]]:
    """Kubernetes dispatch (D11): acquires the cross-replica lease around
    the WHOLE action (including restart's bounded wait), releases it in
    `finally`. Lease contention -> 409 `action_in_progress`, audited, raised
    here directly (distinct from _run_action's other refusals, since it can
    only be known after we've committed to dispatching)."""
    client, reason = get_k8s_client()
    if client is None:
        return False, f"Kubernetes control is unavailable ({reason})", None

    try:
        lease = acquire_lease(LEASE_SESSION_FACTORY, deployment_name, action, target_replicas=0)
    except LeaseBusy:
        _audit_lifecycle(
            db, current_user, request, f"service_{action}", service,
            old_value, {}, success=False, error_message='action_in_progress',
        )
        raise HTTPException(status_code=409, detail={"error": "action_in_progress"})

    remembered: dict = {}

    def _remember(n: int) -> None:
        remembered['n'] = n
        remember_replicas(db, deployment_name, n)

    def _recall() -> int:
        n = recall_replicas(db, deployment_name)
        remembered['n'] = n
        return n

    try:
        if action == 'stop':
            result = await client.stop(deployment_name, remember=_remember)
            replicas_after = 0 if result.success else remembered.get('n')
        elif action == 'start':
            result = await client.start(deployment_name, recall=_recall)
            replicas_after = remembered.get('n') if result.success else None
        else:  # restart
            result = await client.restart(deployment_name, remember=_remember, recall=_recall)
            # D11: the scale-back always targets the remembered count,
            # regardless of whether the bounded wait settled or timed out.
            replicas_after = remembered.get('n')
        return result.success, result.message, replicas_after
    except K8sControlError as exc:
        return False, f"Kubernetes API error ({exc.kind}): {exc.message}", None
    finally:
        release_lease(LEASE_SESSION_FACTORY, lease)


# Service Routes
@router.get("", response_model=ServiceControlListResponse)
async def list_services(
    service_type: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all Athena services with server-resolved manager/state (D3)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(RagService)
    if service_type:
        query = query.filter(RagService.service_type == service_type)

    services = query.order_by(RagService.service_type, RagService.display_name).all()

    inv = await gather_inventory(fresh=False)
    permissions = current_user.get_permissions()

    resolutions = [(svc, resolve_manager(svc, inv, permissions)) for svc in services]

    # D4.4: any Kubernetes target reached by >=2 rows (enabled or disabled)
    # blocks all of them, so an aliased row can't hijack a critical target.
    target_counts: dict = {}
    for _svc, resolution in resolutions:
        if resolution.manager == 'kubernetes' and resolution.target:
            target_counts[resolution.target] = target_counts.get(resolution.target, 0) + 1

    rows = []
    running = stopped = disabled = 0
    for svc, resolution in resolutions:
        if resolution.manager == 'kubernetes' and resolution.target and target_counts[resolution.target] > 1:
            resolution = ManagerResolution(manager='none', note=f"target_collision:{resolution.target}")
        elif resolution.manager == 'kubernetes' and resolution.target:
            # D11: an expired restart lease whose Deployment is stuck at 0
            # replicas means the admin-backend process died mid-wait --
            # surface it so an operator presses Start rather than assuming
            # the service is just "stopped".
            lease_info = read_lease(db, resolution.target)
            if (
                lease_info
                and lease_info.get('action') == 'restart'
                and resolution.k8s_replicas == 0
                and lease_is_expired(lease_info)
            ):
                resolution.note = 'restart_interrupted'

        run_state = derive_run_state(svc.enabled, svc.health_status, resolution.k8s_replicas)
        if run_state == 'disabled':
            disabled += 1
        elif run_state == 'running':
            running += 1
        else:
            stopped += 1

        row = svc.to_dict()
        row['run_state'] = run_state
        row['group'] = group_for(svc)
        row['manager'] = resolution.manager
        row['manager_target'] = resolution.target
        row['manager_note'] = resolution.note
        row['native_state'] = resolution.native_state
        row['actions'] = resolution.actions
        row['confirm_required'] = resolution.confirm_required
        row['k8s_replicas'] = resolution.k8s_replicas
        row['k8s_ready_replicas'] = resolution.k8s_ready_replicas
        rows.append(row)

    return ServiceControlListResponse(
        services=rows,
        counts=ServiceControlCounts(running=running, stopped=stopped, disabled=disabled),
        control_agent=ControlAgentStatus(
            enabled=inv.control_agent.enabled,
            reachable=inv.control_agent.reachable,
            note=inv.control_agent.note,
        ),
        kubernetes=KubernetesStatus(
            enabled=inv.kubernetes.enabled if inv.kubernetes else False,
            available=inv.kubernetes.available if inv.kubernetes else False,
            reason=inv.kubernetes.reason if inv.kubernetes else 'disabled',
        ),
    )


@router.post("/refresh-status", dependencies=[Depends(service_control_rate_limit_dep)])
async def refresh_all_service_status(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Trigger an immediate health-poll cycle for all services.

    Phase 4 reconcile (ian HIGH / xander HIGH-1, ATHENA-1): the previous
    implementation called check_all_services_health which had its own httpx
    loop with legacy status vocabulary ('online'/'degraded'/'offline') and no
    SSRF guard.  Redirected to the Phase 4 poller so status vocabulary and
    SSRF protection are consistent.  Background-task semantics preserved:
    caller gets immediate 200, poll runs asynchronously.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    from app.services.health_poller import _poll_all_services
    import asyncio as _asyncio
    semaphore = _asyncio.Semaphore(get_config().health_poll_concurrency)
    background_tasks.add_task(_poll_all_services, semaphore)

    return {"message": "Health check refresh started", "status": "pending"}


# ATHENA-118 mid-build fix (tessa P1, Medium #2): the literal /ollama/*
# routes MUST be registered before the parametrized /{service_name}/*
# routes below. Starlette matches routes in registration order, and
# /{service_name}/start matches ANY first path segment -- including
# "ollama" -- so if it were registered first, POST /ollama/start would be
# silently swallowed by _run_action (dispatching against a RagService row
# literally named "ollama", 404 if none exists) instead of ever reaching
# the dedicated Ollama handlers below. Phase 3 rewrites these handlers to
# go through _run_action explicitly (D12); until then, this ordering is
# the only thing making them reachable at all. test_service_control_route_
# parity.py pins this explicitly.
class OllamaHealthResponse(BaseModel):
    healthy: bool
    status: str
    api_reachable: bool
    models_loaded: int
    version: Optional[str] = None
    timestamp: str
    host: Optional[str] = None


@router.get("/ollama/health", response_model=OllamaHealthResponse)
async def get_ollama_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get Ollama health status via Control Agent.

    Returns actual API reachability, not just brew services status.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if not get_config().control_agent_enabled:
        return OllamaHealthResponse(
            healthy=False,
            status="control_agent_disabled",
            api_reachable=False,
            models_loaded=0,
            version=None,
            timestamp=datetime.utcnow().isoformat(),
            host=None,
        )

    # Get centralized Ollama URL for display
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=control_agent_headers()) as client:
            response = await client.get(f"{CONTROL_AGENT_URL}/ollama/health")

            if response.status_code == 200:
                data = response.json()
                data['host'] = ollama_url
                return OllamaHealthResponse(**data)
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Control Agent error"
                )

    except httpx.ConnectError:
        # Control Agent not reachable - return unhealthy status
        from datetime import datetime
        return OllamaHealthResponse(
            healthy=False,
            status="control_agent_offline",
            api_reachable=False,
            models_loaded=0,
            version=None,
            timestamp=datetime.utcnow().isoformat(),
            host=ollama_url
        )
    except Exception as e:
        logger.error("ollama_health_check_failed", error=str(e))
        from datetime import datetime
        return OllamaHealthResponse(
            healthy=False,
            status="error",
            api_reachable=False,
            models_loaded=0,
            version=None,
            timestamp=datetime.utcnow().isoformat(),
            host=ollama_url
        )


@router.post("/ollama/start", response_model=ServiceActionResponse, dependencies=[Depends(service_control_rate_limit_dep)])
async def start_ollama(
    current_user: User = Depends(get_current_user)
):
    """Start Ollama service via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await launchd_service_action("ollama", "start")

    logger.info("ollama_start", success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name="ollama",
        action="start",
        success=success,
        message=message
    )


@router.post("/ollama/stop", response_model=ServiceActionResponse, dependencies=[Depends(service_control_rate_limit_dep)])
async def stop_ollama(
    current_user: User = Depends(get_current_user)
):
    """Stop Ollama service via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await launchd_service_action("ollama", "stop")

    logger.info("ollama_stop", success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name="ollama",
        action="stop",
        success=success,
        message=message
    )


@router.post("/ollama/restart", response_model=ServiceActionResponse, dependencies=[Depends(service_control_rate_limit_dep)])
async def restart_ollama(
    current_user: User = Depends(get_current_user)
):
    """Restart Ollama service via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await launchd_service_action("ollama", "restart")

    logger.info("ollama_restart", success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name="ollama",
        action="restart",
        success=success,
        message=message
    )


@router.post("/{service_name}/start", response_model=ServiceActionResponse)
async def start_service(
    service_name: str,
    request: Request,
    body: Optional[ServiceActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(service_control_rate_limit_dep),
):
    """Start an Athena service."""
    return await _run_action(service_name, "start", body or ServiceActionRequest(), request, db, current_user)


@router.post("/{service_name}/stop", response_model=ServiceActionResponse)
async def stop_service(
    service_name: str,
    request: Request,
    body: Optional[ServiceActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(service_control_rate_limit_dep),
):
    """Stop an Athena service."""
    return await _run_action(service_name, "stop", body or ServiceActionRequest(), request, db, current_user)


@router.post("/{service_name}/restart", response_model=ServiceActionResponse)
async def restart_service(
    service_name: str,
    request: Request,
    body: Optional[ServiceActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(service_control_rate_limit_dep),
):
    """Restart an Athena service."""
    return await _run_action(service_name, "restart", body or ServiceActionRequest(), request, db, current_user)


# Container Status Route (via Control Agent)
@router.get("/containers/status")
async def get_containers_status(
    current_user: User = Depends(get_current_user)
):
    """Get real-time status of Athena containers from Control Agent."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    if not get_config().control_agent_enabled:
        logger.info("control_agent_disabled", route="containers_status")
        return []

    try:
        async with httpx.AsyncClient(timeout=10.0, headers=control_agent_headers()) as client:
            response = await client.get(f"{CONTROL_AGENT_URL}/docker/list")

            if response.status_code == 200:
                return response.json()
            else:
                raise HTTPException(
                    status_code=response.status_code,
                    detail="Control Agent error"
                )

    except httpx.ConnectError:
        raise HTTPException(
            status_code=503,
            detail=(
                "Control Agent not reachable. Set CONTROL_AGENT_URL to the correct "
                "host or set CONTROL_AGENT_ENABLED=false to disable."
            ),
        )
    except Exception as e:
        logger.error("container_status_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# Ollama Model Control Routes
@router.get("/ollama/models", response_model=List[OllamaModelResponse])
async def list_ollama_models(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all models available in Ollama."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Use centralized Ollama URL from system_settings
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Get available models
            tags_response = await client.get(f"{ollama_url}/api/tags")
            tags_response.raise_for_status()
            available = tags_response.json().get("models", [])

            # Get currently loaded models
            ps_response = await client.get(f"{ollama_url}/api/ps")
            ps_response.raise_for_status()
            loaded_models = [m["name"] for m in ps_response.json().get("models", [])]

        models = []
        for model in available:
            models.append(OllamaModelResponse(
                name=model["name"],
                size=model.get("size", 0),
                loaded=model["name"] in loaded_models,
                modified_at=model.get("modified_at", "")
            ))

        return models

    except Exception as e:
        logger.error("ollama_models_list_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to list models: {str(e)}")


@router.post(
    "/ollama/models/{model_name:path}/load",
    response_model=ModelActionResponse,
    dependencies=[Depends(service_control_rate_limit_dep)],
)
async def load_ollama_model(
    model_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Load a model into Ollama memory."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Use centralized Ollama URL from system_settings
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Send a simple generate request to load the model
            response = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": model_name, "prompt": "hello", "stream": False}
            )
            response.raise_for_status()

        logger.info("ollama_model_loaded", model=model_name, user=current_user.username)

        return ModelActionResponse(
            model_name=model_name,
            action="load",
            success=True,
            message=f"Model '{model_name}' loaded successfully"
        )

    except Exception as e:
        logger.error("ollama_model_load_failed", model=model_name, error=str(e))
        return ModelActionResponse(
            model_name=model_name,
            action="load",
            success=False,
            message=f"Failed to load model: {str(e)}"
        )


@router.post(
    "/ollama/models/{model_name:path}/unload",
    response_model=ModelActionResponse,
    dependencies=[Depends(service_control_rate_limit_dep)],
)
async def unload_ollama_model(
    model_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Unload a model from Ollama memory."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Use centralized Ollama URL from system_settings
    ollama_url = get_ollama_url(db)

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Ollama unloads via generate with keep_alive=0
            response = await client.post(
                f"{ollama_url}/api/generate",
                json={"model": model_name, "prompt": "", "keep_alive": 0}
            )
            response.raise_for_status()

        logger.info("ollama_model_unloaded", model=model_name, user=current_user.username)

        return ModelActionResponse(
            model_name=model_name,
            action="unload",
            success=True,
            message=f"Model '{model_name}' unloaded successfully"
        )

    except Exception as e:
        logger.error("ollama_model_unload_failed", model=model_name, error=str(e))
        return ModelActionResponse(
            model_name=model_name,
            action="unload",
            success=False,
            message=f"Failed to unload model: {str(e)}"
        )


# Helper Functions
async def docker_service_action(container_name: str, action: str) -> Tuple[bool, str]:
    """
    Execute Docker container action via Control Agent.

    Control Agent provides
    HTTP endpoints for secure Docker container management.
    """
    if not get_config().control_agent_enabled:
        return False, "Control Agent disabled"
    try:
        async with httpx.AsyncClient(timeout=65.0, headers=control_agent_headers()) as client:
            # Map action to Control Agent endpoint
            response = await client.post(
                f"{CONTROL_AGENT_URL}/docker/{action}/{container_name}"
            )

            if response.status_code == 200:
                result = response.json()
                return result.get("success", False), result.get("message", "Unknown response")
            elif response.status_code == 403:
                return False, f"Container '{container_name}' not allowed by Control Agent"
            else:
                return False, f"Control Agent returned status {response.status_code}"

    except httpx.ConnectError:
        logger.warning("control_agent_unreachable", container=container_name, action=action)
        return False, "Control Agent not reachable. Set CONTROL_AGENT_URL to the correct host or set CONTROL_AGENT_ENABLED=false to disable."
    except httpx.TimeoutException:
        return False, "Control Agent request timed out"
    except Exception as e:
        logger.error("docker_action_failed", container=container_name, action=action, error=str(e))
        return False, f"Docker control failed: {str(e)}"


async def process_service_action(port: int, action: str) -> Tuple[bool, str]:
    """
    Execute Python process action via Control Agent.

    Control Agent provides
    HTTP endpoints for managing Python/uvicorn processes by port.
    """
    if not get_config().control_agent_enabled:
        return False, "Control Agent disabled"
    try:
        async with httpx.AsyncClient(timeout=65.0, headers=control_agent_headers()) as client:
            # Map action to Control Agent process endpoint
            response = await client.post(
                f"{CONTROL_AGENT_URL}/process/{action}/{port}"
            )

            if response.status_code == 200:
                result = response.json()
                return result.get("success", False), result.get("message", "Unknown response")
            elif response.status_code == 403:
                return False, f"Port {port} not allowed by Control Agent"
            else:
                return False, f"Control Agent returned status {response.status_code}"

    except httpx.ConnectError:
        logger.warning("control_agent_unreachable", port=port, action=action)
        return False, "Control Agent not reachable. Set CONTROL_AGENT_URL to the correct host or set CONTROL_AGENT_ENABLED=false to disable."
    except httpx.TimeoutException:
        return False, "Control Agent request timed out"
    except Exception as e:
        logger.error("process_action_failed", port=port, action=action, error=str(e))
        return False, f"Process control failed: {str(e)}"


async def launchd_service_action(service_name: str, action: str) -> Tuple[bool, str]:
    """
    Execute launchd service action via Control Agent.

    Supports Ollama service start/stop/restart on macOS via brew services.
    """
    if not get_config().control_agent_enabled:
        return False, "Control Agent disabled"
    # For Ollama, map to the specific endpoint
    if "ollama" in service_name.lower():
        try:
            async with httpx.AsyncClient(timeout=30.0, headers=control_agent_headers()) as client:
                if action == "restart":
                    response = await client.post(f"{CONTROL_AGENT_URL}/ollama/restart")
                elif action == "start":
                    response = await client.post(f"{CONTROL_AGENT_URL}/ollama/start")
                elif action == "stop":
                    response = await client.post(f"{CONTROL_AGENT_URL}/ollama/stop")
                elif action == "status":
                    response = await client.get(f"{CONTROL_AGENT_URL}/ollama/status")
                else:
                    return False, f"Unsupported action '{action}' for Ollama service"

                if response.status_code == 200:
                    result = response.json()
                    return result.get("success", False), result.get("message", "Unknown response")
                else:
                    return False, f"Control Agent returned status {response.status_code}"

        except httpx.ConnectError:
            logger.warning("control_agent_unreachable", service=service_name, action=action)
            return False, "Control Agent not reachable. Set CONTROL_AGENT_URL to the correct host or set CONTROL_AGENT_ENABLED=false to disable."
        except Exception as e:
            logger.error("launchd_action_failed", service=service_name, action=action, error=str(e))
            return False, f"Launchd control failed: {str(e)}"

    return False, f"Launchd control not implemented for service: {service_name}"


# Port-based service control routes (for Voice Pipelines UI)
@router.post("/port/{port}/start", response_model=ServiceActionResponse)
async def start_service_by_port(
    port: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(service_control_rate_limit_dep),
):
    """Start a Python process service by port via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await process_service_action(port, "start")

    _audit_lifecycle(
        db, current_user, request, "service_start", None,
        {}, {"port": port, "message": message}, success=success,
    )

    logger.info("service_start_by_port", port=port, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=f"port-{port}",
        action="start",
        success=success,
        message=message
    )


@router.post("/port/{port}/stop", response_model=ServiceActionResponse)
async def stop_service_by_port(
    port: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(service_control_rate_limit_dep),
):
    """Stop a Python process service by port via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await process_service_action(port, "stop")

    _audit_lifecycle(
        db, current_user, request, "service_stop", None,
        {}, {"port": port, "message": message}, success=success,
    )

    logger.info("service_stop_by_port", port=port, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=f"port-{port}",
        action="stop",
        success=success,
        message=message
    )


@router.post("/port/{port}/restart", response_model=ServiceActionResponse)
async def restart_service_by_port(
    port: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(service_control_rate_limit_dep),
):
    """Restart a Python process service by port via Control Agent."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    success, message = await process_service_action(port, "restart")

    _audit_lifecycle(
        db, current_user, request, "service_restart", None,
        {}, {"port": port, "message": message}, success=success,
    )

    logger.info("service_restart_by_port", port=port, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=f"port-{port}",
        action="restart",
        success=success,
        message=message
    )
