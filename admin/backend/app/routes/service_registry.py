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
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import RagService, User
from app.auth.oidc import get_current_user
from app.utils.service_auth import verify_service_or_oidc
from app.utils.rate_limit import service_registry_rate_limit_dep
from app.utils.url_validators import validate_endpoint_url, parse_endpoint_url, validate_host
from shared.config import get_config
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/service-registry", tags=["service-registry"])

# Allowlist for service names used in inline JS onclick handlers.
# Rejects names that could break out of single-quoted JS string literals.
# (codex r2 M-5 / xander L-2)
_SERVICE_NAME_RE = re.compile(r'^[a-zA-Z0-9_-]{1,64}$')

_WRITE_DEPS = [
    Depends(verify_service_or_oidc),
    Depends(service_registry_rate_limit_dep),
]


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
        if not d.get('enabled'):
            # Overrides whatever health_status the poller last cached before
            # the row was disabled -- that value is no longer being refreshed
            # and must not be read as current state. (ATHENA-112)
            d['health_status'] = 'disabled'
        elif d.get('health_status') is None:
            # Normalise None health_status to 'pending' for UI legibility.
            d['health_status'] = 'pending'
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

@router.get("/services/{service_name}")
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
    if not d.get('enabled'):
        d['health_status'] = 'disabled'
    elif d.get('health_status') is None:
        d['health_status'] = 'pending'
    return d


# ---------------------------------------------------------------------------
# GET /services/{service_name}/url — lightweight URL lookup
# ---------------------------------------------------------------------------

@router.get("/services/{service_name}/url")
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

    if protocol == 'tcp':
        if not host:
            raise HTTPException(status_code=422, detail="'host' is required when protocol='tcp'")
        if not port or not (1 <= port <= 65535):
            raise HTTPException(status_code=422, detail="'port' must be between 1 and 65535 when protocol='tcp'")
        try:
            host = validate_host(host)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        resolved_endpoint_url: Optional[str] = None
        parsed = {'host': host, 'port': port, 'protocol': 'tcp', 'health_endpoint': None}
    else:
        if not endpoint_url:
            raise HTTPException(status_code=422, detail="'endpoint_url' query parameter is required")

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

    display_url = resolved_endpoint_url or f"tcp://{parsed['host']}:{parsed['port']}"

    existing = db.query(RagService).filter(RagService.name == name).first()
    if existing:
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
        logger.info("service_registry_updated", service=name)
        return {
            'service': name,
            'action': 'updated',
            'url': display_url,
            'message': f"Service {name} has been updated",
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
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Toggle the enabled state of a service."""
    svc = db.query(RagService).filter(RagService.name == service_name).first()
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")

    svc.enabled = not svc.enabled
    db.commit()
    logger.info("service_registry_toggled", service=service_name, enabled=svc.enabled)
    return {
        'service': service_name,
        'enabled': svc.enabled,
        'message': f"Service {service_name} has been {'enabled' if svc.enabled else 'disabled'}",
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
