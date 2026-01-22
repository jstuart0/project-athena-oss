"""
Voice Automations API Routes

Provides CRUD operations for voice-created automations.
Supports owner and guest-scoped automations with archival/restoration.
"""

from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import VoiceAutomation

logger = structlog.get_logger()

router = APIRouter(prefix="/api/voice-automations", tags=["voice-automations"])


# Pydantic models for request/response
class TriggerConfig(BaseModel):
    type: str = Field(..., description="Trigger type: time, sunset, sunrise")
    time: Optional[str] = Field(None, description="Time in HH:MM format")
    offset: Optional[str] = Field(None, description="Offset from sun event")


class ConditionConfig(BaseModel):
    type: str = Field(..., description="Condition type: weekday, time_range, state")
    weekdays: Optional[List[str]] = None
    after: Optional[str] = None
    before: Optional[str] = None
    entity_id: Optional[str] = None
    state: Optional[str] = None


class ActionConfig(BaseModel):
    service: str = Field(..., description="Service call (e.g., light.turn_on)")
    entity_id: str = Field(..., description="Target entity")
    data: Optional[dict] = None
    delay: Optional[str] = None


class VoiceAutomationCreate(BaseModel):
    name: str = Field(..., description="Human-readable name")
    ha_automation_id: Optional[str] = None
    owner_type: str = Field("owner", description="owner or guest")
    guest_session_id: Optional[str] = None
    guest_name: Optional[str] = None
    created_by_room: Optional[str] = None
    trigger_config: dict = Field(..., description="Trigger configuration")
    conditions_config: Optional[List[dict]] = None
    actions_config: List[dict] = Field(..., description="Actions to perform")
    is_one_time: bool = False
    end_date: Optional[str] = None


class VoiceAutomationResponse(BaseModel):
    id: int
    name: str
    ha_automation_id: Optional[str]
    owner_type: str
    guest_session_id: Optional[str]
    guest_name: Optional[str]
    created_by_room: Optional[str]
    trigger_config: dict
    conditions_config: Optional[List[dict]]
    actions_config: List[dict]
    is_one_time: bool
    end_date: Optional[str]
    status: str
    archived_at: Optional[str]
    archive_reason: Optional[str]
    last_triggered_at: Optional[str]
    trigger_count: int
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class VoiceAutomationSummary(BaseModel):
    id: int
    name: str
    description: str
    trigger_count: int
    last_used: Optional[str]


# Routes

@router.get("", response_model=List[VoiceAutomationResponse])
async def list_automations(
    owner_type: Optional[str] = Query(None, description="Filter by owner type"),
    guest_name: Optional[str] = Query(None, description="Filter by guest name"),
    guest_session_id: Optional[str] = Query(None, description="Filter by session"),
    status: Optional[str] = Query("active", description="Filter by status"),
    include_archived: bool = Query(False, description="Include archived automations"),
    name_search: Optional[str] = Query(None, description="Search by name"),
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """List voice automations with optional filters."""
    query = db.query(VoiceAutomation)

    if owner_type:
        query = query.filter(VoiceAutomation.owner_type == owner_type)

    if guest_name:
        query = query.filter(VoiceAutomation.guest_name == guest_name)

    if guest_session_id:
        query = query.filter(VoiceAutomation.guest_session_id == guest_session_id)

    if status and not include_archived:
        query = query.filter(VoiceAutomation.status == status)
    elif include_archived:
        query = query.filter(VoiceAutomation.status.in_(["active", "archived"]))

    if name_search:
        query = query.filter(VoiceAutomation.name.ilike(f"%{name_search}%"))

    automations = query.order_by(VoiceAutomation.created_at.desc()).all()

    return [VoiceAutomationResponse(**a.to_dict()) for a in automations]


@router.get("/guest/{guest_name}/archived", response_model=List[VoiceAutomationSummary])
async def get_archived_for_guest(
    guest_name: str,
    db: Session = Depends(get_db)
):
    """
    Get archived automations for a returning guest.

    Used to prompt returning guests about restoring their previous automations.
    No authentication required - called internally by orchestrator.
    """
    automations = db.query(VoiceAutomation).filter(
        VoiceAutomation.guest_name == guest_name,
        VoiceAutomation.status == "archived"
    ).order_by(VoiceAutomation.trigger_count.desc()).all()

    return [VoiceAutomationSummary(**a.to_summary()) for a in automations]


@router.get("/{automation_id}", response_model=VoiceAutomationResponse)
async def get_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """Get a specific automation by ID."""
    automation = db.query(VoiceAutomation).filter(
        VoiceAutomation.id == automation_id
    ).first()

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    return VoiceAutomationResponse(**automation.to_dict())


@router.post("", response_model=VoiceAutomationResponse)
async def create_automation(
    automation: VoiceAutomationCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new voice automation.

    No authentication required - called internally by orchestrator/automation agent.
    """
    db_automation = VoiceAutomation(
        name=automation.name,
        ha_automation_id=automation.ha_automation_id,
        owner_type=automation.owner_type,
        guest_session_id=automation.guest_session_id,
        guest_name=automation.guest_name,
        created_by_room=automation.created_by_room,
        trigger_config=automation.trigger_config,
        conditions_config=automation.conditions_config,
        actions_config=automation.actions_config,
        is_one_time=automation.is_one_time,
        status="active"
    )

    db.add(db_automation)
    db.commit()
    db.refresh(db_automation)

    logger.info(
        "voice_automation_created",
        automation_id=db_automation.id,
        name=automation.name,
        owner_type=automation.owner_type,
        guest_name=automation.guest_name
    )

    return VoiceAutomationResponse(**db_automation.to_dict())


@router.post("/{automation_id}/archive")
async def archive_automation(
    automation_id: int,
    reason: str = Query("user_deleted", description="Archive reason"),
    db: Session = Depends(get_db)
):
    """
    Archive an automation (soft delete).

    Used for guest automations when guest departs or manually deleted.
    """
    automation = db.query(VoiceAutomation).filter(
        VoiceAutomation.id == automation_id
    ).first()

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    automation.status = "archived"
    automation.archived_at = datetime.utcnow()
    automation.archive_reason = reason

    db.commit()

    logger.info(
        "voice_automation_archived",
        automation_id=automation_id,
        reason=reason
    )

    return {"status": "archived", "automation_id": automation_id}


@router.post("/{automation_id}/restore")
async def restore_automation(
    automation_id: int,
    new_session_id: Optional[str] = Query(None, description="New guest session ID"),
    db: Session = Depends(get_db)
):
    """
    Restore an archived automation.

    Used for returning guests who want their previous automations back.
    """
    automation = db.query(VoiceAutomation).filter(
        VoiceAutomation.id == automation_id,
        VoiceAutomation.status == "archived"
    ).first()

    if not automation:
        raise HTTPException(status_code=404, detail="Archived automation not found")

    automation.status = "active"
    automation.archived_at = None
    automation.archive_reason = None

    if new_session_id:
        automation.guest_session_id = new_session_id

    db.commit()

    logger.info(
        "voice_automation_restored",
        automation_id=automation_id,
        new_session_id=new_session_id
    )

    return {"status": "restored", "automation_id": automation_id}


@router.delete("/{automation_id}")
async def delete_automation(
    automation_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user)
):
    """
    Permanently delete an automation.

    Only owner automations should be permanently deleted.
    Guest automations should be archived instead.
    """
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    automation = db.query(VoiceAutomation).filter(
        VoiceAutomation.id == automation_id
    ).first()

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    db.delete(automation)
    db.commit()

    logger.info(
        "voice_automation_deleted",
        automation_id=automation_id,
        user=current_user.username
    )

    return {"status": "deleted", "automation_id": automation_id}


@router.post("/guest-departure/{session_id}")
async def handle_guest_departure(
    session_id: str,
    db: Session = Depends(get_db)
):
    """
    Archive all automations for a departing guest.

    Called by session manager when guest mode ends.
    """
    automations = db.query(VoiceAutomation).filter(
        VoiceAutomation.guest_session_id == session_id,
        VoiceAutomation.status == "active"
    ).all()

    count = 0
    for automation in automations:
        automation.status = "archived"
        automation.archived_at = datetime.utcnow()
        automation.archive_reason = "guest_departed"
        count += 1

    db.commit()

    logger.info(
        "guest_automations_archived",
        session_id=session_id,
        count=count
    )

    return {"status": "archived", "count": count}


@router.post("/{automation_id}/triggered")
async def record_trigger(
    automation_id: int,
    db: Session = Depends(get_db)
):
    """
    Record that an automation was triggered.

    Called by Home Assistant webhook or orchestrator.
    """
    automation = db.query(VoiceAutomation).filter(
        VoiceAutomation.id == automation_id
    ).first()

    if not automation:
        raise HTTPException(status_code=404, detail="Automation not found")

    automation.last_triggered_at = datetime.utcnow()
    automation.trigger_count += 1

    # Handle one-time automations
    if automation.is_one_time:
        automation.status = "archived"
        automation.archived_at = datetime.utcnow()
        automation.archive_reason = "one_time_completed"

    db.commit()

    return {
        "automation_id": automation_id,
        "trigger_count": automation.trigger_count,
        "archived": automation.status == "archived"
    }


# Internal routes (no auth required - called by orchestrator)

@router.get("/internal/by-guest-name/{guest_name}", response_model=List[VoiceAutomationResponse])
async def get_automations_by_guest_name(
    guest_name: str,
    include_archived: bool = Query(False),
    db: Session = Depends(get_db)
):
    """Get all automations for a guest by name (internal use)."""
    query = db.query(VoiceAutomation).filter(
        VoiceAutomation.guest_name == guest_name
    )

    if not include_archived:
        query = query.filter(VoiceAutomation.status == "active")

    automations = query.order_by(VoiceAutomation.created_at.desc()).all()

    return [VoiceAutomationResponse(**a.to_dict()) for a in automations]


class ArchiveGuestRequest(BaseModel):
    guest_session_id: Optional[str] = None
    guest_name: Optional[str] = None
    reason: str = "guest_departed"


class RestoreGuestRequest(BaseModel):
    guest_name: str
    new_session_id: Optional[str] = None


@router.post("/archive-guest")
async def archive_guest_automations(
    request: ArchiveGuestRequest,
    db: Session = Depends(get_db)
):
    """
    Archive all automations for a guest.

    Called when a guest departs. Can match by session_id or guest_name.
    """
    query = db.query(VoiceAutomation).filter(
        VoiceAutomation.status == "active",
        VoiceAutomation.owner_type == "guest"
    )

    if request.guest_session_id:
        query = query.filter(VoiceAutomation.guest_session_id == request.guest_session_id)
    elif request.guest_name:
        query = query.filter(VoiceAutomation.guest_name == request.guest_name)
    else:
        raise HTTPException(
            status_code=400,
            detail="Must provide either guest_session_id or guest_name"
        )

    automations = query.all()
    count = 0

    for automation in automations:
        automation.status = "archived"
        automation.archived_at = datetime.utcnow()
        automation.archive_reason = request.reason
        count += 1

    db.commit()

    logger.info(
        "guest_automations_archived",
        guest_name=request.guest_name,
        guest_session_id=request.guest_session_id,
        count=count
    )

    return {"archived_count": count}


@router.post("/restore-guest")
async def restore_guest_automations(
    request: RestoreGuestRequest,
    db: Session = Depends(get_db)
):
    """
    Restore all archived automations for a returning guest.

    Matches by guest_name and optionally updates the session_id.
    """
    automations = db.query(VoiceAutomation).filter(
        VoiceAutomation.guest_name == request.guest_name,
        VoiceAutomation.status == "archived"
    ).all()

    count = 0
    for automation in automations:
        automation.status = "active"
        automation.archived_at = None
        automation.archive_reason = None
        if request.new_session_id:
            automation.guest_session_id = request.new_session_id
        count += 1

    db.commit()

    logger.info(
        "guest_automations_restored",
        guest_name=request.guest_name,
        count=count
    )

    return {"restored_count": count}
