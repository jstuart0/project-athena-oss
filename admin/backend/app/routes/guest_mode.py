"""
Guest Mode API routes.

Provides configuration management for guest mode (Airbnb/vacation rental integration).
Includes CRUD operations for manual guest entries and guest history tracking.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, EmailStr
from datetime import datetime, timedelta
import structlog
import hashlib
import uuid

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, GuestModeConfig, CalendarEvent, ModeOverride, AuditLog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/guest-mode", tags=["guest_mode"])


class GuestModeConfigCreate(BaseModel):
    """Request model for creating guest mode configuration."""
    enabled: bool = False
    calendar_source: str = "ical"
    calendar_url: Optional[str] = None
    calendar_poll_interval_minutes: int = 10
    buffer_before_checkin_hours: int = 2
    buffer_after_checkout_hours: int = 1
    owner_pin: Optional[str] = None  # Will be hashed before storage
    override_timeout_minutes: int = 60
    guest_allowed_intents: List[str] = []
    guest_restricted_entities: List[str] = []
    guest_allowed_domains: List[str] = []
    max_queries_per_minute_guest: int = 10
    max_queries_per_minute_owner: int = 100
    guest_data_retention_hours: int = 24
    auto_purge_enabled: bool = True
    config: dict = {}


class GuestModeConfigUpdate(BaseModel):
    """Request model for updating guest mode configuration."""
    enabled: Optional[bool] = None
    calendar_source: Optional[str] = None
    calendar_url: Optional[str] = None
    calendar_poll_interval_minutes: Optional[int] = None
    buffer_before_checkin_hours: Optional[int] = None
    buffer_after_checkout_hours: Optional[int] = None
    owner_pin: Optional[str] = None  # Will be hashed before storage
    override_timeout_minutes: Optional[int] = None
    guest_allowed_intents: Optional[List[str]] = None
    guest_restricted_entities: Optional[List[str]] = None
    guest_allowed_domains: Optional[List[str]] = None
    max_queries_per_minute_guest: Optional[int] = None
    max_queries_per_minute_owner: Optional[int] = None
    guest_data_retention_hours: Optional[int] = None
    auto_purge_enabled: Optional[bool] = None
    config: Optional[dict] = None


class GuestModeConfigResponse(BaseModel):
    """Response model for guest mode configuration."""
    id: int
    enabled: bool
    calendar_source: str
    calendar_url: Optional[str] = None
    calendar_poll_interval_minutes: int
    buffer_before_checkin_hours: int
    buffer_after_checkout_hours: int
    override_timeout_minutes: int
    guest_allowed_intents: List[str]
    guest_restricted_entities: List[str]
    guest_allowed_domains: List[str]
    max_queries_per_minute_guest: int
    max_queries_per_minute_owner: int
    guest_data_retention_hours: int
    auto_purge_enabled: bool
    config: dict
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class CalendarEventResponse(BaseModel):
    """Response model for calendar events."""
    id: int
    external_id: str
    source: str
    title: Optional[str] = None
    checkin: str
    checkout: str
    guest_name: Optional[str] = None
    status: str
    synced_at: str

    class Config:
        from_attributes = True


class ModeOverrideResponse(BaseModel):
    """Response model for mode overrides."""
    id: int
    mode: str
    activated_by: Optional[str] = None
    activated_at: str
    expires_at: Optional[str] = None
    voice_device_id: Optional[str] = None
    deactivated_at: Optional[str] = None

    class Config:
        from_attributes = True


# ============================================================================
# Manual Guest Entry Schemas
# ============================================================================

class GuestEntryCreate(BaseModel):
    """Request model for creating a manual guest entry."""
    checkin: datetime
    checkout: datetime
    guest_name: str
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    notes: Optional[str] = None
    is_test: bool = False


class GuestEntryUpdate(BaseModel):
    """Request model for updating a guest entry."""
    checkin: Optional[datetime] = None
    checkout: Optional[datetime] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    notes: Optional[str] = None
    status: Optional[str] = None


class GuestEntryResponse(BaseModel):
    """Response model for guest entries."""
    id: int
    external_id: str
    source: str
    title: Optional[str] = None
    checkin: str
    checkout: str
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    guest_phone: Optional[str] = None
    notes: Optional[str] = None
    status: str
    created_by: str
    is_test: bool = False
    guest_count: int = 1
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True

    @classmethod
    def from_orm_event(cls, event: CalendarEvent, guest_count: int = None) -> "GuestEntryResponse":
        """Convert CalendarEvent to GuestEntryResponse."""
        # Calculate guest count if not provided
        if guest_count is None:
            guest_count = len(event.guests) if hasattr(event, 'guests') and event.guests else 1
        return cls(
            id=event.id,
            external_id=event.external_id,
            source=event.source,
            title=event.title,
            checkin=event.checkin.isoformat() if event.checkin else None,
            checkout=event.checkout.isoformat() if event.checkout else None,
            guest_name=event.guest_name,
            guest_email=event.guest_email,
            guest_phone=event.guest_phone,
            notes=event.notes,
            status=event.status,
            created_by=event.created_by or 'ical_sync',
            is_test=event.is_test if hasattr(event, 'is_test') else False,
            guest_count=guest_count,
            created_at=event.created_at.isoformat() if event.created_at else None,
            updated_at=event.updated_at.isoformat() if event.updated_at else None,
        )


class GuestHistoryResponse(BaseModel):
    """Response model for guest history with pagination."""
    total: int
    limit: int
    offset: int
    entries: List[GuestEntryResponse]


def create_audit_log(
    db: Session,
    user: User,
    action: str,
    resource_type: str,
    resource_id: int = None,
    old_value: dict = None,
    new_value: dict = None,
    request: Request = None
):
    """Helper function to create audit log entries."""
    audit = AuditLog(
        user_id=user.id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    )
    db.add(audit)
    db.commit()
    logger.info("audit_log_created", action=action, resource_type=resource_type, resource_id=resource_id)


def hash_pin(pin: str) -> str:
    """Hash a PIN for secure storage."""
    return hashlib.sha256(pin.encode()).hexdigest()


@router.get("/config", response_model=GuestModeConfigResponse)
async def get_guest_mode_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get guest mode configuration (returns first/only config, creates default if none exists)."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(GuestModeConfig).first()
    if not config:
        # Auto-create default configuration
        config = GuestModeConfig(
            enabled=False,
            calendar_source="ical",
            calendar_poll_interval_minutes=10,
            buffer_before_checkin_hours=2,
            buffer_after_checkout_hours=1,
            override_timeout_minutes=60,
            guest_allowed_intents=[],
            guest_restricted_entities=[],
            guest_allowed_domains=[],
            max_queries_per_minute_guest=10,
            max_queries_per_minute_owner=100,
            guest_data_retention_hours=24,
            auto_purge_enabled=True,
            config={},
            created_by_id=current_user.id
        )
        db.add(config)
        db.commit()
        db.refresh(config)
        logger.info("guest_mode_config_auto_created", config_id=config.id, user=current_user.username)

    return config


@router.post("/config", response_model=GuestModeConfigResponse)
async def create_guest_mode_config(
    config_data: GuestModeConfigCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create guest mode configuration (only one config allowed)."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if config already exists
    existing = db.query(GuestModeConfig).first()
    if existing:
        raise HTTPException(status_code=400, detail="Guest mode configuration already exists. Use PATCH to update.")

    # Hash PIN if provided
    owner_pin_hash = hash_pin(config_data.owner_pin) if config_data.owner_pin else None

    # Create configuration
    config = GuestModeConfig(
        enabled=config_data.enabled,
        calendar_source=config_data.calendar_source,
        calendar_url=config_data.calendar_url,
        calendar_poll_interval_minutes=config_data.calendar_poll_interval_minutes,
        buffer_before_checkin_hours=config_data.buffer_before_checkin_hours,
        buffer_after_checkout_hours=config_data.buffer_after_checkout_hours,
        owner_pin=owner_pin_hash,
        override_timeout_minutes=config_data.override_timeout_minutes,
        guest_allowed_intents=config_data.guest_allowed_intents,
        guest_restricted_entities=config_data.guest_restricted_entities,
        guest_allowed_domains=config_data.guest_allowed_domains,
        max_queries_per_minute_guest=config_data.max_queries_per_minute_guest,
        max_queries_per_minute_owner=config_data.max_queries_per_minute_owner,
        guest_data_retention_hours=config_data.guest_data_retention_hours,
        auto_purge_enabled=config_data.auto_purge_enabled,
        config=config_data.config,
        created_by_id=current_user.id
    )

    db.add(config)
    db.commit()
    db.refresh(config)

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='create',
        resource_type='guest_mode_config',
        resource_id=config.id,
        new_value=config.to_dict(),
        request=request
    )

    logger.info("guest_mode_config_created", config_id=config.id, user=current_user.username)

    return config


@router.patch("/config", response_model=GuestModeConfigResponse)
async def update_guest_mode_config(
    config_data: GuestModeConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update guest mode configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(GuestModeConfig).first()
    if not config:
        raise HTTPException(status_code=404, detail="Guest mode configuration not found")

    # Store old values for audit
    old_value = config.to_dict()

    # Update fields
    if config_data.enabled is not None:
        config.enabled = config_data.enabled
    if config_data.calendar_source is not None:
        config.calendar_source = config_data.calendar_source
    if config_data.calendar_url is not None:
        config.calendar_url = config_data.calendar_url
    if config_data.calendar_poll_interval_minutes is not None:
        config.calendar_poll_interval_minutes = config_data.calendar_poll_interval_minutes
    if config_data.buffer_before_checkin_hours is not None:
        config.buffer_before_checkin_hours = config_data.buffer_before_checkin_hours
    if config_data.buffer_after_checkout_hours is not None:
        config.buffer_after_checkout_hours = config_data.buffer_after_checkout_hours
    if config_data.owner_pin is not None:
        config.owner_pin = hash_pin(config_data.owner_pin)
    if config_data.override_timeout_minutes is not None:
        config.override_timeout_minutes = config_data.override_timeout_minutes
    if config_data.guest_allowed_intents is not None:
        config.guest_allowed_intents = config_data.guest_allowed_intents
    if config_data.guest_restricted_entities is not None:
        config.guest_restricted_entities = config_data.guest_restricted_entities
    if config_data.guest_allowed_domains is not None:
        config.guest_allowed_domains = config_data.guest_allowed_domains
    if config_data.max_queries_per_minute_guest is not None:
        config.max_queries_per_minute_guest = config_data.max_queries_per_minute_guest
    if config_data.max_queries_per_minute_owner is not None:
        config.max_queries_per_minute_owner = config_data.max_queries_per_minute_owner
    if config_data.guest_data_retention_hours is not None:
        config.guest_data_retention_hours = config_data.guest_data_retention_hours
    if config_data.auto_purge_enabled is not None:
        config.auto_purge_enabled = config_data.auto_purge_enabled
    if config_data.config is not None:
        config.config = config_data.config

    db.commit()
    db.refresh(config)

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='update',
        resource_type='guest_mode_config',
        resource_id=config.id,
        old_value=old_value,
        new_value=config.to_dict(),
        request=request
    )

    logger.info("guest_mode_config_updated", config_id=config.id, user=current_user.username)

    return config


@router.get("/events", response_model=List[CalendarEventResponse])
async def list_calendar_events(
    status: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List calendar events with optional filtering."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(CalendarEvent)

    if status:
        query = query.filter(CalendarEvent.status == status)

    # Order by checkin descending (most recent first)
    query = query.order_by(CalendarEvent.checkin.desc())

    events = query.limit(limit).all()

    return events


# NOTE: These routes MUST come before /events/{event_id} to avoid FastAPI matching
# "current" or "upcoming" as an event_id integer, which causes 422 errors.

@router.get("/events/current")
async def get_current_guests(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get currently active guest stays."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    now = datetime.utcnow()
    entries = db.query(CalendarEvent).filter(
        CalendarEvent.checkin <= now,
        CalendarEvent.checkout >= now,
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.status == "confirmed"
    ).order_by(CalendarEvent.checkout.asc()).all()

    return {"entries": [GuestEntryResponse.from_orm_event(e) for e in entries]}


@router.get("/internal/current-guest")
async def get_current_guest_internal(
    db: Session = Depends(get_db)
):
    """
    Internal endpoint for service-to-service calls to get current guest.

    This endpoint does NOT require authentication and should only be
    accessible from within the cluster network. Returns the first current
    guest if any, or null if no current guest.

    Used by: Jarvis Web backend for guest context injection
    """
    now = datetime.utcnow()
    entry = db.query(CalendarEvent).filter(
        CalendarEvent.checkin <= now,
        CalendarEvent.checkout >= now,
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.status == "confirmed"
    ).order_by(CalendarEvent.checkout.asc()).first()

    if entry:
        logger.info("internal_current_guest_found", guest_name=entry.guest_name, guest_id=entry.id)
        return {
            "has_guest": True,
            "id": entry.id,
            "guest_name": entry.guest_name,
            "guest_email": entry.guest_email,
            "checkin": entry.checkin.isoformat() if entry.checkin else None,
            "checkout": entry.checkout.isoformat() if entry.checkout else None
        }

    logger.info("internal_current_guest_none")
    return {"has_guest": False}


@router.get("/events/upcoming")
async def get_upcoming_guests(
    days: int = Query(30, le=365),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get upcoming guest stays within specified days."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    now = datetime.utcnow()
    future = now + timedelta(days=days)

    entries = db.query(CalendarEvent).filter(
        CalendarEvent.checkin > now,
        CalendarEvent.checkin <= future,
        CalendarEvent.deleted_at.is_(None)
    ).order_by(CalendarEvent.checkin.asc()).all()

    return {"entries": [GuestEntryResponse.from_orm_event(e) for e in entries]}


@router.get("/events/{event_id}", response_model=CalendarEventResponse)
async def get_calendar_event(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific calendar event."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    event = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Calendar event not found")

    return event


@router.get("/overrides", response_model=List[ModeOverrideResponse])
async def list_mode_overrides(
    active_only: bool = False,
    limit: int = 50,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List mode overrides."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(ModeOverride)

    if active_only:
        now = datetime.utcnow()
        query = query.filter(
            ModeOverride.activated_at <= now,
            ModeOverride.deactivated_at == None  # Not deactivated
        ).filter(
            (ModeOverride.expires_at == None) | (ModeOverride.expires_at > now)  # Not expired
        )

    # Order by activation time descending
    query = query.order_by(ModeOverride.activated_at.desc())

    overrides = query.limit(limit).all()

    return overrides


# ============================================================================
# Manual Guest Entry CRUD Endpoints
# ============================================================================

@router.post("/events", response_model=GuestEntryResponse)
async def create_manual_guest_entry(
    entry: GuestEntryCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a manual guest entry."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Generate unique external ID for manual entries
    prefix = "test_" if entry.is_test else "manual_"
    external_id = f"{prefix}{uuid.uuid4().hex[:12]}"

    # Add [TEST] prefix to title for test entries
    title = f"{'[TEST] ' if entry.is_test else ''}Guest: {entry.guest_name}"

    db_entry = CalendarEvent(
        external_id=external_id,
        source="manual",
        title=title,
        checkin=entry.checkin,
        checkout=entry.checkout,
        guest_name=entry.guest_name,
        guest_email=entry.guest_email,
        guest_phone=entry.guest_phone,
        notes=entry.notes,
        status="confirmed",
        created_by="manual",
        is_test=entry.is_test
    )
    db.add(db_entry)
    db.commit()
    db.refresh(db_entry)

    # Also create a Guest record (primary guest)
    from app.models import Guest
    primary_guest = Guest(
        calendar_event_id=db_entry.id,
        name=entry.guest_name,
        email=entry.guest_email,
        phone=entry.guest_phone,
        is_primary=True,
        is_test=entry.is_test
    )
    db.add(primary_guest)
    db.commit()

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='create',
        resource_type='calendar_event',
        resource_id=db_entry.id,
        new_value=db_entry.to_dict(),
        request=request
    )

    logger.info("manual_guest_entry_created", entry_id=db_entry.id, guest_name=entry.guest_name, is_test=entry.is_test)

    return GuestEntryResponse.from_orm_event(db_entry, guest_count=1)


@router.patch("/events/{event_id}", response_model=GuestEntryResponse)
async def update_guest_entry(
    event_id: int,
    entry: GuestEntryUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a guest entry (manual entries allow full edits, iCal entries only notes/status)."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    db_entry = db.query(CalendarEvent).filter(
        CalendarEvent.id == event_id,
        CalendarEvent.deleted_at.is_(None)
    ).first()

    if not db_entry:
        raise HTTPException(status_code=404, detail="Guest entry not found")

    # Store old values for audit
    old_value = db_entry.to_dict()

    # Get update data (exclude None values)
    update_data = entry.model_dump(exclude_unset=True)

    # Only allow full edits on manual entries
    if db_entry.created_by != "manual":
        # For iCal entries, only allow notes and status updates
        allowed_fields = {"notes", "status"}
        update_data = {k: v for k, v in update_data.items() if k in allowed_fields}

    # Apply updates
    for field, value in update_data.items():
        setattr(db_entry, field, value)

    # Update title if guest_name changed
    if "guest_name" in update_data and db_entry.created_by == "manual":
        db_entry.title = f"Guest: {update_data['guest_name']}"

    db.commit()
    db.refresh(db_entry)

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='update',
        resource_type='calendar_event',
        resource_id=db_entry.id,
        old_value=old_value,
        new_value=db_entry.to_dict(),
        request=request
    )

    logger.info("guest_entry_updated", entry_id=db_entry.id)

    return GuestEntryResponse.from_orm_event(db_entry)


@router.delete("/events/{event_id}")
async def delete_guest_entry(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Soft delete a manual guest entry (only manual entries can be deleted)."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    db_entry = db.query(CalendarEvent).filter(
        CalendarEvent.id == event_id,
        CalendarEvent.created_by == "manual",
        CalendarEvent.deleted_at.is_(None)
    ).first()

    if not db_entry:
        raise HTTPException(
            status_code=404,
            detail="Manual guest entry not found or already deleted"
        )

    # Store old values for audit
    old_value = db_entry.to_dict()

    # Soft delete
    db_entry.deleted_at = datetime.utcnow()
    db.commit()

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='delete',
        resource_type='calendar_event',
        resource_id=db_entry.id,
        old_value=old_value,
        request=request
    )

    logger.info("guest_entry_deleted", entry_id=event_id)

    return {"message": "Guest entry deleted", "id": event_id}


@router.get("/history", response_model=GuestHistoryResponse)
async def get_guest_history(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    guest_name: Optional[str] = Query(None),
    include_deleted: bool = Query(False),
    include_test: bool = Query(False),
    limit: int = Query(50, le=200),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get guest history with filtering and pagination."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(CalendarEvent)

    # Filter by deleted status
    if not include_deleted:
        query = query.filter(CalendarEvent.deleted_at.is_(None))

    # Filter test data unless explicitly requested
    if not include_test:
        query = query.filter(CalendarEvent.is_test == False)

    # Filter by date range (based on checkout date for history)
    if start_date:
        query = query.filter(CalendarEvent.checkout >= start_date)
    if end_date:
        query = query.filter(CalendarEvent.checkin <= end_date)

    # Filter by guest name (partial match, case-insensitive)
    if guest_name:
        query = query.filter(
            CalendarEvent.guest_name.ilike(f"%{guest_name}%")
        )

    # Get total count before pagination
    total = query.count()

    # Order by checkout date descending (most recent first)
    entries = query.order_by(CalendarEvent.checkout.desc())\
        .offset(offset).limit(limit).all()

    return GuestHistoryResponse(
        total=total,
        limit=limit,
        offset=offset,
        entries=[GuestEntryResponse.from_orm_event(e) for e in entries]
    )


# ============================================================================
# Test Data Management Endpoints
# ============================================================================

def clear_overlapping_test_data(db: Session, checkin: datetime, checkout: datetime) -> int:
    """
    Clear test data that overlaps with a real reservation's date range.

    Returns number of test events cleared.
    """
    from app.models import Guest

    # Find overlapping test events
    overlapping = db.query(CalendarEvent).filter(
        CalendarEvent.is_test == True,
        CalendarEvent.deleted_at.is_(None),
        # Overlap condition: test.checkin < real.checkout AND test.checkout > real.checkin
        CalendarEvent.checkin < checkout,
        CalendarEvent.checkout > checkin
    ).all()

    if not overlapping:
        return 0

    event_ids = [e.id for e in overlapping]

    # Delete associated test guests
    db.query(Guest).filter(
        Guest.calendar_event_id.in_(event_ids),
        Guest.is_test == True
    ).delete(synchronize_session=False)

    # Soft delete the test events
    for event in overlapping:
        event.deleted_at = datetime.utcnow()

    db.commit()

    logger.info("overlapping_test_data_cleared",
                cleared_count=len(overlapping),
                real_checkin=checkin.isoformat(),
                real_checkout=checkout.isoformat())

    return len(overlapping)


@router.delete("/test-data")
async def clear_test_data(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Clear all test reservations and their associated guests."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    from app.models import Guest

    # Delete test guests first (foreign key constraint)
    deleted_guests = db.query(Guest).filter(Guest.is_test == True).delete()

    # Delete test calendar events
    deleted_events = db.query(CalendarEvent).filter(CalendarEvent.is_test == True).delete()

    db.commit()

    # Create audit log
    create_audit_log(
        db=db,
        user=current_user,
        action='delete_test_data',
        resource_type='guest_mode',
        new_value={'deleted_events': deleted_events, 'deleted_guests': deleted_guests},
        request=request
    )

    logger.info("test_data_cleared", deleted_events=deleted_events, deleted_guests=deleted_guests)

    return {
        "message": "Test data cleared",
        "deleted_events": deleted_events,
        "deleted_guests": deleted_guests
    }
