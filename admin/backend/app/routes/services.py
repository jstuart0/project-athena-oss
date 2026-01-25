"""
Service registry API routes.

Provides CRUD operations for service management and health monitoring.
"""
import os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
import structlog
import aiohttp
import asyncio
import ssl

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, ServiceRegistry, ServerConfig, AuditLog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/services", tags=["services"])


async def _perform_health_check(service: ServiceRegistry) -> bool:
    """Internal function to perform health check on a service."""
    import time
    start = time.time()

    try:
        # Special handling for Wyoming protocol services (Whisper, Piper)
        # These don't have HTTP endpoints, use TCP check instead
        if 'whisper' in service.service_name.lower() or 'piper' in service.service_name.lower():
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((service.server.ip_address, service.port))
            sock.close()
            elapsed = int((time.time() - start) * 1000)

            if result == 0:
                service.status = 'online'
                service.last_response_time = elapsed
                service.last_checked = datetime.utcnow()
                return True
            else:
                service.status = 'offline'
                service.last_checked = datetime.utcnow()
                return False

        if service.health_endpoint:
            # Special handling for HA-API: use domain name instead of IP
            # Direct IP access from Kubernetes pods is blocked by HA firewall
            if 'ha-api' in service.service_name.lower():
                host = "localhost"
                port = 443  # Use standard HTTPS port for domain access
            else:
                host = service.server.ip_address
                port = service.port

            url = f"{service.protocol}://{host}:{port}{service.health_endpoint}"

            # Create SSL context that doesn't verify certificates (for self-signed certs)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    elapsed = int((time.time() - start) * 1000)
                    if resp.status == 200:
                        service.status = 'online'
                        service.last_response_time = elapsed
                        service.last_checked = datetime.utcnow()
                        return True
                    elif resp.status == 401:
                        # Gateway and other services may require auth on /health
                        # Treat 401 as service running (auth required)
                        service.status = 'online'
                        service.last_response_time = elapsed
                        service.last_checked = datetime.utcnow()
                        return True
                    else:
                        service.status = 'degraded'
                        service.last_response_time = elapsed
                        service.last_checked = datetime.utcnow()
                        return False
        else:
            # TCP port check for services without HTTP health endpoint
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((service.server.ip_address, service.port))
            sock.close()
            elapsed = int((time.time() - start) * 1000)

            if result == 0:
                service.status = 'online'
                service.last_response_time = elapsed
                service.last_checked = datetime.utcnow()
                return True
            else:
                service.status = 'offline'
                service.last_checked = datetime.utcnow()
                return False
    except Exception as e:
        elapsed = int((time.time() - start) * 1000)
        service.status = 'offline'
        service.last_checked = datetime.utcnow()
        logger.error("service_health_check_failed", service=service.service_name, error=str(e))
        return False


class ServiceCreate(BaseModel):
    """Request model for registering a service."""
    server_id: int
    service_name: str
    port: int
    health_endpoint: str = None
    protocol: str = "http"


class ServiceUpdate(BaseModel):
    """Request model for updating a service."""
    health_endpoint: str = None
    protocol: str = None
    status: str = None


class ServiceResponse(BaseModel):
    """Response model for service data."""
    id: int
    server_id: int
    server_name: Optional[str] = None
    ip_address: Optional[str] = None
    service_name: str
    port: int
    health_endpoint: Optional[str] = None
    protocol: str
    status: str
    last_response_time: Optional[int] = None
    last_checked: Optional[str] = None

    class Config:
        from_attributes = True


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    service: ServiceRegistry,
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
    "ollama": os.getenv("OLLAMA_URL", "http://localhost:11434"),
    "redis": f"redis://{os.getenv('REDIS_SERVICE_HOST', 'redis')}:{os.getenv('REDIS_SERVICE_PORT', '6379')}",
    "qdrant": os.getenv("QDRANT_URL", "http://qdrant:6333"),
    "mlx": os.getenv("MLX_URL", ""),
}


def get_service_url_from_db(db: Session, service_name: str) -> Optional[dict]:
    """
    Look up service URL from database.
    Returns dict with url, protocol, health_endpoint or None if not found.
    """
    # Try exact match first, then partial match
    service = db.query(ServiceRegistry).filter(
        ServiceRegistry.service_name.ilike(f"%{service_name}%")
    ).first()

    if service and service.server:
        return {
            "url": f"{service.protocol}://{service.server.ip_address}:{service.port}",
            "protocol": service.protocol,
            "health_endpoint": service.health_endpoint,
            "ip": service.server.ip_address,
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


@router.get("/gateway/health")
async def check_gateway_health(db: Session = Depends(get_db)):
    """Quick health check for Gateway service. URL sourced from database."""
    import time
    start = time.time()

    url = get_service_url(db, "gateway")
    if not url:
        return {"status": "not_configured", "error": "Gateway not found in database"}

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
async def check_orchestrator_health(db: Session = Depends(get_db)):
    """Quick health check for Orchestrator service. URL sourced from database."""
    import time
    start = time.time()

    url = get_service_url(db, "orchestrator")
    if not url:
        return {"status": "not_configured", "error": "Orchestrator not found in database"}

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
async def check_ollama_health(db: Session = Depends(get_db)):
    """Quick health check for Ollama LLM server. URL sourced from database."""
    import time
    start = time.time()

    url = get_service_url(db, "ollama")
    if not url:
        return {"status": "not_configured", "error": "Ollama not found in database"}

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
async def check_redis_health(db: Session = Depends(get_db)):
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
async def check_qdrant_health(db: Session = Depends(get_db)):
    """Quick health check for Qdrant vector database. URL sourced from database."""
    import time
    start = time.time()

    qdrant_url = get_service_url(db, "qdrant")
    if not qdrant_url:
        return {"status": "not_configured", "error": "Qdrant not found in database"}

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
async def check_mlx_health(db: Session = Depends(get_db)):
    """Quick health check for MLX-LM server on Apple Silicon. URL sourced from database."""
    import time
    start = time.time()

    # Try database first, then environment variable
    mlx_url = get_service_url(db, "mlx")

    if not mlx_url:
        return {"status": "not_configured", "error": "MLX not found in database or MLX_URL not set"}

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
    server_id: int = None,
    status: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all services with optional filtering."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(ServiceRegistry)

    if server_id:
        query = query.filter(ServiceRegistry.server_id == server_id)
    if status:
        query = query.filter(ServiceRegistry.status == status)

    services = query.order_by(ServiceRegistry.service_name).all()
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

    service = db.query(ServiceRegistry).filter(ServiceRegistry.id == service_id).first()
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

    # Verify server exists
    server = db.query(ServerConfig).filter(ServerConfig.id == service_data.server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail=f"Server ID {service_data.server_id} not found")

    # Check if service already exists
    existing = db.query(ServiceRegistry).filter(
        ServiceRegistry.server_id == service_data.server_id,
        ServiceRegistry.service_name == service_data.service_name,
        ServiceRegistry.port == service_data.port
    ).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Service '{service_data.service_name}' already registered on server {server.name}:{service_data.port}"
        )

    # Create service
    service = ServiceRegistry(
        server_id=service_data.server_id,
        service_name=service_data.service_name,
        port=service_data.port,
        health_endpoint=service_data.health_endpoint,
        protocol=service_data.protocol,
        status='unknown'
    )
    db.add(service)
    db.commit()
    db.refresh(service)

    # Audit log
    create_audit_log(
        db, current_user, 'create', service,
        new_value={'service': service.service_name, 'port': service.port, 'server': server.name},
        request=request
    )

    logger.info("service_registered", service_id=service.id, name=service.service_name,
                server=server.name, user=current_user.username)

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

    service = db.query(ServiceRegistry).filter(ServiceRegistry.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    # Store old values for audit
    old_value = {
        'health_endpoint': service.health_endpoint,
        'protocol': service.protocol,
        'status': service.status
    }

    # Update fields
    if service_data.health_endpoint is not None:
        service.health_endpoint = service_data.health_endpoint
    if service_data.protocol is not None:
        service.protocol = service_data.protocol
    if service_data.status is not None:
        service.status = service_data.status

    db.commit()
    db.refresh(service)

    # Audit log
    new_value = {
        'health_endpoint': service.health_endpoint,
        'protocol': service.protocol,
        'status': service.status
    }
    create_audit_log(db, current_user, 'update', service, old_value=old_value, new_value=new_value, request=request)

    logger.info("service_updated", service_id=service.id, name=service.service_name, user=current_user.username)

    return service.to_dict()


@router.post("/{service_id}/check")
async def check_service_health(
    service_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Check service health and update status."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    service = db.query(ServiceRegistry).filter(ServiceRegistry.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    # Perform health check
    import time
    start = time.time()

    try:
        if service.health_endpoint:
            url = f"{service.protocol}://{service.server.ip_address}:{service.port}{service.health_endpoint}"

            # Create SSL context that doesn't verify certificates (for self-signed certs)
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    elapsed = int((time.time() - start) * 1000)
                    if resp.status == 200:
                        service.status = 'online'
                        service.last_response_time = elapsed
                    else:
                        service.status = 'degraded'
                        service.last_response_time = elapsed
        else:
            # TCP check if no health endpoint
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((service.server.ip_address, service.port))
            sock.close()
            elapsed = int((time.time() - start) * 1000)

            if result == 0:
                service.status = 'online'
                service.last_response_time = elapsed
            else:
                service.status = 'offline'
                service.last_response_time = None

    except Exception as e:
        service.status = 'offline'
        service.last_response_time = None
        logger.error("service_health_check_failed", service_id=service_id, error=str(e))

    service.last_checked = datetime.utcnow()
    db.commit()

    return {
        "service_id": service.id,
        "service_name": service.service_name,
        "status": service.status,
        "last_response_time": service.last_response_time,
        "last_checked": service.last_checked.isoformat()
    }


@router.post("/status/all")
async def get_all_service_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Check status of all services."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    services = db.query(ServiceRegistry).all()

    # Check each service health
    checked = 0
    healthy = 0
    unhealthy = 0

    for service in services:
        checked += 1
        is_healthy = await _perform_health_check(service)

        if is_healthy:
            healthy += 1
        else:
            unhealthy += 1

    # Commit all status updates
    db.commit()

    return {
        "checked": checked,
        "healthy": healthy,
        "unhealthy": unhealthy
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

    service = db.query(ServiceRegistry).filter(ServiceRegistry.id == service_id).first()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    service_name = service.service_name

    # Audit log before deletion
    old_value = {'service': service.service_name, 'port': service.port, 'server': service.server.name}
    create_audit_log(db, current_user, 'delete', service, old_value=old_value, request=request)

    # Delete
    db.delete(service)
    db.commit()

    logger.info("service_deleted", service_id=service_id, name=service_name, user=current_user.username)

    return None
