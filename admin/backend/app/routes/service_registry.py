"""Service Registry API routes.

Phase 2 (ATHENA-1): asyncpg client replaced with SQLAlchemy ORM against the
admin DB's athena_service_registry table.  Health pings are no longer inlined on the GET
list — callers receive the cached health_status + last_health_check written by
the Phase 4 background poller.  Between Phase 2 and Phase 4, those columns
will be NULL / unknown — that is the documented transient state.

Auth: GET /services requires get_current_user (OIDC bearer; Phase 4 reconcile
xander MED-2).  Other GET endpoints (single service, URL lookup) remain
unauthenticated — they expose only non-sensitive lookups.  Write (POST /
DELETE) endpoints require dual-auth: X-Service-Key (Control Agent / internal
callers) OR Bearer JWT / X-API-Key (admin UI) via verify_service_or_oidc.
(xander CRIT-1 / D9 / ATHENA-1)

Rate limit: write endpoints are capped at service_registry_write_per_minute
requests per IP per 60 s via service_registry_rate_limit_dep.
(xander HIGH-4 / ATHENA-1)
"""
import asyncio
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import RagService, User
from app.auth.oidc import get_current_user
from app.utils.service_auth import require_service_or_user_permission, require_user_permission, verify_service_or_oidc
from app.utils.rate_limit import service_registry_rate_limit_dep
from app.utils.url_validators import validate_endpoint_url, parse_endpoint_url, validate_host
from app.utils.service_state import normalized_health_status
from shared.config import get_config
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/service-registry", tags=["service-registry"])

# Allowlist for service names used in inline JS onclick handlers.
# Rejects names that could break out of single-quoted JS string literals.
# (codex r2 M-5 / xander L-2)
_SERVICE_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')

# Loose sanity check on host_label (ATHENA-108 follow-up): it is a match
# key only -- never persisted -- but is still worth bounding so a caller
# can't probe with pathological input. Hostnames may contain dots
# (K8s DNS names never do, but this stays permissive rather than coupling
# to that convention).
_HOST_LABEL_RE = re.compile(r'^[a-zA-Z0-9_.-]{1,255}$')

_WRITE_DEPS = [
    Depends(verify_service_or_oidc),
    Depends(service_registry_rate_limit_dep),
]


def _resolve_by_name_or_host_label(
    db: Session, name: str, host_label: Optional[str]
) -> Optional[RagService]:
    """Match an existing row by `name`; if none found and `host_label` is
    given, fall back to a case-insensitive match on the row's `host`
    column. `host` is NOT a unique column (two rows can legitimately share
    one, e.g. a decommissioned duplicate never cleaned up) -- if host_label
    matches more than one row, refuses to guess which one the caller meant
    and raises 409 `host_label_ambiguous` instead of silently updating an
    arbitrary match. (codex diff-review Medium, ATHENA-108 follow-up)
    """
    existing = db.query(RagService).filter(RagService.name == name).first()
    if existing is not None or not host_label:
        return existing

    host_matches = db.query(RagService).filter(
        func.lower(RagService.host) == host_label.lower()
    ).limit(2).all()
    if len(host_matches) > 1:
        logger.warning(
            "service_registry_host_label_ambiguous",
            host_label=host_label,
            requested_name=name,
            matched_names=[row.name for row in host_matches],
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "host_label_ambiguous",
                "message": (
                    f"host_label {host_label!r} matches more than one "
                    "registry row; refusing to update an arbitrary one"
                ),
            },
        )
    return host_matches[0] if host_matches else None


# ---------------------------------------------------------------------------
# GET /services — list all services (cached health; no inline pings)
# ---------------------------------------------------------------------------

@router.get("/services")
async def get_all_services(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> Dict[str, Any]:
    """Return all service registry entries with cached health status.

    Health is read from athena_service_registry.health_status / last_health_check, NOT
    from a live ping.  The Phase 4 background poller keeps those columns fresh.
    Between Phase 2 and Phase 4 they will be NULL — callers should treat NULL
    as 'pending' (not alarming).  (ATHENA-1 transient state documented in plan)

    Response envelope includes control_agent_enabled so the UI can disable
    start/stop/restart buttons when the Control Agent is not available. (ruby B2)

    Auth: requires valid OIDC session.  Previously unauthenticated, exposing
    host/port/endpoint_url topology to unauthenticated callers.  Pre-
    consolidation backward-compat claim no longer applies.
    (xander MED-2, ATHENA-1 Phase 4 reconcile)

    ATHENA-112: overall_health and healthy_services are computed over ENABLED
    rows only -- a disabled service (deliberately turned off by an operator)
    must not drag the dashboard into 'degraded'/'unhealthy'.  A disabled row's
    health_status is reported as the literal string 'disabled' regardless of
    its last cached poller value, since that cached value goes stale the
    moment the row is disabled and the poller stops touching it.
    total_services still counts every row (enabled + disabled) for
    backward compatibility; enabled_services / disabled_services are new.
    """
    services = db.query(RagService).order_by(RagService.name).all()

    service_list = []
    enabled_list = []
    for svc in services:
        d = svc.to_dict()
        d['health_status'] = normalized_health_status(d.get('enabled', False), d.get('health_status'))
        service_list.append(d)
        if d.get('enabled'):
            enabled_list.append(d)

    return {
        'services': service_list,
        'total_services': len(service_list),
        'enabled_services': len(enabled_list),
        'disabled_services': len(service_list) - len(enabled_list),
        'healthy_services': sum(1 for s in enabled_list if s.get('health_status') == 'healthy'),
        'overall_health': _overall_health(enabled_list),
        'control_agent_enabled': get_config().control_agent_enabled,  # ruby B2
    }


def _overall_health(services: list) -> str:
    """Compute aggregate health over the given services (ATHENA-112: caller
    must pass the ENABLED subset -- this function has no opinion on enabled
    state itself, it just averages whatever list it's given)."""
    if not services:
        return 'unknown'
    healthy = sum(1 for s in services if s.get('health_status') == 'healthy')
    if healthy == len(services):
        return 'healthy'
    return 'degraded' if healthy > 0 else 'unhealthy'


# ---------------------------------------------------------------------------
# GET /services/{service_name} — single service
# ---------------------------------------------------------------------------

@router.get("/services/{service_name}", dependencies=[Depends(require_user_permission("read"))])
async def get_service(
    service_name: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Return a single service by name with cached health status.

    Applies the same disabled-row override as GET /services (ATHENA-112):
    a disabled row always reports health_status='disabled', never a stale
    cached value from before it was turned off. (codex diff review)
    """
    svc = db.query(RagService).filter(RagService.name == service_name).first()
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")
    d = svc.to_dict()
    d['health_status'] = normalized_health_status(d.get('enabled', False), d.get('health_status'))
    return d


# ---------------------------------------------------------------------------
# GET /services/{service_name}/url — lightweight URL lookup
# ---------------------------------------------------------------------------

@router.get("/services/{service_name}/url", dependencies=[Depends(require_service_or_user_permission("read"))])
async def get_service_url(
    service_name: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Return the endpoint URL for a service (lightweight, no health check)."""
    svc = db.query(RagService).filter(RagService.name == service_name).first()
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")
    if not svc.enabled:
        raise HTTPException(status_code=503, detail=f"Service {service_name} is disabled")

    url = svc.endpoint_url or f"{svc.protocol or 'http'}://{svc.host}:{svc.port}"
    return {'service': service_name, 'url': url}


# ---------------------------------------------------------------------------
# POST /services — register or update (upsert)
# ---------------------------------------------------------------------------

@router.post("/services", dependencies=_WRITE_DEPS)
async def register_service(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    name: str = "",
    host_label: Optional[str] = None,
    endpoint_url: str = "",
    display_name: Optional[str] = None,
    service_type: Optional[str] = None,
    cache_ttl: Optional[int] = None,
    timeout: Optional[int] = None,
    rate_limit: Optional[int] = None,
    protocol: Optional[str] = None,
    host: Optional[str] = None,
    port: Optional[int] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    """Register or update (upsert) a service.

    Idempotent: safe to call repeatedly (Phase 3 CA startup-upsert relies on this).
    Accepts query params to match the pre-existing calling convention used by the
    Control Agent in Phase 3.

    protocol='tcp' (ATHENA-109): registers a row checked by raw TCP connect
    instead of an HTTP(S) request.  A TCP check has no scheme or path, so this
    branch takes host/port directly and does not require (or store)
    endpoint_url -- validate_endpoint_url only accepts http/https schemes and
    would reject a "tcp://" URL.  host still passes through validate_host()
    for the same SSRF protections applied to http(s) rows.

    Partial update on an existing row (codex diff review): service_type,
    cache_ttl, timeout, rate_limit, and enabled are each applied ONLY if the
    caller actually passed them; an omitted field keeps the row's current
    value. Defaults ('api', 300, 5000, 100, True respectively) apply only
    when INSERTING a new row. This matters because the admin UI's row editor
    (service-control.js) calls this same upsert route to change just a
    protocol/host/port/display_name -- before this fix, editing a row's check
    type silently reset its cache_ttl/timeout/rate_limit to defaults and
    force-re-enabled it even if an operator had deliberately disabled it.
    The Control Agent's startup-upsert IS affected, and that's the point:
    its payload (src/control_agent/main.py's sync_registry_loop) sends name/
    endpoint_url/service_type/cache_ttl/timeout/rate_limit but never
    `enabled` (test_phase3_ca_upsert.py seeds/asserts exactly that payload
    shape). Before this partial-update fix, every CA restart re-ran the
    upsert and reset `enabled` to the INSERT default (True) for every row,
    silently re-enabling anything an operator had deliberately disabled.
    Partial-update semantics mean an omitted `enabled` now keeps the row's
    current value, so an operator-disabled row stays disabled across CA
    restarts.

    endpoint_url is ALSO now partial-update-safe on an existing row (xander
    diff-review Critical, 2026-09-28): shared.service_registry.
    register_service() self-registers with no location info of its own by
    design (see that function's comment) -- an existing, seeded row (e.g.
    ATHENA-119's OSS_SERVICE_REGISTRY "athena-rag-weather") must keep its
    correct K8s host/port/protocol when a RAG process pings this route with
    endpoint_url omitted. endpoint_url stays REQUIRED when creating a
    brand-new row (there is no prior host to fall back to), and unchanged
    for protocol='tcp', whose host/port validation is independent.

    host_label (ATHENA-108 follow-up): shared.service_registry.
    register_service() now derives `name` as "<connector>-rag" -- a value
    that doesn't match a row seeded/renamed under a different convention
    (e.g. a plain connector name whose host is "athena-rag-<connector>").
    When no row matches `name`, host_label is used as a fallback lookup key
    against the row's `host` column (case-insensitive). It is a match key
    only -- never stored -- so a match-by-host still returns the row's own
    `name`, not the caller's derived one. `host` is not a unique column: if
    host_label matches more than one row, the upsert refuses to guess and
    returns 409 `host_label_ambiguous` rather than updating an arbitrary
    match (codex diff-review Medium, follow-up).
    """
    if not name:
        raise HTTPException(status_code=422, detail="'name' query parameter is required")
    # Service name allowlist: must match identifier-safe chars so it cannot
    # break out of single-quoted JS onclick handlers in the admin UI.
    # (codex r2 M-5 / xander L-2)
    if not _SERVICE_NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail="'name' must match ^[a-zA-Z0-9_-]{1,64}$",
        )
    if host_label is not None and not _HOST_LABEL_RE.fullmatch(host_label):
        raise HTTPException(
            status_code=422,
            detail="'host_label' must match ^[a-zA-Z0-9_.-]{1,255}$",
        )

    existing = _resolve_by_name_or_host_label(db, name, host_label)

    resolved_endpoint_url: Optional[str] = None
    parsed: Optional[Dict[str, Any]] = None

    if protocol == 'tcp':
        if not host:
            raise HTTPException(status_code=422, detail="'host' is required when protocol='tcp'")
        if not port or not (1 <= port <= 65535):
            raise HTTPException(status_code=422, detail="'port' must be between 1 and 65535 when protocol='tcp'")
        try:
            host = validate_host(host)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        parsed = {'host': host, 'port': port, 'protocol': 'tcp', 'health_endpoint': None}
    elif endpoint_url:
        # SSRF protection: validate scheme + host before persisting.
        # The Phase 4 health poller will make HTTP requests to stored endpoint_url
        # values; a stored IMDS or cluster-internal URL would be polled silently.
        # (xander M-3, ATHENA-1 Phase 2 reconcile)
        try:
            endpoint_url = validate_endpoint_url(endpoint_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        # Parse endpoint_url → host/port/protocol/health_endpoint so that the NOT
        # NULL columns added by migration 055 are populated and the Phase 4 poller
        # can reach newly-registered services.  (codex r2 H-2)
        try:
            parsed = parse_endpoint_url(endpoint_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        resolved_endpoint_url = endpoint_url
    elif existing is None:
        raise HTTPException(status_code=422, detail="'endpoint_url' query parameter is required")
    # else: protocol != 'tcp', endpoint_url omitted, existing row found --
    # partial update (xander diff-review Critical): location fields below
    # are left untouched.

    if parsed is not None:
        display_url = resolved_endpoint_url or f"tcp://{parsed['host']}:{parsed['port']}"
    else:
        display_url = existing.endpoint_url or f"tcp://{existing.host}:{existing.port}"

    if existing:
        if parsed is not None:
            existing.endpoint_url = resolved_endpoint_url
            existing.host = parsed['host']
            existing.port = parsed['port']
            existing.protocol = parsed['protocol']
            existing.health_endpoint = parsed['health_endpoint']
        if display_name is not None:
            existing.display_name = display_name
        # Partial update (codex diff review): each of these is applied only
        # if the caller passed it. Omitting `enabled` in particular must
        # never implicitly re-enable a row an operator deliberately disabled.
        if service_type is not None:
            existing.service_type = service_type
        if cache_ttl is not None:
            existing.cache_ttl = cache_ttl
        if timeout is not None:
            existing.timeout = timeout
        if rate_limit is not None:
            existing.rate_limit = rate_limit
        if enabled is not None:
            existing.enabled = enabled
        # Do NOT touch updated_at explicitly — let onupdate handle it so it only
        # advances on this config-change write.
        db.commit()
        # Matched-by-host_label rows keep their own `name` (never renamed by
        # this upsert) -- report the row that was actually touched, not the
        # caller's derived name, so a mismatched match is never masked.
        matched_name = existing.name
        logger.info(
            "service_registry_updated",
            service=matched_name,
            requested_name=name,
            matched_by="host_label" if matched_name != name else "name",
        )
        return {
            'service': matched_name,
            'action': 'updated',
            'url': display_url,
            'message': f"Service {matched_name} has been updated",
        }
    else:
        svc = RagService(
            name=name,
            display_name=display_name or name.replace('-', ' ').title(),
            service_type=service_type or 'api',
            endpoint_url=resolved_endpoint_url,
            host=parsed['host'],
            port=parsed['port'],
            protocol=parsed['protocol'],
            health_endpoint=parsed['health_endpoint'],
            headers={'Content-Type': 'application/json'},
            cache_ttl=cache_ttl if cache_ttl is not None else 300,
            timeout=timeout if timeout is not None else 5000,
            rate_limit=rate_limit if rate_limit is not None else 100,
            enabled=enabled if enabled is not None else True,
        )
        db.add(svc)
        db.commit()
        logger.info("service_registry_created", service=name)
        return {
            'service': name,
            'action': 'created',
            'url': display_url,
            'message': f"Service {name} has been registered",
        }


# ---------------------------------------------------------------------------
# POST /services/{service_name}/toggle
# ---------------------------------------------------------------------------

@router.post("/services/{service_name}/toggle", dependencies=_WRITE_DEPS)
async def toggle_service(
    request: Request,
    response: Response,
    service_name: str,
    host_label: Optional[str] = None,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Toggle the enabled state of a service.

    host_label (ATHENA-108 follow-up): same fallback as POST /services --
    shared.service_registry.unregister_service() posts the "-rag"-derived
    name, which may not match an existing row's actual `name`; host_label
    locates it by host instead (ambiguity-safe, see
    _resolve_by_name_or_host_label). The response always reports the
    matched row's own name.
    """
    if host_label is not None and not _HOST_LABEL_RE.fullmatch(host_label):
        raise HTTPException(
            status_code=422,
            detail="'host_label' must match ^[a-zA-Z0-9_.-]{1,255}$",
        )
    svc = _resolve_by_name_or_host_label(db, service_name, host_label)
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

    svc.enabled = not svc.enabled
    db.commit()
    logger.info(
        "service_registry_toggled",
        service=svc.name,
        requested_name=service_name,
        enabled=svc.enabled,
    )
    return {
        'service': svc.name,
        'enabled': svc.enabled,
        'message': f"Service {svc.name} has been {'enabled' if svc.enabled else 'disabled'}",
    }


# ---------------------------------------------------------------------------
# POST /services/{service_name}/refresh
# ---------------------------------------------------------------------------

@router.post("/services/{service_name}/refresh", dependencies=_WRITE_DEPS)
async def refresh_service(
    request: Request,
    response: Response,
    service_name: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Touch updated_at to signal the service definition was refreshed."""
    svc = db.query(RagService).filter(RagService.name == service_name).first()
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

    # Explicit datetime write triggers the updated_at onupdate hook.
    svc.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("service_registry_refreshed", service=service_name)
    return {
        'service': service_name,
        'message': f"Service {service_name} registration refreshed",
    }


# ---------------------------------------------------------------------------
# DELETE /services/{service_name}
# ---------------------------------------------------------------------------

@router.delete("/services/{service_name}", dependencies=_WRITE_DEPS)
async def remove_service(
    request: Request,
    response: Response,
    service_name: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Remove a service from the registry."""
    svc = db.query(RagService).filter(RagService.name == service_name).first()
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

    db.delete(svc)
    db.commit()
    logger.info("service_registry_deleted", service=service_name)
    return {
        'service': service_name,
        'message': f"Service {service_name} has been removed from registry",
    }


# ---------------------------------------------------------------------------
# POST /services/poll-now — trigger a full immediate poll cycle (Phase 4)
# ---------------------------------------------------------------------------

@router.post("/services/poll-now", dependencies=_WRITE_DEPS)
async def poll_now(
    request: Request,
    response: Response,
) -> Dict[str, Any]:
    """Trigger an immediate health-poll cycle for all enabled services.

    Runs the full poll cycle once outside the background loop and returns a
    summary.  Used by the "Refresh Status" button in the admin UI.
    Dual-auth + rate-limit via _WRITE_DEPS (same as other write endpoints).
    (ATHENA-1 Phase 4; ruby B4; plan §Phase 4 D)
    """
    from app.services.health_poller import _poll_all_services
    semaphore = asyncio.Semaphore(get_config().health_poll_concurrency)
    summary = await _poll_all_services(semaphore)
    return {
        'queued': True,
        'services_polled': summary.get('services_polled', 0),
        'healthy': summary.get('healthy', 0),
        'unhealthy': summary.get('unhealthy', 0),
        'unknown': summary.get('unknown', 0),
    }


# ---------------------------------------------------------------------------
# POST /services/{service_name}/check — per-row on-demand health check
# ---------------------------------------------------------------------------

@router.post("/services/{service_name}/check", dependencies=_WRITE_DEPS)
async def check_service_health(
    request: Request,
    response: Response,
    service_name: str,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Trigger an on-demand health check for a single service.

    Issues one ping and writes results back synchronously so the UI can
    re-fetch immediately after the call.  Used by the per-row "Refresh"
    button. (ATHENA-1 Phase 4; plan §Phase 4 D)
    """
    from app.services.health_poller import _poll_one, _classify_and_sanitize
    from datetime import datetime

    svc = db.query(RagService).filter(RagService.name == service_name).first()
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")
    if not svc.enabled:
        raise HTTPException(status_code=409, detail=f"Service {service_name} is disabled")
    if not svc.host or not svc.port:
        raise HTTPException(status_code=422, detail=f"Service {service_name} has no host/port configured")

    cfg = get_config()
    semaphore = asyncio.Semaphore(1)
    import httpx as _httpx
    async with _httpx.AsyncClient(timeout=float(cfg.health_poll_timeout_seconds)) as client:
        result = await _poll_one(
            client,
            semaphore,
            svc.id,
            svc.name,
            svc.host or '',
            svc.port or 0,
            svc.health_endpoint or '/health',
            svc.protocol or 'http',
        )

    svc_id, status, response_time_ms, error_category, error_detail, health_message = result
    last_error = f'{error_category}:{error_detail}' if error_category != 'ok' else None

    db.query(RagService).filter(RagService.id == svc_id).update(
        {
            RagService.health_status: status,
            RagService.last_health_check: datetime.utcnow(),
            RagService.last_response_time_ms: response_time_ms,
            RagService.last_error: last_error,
            RagService.health_message: health_message,
        },
        synchronize_session=False,
    )
    db.commit()

    logger.info('service_health_checked', service=service_name, status=status)
    return {
        'service': service_name,
        'health_status': status,
        'last_response_time_ms': response_time_ms,
        'last_error': last_error,
        'health_message': health_message,
    }
