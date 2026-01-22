"""
Server configuration API routes.

Provides CRUD operations for server management and health monitoring.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
import structlog
import aiohttp

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, ServerConfig, AuditLog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/servers", tags=["servers"])


class ServerCreate(BaseModel):
    """Request model for creating a server."""
    name: str
    hostname: str = None
    ip_address: str
    role: str = None
    config: dict = None


class ServerUpdate(BaseModel):
    """Request model for updating a server."""
    hostname: str = None
    ip_address: str = None
    role: str = None
    status: str = None
    config: dict = None


class ServerResponse(BaseModel):
    """Response model for server data."""
    id: int
    name: str
    hostname: Optional[str] = None
    ip_address: str
    role: Optional[str] = None
    status: str
    config: Optional[dict] = None
    last_checked: Optional[str] = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    server: ServerConfig,
    old_value: dict = None,
    new_value: dict = None,
    request: Request = None
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='server',
        resource_id=server.id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='server', resource_id=server.id)


@router.get("")
async def list_servers(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all servers."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    servers = db.query(ServerConfig).order_by(ServerConfig.name).all()
    return {"servers": [s.to_dict() for s in servers]}


@router.get("/{server_id}", response_model=ServerResponse)
async def get_server(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific server by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    server = db.query(ServerConfig).filter(ServerConfig.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    return server.to_dict()


@router.post("", response_model=ServerResponse, status_code=201)
async def create_server(
    server_data: ServerCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new server."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if server name already exists
    existing = db.query(ServerConfig).filter(ServerConfig.name == server_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Server '{server_data.name}' already exists")

    # Create server
    server = ServerConfig(
        name=server_data.name,
        hostname=server_data.hostname,
        ip_address=server_data.ip_address,
        role=server_data.role,
        status='unknown',
        config=server_data.config
    )
    db.add(server)
    db.commit()
    db.refresh(server)

    # Audit log
    create_audit_log(
        db, current_user, 'create', server,
        new_value={'name': server.name, 'ip': server.ip_address, 'role': server.role},
        request=request
    )

    logger.info("server_created", server_id=server.id, name=server.name, user=current_user.username)

    return server.to_dict()


@router.put("/{server_id}", response_model=ServerResponse)
async def update_server(
    server_id: int,
    server_data: ServerUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing server."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    server = db.query(ServerConfig).filter(ServerConfig.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Store old values for audit
    old_value = {
        'hostname': server.hostname,
        'ip_address': server.ip_address,
        'role': server.role,
        'status': server.status
    }

    # Update fields
    if server_data.hostname is not None:
        server.hostname = server_data.hostname
    if server_data.ip_address is not None:
        server.ip_address = server_data.ip_address
    if server_data.role is not None:
        server.role = server_data.role
    if server_data.status is not None:
        server.status = server_data.status
    if server_data.config is not None:
        server.config = server_data.config

    db.commit()
    db.refresh(server)

    # Audit log
    new_value = {
        'hostname': server.hostname,
        'ip_address': server.ip_address,
        'role': server.role,
        'status': server.status
    }
    create_audit_log(db, current_user, 'update', server, old_value=old_value, new_value=new_value, request=request)

    logger.info("server_updated", server_id=server.id, name=server.name, user=current_user.username)

    return server.to_dict()


@router.post("/{server_id}/check")
async def check_server_health(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Check server health and update status."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    server = db.query(ServerConfig).filter(ServerConfig.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    # Simple ping check - try to reach any service on this server
    try:
        import asyncio
        import socket

        # Try TCP connection to port 22 (SSH) or port 80 (HTTP)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((server.ip_address, 22))
        sock.close()

        if result == 0:
            server.status = 'online'
        else:
            # Try port 8000 (common service port)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((server.ip_address, 8000))
            sock.close()
            server.status = 'online' if result == 0 else 'offline'
    except Exception as e:
        server.status = 'offline'
        logger.error("server_health_check_failed", server_id=server_id, error=str(e))

    server.last_checked = datetime.utcnow()
    db.commit()

    return {
        "server_id": server.id,
        "name": server.name,
        "status": server.status,
        "last_checked": server.last_checked.isoformat()
    }


@router.delete("/{server_id}", status_code=204)
async def delete_server(
    server_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a server."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    server = db.query(ServerConfig).filter(ServerConfig.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    server_name = server.name

    # Audit log before deletion
    old_value = {'name': server.name, 'ip': server.ip_address, 'role': server.role}
    create_audit_log(db, current_user, 'delete', server, old_value=old_value, request=request)

    # Delete
    db.delete(server)
    db.commit()

    logger.info("server_deleted", server_id=server_id, name=server_name, user=current_user.username)

    return None


@router.get("/{server_id}/services")
async def get_server_services(
    server_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all services registered to this server."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    server = db.query(ServerConfig).filter(ServerConfig.id == server_id).first()
    if not server:
        raise HTTPException(status_code=404, detail="Server not found")

    return {
        "server_id": server.id,
        "server_name": server.name,
        "services": [s.to_dict() for s in server.services]
    }
