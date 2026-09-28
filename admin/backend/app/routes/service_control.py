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
from urllib.parse import urlparse
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Response
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
from app.utils.rag_urls import check_ssrf_safe
from app.routes.service_registry import _SERVICE_NAME_RE
from app.routes.services import create_audit_log
from app.services.service_managers import (
    CONTROL_AGENT_URL,
    ManagerResolution,
    _clear_inventory_cache,
    _get_ollama_port,
    gather_inventory,
    group_for,
    resolve_manager,
    resolve_ollama_manager,
)
from app.services.k8s_control import K8sControlError, get_k8s_client
from app.services.service_control_settings import (
    LeaseBusy,
    acquire_lease,
    clear_interrupted,
    mark_interrupted,
    read_interrupted,
    read_lease,
    lease_is_expired,
    recall_replicas,
    release_lease,
    remember_replicas,
    renew_lease,
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
    # P4b (ruby H2): these already exist on RagService.to_dict() but were
    # missing here, so `extra="ignore"` silently dropped them from the
    # envelope -- the frontend's health-status rendering had nothing to
    # read. Purely additive; every existing consumer of ServiceControlRow
    # already tolerates unknown-but-present fields.
    protocol: Optional[str] = None
    endpoint_url: Optional[str] = None
    health_status: Optional[str] = None
    health_message: Optional[str] = None
    last_response_time_ms: Optional[int] = None

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
    # native_actions is the manager's real, ungated availability (what the
    # 409 action_not_available check is measured against); actions/
    # allowed_actions is the per-user-gated list the UI renders from (D20 --
    # an operator never sees a dead button for a critical target).
    # allowed_actions is a plan-D3-named alias of actions, not a second
    # independent gate -- both always carry the same value.
    native_actions: List[str] = []
    actions: List[str] = []
    allowed_actions: List[str] = []
    confirm_required: bool = False
    # codex diff review r2 High #1: the server-resolved confirm target --
    # `process:<port>` for a CA process, the container name for CA docker,
    # 'ollama' for Ollama, the Deployment label for Kubernetes. The
    # frontend must type-confirm against THIS value, never guess it from
    # manager_target (which, for a CA process, is just the port number).
    confirm_name: Optional[str] = None
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
    namespace: Optional[str] = None  # plan step 13 (valerie)


class ServiceControlListResponse(BaseModel):
    services: List[ServiceControlRow]
    counts: ServiceControlCounts
    control_agent: ControlAgentStatus
    kubernetes: KubernetesStatus
    # D3 (valerie): the registry row name backing the Ollama card's own
    # resolution, or None when no registry row backs it (the synthetic
    # row resolve_ollama_manager falls back to) -- lets the frontend
    # correlate the Ollama panel with a table row without a second lookup.
    ollama_row: Optional[str] = None


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
    """Lifecycle-action core for the generic /{service_name}/{action}
    routes (D9/D10/D20): permission -> name validation -> row lookup ->
    resolution -> _execute_resolved_action (the shared post-resolution
    core, also used by the Ollama routes, D12)."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if not _SERVICE_NAME_RE.match(service_name):
        raise HTTPException(status_code=422, detail="Invalid service name")

    service = db.query(RagService).filter(RagService.name == service_name).first()
    if not service:
        raise HTTPException(status_code=404, detail=f"Service '{service_name}' not found")

    inv = await gather_inventory(fresh=True)
    permissions = current_user.get_permissions()
    resolution = resolve_manager(service, inv, permissions, ollama_port=_get_ollama_port(db))

    return await _execute_resolved_action(action, body, request, db, current_user, resolution, service, permissions, inv=inv)


async def _run_ollama_action(
    action: str,
    body: ServiceActionRequest,
    request: Optional[Request],
    db: Session,
    current_user: User,
) -> ServiceActionResponse:
    """Ollama lifecycle-action core (D12): resolves through
    resolve_ollama_manager (CA-host-match / Kubernetes / synthetic row),
    then shares the exact same post-resolution core as the generic
    routes. `manager == 'none'` is refused with 409 `ollama_not_manageable`
    rather than the generic `action_not_available` (D12), before any CA or
    k8s call."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    inv = await gather_inventory(fresh=True)
    permissions = current_user.get_permissions()
    resolution, row_name = resolve_ollama_manager(db, inv, permissions)
    service = db.query(RagService).filter(RagService.name == row_name).first() if row_name else None

    return await _execute_resolved_action(
        action, body, request, db, current_user, resolution, service, permissions,
        unmanaged_error='ollama_not_manageable', audit_name='ollama', inv=inv,
    )


async def _fresh_k8s_replica_count(deployment_name: str) -> Optional[int]:
    """A live (never cached) read of one Deployment's spec replica count,
    used ONLY to decide whether to clear a stale restart_interrupted
    marker (codex diff review r3 Medium). The envelope's own
    resolution.k8s_replicas can be up to 10s stale (the shared inventory
    cache) -- trusting it here could delete a still-valid marker within
    that window. Returns None (never clears) on any error: a client
    construction failure, an unreachable API, or the Deployment not
    existing are all reasons to leave the marker alone, not delete it on
    unproven grounds."""
    client, reason = get_k8s_client()
    if client is None:
        return None
    try:
        scale = await client.get_scale(deployment_name)
    except K8sControlError:
        return None
    except Exception:  # noqa: BLE001 -- never let this check crash the envelope
        return None
    return scale.spec_replicas


def _kubernetes_target_counts(db: Session, inv, permissions: set) -> dict:
    """D4.4: how many registry rows (enabled OR disabled) resolve to each
    Kubernetes target. Shared by the envelope (list_services) and the
    action core below, so a collision blocks both display AND dispatch --
    xander P2 Medium #1 found that only the display side was guarded."""
    counts: dict = {}
    for svc in db.query(RagService).all():
        res = resolve_manager(svc, inv, permissions)
        if res.manager == 'kubernetes' and res.target:
            counts[res.target] = counts.get(res.target, 0) + 1
    return counts


async def _execute_resolved_action(
    action: str,
    body: ServiceActionRequest,
    request: Optional[Request],
    db: Session,
    current_user: User,
    resolution: ManagerResolution,
    service: Optional[RagService],
    permissions: set,
    unmanaged_error: Optional[str] = None,
    audit_name: Optional[str] = None,
    inv=None,
) -> ServiceActionResponse:
    """Shared post-resolution dispatch/audit core (D9/D10/D20/D12).

    Order (mozart r3a amendment, xander P2 fix #1): the manage_infrastructure
    gate for a critical target (403, evaluated BEFORE action availability) ->
    target-collision (409, closes the dispatch-side gap the envelope's own
    collision guard didn't cover) -> action-not-available (409, native/
    un-gated actions) -> typed confirm (409) -> dispatch -> audit. Only
    steps at or after resolution write an audit row. `unmanaged_error`
    (e.g. Ollama's `ollama_not_manageable`) short-circuits with 409 before
    the generic action-availability check when the manager is `none` --
    distinct from a resolvable-but-currently-unavailable action.
    """
    name = audit_name or (service.name if service else "unknown")

    old_value = {
        "run_state": derive_run_state(service.enabled, service.health_status, resolution.k8s_replicas) if service else None,
        "health_status": service.health_status if service else None,
        "native_state": resolution.native_state,
        "k8s_replicas": resolution.k8s_replicas,
    }

    if unmanaged_error and resolution.manager == 'none':
        _audit_lifecycle(
            db, current_user, request, f"service_{action}", service,
            old_value, {}, success=False, error_message=unmanaged_error,
        )
        raise HTTPException(status_code=409, detail={"error": unmanaged_error, "manager_note": resolution.note})

    if resolution.critical and 'manage_infrastructure' not in permissions:
        _audit_lifecycle(
            db, current_user, request, f"service_{action}", service,
            old_value, {}, success=False, error_message='insufficient_role',
        )
        raise HTTPException(status_code=403, detail={"error": "insufficient_role"})

    if inv is not None and resolution.manager == 'kubernetes' and resolution.target:
        target_counts = _kubernetes_target_counts(db, inv, permissions)
        if target_counts.get(resolution.target, 0) > 1:
            collision_note = f"target_collision:{resolution.target}"
            _audit_lifecycle(
                db, current_user, request, f"service_{action}", service,
                old_value, {}, success=False, error_message=collision_note,
            )
            raise HTTPException(status_code=409, detail={"error": collision_note})

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
    dispatch_error_code: Optional[str] = None

    try:
        if resolution.kind == 'process':
            success, message = await process_service_action(int(resolution.target), action)
        elif resolution.kind == 'docker':
            success, message = await docker_service_action(resolution.target, action)
        elif resolution.kind == 'ollama':
            success, message = await launchd_service_action("ollama", action)
        elif resolution.kind == 'kubernetes':
            success, message, replicas_after, dispatch_error_code = await _dispatch_kubernetes_action(
                resolution.target, action, db, current_user, request, service, old_value,
            )
        else:
            success, message = False, f"'{name}' is not managed by any control plane"
    except HTTPException:
        # A deliberate, already-audited refusal raised by nested dispatch
        # code (e.g. _dispatch_kubernetes_action's 409 action_in_progress
        # on lease contention) -- propagate as-is, never re-wrap as a 500.
        raise
    except Exception as exc:  # noqa: BLE001 -- xander P2 fix #2: any dispatch-time
        # exception (token-file FileNotFoundError/OSError, a remember/
        # recall DB error, a lease OperationalError, ...) must still audit
        # and return a structured response, never a bare unaudited 500.
        # _dispatch_kubernetes_action's own finally already released the
        # lease regardless of exception type before this is reached.
        error_kind = type(exc).__name__
        logger.error("service_control_dispatch_failed", action=action, service=name, error_kind=error_kind)
        _audit_lifecycle(
            db, current_user, request, f"service_{action}", service,
            old_value, {}, success=False, error_message=error_kind,
        )
        raise HTTPException(status_code=500, detail={"error": "dispatch_failed", "kind": error_kind})

    new_value = {
        "manager": resolution.manager,
        "kind": resolution.kind,
        "target": resolution.target,
        "message": message,
        "replicas_after": replicas_after,
    }
    _audit_lifecycle(
        db, current_user, request, f"service_{action}", service, old_value, new_value,
        success=success, error_message=dispatch_error_code,
    )

    logger.info(f"service_{action}", service=name, success=success, user=current_user.username)

    return ServiceActionResponse(
        service_name=name,
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
    service: Optional[RagService],
    old_value: dict,
) -> Tuple[bool, str, Optional[int], Optional[str]]:
    """Kubernetes dispatch (D11): acquires the cross-replica lease around
    the WHOLE action (including restart's bounded wait), releases it in
    `finally`. Lease contention -> 409 `action_in_progress`, audited, raised
    here directly (distinct from _run_action's other refusals, since it can
    only be known after we've committed to dispatching).

    Returns (success, message, replicas_after, error_code). error_code is
    None for a plain success/failure and 'restart_superseded' when the
    scale-back was skipped because another replica took over the lease --
    valerie's audit (T9): this must NOT audit here itself, or the caller's
    own single trailing audit call produces a SECOND row for the same
    request. error_code is threaded back so the caller's one audit call
    can carry the right error_message."""
    client, reason = get_k8s_client()
    if client is None:
        return False, f"Kubernetes control is unavailable ({reason})", None, None

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

    def _renew_lease() -> bool:
        return renew_lease(LEASE_SESSION_FACTORY, lease)

    error_code: Optional[str] = None
    try:
        if action == 'stop':
            result = await client.stop(deployment_name, remember=_remember)
            replicas_after = 0 if result.success else remembered.get('n')
            if result.success:
                # codex diff review r2 Medium #3: clear on ANY successful
                # k8s action for this deployment, not just start -- a stop
                # or a settled restart are equally valid evidence the row
                # isn't wedged.
                clear_interrupted(LEASE_SESSION_FACTORY, deployment_name)
        elif action == 'start':
            result = await client.start(deployment_name, recall=_recall)
            replicas_after = remembered.get('n') if result.success else None
            if result.success:
                # codex diff review r1 Critical #4: the operator's recovery
                # action succeeded, so the row stops reading as interrupted.
                clear_interrupted(LEASE_SESSION_FACTORY, deployment_name)
        else:  # restart
            result = await client.restart(
                deployment_name, remember=_remember, recall=_recall, renew_lease=_renew_lease,
            )
            # D11: the scale-back always targets the remembered count,
            # regardless of whether the bounded wait settled or timed out.
            replicas_after = remembered.get('n')
            if result.scaleback == 'skipped':
                # codex diff review r1 Critical #3: another replica took
                # over the lease mid-wait -- our own scale-back was
                # deliberately not attempted, so B's own in-flight action
                # is never raced. valerie's audit (T9): do NOT audit here --
                # the caller's single trailing audit call carries this code,
                # so exactly one audit row is written per request.
                error_code = 'restart_superseded'
            elif result.scaleback == 'failed':
                # codex diff review r1 Critical #4: the lease was released
                # (below, in this function's own finally) regardless of
                # whether the scale-back PATCH landed, so restart_interrupted
                # can't rely solely on an expired lease to catch this case.
                mark_interrupted(LEASE_SESSION_FACTORY, deployment_name)
            elif result.success:
                clear_interrupted(LEASE_SESSION_FACTORY, deployment_name)
        return result.success, result.message, replicas_after, error_code
    except K8sControlError as exc:
        if exc.kind == 'forbidden':
            return False, (
                f"Not permitted by the cluster Role for deployment '{deployment_name}' "
                "— see docs/CONFIGURATION.md § Service Control on Kubernetes"
            ), None, None
        return False, f"Kubernetes API error ({exc.kind}): {exc.message}", None, None
    finally:
        # F54 (ATHENA-118 codex r4, Low): nested so the cache invalidation
        # below always runs even if release_lease itself raises -- a bare
        # sibling statement after a raising release_lease would never run,
        # leaving _run_action's fresh=True snapshot wedged in the cache
        # fresh=False reads next (see the invalidation's own comment).
        try:
            release_lease(LEASE_SESSION_FACTORY, lease)
        finally:
            # codex diff review r3 Medium: invalidate the shared 10s inventory
            # cache after EVERY k8s mutation attempt (success or failure) --
            # _run_action's own gather_inventory(fresh=True) snapshot, taken
            # before/during this dispatch, is written into this SAME cache
            # gather_inventory(fresh=False) later reads (list_services always
            # calls fresh=False). Without this, a scale-back failure that sets
            # the interrupted marker could be immediately followed by an
            # envelope read that sees stale cached non-zero replicas and
            # lazily deletes the marker it just set.
            _clear_inventory_cache()


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
    ollama_port = _get_ollama_port(db)

    resolutions = [(svc, resolve_manager(svc, inv, permissions, ollama_port=ollama_port)) for svc in services]

    # D4.4: any Kubernetes target reached by >=2 rows (enabled or disabled)
    # blocks all of them, so an aliased row can't hijack a critical target.
    # Shared with _execute_resolved_action so the dispatch side enforces
    # the exact same collision the envelope displays (xander P2 fix #1).
    target_counts = _kubernetes_target_counts(db, inv, permissions)

    rows = []
    running = stopped = disabled = 0
    for svc, resolution in resolutions:
        if resolution.manager == 'kubernetes' and resolution.target and target_counts[resolution.target] > 1:
            resolution = ManagerResolution(manager='none', note=f"target_collision:{resolution.target}")
        elif resolution.manager == 'kubernetes' and resolution.target:
            # D11: an expired restart lease whose Deployment is stuck at 0
            # replicas means the admin-backend process died mid-wait --
            # surface it so an operator presses Start rather than assuming
            # the service is just "stopped". codex diff review r1 Critical
            # #4: this alone misses the case where the scale-back PATCH
            # failed (the lease is still released in _dispatch_kubernetes_
            # action's own finally regardless), so an explicit marker in
            # system_settings is checked too -- either signal is sufficient.
            lease_info = read_lease(db, resolution.target)
            lease_expired_mid_restart = bool(
                lease_info
                and lease_info.get('action') == 'restart'
                and resolution.k8s_replicas == 0
                and lease_is_expired(lease_info)
            )
            marker_set = read_interrupted(db, resolution.target)
            if resolution.k8s_replicas == 0 and (lease_expired_mid_restart or marker_set):
                resolution.note = 'restart_interrupted'
            elif marker_set and resolution.k8s_replicas != 0:
                # codex diff review r2 Medium #3 / r3 Medium: an out-of-band
                # `kubectl scale` recovery means the marker MAY be stale --
                # but resolution.k8s_replicas can itself be up to 10s stale
                # (the shared inventory cache _run_action's own mutation
                # wrote into), so it is not proof enough on its own to
                # delete the marker. Require a live, uncached read of this
                # Deployment's actual scale before clearing; on any doubt
                # (fresh read fails) the marker is left alone -- badge stays
                # visible over deletion) rather than letting it resurface
                # on the next restart.
                fresh_replicas = await _fresh_k8s_replica_count(resolution.target)
                if fresh_replicas is not None and fresh_replicas != 0:
                    clear_interrupted(LEASE_SESSION_FACTORY, resolution.target)
                else:
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
        row['native_actions'] = resolution.native_actions
        row['actions'] = resolution.actions
        row['allowed_actions'] = resolution.actions
        row['confirm_required'] = resolution.confirm_required
        # codex diff review r2 High #1: confirm_name is the server-resolved
        # target the frontend must type-confirm against -- for a CA process
        # this is 'process:<port>', not the row's own manager_target (which
        # for a process is just the bare port number). Never derived
        # client-side.
        row['confirm_name'] = resolution.confirm_name
        row['k8s_replicas'] = resolution.k8s_replicas
        row['k8s_ready_replicas'] = resolution.k8s_ready_replicas
        rows.append(row)

    # D3 (valerie): the row backing the Ollama card's own resolution, so the
    # frontend can correlate the panel with a table row without a second
    # lookup. resolve_ollama_manager's own resolution is discarded here --
    # list_services already resolved every registry row above; this call
    # only needs which row_name (if any) backs the Ollama card.
    _, ollama_row_name = resolve_ollama_manager(db, inv, permissions)

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
            namespace=inv.kubernetes.namespace if inv.kubernetes else None,
        ),
        ollama_row=ollama_row_name,
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


# The literal /ollama/* routes MUST be registered before the parametrized
# /{service_name}/* routes below. Starlette matches routes in registration
# order, and /{service_name}/start matches ANY first path segment --
# including "ollama" -- so if it were registered first, POST /ollama/start
# would be silently swallowed by _run_action instead of ever reaching the
# handlers below. test_service_control_route_parity.py pins this.
class OllamaHealthResponse(BaseModel):
    healthy: bool
    status: str
    api_reachable: bool
    models_loaded: int
    version: Optional[str] = None
    timestamp: str
    host: Optional[str] = None
    manager: str = "none"
    manager_target: Optional[str] = None
    manager_note: Optional[str] = None
    native_actions: List[str] = []
    allowed_actions: List[str] = []
    confirm_required: bool = False
    confirm_name: Optional[str] = None
    row_name: Optional[str] = None


def _ollama_health_response(
    resolution: ManagerResolution,
    row_name: Optional[str],
    host: str,
    *,
    status: str,
    api_reachable: bool,
    models_loaded: int = 0,
    version: Optional[str] = None,
    manager_note: Optional[str] = None,
) -> OllamaHealthResponse:
    return OllamaHealthResponse(
        healthy=status in ("healthy", "idle"),
        status=status,
        api_reachable=api_reachable,
        models_loaded=models_loaded,
        version=version,
        timestamp=datetime.utcnow().isoformat(),
        host=host,
        manager=resolution.manager,
        manager_target=resolution.target,
        manager_note=manager_note if manager_note is not None else resolution.note,
        native_actions=resolution.native_actions,
        allowed_actions=resolution.actions,
        confirm_required=resolution.confirm_required,
        confirm_name=resolution.confirm_name,
        row_name=row_name,
    )


@router.get("/ollama/health", response_model=OllamaHealthResponse)
async def get_ollama_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get Ollama health status via a direct probe of the resolved Ollama
    URL (D12) -- no Control Agent call. Controls (manager/actions/confirm)
    come from resolve_ollama_manager, the same resolver every other row
    uses (D4/D9/D20)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    ollama_url = get_ollama_url(db)
    host = urlparse(ollama_url).netloc or ollama_url

    inv = await gather_inventory(fresh=False)
    permissions = current_user.get_permissions()
    resolution, row_name = resolve_ollama_manager(db, inv, permissions)

    version_url = f"{ollama_url}/api/version"
    allowed, reason = await check_ssrf_safe(version_url)
    if not allowed:
        return _ollama_health_response(
            resolution, row_name, host,
            status="ssrf_blocked", api_reachable=False, manager_note=reason,
        )

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            version_response = await client.get(version_url)
            if version_response.status_code != 200:
                return _ollama_health_response(resolution, row_name, host, status="offline", api_reachable=False)
            version = version_response.json().get("version")

            ps_url = f"{ollama_url}/api/ps"
            allowed_ps, _reason_ps = await check_ssrf_safe(ps_url)
            models_loaded = 0
            if allowed_ps:
                ps_response = await client.get(ps_url)
                if ps_response.status_code == 200:
                    models_loaded = len(ps_response.json().get("models", []))

        status = "healthy" if models_loaded > 0 else "idle"
        return _ollama_health_response(
            resolution, row_name, host,
            status=status, api_reachable=True, models_loaded=models_loaded, version=version,
        )

    except (httpx.ConnectError, httpx.TimeoutException):
        return _ollama_health_response(resolution, row_name, host, status="offline", api_reachable=False)
    except Exception as e:
        logger.error("ollama_health_check_failed", error=str(e))
        return _ollama_health_response(resolution, row_name, host, status="error", api_reachable=False)


@router.post("/ollama/start", response_model=ServiceActionResponse, dependencies=[Depends(service_control_rate_limit_dep)])
async def start_ollama(
    request: Request,
    body: Optional[ServiceActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Start Ollama, dispatched through resolve_ollama_manager (D12)."""
    return await _run_ollama_action("start", body or ServiceActionRequest(), request, db, current_user)


@router.post("/ollama/stop", response_model=ServiceActionResponse, dependencies=[Depends(service_control_rate_limit_dep)])
async def stop_ollama(
    request: Request,
    body: Optional[ServiceActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Stop Ollama, dispatched through resolve_ollama_manager (D12)."""
    return await _run_ollama_action("stop", body or ServiceActionRequest(), request, db, current_user)


@router.post("/ollama/restart", response_model=ServiceActionResponse, dependencies=[Depends(service_control_rate_limit_dep)])
async def restart_ollama(
    request: Request,
    body: Optional[ServiceActionRequest] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Restart Ollama, dispatched through resolve_ollama_manager (D12)."""
    return await _run_ollama_action("restart", body or ServiceActionRequest(), request, db, current_user)


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
    response: Response,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all models available in Ollama (D21 SSRF gate on both calls;
    D12 timeout 10s). A /api/tags failure is a hard 502 -- there's no
    model list to render. A /api/ps failure alone degrades to every model
    reporting loaded=False, flagged via a response header rather than
    failing the whole request -- the model list itself is still real."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    ollama_url = get_ollama_url(db)
    tags_url = f"{ollama_url}/api/tags"

    allowed, reason = await check_ssrf_safe(tags_url)
    if not allowed:
        raise HTTPException(status_code=403, detail={"error": "ssrf_blocked", "reason": reason})

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            tags_response = await client.get(tags_url)
            tags_response.raise_for_status()
            available = tags_response.json().get("models", [])
    except Exception as e:
        logger.error("ollama_models_list_failed", error=str(e))
        raise HTTPException(status_code=502, detail={"error": "models_endpoint_unreachable", "reason": str(e)})

    loaded_models: List[str] = []
    ps_url = f"{ollama_url}/api/ps"
    ps_allowed, _ps_reason = await check_ssrf_safe(ps_url)
    if ps_allowed:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                ps_response = await client.get(ps_url)
                ps_response.raise_for_status()
                loaded_models = [m["name"] for m in ps_response.json().get("models", [])]
        except Exception as e:
            logger.warning("ollama_ps_failed_degrading", error=str(e))
            response.headers["X-Athena-Ollama-Ps"] = "unavailable"
    else:
        response.headers["X-Athena-Ollama-Ps"] = "unavailable"

    return [
        OllamaModelResponse(
            name=model["name"],
            size=model.get("size", 0),
            loaded=model["name"] in loaded_models,
            modified_at=model.get("modified_at", ""),
        )
        for model in available
    ]


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

    ollama_url = get_ollama_url(db)
    generate_url = f"{ollama_url}/api/generate"

    allowed, reason = await check_ssrf_safe(generate_url)
    if not allowed:
        return ModelActionResponse(
            model_name=model_name, action="load", success=False,
            message=f"Blocked by SSRF guard: {reason}",
        )

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Send a simple generate request to load the model
            response = await client.post(
                generate_url,
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

    ollama_url = get_ollama_url(db)
    generate_url = f"{ollama_url}/api/generate"

    allowed, reason = await check_ssrf_safe(generate_url)
    if not allowed:
        return ModelActionResponse(
            model_name=model_name, action="unload", success=False,
            message=f"Blocked by SSRF guard: {reason}",
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Ollama unloads via generate with keep_alive=0
            response = await client.post(
                generate_url,
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
