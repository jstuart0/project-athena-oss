"""
Service registry API routes.

Provides CRUD operations for service management and health monitoring.
"""
import os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel, field_validator
from datetime import datetime
import structlog
import aiohttp
import asyncio
import ssl

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, RagService, AuditLog
from app.utils.url_validators import validate_protocol, validate_host, validate_health_endpoint
from app.services.health_poller import _validate_service_url
from shared.config import get_config

logger = structlog.get_logger()

router = APIRouter(prefix="/api/services", tags=["services"])



# Private aliases kept for ServiceCreate / ServiceUpdate field_validator calls
# below; the shared implementations live in app.utils.url_validators so that
# service_registry.py can apply the same SSRF rules without duplicating logic.
_validate_protocol = validate_protocol
_validate_host = validate_host
_validate_health_endpoint = validate_health_endpoint


class ServiceCreate(BaseModel):
    """Request model for registering a service."""
    name: str
    display_name: str
    host: str
    port: int
    protocol: str = "http"
    health_endpoint: Optional[str] = "/health"
    service_type: Optional[str] = None
    description: Optional[str] = None
    control_method: Optional[str] = "none"
    container_name: Optional[str] = None
    auto_start: bool = True
    enabled: bool = True

    @field_validator('protocol')
    @classmethod
    def validate_protocol(cls, v: str) -> str:
        return _validate_protocol(v)

    @field_validator('host')
    @classmethod
    def validate_host(cls, v: str) -> str:
        return _validate_host(v)

    @field_validator('health_endpoint')
    @classmethod
    def validate_health_endpoint(cls, v: Optional[str]) -> Optional[str]:
        return _validate_health_endpoint(v)


class ServiceUpdate(BaseModel):
    """Request model for updating a service."""
    display_name: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    health_endpoint: Optional[str] = None
    protocol: Optional[str] = None
    service_type: Optional[str] = None
    description: Optional[str] = None
    control_method: Optional[str] = None
    container_name: Optional[str] = None
    auto_start: Optional[bool] = None
    enabled: Optional[bool] = None

    @field_validator('protocol')
    @classmethod
    def validate_protocol(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_protocol(v)

    @field_validator('host')
    @classmethod
    def validate_host(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_host(v)

    @field_validator('health_endpoint')
    @classmethod
    def validate_health_endpoint(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_health_endpoint(v)


class ServiceResponse(BaseModel):
    """Response model for service data."""
    id: int
    name: str
    display_name: str
    host: Optional[str] = None
    port: Optional[int] = None
    protocol: Optional[str] = None
    health_endpoint: Optional[str] = None
    service_type: Optional[str] = None
    description: Optional[str] = None
    control_method: Optional[str] = None
    container_name: Optional[str] = None
    auto_start: bool = True
    enabled: bool = True
    health_status: Optional[str] = None
    is_running: bool = False
    last_health_check: Optional[str] = None
    last_response_time_ms: Optional[int] = None
    last_error: Optional[str] = None
    health_message: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    service: RagService,
    old_value: dict = None,
    new_value: dict = None,
    request: Request = None
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='service',
        resource_id=service.id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='service', resource_id=service.id)


# =============================================================================
# Quick Health Check Endpoints (for status bar)
# =============================================================================

# Environment variable fallbacks (used when service not in database)
ENV_FALLBACKS = {
    "gateway": os.getenv("GATEWAY_URL", "http://localhost:8000"),
    "orchestrator": os.getenv("ORCHESTRATOR_URL", "http://localhost:8001"),
    "ollama": get_config().ollama_url,
    "redis": f"redis://{os.getenv('REDIS_SERVICE_HOST', 'redis')}:{os.getenv('REDIS_SERVICE_PORT', '6379')}",
    "qdrant": os.getenv("QDRANT_URL", "http://qdrant:6333"),
    "mlx": os.getenv("MLX_URL", ""),
}


def get_service_url_from_db(db: Session, service_name: str) -> Optional[dict]:
    """
    Look up service URL from database.
    Returns dict with url, protocol, health_endpoint or None if not found.
    """
    service = db.query(RagService).filter(
        func.lower(RagService.name) == service_name.lower()
    ).first()

    if service and service.host:
        return {
            "url": f"{service.protocol or 'http'}://{service.host}:{service.port}",
            "protocol": service.protocol or 'http',
            "health_endpoint": service.health_endpoint,
            "ip": service.host,
            "port": service.port,
        }
    return None


def get_service_url(db: Session, service_name: str, fallback_key: str = None) -> str:
    """Get service URL from database, falling back to environment variable."""
    db_info = get_service_url_from_db(db, service_name)
    if db_info:
        return db_info["url"]

    # Fall back to environment variable
    key = fallback_key or service_name.lower()
    return ENV_FALLBACKS.get(key, "")


def _ssrf_check_url(url: str, health_path: str = "/health") -> Optional[dict]:
    """Apply the SSRF allowlist guard to a URL before an outbound request.

    Returns a dict suitable for returning directly as the endpoint response if
    the URL is blocked, or None if the URL is allowed.  Callers MUST check the
    return value and short-circuit when it is not None.

    Phase 3 H-3 (codex r2): applies the same _validate_service_url guard used
    by the background health poller to the 6 per-service-type quick-check
    endpoints that previously bypassed it.
    """
    from urllib.parse import urlparse as _urlparse
    try:
        parsed = _urlparse(url)
        host = parsed.hostname or ''
        port = parsed.port
        if port is None:
            port = 443 if parsed.scheme == 'https' else 80
    except Exception:
        return {"status": "ssrf_blocked", "error": "malformed URL"}

    allowed, reason = _validate_service_url(host, port, health_path)
    if not allowed:
        logger.warning("quick_check_ssrf_blocked", url=url, reason=reason)
        return {"status": "ssrf_blocked", "error": f"SSRF guard: {reason}"}
    return None


@router.get("/gateway/health")
async def check_gateway_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Quick health check for Gateway service. URL sourced from database."""
    import time
    start = time.time()

    url = get_service_url(db, "gateway")
    if not url:
        return {"status": "not_configured", "error": "Gateway not found in database"}

    blocked = _ssrf_check_url(url, "/health")
    if blocked:
        return blocked

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"{url}/health",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                elapsed = int((time.time() - start) * 1000)
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "status": "online",
                        "response_time_ms": elapsed,
                        "details": data
                    }
                else:
                    return {
                        "status": "degraded",
                        "response_time_ms": elapsed,
                        "http_status": resp.status
                    }
    except asyncio.TimeoutError:
        return {"status": "offline", "error": "timeout"}
    except Exception as e:
        logger.error("gateway_health_check_failed", error=str(e))
        return {"status": "offline", "error": str(e)}


@router.get("/orchestrator/health")
async def check_orchestrator_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Quick health check for Orchestrator service. URL sourced from database."""
    import time
    start = time.time()

    url = get_service_url(db, "orchestrator")
    if not url:
        return {"status": "not_configured", "error": "Orchestrator not found in database"}

    blocked = _ssrf_check_url(url, "/health")
    if blocked:
        return blocked

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"{url}/health",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                elapsed = int((time.time() - start) * 1000)
                if resp.status == 200:
                    data = await resp.json()
                    return {
                        "status": "online",
                        "response_time_ms": elapsed,
                        "details": data
                    }
                else:
                    return {
                        "status": "degraded",
                        "response_time_ms": elapsed,
                        "http_status": resp.status
                    }
    except asyncio.TimeoutError:
        return {"status": "offline", "error": "timeout"}
    except Exception as e:
        logger.error("orchestrator_health_check_failed", error=str(e))
        return {"status": "offline", "error": str(e)}


@router.get("/ollama/health")
async def check_ollama_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Quick health check for Ollama LLM server. URL sourced from database."""
    import time
    start = time.time()

    url = get_service_url(db, "ollama")
    if not url:
        return {"status": "not_configured", "error": "Ollama not found in database"}

    blocked = _ssrf_check_url(url, "/api/tags")
    if blocked:
        return blocked

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Ollama uses /api/tags to check if running
            async with session.get(
                f"{url}/api/tags",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                elapsed = int((time.time() - start) * 1000)
                if resp.status == 200:
                    data = await resp.json()
                    models = data.get("models", [])
                    return {
                        "status": "online",
                        "response_time_ms": elapsed,
                        "models_available": len(models),
                        "model_names": [m.get("name") for m in models[:5]]  # First 5
                    }
                else:
                    return {
                        "status": "degraded",
                        "response_time_ms": elapsed,
                        "http_status": resp.status
                    }
    except asyncio.TimeoutError:
        return {"status": "offline", "error": "timeout"}
    except Exception as e:
        logger.error("ollama_health_check_failed", error=str(e))
        return {"status": "offline", "error": str(e)}


@router.get("/redis/health")
async def check_redis_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Quick health check for Redis cache server. URL sourced from database."""
    import time
    start = time.time()

    # Try database first
    db_info = get_service_url_from_db(db, "redis")
    if db_info:
        redis_host = db_info["ip"]
        redis_port = db_info["port"]
    else:
        # Fall back to environment variables
        redis_host = os.getenv("REDIS_SERVICE_HOST", os.getenv("REDIS_HOST", "redis"))
        redis_port = int(os.getenv("REDIS_SERVICE_PORT", "6379"))

    # SSRF guard (codex r2 H-3): Redis uses a raw socket; guard the host/port
    # directly since there is no URL to pass to _ssrf_check_url.
    redis_allowed, redis_block_reason = _validate_service_url(redis_host, redis_port, "/")
    if not redis_allowed:
        logger.warning("quick_check_ssrf_blocked", service="redis", reason=redis_block_reason)
        return {"status": "ssrf_blocked", "error": f"SSRF guard: {redis_block_reason}"}

    try:
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((redis_host, redis_port))
        sock.close()
        elapsed = int((time.time() - start) * 1000)

        if result == 0:
            # Try a PING command for deeper health check
            try:
                import redis
                r = redis.Redis(host=redis_host, port=redis_port, socket_timeout=2)
                pong = r.ping()
                r.close()
                if pong:
                    return {
                        "status": "online",
                        "response_time_ms": elapsed,
                        "ping": "PONG"
                    }
            except Exception:
                # TCP connection worked but Redis command failed
                return {
                    "status": "degraded",
                    "response_time_ms": elapsed,
                    "error": "TCP connected but PING failed"
                }

            return {
                "status": "online",
                "response_time_ms": elapsed
            }
        else:
            return {"status": "offline", "error": "connection refused"}
    except Exception as e:
        logger.error("redis_health_check_failed", error=str(e))
        return {"status": "offline", "error": str(e)}


@router.get("/qdrant/health")
async def check_qdrant_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Quick health check for Qdrant vector database. URL sourced from database."""
    import time
    start = time.time()

    qdrant_url = get_service_url(db, "qdrant")
    if not qdrant_url:
        return {"status": "not_configured", "error": "Qdrant not found in database"}

    blocked = _ssrf_check_url(qdrant_url, "/healthz")
    if blocked:
        return blocked

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"{qdrant_url}/healthz",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                elapsed = int((time.time() - start) * 1000)
                if resp.status == 200:
                    return {
                        "status": "online",
                        "response_time_ms": elapsed
                    }
                else:
                    return {
                        "status": "degraded",
                        "response_time_ms": elapsed,
                        "http_status": resp.status
                    }
    except asyncio.TimeoutError:
        return {"status": "offline", "error": "timeout"}
    except Exception as e:
        logger.error("qdrant_health_check_failed", error=str(e))
        return {"status": "offline", "error": str(e)}


@router.get("/mlx/health")
async def check_mlx_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Quick health check for MLX-LM server on Apple Silicon. URL sourced from database."""
    import time
    start = time.time()

    # Try database first, then environment variable
    mlx_url = get_service_url(db, "mlx")

    if not mlx_url:
        return {"status": "not_configured", "error": "MLX not found in database or MLX_URL not set"}

    blocked = _ssrf_check_url(mlx_url, "/v1/models")
    if blocked:
        return blocked

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # MLX-LM uses OpenAI-compatible API, check /v1/models endpoint
            async with session.get(
                f"{mlx_url}/v1/models",
                timeout=aiohttp.ClientTimeout(total=3)
            ) as resp:
                elapsed = int((time.time() - start) * 1000)
                if resp.status == 200:
                    try:
                        data = await resp.json()
                        models = data.get("data", [])
                        return {
                            "status": "online",
                            "response_time_ms": elapsed,
                            "models_available": len(models),
                            "model_names": [m.get("id") for m in models[:3]]
                        }
                    except Exception:
                        return {
                            "status": "online",
                            "response_time_ms": elapsed
                        }
                else:
                    return {
                        "status": "degraded",
                        "response_time_ms": elapsed,
                        "http_status": resp.status
                    }
    except asyncio.TimeoutError:
        return {"status": "offline", "error": "timeout"}
    except Exception as e:
        logger.error("mlx_health_check_failed", error=str(e))
        return {"status": "offline", "error": str(e)}


# =============================================================================
# Service CRUD Endpoints
# =============================================================================

@router.get("")
async def list_services(
    service_type: str = None,
    status: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all services with optional filtering."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(RagService)

    if service_type:
        query = query.filter(RagService.service_type == service_type)
    if status:
        query = query.filter(RagService.health_status == status)

    services = query.order_by(RagService.name).all()
    return {"services": [s.to_dict() for s in services]}


@router.get("/{service_id}", response_model=ServiceResponse)
async def get_service(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific service by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(RagService).filter(RagService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    return service.to_dict()


@router.post("", response_model=ServiceResponse, status_code=201)
async def register_service(
    service_data: ServiceCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Register a new service."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if service name already exists
    existing = db.query(RagService).filter(RagService.name == service_data.name).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Service '{service_data.name}' already registered"
        )

    service = RagService(
        name=service_data.name,
        display_name=service_data.display_name,
        host=service_data.host,
        port=service_data.port,
        protocol=service_data.protocol,
        health_endpoint=service_data.health_endpoint,
        service_type=service_data.service_type,
        description=service_data.description,
        control_method=service_data.control_method,
        container_name=service_data.container_name,
        auto_start=service_data.auto_start,
        enabled=service_data.enabled,
    )
    db.add(service)
    db.commit()
    db.refresh(service)

    create_audit_log(
        db, current_user, 'create', service,
        new_value={'name': service.name, 'host': service.host, 'port': service.port},
        request=request
    )

    logger.info("service_registered", service_id=service.id, name=service.name,
                user=current_user.username)

    return service.to_dict()


@router.put("/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: int,
    service_data: ServiceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing service."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(RagService).filter(RagService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    old_value = service.to_dict()

    if service_data.display_name is not None:
        service.display_name = service_data.display_name
    if service_data.host is not None:
        service.host = service_data.host
    if service_data.port is not None:
        service.port = service_data.port
    if service_data.health_endpoint is not None:
        service.health_endpoint = service_data.health_endpoint
    if service_data.protocol is not None:
        service.protocol = service_data.protocol
    if service_data.service_type is not None:
        service.service_type = service_data.service_type
    if service_data.description is not None:
        service.description = service_data.description
    if service_data.control_method is not None:
        service.control_method = service_data.control_method
    if service_data.container_name is not None:
        service.container_name = service_data.container_name
    if service_data.auto_start is not None:
        service.auto_start = service_data.auto_start
    if service_data.enabled is not None:
        service.enabled = service_data.enabled

    db.commit()
    db.refresh(service)

    create_audit_log(db, current_user, 'update', service,
                     old_value=old_value, new_value=service.to_dict(), request=request)

    logger.info("service_updated", service_id=service.id, name=service.name,
                user=current_user.username)

    return service.to_dict()


@router.post("/{service_id}/check")
async def check_service_health(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Check service health and update status.

    Phase 4 reconcile (xander HIGH-1, ATHENA-1): redirected from the legacy
    _perform_health_check path (no SSRF guard, legacy status vocabulary) to the
    Phase 4 _poll_one so SSRF protection and canonical status values are uniform.
    Response shape preserved for frontend compatibility.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(RagService).filter(RagService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    from app.services.health_poller import _poll_one
    from shared.config import get_config as _get_config
    import asyncio as _asyncio
    import httpx as _httpx
    from datetime import datetime as _dt

    cfg = _get_config()
    semaphore = _asyncio.Semaphore(1)
    async with _httpx.AsyncClient(timeout=float(cfg.health_poll_timeout_seconds)) as client:
        result = await _poll_one(
            client,
            semaphore,
            service.id,
            service.name,
            service.host or '',
            service.port or 0,
            service.health_endpoint or '/health',
            service.protocol or 'http',
        )

    svc_id, status, response_time_ms, error_category, error_detail, health_message = result
    last_error = f'{error_category}:{error_detail}' if error_category != 'ok' else None

    db.query(RagService).filter(RagService.id == svc_id).update(
        {
            RagService.health_status: status,
            RagService.last_health_check: _dt.utcnow(),
            RagService.last_response_time_ms: response_time_ms,
            RagService.last_error: last_error,
        },
        synchronize_session=False,
    )
    db.commit()
    db.refresh(service)

    return {
        "service_id": service.id,
        "service_name": service.name,
        "status": service.health_status,
        "last_response_time_ms": service.last_response_time_ms,
        "last_health_check": service.last_health_check.isoformat() if service.last_health_check else None,
    }


@router.post("/status/all")
async def get_all_service_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Check status of all services.

    Phase 4 reconcile (xander HIGH-1, ATHENA-1): redirected from the legacy
    _perform_health_check bulk loop (no SSRF guard, legacy vocabulary) to the
    Phase 4 _poll_all_services.  Response shape {checked, healthy, unhealthy}
    preserved for frontend compatibility.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    from app.services.health_poller import _poll_all_services
    from shared.config import get_config as _get_config
    import asyncio as _asyncio

    semaphore = _asyncio.Semaphore(_get_config().health_poll_concurrency)
    summary = await _poll_all_services(semaphore)

    return {
        "checked": summary.get('services_polled', 0),
        "healthy": summary.get('healthy', 0),
        "unhealthy": summary.get('unhealthy', 0),
    }


@router.delete("/{service_id}", status_code=204)
async def delete_service(
    service_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a service."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(RagService).filter(RagService.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    service_name = service.name

    create_audit_log(db, current_user, 'delete', service,
                     old_value={'name': service.name, 'host': service.host, 'port': service.port},
                     request=request)

    db.delete(service)
    db.commit()

    logger.info("service_deleted", service_id=service_id, name=service_name,
                user=current_user.username)

    return None
