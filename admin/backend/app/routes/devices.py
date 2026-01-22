"""
Device management API routes.

Provides CRUD operations for tracking Wyoming devices, Jetsons, and services.
"""
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Device, AuditLog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/devices", tags=["devices"])


class DeviceCreate(BaseModel):
    """Request model for creating a device."""
    device_type: str  # 'wyoming', 'jetson', 'service'
    name: str
    hostname: str = None
    ip_address: str = None
    port: int = None
    zone: str = None  # Physical location (e.g., 'office', 'kitchen')
    config: dict = None


class DeviceUpdate(BaseModel):
    """Request model for updating a device."""
    hostname: str = None
    ip_address: str = None
    port: int = None
    zone: str = None
    status: str = None  # 'online', 'offline', 'degraded', 'unknown'
    config: dict = None


class DeviceResponse(BaseModel):
    """Response model for device data."""
    id: int
    device_type: str
    name: str
    hostname: str = None
    ip_address: str = None
    port: int = None
    zone: str = None
    status: str
    last_seen: str = None
    config: dict = None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    device: Device,
    old_value: dict = None,
    new_value: dict = None,
    request: Request = None
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type='device',
        resource_id=device.id,
        device_id=device.id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type='device', resource_id=device.id)


@router.get("", response_model=List[DeviceResponse])
async def list_devices(
    device_type: str = None,
    zone: str = None,
    status: str = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all devices with optional filtering."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(Device)

    if device_type:
        query = query.filter(Device.device_type == device_type)
    if zone:
        query = query.filter(Device.zone == zone)
    if status:
        query = query.filter(Device.status == status)

    devices = query.order_by(Device.zone, Device.name).all()

    return [device.to_dict() for device in devices]


@router.get("/zones")
async def list_zones(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all unique zones where devices are deployed."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    zones = db.query(Device.zone).distinct().filter(Device.zone.isnot(None)).all()
    return {"zones": [z[0] for z in zones]}


@router.get("/{device_id}", response_model=DeviceResponse)
async def get_device(
    device_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific device by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    return device.to_dict()


@router.post("", response_model=DeviceResponse, status_code=201)
async def create_device(
    device_data: DeviceCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new device."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate device type
    valid_types = ['wyoming', 'jetson', 'service']
    if device_data.device_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid device type. Must be one of: {', '.join(valid_types)}"
        )

    # Check if device name already exists
    existing = db.query(Device).filter(Device.name == device_data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Device '{device_data.name}' already exists")

    # Create device
    device = Device(
        device_type=device_data.device_type,
        name=device_data.name,
        hostname=device_data.hostname,
        ip_address=device_data.ip_address,
        port=device_data.port,
        zone=device_data.zone,
        status='unknown',
        config=device_data.config
    )
    db.add(device)
    db.commit()
    db.refresh(device)

    # Audit log
    create_audit_log(
        db, current_user, 'create', device,
        new_value={'name': device.name, 'type': device.device_type, 'zone': device.zone},
        request=request
    )

    logger.info("device_created", device_id=device.id, name=device.name, type=device.device_type,
                user=current_user.username)

    return device.to_dict()


@router.put("/{device_id}", response_model=DeviceResponse)
async def update_device(
    device_id: int,
    device_data: DeviceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing device."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Store old values for audit
    old_value = {
        'hostname': device.hostname,
        'ip_address': device.ip_address,
        'port': device.port,
        'zone': device.zone,
        'status': device.status
    }

    # Update fields
    if device_data.hostname is not None:
        device.hostname = device_data.hostname
    if device_data.ip_address is not None:
        device.ip_address = device_data.ip_address
    if device_data.port is not None:
        device.port = device_data.port
    if device_data.zone is not None:
        device.zone = device_data.zone
    if device_data.status is not None:
        device.status = device_data.status
    if device_data.config is not None:
        device.config = device_data.config

    db.commit()
    db.refresh(device)

    # Audit log
    new_value = {
        'hostname': device.hostname,
        'ip_address': device.ip_address,
        'port': device.port,
        'zone': device.zone,
        'status': device.status
    }
    create_audit_log(db, current_user, 'update', device, old_value=old_value, new_value=new_value, request=request)

    logger.info("device_updated", device_id=device.id, name=device.name, user=current_user.username)

    return device.to_dict()


@router.post("/{device_id}/heartbeat")
async def device_heartbeat(
    device_id: int,
    status: str = 'online',
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update device last_seen timestamp (heartbeat).

    This endpoint can be called periodically by devices to report they're alive.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    device.last_seen = datetime.utcnow()
    device.status = status
    db.commit()

    return {
        "device_id": device.id,
        "name": device.name,
        "status": device.status,
        "last_seen": device.last_seen.isoformat()
    }


@router.delete("/{device_id}", status_code=204)
async def delete_device(
    device_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a device."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    device = db.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    device_name = device.name

    # Audit log before deletion
    old_value = {'name': device.name, 'type': device.device_type, 'zone': device.zone}
    create_audit_log(db, current_user, 'delete', device, old_value=old_value, request=request)

    # Delete
    db.delete(device)
    db.commit()

    logger.info("device_deleted", device_id=device_id, name=device_name, user=current_user.username)

    return None
