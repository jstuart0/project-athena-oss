"""Service Registry API routes.

Phase 2 (ATHENA-1): asyncpg client replaced with SQLAlchemy ORM against the
admin DB's rag_services table.  Health pings are no longer inlined on the GET
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
from app.utils.url_validators import validate_endpoint_url, parse_endpoint_url
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

    Health is read from rag_services.health_status / last_health_check, NOT
    from a live ping.  The Phase 4 background poller keeps those columns fresh.
    Between Phase 2 and Phase 4 they will be NULL — callers should treat NULL
    as 'pending' (not alarming).  (ATHENA-1 transient state documented in plan)

    Response envelope includes control_agent_enabled so the UI can disable
    start/stop/restart buttons when the Control Agent is not available. (ruby B2)

    Auth: requires valid OIDC session.  Previously unauthenticated, exposing
    host/port/endpoint_url topology to unauthenticated callers.  Pre-
    consolidation backward-compat claim no longer applies.
    (xander MED-2, ATHENA-1 Phase 4 reconcile)
    """
    services = db.query(RagService).order_by(RagService.name).all()

    service_list = []
    for svc in services:
        d = svc.to_dict()
        # Normalise None health_status to 'pending' for UI legibility.
        if d.get('health_status') is None:
            d['health_status'] = 'pending'
        service_list.append(d)

    return {
        'services': service_list,
        'total_services': len(service_list),
        'healthy_services': sum(1 for s in service_list if s.get('health_status') == 'healthy'),
        'overall_health': _overall_health(service_list),
        'control_agent_enabled': get_config().control_agent_enabled,  # ruby B2
    }


def _overall_health(services: list) -> str:
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
    """Return a single service by name with cached health status."""
    svc = db.query(RagService).filter(RagService.name == service_name).first()
    if not svc:
        raise HTTPException(status_code=404, detail=f"Service {service_name} not found")
    d = svc.to_dict()
    if d.get('health_status') is None:
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
    service_type: str = 'api',
    cache_ttl: int = 300,
    timeout: int = 5000,
    rate_limit: int = 100,
) -> Dict[str, Any]:
    """Register or update (upsert) a service.

    Idempotent: safe to call repeatedly (Phase 3 CA startup-upsert relies on this).
    Accepts query params to match the pre-existing calling convention used by the
    Control Agent in Phase 3.
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

    existing = db.query(RagService).filter(RagService.name == name).first()
    if existing:
        existing.endpoint_url = endpoint_url
        existing.host = parsed['host']
        existing.port = parsed['port']
        existing.protocol = parsed['protocol']
        existing.health_endpoint = parsed['health_endpoint']
        if display_name is not None:
            existing.display_name = display_name
        existing.service_type = service_type
        existing.cache_ttl = cache_ttl
        existing.timeout = timeout
        existing.rate_limit = rate_limit
        existing.enabled = True
        # Do NOT touch updated_at explicitly — let onupdate handle it so it only
        # advances on this config-change write.
        db.commit()
        logger.info("service_registry_updated", service=name)
        return {
            'service': name,
            'action': 'updated',
            'url': endpoint_url,
            'message': f"Service {name} has been updated",
        }
    else:
        svc = RagService(
            name=name,
            display_name=display_name or name.replace('-', ' ').title(),
            service_type=service_type,
            endpoint_url=endpoint_url,
            host=parsed['host'],
            port=parsed['port'],
            protocol=parsed['protocol'],
            health_endpoint=parsed['health_endpoint'],
            headers={'Content-Type': 'application/json'},
            cache_ttl=cache_ttl,
            timeout=timeout,
            rate_limit=rate_limit,
            enabled=True,
        )
        db.add(svc)
        db.commit()
        logger.info("service_registry_created", service=name)
        return {
            'service': name,
            'action': 'created',
            'url': endpoint_url,
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
