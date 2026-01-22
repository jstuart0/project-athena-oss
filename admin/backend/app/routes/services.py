"""
Service registry API routes.

Provides CRUD operations for service management and health monitoring.
"""
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

SERVICE_HOST = os.getenv("SERVICE_HOST", "localhost")


@router.get("/gateway/health")
async def check_gateway_health():
    """Quick health check for Gateway service."""
    import time
    start = time.time()

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"http://{SERVICE_HOST}:8000/health",
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
async def check_orchestrator_health():
    """Quick health check for Orchestrator service."""
    import time
    start = time.time()

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"http://{SERVICE_HOST}:8001/health",
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
async def check_ollama_health():
    """Quick health check for Ollama LLM server."""
    import time
    start = time.time()

    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Ollama uses /api/tags to check if running
            async with session.get(
                f"http://{SERVICE_HOST}:11434/api/tags",
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
