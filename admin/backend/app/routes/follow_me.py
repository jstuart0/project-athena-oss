"""
Follow-Me Audio Configuration Routes

Manages configuration for the follow-me audio feature that
transfers music playback based on motion detection.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import Optional, List, Dict, Any
from datetime import datetime

from app.auth.oidc import get_current_user
from app.database import get_db
from app.models import FollowMeConfig, RoomMotionSensor, FollowMeExcludedRoom

router = APIRouter(prefix="/api/follow-me", tags=["Follow-Me Audio"])


# ==============================================================================
# Pydantic Models
# ==============================================================================

class FollowMeConfigUpdate(BaseModel):
    """Update follow-me configuration."""
    enabled: Optional[bool] = None
    mode: Optional[str] = Field(None, pattern="^(off|single|party)$")
    debounce_seconds: Optional[float] = Field(None, ge=1.0, le=30.0)
    grace_period_seconds: Optional[float] = Field(None, ge=5.0, le=120.0)
    min_motion_duration_seconds: Optional[float] = Field(None, ge=0.5, le=10.0)
    quiet_hours_start: Optional[int] = Field(None, ge=0, le=23)
    quiet_hours_end: Optional[int] = Field(None, ge=0, le=23)


class RoomMotionSensorCreate(BaseModel):
    """Create room motion sensor mapping."""
    room_name: str
    motion_entity_id: str
    enabled: bool = True
    priority: int = 0


class RoomMotionSensorUpdate(BaseModel):
    """Update room motion sensor mapping."""
    motion_entity_id: Optional[str] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class ExcludedRoomCreate(BaseModel):
    """Create excluded room."""
    room_name: str
    reason: Optional[str] = None


# ==============================================================================
# Config Routes
# ==============================================================================

@router.get("/config")
async def get_config(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get current follow-me configuration."""
    config = db.query(FollowMeConfig).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")
    return config.to_dict()


@router.put("/config")
async def update_config(
    config_update: FollowMeConfigUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Update follow-me configuration."""
    config = db.query(FollowMeConfig).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    update_data = config_update.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No updates provided")

    for key, value in update_data.items():
        setattr(config, key, value)

    db.commit()
    db.refresh(config)
    return config.to_dict()


# ==============================================================================
# Room Motion Sensor Routes
# ==============================================================================

@router.get("/rooms")
async def list_room_sensors(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all room motion sensor mappings."""
    sensors = db.query(RoomMotionSensor).order_by(
        RoomMotionSensor.priority.desc(),
        RoomMotionSensor.room_name
    ).all()
    return [s.to_dict() for s in sensors]


@router.post("/rooms")
async def create_room_sensor(
    sensor: RoomMotionSensorCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Create or update room motion sensor mapping."""
    existing = db.query(RoomMotionSensor).filter(
        RoomMotionSensor.room_name == sensor.room_name
    ).first()

    if existing:
        # Update existing
        existing.motion_entity_id = sensor.motion_entity_id
        existing.enabled = sensor.enabled
        existing.priority = sensor.priority
        db.commit()
        db.refresh(existing)
        return existing.to_dict()
    else:
        # Create new
        new_sensor = RoomMotionSensor(
            room_name=sensor.room_name,
            motion_entity_id=sensor.motion_entity_id,
            enabled=sensor.enabled,
            priority=sensor.priority
        )
        db.add(new_sensor)
        db.commit()
        db.refresh(new_sensor)
        return new_sensor.to_dict()


@router.put("/rooms/{room_name}")
async def update_room_sensor(
    room_name: str,
    sensor: RoomMotionSensorUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Update room motion sensor mapping."""
    existing = db.query(RoomMotionSensor).filter(
        RoomMotionSensor.room_name == room_name
    ).first()

    if not existing:
        raise HTTPException(status_code=404, detail="Room not found")

    update_data = sensor.dict(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No updates provided")

    for key, value in update_data.items():
        setattr(existing, key, value)

    db.commit()
    db.refresh(existing)
    return existing.to_dict()


@router.delete("/rooms/{room_name}")
async def delete_room_sensor(
    room_name: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, str]:
    """Delete room motion sensor mapping."""
    existing = db.query(RoomMotionSensor).filter(
        RoomMotionSensor.room_name == room_name
    ).first()

    if not existing:
        raise HTTPException(status_code=404, detail="Room not found")

    db.delete(existing)
    db.commit()
    return {"message": "Room sensor deleted"}


# ==============================================================================
# Excluded Rooms Routes
# ==============================================================================

@router.get("/excluded")
async def list_excluded_rooms(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all excluded rooms."""
    rooms = db.query(FollowMeExcludedRoom).order_by(
        FollowMeExcludedRoom.room_name
    ).all()
    return [r.to_dict() for r in rooms]


@router.post("/excluded")
async def add_excluded_room(
    room: ExcludedRoomCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Add room to exclusion list."""
    existing = db.query(FollowMeExcludedRoom).filter(
        FollowMeExcludedRoom.room_name == room.room_name
    ).first()

    if existing:
        existing.reason = room.reason
        db.commit()
        db.refresh(existing)
        return existing.to_dict()
    else:
        new_room = FollowMeExcludedRoom(
            room_name=room.room_name,
            reason=room.reason
        )
        db.add(new_room)
        db.commit()
        db.refresh(new_room)
        return new_room.to_dict()


@router.delete("/excluded/{room_name}")
async def remove_excluded_room(
    room_name: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, str]:
    """Remove room from exclusion list."""
    existing = db.query(FollowMeExcludedRoom).filter(
        FollowMeExcludedRoom.room_name == room_name
    ).first()

    if not existing:
        raise HTTPException(status_code=404, detail="Room not found")

    db.delete(existing)
    db.commit()
    return {"message": "Room removed from exclusions"}


# ==============================================================================
# Internal Endpoint (No Auth)
# ==============================================================================

@router.get("/internal/config")
async def get_internal_config(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """
    Get follow-me config for orchestrator (no auth required).
    Used internally by the orchestrator service.
    """
    config = db.query(FollowMeConfig).first()

    rooms = db.query(RoomMotionSensor).filter(
        RoomMotionSensor.enabled == True
    ).order_by(RoomMotionSensor.priority.desc()).all()

    excluded = db.query(FollowMeExcludedRoom).all()

    return {
        "config": config.to_dict() if config else None,
        "room_motion_mapping": {r.room_name: r.motion_entity_id for r in rooms},
        "excluded_rooms": [r.room_name for r in excluded]
    }
