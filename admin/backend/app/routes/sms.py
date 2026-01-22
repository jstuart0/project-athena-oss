"""
SMS Notifications API routes.

Provides configuration and management for SMS notifications to guests.
Includes settings management, history viewing, and manual send capabilities.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
from pydantic import BaseModel
from datetime import datetime, date
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import (
    User, SMSSettings, GuestSMSPreference, SMSHistory,
    SMSCostTracking, CalendarEvent, AuditLog,
    SMSTemplate, ScheduledSMS, TipPrompt, SMSIncoming
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/sms", tags=["sms"])

# Separate router for tips (uses /api prefix, not /api/sms)
tips_router = APIRouter(prefix="/api", tags=["tips"])


# =============================================================================
# Pydantic Models
# =============================================================================


class SMSSettingsResponse(BaseModel):
    """Response model for SMS settings."""
    id: int
    enabled: bool
    test_mode: bool
    auto_offer_mode: str
    rate_limit_per_minute: int
    rate_limit_per_stay: int
    from_number: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SMSSettingsUpdate(BaseModel):
    """Request model for updating SMS settings."""
    enabled: Optional[bool] = None
    test_mode: Optional[bool] = None
    auto_offer_mode: Optional[str] = None
    rate_limit_per_minute: Optional[int] = None
    rate_limit_per_stay: Optional[int] = None
    from_number: Optional[str] = None


class GuestSMSPreferenceResponse(BaseModel):
    """Response model for guest SMS preferences."""
    id: int
    calendar_event_id: int
    sms_enabled: bool
    dont_ask_again: bool
    preferred_phone: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class GuestSMSPreferenceUpdate(BaseModel):
    """Request model for updating guest SMS preferences."""
    sms_enabled: Optional[bool] = None
    dont_ask_again: Optional[bool] = None
    preferred_phone: Optional[str] = None


class SMSHistoryResponse(BaseModel):
    """Response model for SMS history entries."""
    id: int
    calendar_event_id: Optional[int] = None
    guest_name: Optional[str] = None
    phone_number: str
    content: str
    content_summary: Optional[str] = None
    content_type: Optional[str] = None
    triggered_by: Optional[str] = None
    original_query: Optional[str] = None
    twilio_sid: Optional[str] = None
    status: str
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    segment_count: int
    created_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    delivered_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ManualSMSRequest(BaseModel):
    """Request model for sending SMS manually from admin UI."""
    phone_number: str
    content: str
    calendar_event_id: Optional[int] = None
    content_type: Optional[str] = "admin"


class SMSCostSummaryResponse(BaseModel):
    """Response model for SMS cost summary."""
    monthly_breakdown: List[dict]
    totals: dict
    phone_number_cost: dict


class SMSCostByStayResponse(BaseModel):
    """Response model for SMS costs by stay."""
    stay_id: int
    guest_name: Optional[str] = None
    checkin: Optional[str] = None
    checkout: Optional[str] = None
    message_count: int
    cost_cents: int
    cost_formatted: str


# =============================================================================
# Template Pydantic Models
# =============================================================================


class SMSTemplateResponse(BaseModel):
    """Response model for SMS templates."""
    id: int
    name: str
    category: str
    subject: Optional[str] = None
    body: str
    variables: Optional[List[str]] = None
    enabled: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class SMSTemplateCreate(BaseModel):
    """Request model for creating SMS template."""
    name: str
    category: str
    subject: Optional[str] = None
    body: str
    variables: Optional[List[str]] = None
    enabled: bool = True


class SMSTemplateUpdate(BaseModel):
    """Request model for updating SMS template."""
    name: Optional[str] = None
    category: Optional[str] = None
    subject: Optional[str] = None
    body: Optional[str] = None
    variables: Optional[List[str]] = None
    enabled: Optional[bool] = None


# =============================================================================
# Scheduled SMS Pydantic Models
# =============================================================================


class ScheduledSMSResponse(BaseModel):
    """Response model for scheduled SMS configurations."""
    id: int
    name: str
    trigger_type: str  # 'before_checkin', 'after_checkin', 'before_checkout', 'time_of_day'
    trigger_offset_hours: int
    trigger_time: Optional[datetime] = None
    template_id: Optional[int] = None
    custom_message: Optional[str] = None
    enabled: bool
    send_to_all_guests: bool
    min_stay_nights: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ScheduledSMSCreate(BaseModel):
    """Request model for creating scheduled SMS configuration."""
    name: str
    trigger_type: str
    trigger_offset_hours: int = 0
    trigger_time: Optional[datetime] = None
    template_id: Optional[int] = None
    custom_message: Optional[str] = None
    enabled: bool = True
    send_to_all_guests: bool = False
    min_stay_nights: int = 0


class ScheduledSMSUpdate(BaseModel):
    """Request model for updating scheduled SMS configuration."""
    name: Optional[str] = None
    trigger_type: Optional[str] = None
    trigger_offset_hours: Optional[int] = None
    trigger_time: Optional[datetime] = None
    template_id: Optional[int] = None
    custom_message: Optional[str] = None
    enabled: Optional[bool] = None
    send_to_all_guests: Optional[bool] = None
    min_stay_nights: Optional[int] = None


# =============================================================================
# Tips Pydantic Models
# =============================================================================


class TipResponse(BaseModel):
    """Response model for tips."""
    id: int
    category: str
    title: str
    content: str
    priority: int
    enabled: bool
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class TipCreate(BaseModel):
    """Request model for creating tip."""
    category: str
    title: str
    content: str
    priority: int = 0
    enabled: bool = True
    start_date: Optional[date] = None
    end_date: Optional[date] = None


class TipUpdate(BaseModel):
    """Request model for updating tip."""
    category: Optional[str] = None
    title: Optional[str] = None
    content: Optional[str] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None


# =============================================================================
# Incoming SMS Pydantic Models
# =============================================================================


class SMSIncomingResponse(BaseModel):
    """Response model for incoming SMS."""
    id: int
    from_number: str
    to_number: str
    body: str
    twilio_sid: Optional[str] = None
    status: str
    processed: bool
    response_sent: bool
    calendar_event_id: Optional[int] = None
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# =============================================================================
# Settings Endpoints
# =============================================================================


@router.get("/settings", response_model=SMSSettingsResponse)
async def get_sms_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get global SMS settings."""
    settings = db.query(SMSSettings).filter(SMSSettings.id == 1).first()

    if not settings:
        # Create default settings if not exists
        settings = SMSSettings(
            id=1,
            enabled=False,
            test_mode=True,
            auto_offer_mode='smart',
            rate_limit_per_minute=10,
            rate_limit_per_stay=50,
        )
        db.add(settings)
        db.commit()
        db.refresh(settings)

    return settings.to_dict()


@router.put("/settings", response_model=SMSSettingsResponse)
async def update_sms_settings(
    settings_update: SMSSettingsUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update global SMS settings."""
    settings = db.query(SMSSettings).filter(SMSSettings.id == 1).first()

    if not settings:
        settings = SMSSettings(id=1)
        db.add(settings)

    update_data = settings_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(settings, field, value)

    db.commit()
    db.refresh(settings)

    # Log the update
    db.add(AuditLog(
        action="update_sms_settings",
        resource_type="sms_settings",
        resource_id=settings.id,
        user_id=current_user.id,
        new_value=update_data,
        success=True,
    ))
    db.commit()

    logger.info("SMS settings updated", user=current_user.username, changes=update_data)

    return settings.to_dict()


# =============================================================================
# Guest Preferences Endpoints
# =============================================================================


@router.get("/preferences/{event_id}", response_model=GuestSMSPreferenceResponse)
async def get_guest_preferences(
    event_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get SMS preferences for a specific guest stay."""
    pref = db.query(GuestSMSPreference).filter(
        GuestSMSPreference.calendar_event_id == event_id
    ).first()

    if not pref:
        # Return defaults if no preferences set
        return {
            "id": 0,
            "calendar_event_id": event_id,
            "sms_enabled": True,
            "dont_ask_again": False,
            "preferred_phone": None,
            "created_at": None,
            "updated_at": None,
        }

    return pref.to_dict()


@router.put("/preferences/{event_id}", response_model=GuestSMSPreferenceResponse)
async def update_guest_preferences(
    event_id: int,
    pref_update: GuestSMSPreferenceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update SMS preferences for a specific guest stay."""
    # Verify event exists
    event = db.query(CalendarEvent).filter(CalendarEvent.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Calendar event not found")

    pref = db.query(GuestSMSPreference).filter(
        GuestSMSPreference.calendar_event_id == event_id
    ).first()

    if not pref:
        pref = GuestSMSPreference(calendar_event_id=event_id)
        db.add(pref)

    update_data = pref_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(pref, field, value)

    db.commit()
    db.refresh(pref)

    logger.info("Guest SMS preferences updated", event_id=event_id, changes=update_data)

    return pref.to_dict()


# =============================================================================
# History Endpoints
# =============================================================================


@router.get("/history", response_model=List[SMSHistoryResponse])
async def get_sms_history(
    event_id: Optional[int] = Query(None, description="Filter by calendar event ID"),
    status: Optional[str] = Query(None, description="Filter by status"),
    content_type: Optional[str] = Query(None, description="Filter by content type"),
    limit: int = Query(50, le=200, description="Maximum records to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get SMS history with optional filtering."""
    query = db.query(SMSHistory)

    if event_id:
        query = query.filter(SMSHistory.calendar_event_id == event_id)
    if status:
        query = query.filter(SMSHistory.status == status)
    if content_type:
        query = query.filter(SMSHistory.content_type == content_type)

    history = query.order_by(desc(SMSHistory.created_at)).offset(offset).limit(limit).all()

    # Enrich with guest names
    results = []
    for entry in history:
        entry_dict = entry.to_dict()
        if entry.calendar_event:
            entry_dict["guest_name"] = entry.calendar_event.guest_name
        results.append(entry_dict)

    return results


@router.get("/history/{history_id}", response_model=SMSHistoryResponse)
async def get_sms_history_entry(
    history_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific SMS history entry."""
    entry = db.query(SMSHistory).filter(SMSHistory.id == history_id).first()

    if not entry:
        raise HTTPException(status_code=404, detail="SMS history entry not found")

    entry_dict = entry.to_dict()
    if entry.calendar_event:
        entry_dict["guest_name"] = entry.calendar_event.guest_name

    return entry_dict


# =============================================================================
# Manual Send Endpoint
# =============================================================================


@router.post("/send", response_model=SMSHistoryResponse)
async def send_sms_manually(
    request: ManualSMSRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Send SMS manually from admin UI."""
    # Get settings
    settings = db.query(SMSSettings).filter(SMSSettings.id == 1).first()

    if not settings or not settings.enabled:
        raise HTTPException(status_code=400, detail="SMS feature is not enabled")

    # Create history entry
    history = SMSHistory(
        calendar_event_id=request.calendar_event_id,
        phone_number=request.phone_number,
        content=request.content,
        content_summary=request.content[:50] + "..." if len(request.content) > 50 else request.content,
        content_type=request.content_type,
        triggered_by="admin",
        status="queued",
    )
    db.add(history)
    db.commit()
    db.refresh(history)

    # In test mode, just mark as "sent" without actually sending
    if settings.test_mode:
        history.status = "sent"
        history.sent_at = datetime.utcnow()
        history.twilio_sid = "test_mode"
        db.commit()
        db.refresh(history)

        logger.info(
            "SMS sent (test mode)",
            phone=request.phone_number,
            content_length=len(request.content),
            user=current_user.username
        )
    else:
        # TODO: Actually send via Twilio
        # For now, mark as queued - actual sending will be handled by SMS service
        logger.info(
            "SMS queued for sending",
            phone=request.phone_number,
            content_length=len(request.content),
            user=current_user.username
        )

    # Log the action
    db.add(AuditLog(
        action="send_sms_manual",
        resource_type="sms_history",
        resource_id=history.id,
        user_id=current_user.id,
        new_value={
            "phone_number": request.phone_number,
            "content_length": len(request.content),
            "test_mode": settings.test_mode,
        },
        success=True,
    ))
    db.commit()

    return history.to_dict()


# =============================================================================
# Cost Tracking Endpoints
# =============================================================================


@router.get("/costs/summary", response_model=SMSCostSummaryResponse)
async def get_sms_cost_summary(
    months: int = Query(12, le=24, description="Number of months to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get SMS cost summary with monthly breakdown."""
    # Get monthly aggregations
    monthly = db.query(SMSCostTracking).filter(
        SMSCostTracking.month.isnot(None)
    ).order_by(desc(SMSCostTracking.month)).limit(months).all()

    monthly_breakdown = [
        {
            "month": m.month.isoformat() if m.month else None,
            "message_count": m.message_count,
            "segment_count": m.segment_count,
            "outgoing": m.outgoing_count,
            "incoming": m.incoming_count,
            "cost_cents": m.estimated_cost_cents,
            "cost_formatted": f"${m.estimated_cost_cents / 100:.2f}",
        }
        for m in monthly
    ]

    # Calculate totals
    total_messages = sum(m.message_count for m in monthly)
    total_cost_cents = sum(m.estimated_cost_cents for m in monthly)

    return {
        "monthly_breakdown": monthly_breakdown,
        "totals": {
            "messages": total_messages,
            "cost_cents": total_cost_cents,
            "cost_formatted": f"${total_cost_cents / 100:.2f}",
        },
        "phone_number_cost": {
            "monthly_cents": 115,  # ~$1.15/month for US number
            "monthly_formatted": "$1.15",
        },
    }


@router.get("/costs/by-stay", response_model=List[SMSCostByStayResponse])
async def get_sms_costs_by_stay(
    limit: int = Query(20, le=100, description="Maximum records to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get SMS costs broken down by guest stay."""
    stay_costs = db.query(SMSCostTracking).filter(
        SMSCostTracking.calendar_event_id.isnot(None)
    ).order_by(desc(SMSCostTracking.updated_at)).limit(limit).all()

    results = []
    for sc in stay_costs:
        event = sc.calendar_event
        results.append({
            "stay_id": sc.calendar_event_id,
            "guest_name": event.guest_name if event else "Unknown",
            "checkin": event.checkin.isoformat() if event and event.checkin else None,
            "checkout": event.checkout.isoformat() if event and event.checkout else None,
            "message_count": sc.message_count,
            "cost_cents": sc.estimated_cost_cents,
            "cost_formatted": f"${sc.estimated_cost_cents / 100:.2f}",
        })

    return results


# =============================================================================
# Internal API Endpoints (for orchestrator)
# =============================================================================


@router.get("/internal/current-preferences")
async def get_current_guest_preferences(
    db: Session = Depends(get_db)
):
    """
    Get SMS preferences for current active guest.

    Used internally by orchestrator - no auth required.
    """
    now = datetime.utcnow()

    # Find active stay
    event = db.query(CalendarEvent).filter(
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.checkin <= now,
        CalendarEvent.checkout >= now,
        CalendarEvent.status == 'confirmed',
    ).first()

    if not event:
        return {
            "has_active_stay": False,
            "preferences": None,
            "guest_phone": None,
        }

    # Get preferences
    pref = db.query(GuestSMSPreference).filter(
        GuestSMSPreference.calendar_event_id == event.id
    ).first()

    return {
        "has_active_stay": True,
        "event_id": event.id,
        "guest_name": event.guest_name,
        "guest_phone": pref.preferred_phone if pref else event.guest_phone,
        "preferences": pref.to_dict() if pref else {
            "sms_enabled": True,
            "dont_ask_again": False,
        },
    }


@router.post("/internal/log-send")
async def log_sms_send(
    phone_number: str,
    content: str,
    status: str,
    twilio_sid: Optional[str] = None,
    calendar_event_id: Optional[int] = None,
    content_type: Optional[str] = None,
    triggered_by: Optional[str] = None,
    segment_count: int = 1,
    error_message: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Log an SMS send from orchestrator.

    Used internally - no auth required.
    """
    history = SMSHistory(
        calendar_event_id=calendar_event_id,
        phone_number=phone_number,
        content=content,
        content_summary=content[:50] + "..." if len(content) > 50 else content,
        content_type=content_type,
        triggered_by=triggered_by,
        twilio_sid=twilio_sid,
        status=status,
        error_message=error_message,
        segment_count=segment_count,
        sent_at=datetime.utcnow() if status in ['sent', 'delivered'] else None,
    )
    db.add(history)
    db.commit()

    # Update cost tracking
    if calendar_event_id:
        _update_cost_tracking(db, calendar_event_id, segment_count, "outgoing")

    # Update monthly tracking
    _update_monthly_cost_tracking(db, segment_count, "outgoing")

    return {"id": history.id, "status": "logged"}


def _update_cost_tracking(
    db: Session,
    calendar_event_id: int,
    segment_count: int,
    direction: str
):
    """Update per-stay cost tracking."""
    tracking = db.query(SMSCostTracking).filter(
        SMSCostTracking.calendar_event_id == calendar_event_id,
        SMSCostTracking.month.is_(None)
    ).first()

    if not tracking:
        tracking = SMSCostTracking(calendar_event_id=calendar_event_id)
        db.add(tracking)

    tracking.message_count += 1
    tracking.segment_count += segment_count

    if direction == "outgoing":
        tracking.outgoing_count += 1
        tracking.outgoing_sms_cents += segment_count  # ~$0.01 per segment
    else:
        tracking.incoming_count += 1
        tracking.incoming_sms_cents += segment_count

    tracking.estimated_cost_cents = tracking.outgoing_sms_cents + tracking.incoming_sms_cents
    db.commit()


def _update_monthly_cost_tracking(
    db: Session,
    segment_count: int,
    direction: str
):
    """Update monthly cost tracking."""
    today = date.today()
    month_start = today.replace(day=1)

    tracking = db.query(SMSCostTracking).filter(
        SMSCostTracking.calendar_event_id.is_(None),
        SMSCostTracking.month == month_start
    ).first()

    if not tracking:
        tracking = SMSCostTracking(month=month_start)
        db.add(tracking)

    tracking.message_count += 1
    tracking.segment_count += segment_count

    if direction == "outgoing":
        tracking.outgoing_count += 1
        tracking.outgoing_sms_cents += segment_count
    else:
        tracking.incoming_count += 1
        tracking.incoming_sms_cents += segment_count

    tracking.estimated_cost_cents = tracking.outgoing_sms_cents + tracking.incoming_sms_cents
    db.commit()


# =============================================================================
# Template Endpoints
# =============================================================================


@router.get("/templates", response_model=List[SMSTemplateResponse])
async def get_sms_templates(
    category: Optional[str] = Query(None, description="Filter by category"),
    enabled_only: bool = Query(False, description="Only return enabled templates"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all SMS templates."""
    query = db.query(SMSTemplate)

    if category:
        query = query.filter(SMSTemplate.category == category)
    if enabled_only:
        query = query.filter(SMSTemplate.enabled == True)

    templates = query.order_by(SMSTemplate.category, SMSTemplate.name).all()

    return [t.to_dict() for t in templates]


@router.get("/templates/{template_id}", response_model=SMSTemplateResponse)
async def get_sms_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific SMS template."""
    template = db.query(SMSTemplate).filter(SMSTemplate.id == template_id).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    return template.to_dict()


@router.post("/templates", response_model=SMSTemplateResponse)
async def create_sms_template(
    template: SMSTemplateCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new SMS template."""
    # Check for duplicate name
    existing = db.query(SMSTemplate).filter(SMSTemplate.name == template.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="Template with this name already exists")

    new_template = SMSTemplate(
        name=template.name,
        category=template.category,
        subject=template.subject,
        body=template.body,
        variables=template.variables,
        enabled=template.enabled,
    )
    db.add(new_template)
    db.commit()
    db.refresh(new_template)

    # Log the action
    db.add(AuditLog(
        action="create_sms_template",
        resource_type="sms_template",
        resource_id=new_template.id,
        user_id=current_user.id,
        new_value={"name": template.name, "category": template.category},
        success=True,
    ))
    db.commit()

    logger.info("SMS template created", template_id=new_template.id, name=template.name)

    return new_template.to_dict()


@router.patch("/templates/{template_id}", response_model=SMSTemplateResponse)
async def update_sms_template(
    template_id: int,
    template_update: SMSTemplateUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an SMS template."""
    template = db.query(SMSTemplate).filter(SMSTemplate.id == template_id).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    update_data = template_update.model_dump(exclude_unset=True)

    # Check for duplicate name if name is being changed
    if "name" in update_data and update_data["name"] != template.name:
        existing = db.query(SMSTemplate).filter(SMSTemplate.name == update_data["name"]).first()
        if existing:
            raise HTTPException(status_code=400, detail="Template with this name already exists")

    for field, value in update_data.items():
        setattr(template, field, value)

    db.commit()
    db.refresh(template)

    # Log the action
    db.add(AuditLog(
        action="update_sms_template",
        resource_type="sms_template",
        resource_id=template.id,
        user_id=current_user.id,
        new_value=update_data,
        success=True,
    ))
    db.commit()

    logger.info("SMS template updated", template_id=template.id, changes=update_data)

    return template.to_dict()


@router.delete("/templates/{template_id}")
async def delete_sms_template(
    template_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an SMS template."""
    template = db.query(SMSTemplate).filter(SMSTemplate.id == template_id).first()

    if not template:
        raise HTTPException(status_code=404, detail="Template not found")

    template_name = template.name
    db.delete(template)
    db.commit()

    # Log the action
    db.add(AuditLog(
        action="delete_sms_template",
        resource_type="sms_template",
        resource_id=template_id,
        user_id=current_user.id,
        old_value={"name": template_name},
        success=True,
    ))
    db.commit()

    logger.info("SMS template deleted", template_id=template_id, name=template_name)

    return {"status": "deleted", "id": template_id}


# =============================================================================
# Scheduled SMS Endpoints
# =============================================================================


@router.get("/scheduled", response_model=List[ScheduledSMSResponse])
async def get_scheduled_sms(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, le=200, description="Maximum records to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all scheduled SMS messages."""
    query = db.query(ScheduledSMS)

    if status:
        # Status parameter filters by enabled state (true/false)
        if status.lower() in ('enabled', 'true', '1'):
            query = query.filter(ScheduledSMS.enabled == True)
        elif status.lower() in ('disabled', 'false', '0'):
            query = query.filter(ScheduledSMS.enabled == False)

    scheduled = query.order_by(desc(ScheduledSMS.created_at)).limit(limit).all()

    return [s.to_dict() for s in scheduled]


@router.get("/scheduled/{scheduled_id}", response_model=ScheduledSMSResponse)
async def get_scheduled_sms_by_id(
    scheduled_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific scheduled SMS."""
    scheduled = db.query(ScheduledSMS).filter(ScheduledSMS.id == scheduled_id).first()

    if not scheduled:
        raise HTTPException(status_code=404, detail="Scheduled SMS not found")

    return scheduled.to_dict()


@router.post("/scheduled", response_model=ScheduledSMSResponse)
async def create_scheduled_sms(
    scheduled: ScheduledSMSCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new scheduled SMS configuration."""
    new_scheduled = ScheduledSMS(
        name=scheduled.name,
        trigger_type=scheduled.trigger_type,
        trigger_offset_hours=scheduled.trigger_offset_hours,
        trigger_time=scheduled.trigger_time,
        template_id=scheduled.template_id,
        custom_message=scheduled.custom_message,
        enabled=scheduled.enabled,
        send_to_all_guests=scheduled.send_to_all_guests,
        min_stay_nights=scheduled.min_stay_nights,
    )
    db.add(new_scheduled)
    db.commit()
    db.refresh(new_scheduled)

    # Log the action
    db.add(AuditLog(
        action="create_scheduled_sms",
        resource_type="scheduled_sms",
        resource_id=new_scheduled.id,
        user_id=current_user.id,
        new_value={"name": scheduled.name, "trigger_type": scheduled.trigger_type},
        success=True,
    ))
    db.commit()

    logger.info("Scheduled SMS configuration created", scheduled_id=new_scheduled.id)

    return new_scheduled.to_dict()


@router.patch("/scheduled/{scheduled_id}", response_model=ScheduledSMSResponse)
async def update_scheduled_sms(
    scheduled_id: int,
    scheduled_update: ScheduledSMSUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a scheduled SMS."""
    scheduled = db.query(ScheduledSMS).filter(ScheduledSMS.id == scheduled_id).first()

    if not scheduled:
        raise HTTPException(status_code=404, detail="Scheduled SMS not found")

    if scheduled.status == "sent":
        raise HTTPException(status_code=400, detail="Cannot update sent SMS")

    update_data = scheduled_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(scheduled, field, value)

    db.commit()
    db.refresh(scheduled)

    logger.info("Scheduled SMS updated", scheduled_id=scheduled.id, changes=update_data)

    return scheduled.to_dict()


@router.delete("/scheduled/{scheduled_id}")
async def delete_scheduled_sms(
    scheduled_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a scheduled SMS."""
    scheduled = db.query(ScheduledSMS).filter(ScheduledSMS.id == scheduled_id).first()

    if not scheduled:
        raise HTTPException(status_code=404, detail="Scheduled SMS not found")

    if scheduled.status == "sent":
        raise HTTPException(status_code=400, detail="Cannot delete sent SMS")

    db.delete(scheduled)
    db.commit()

    logger.info("Scheduled SMS deleted", scheduled_id=scheduled_id)

    return {"status": "deleted", "id": scheduled_id}


# =============================================================================
# Incoming SMS Endpoints
# =============================================================================


@router.get("/incoming", response_model=List[SMSIncomingResponse])
async def get_incoming_sms(
    processed: Optional[bool] = Query(None, description="Filter by processed status"),
    limit: int = Query(50, le=200, description="Maximum records to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get incoming SMS messages."""
    query = db.query(SMSIncoming)

    if processed is not None:
        query = query.filter(SMSIncoming.processed == processed)

    incoming = query.order_by(desc(SMSIncoming.received_at)).limit(limit).all()

    return [i.to_dict() for i in incoming]


@router.get("/incoming/{incoming_id}", response_model=SMSIncomingResponse)
async def get_incoming_sms_by_id(
    incoming_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific incoming SMS."""
    incoming = db.query(SMSIncoming).filter(SMSIncoming.id == incoming_id).first()

    if not incoming:
        raise HTTPException(status_code=404, detail="Incoming SMS not found")

    return incoming.to_dict()


# =============================================================================
# Tips Endpoints (on /api prefix, not /api/sms)
# =============================================================================


@tips_router.get("/tips", response_model=List[TipResponse])
async def get_tips(
    category: Optional[str] = Query(None, description="Filter by category"),
    enabled_only: bool = Query(False, description="Only return enabled tips"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all tips."""
    query = db.query(TipPrompt)

    if category:
        query = query.filter(TipPrompt.tip_type == category)
    if enabled_only:
        query = query.filter(TipPrompt.enabled == True)

    tips = query.order_by(TipPrompt.priority.desc(), TipPrompt.tip_type).all()

    return [t.to_dict() for t in tips]


@tips_router.get("/tips/{tip_id}", response_model=TipResponse)
async def get_tip(
    tip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific tip."""
    tip = db.query(TipPrompt).filter(TipPrompt.id == tip_id).first()

    if not tip:
        raise HTTPException(status_code=404, detail="Tip not found")

    return tip.to_dict()


@tips_router.post("/tips", response_model=TipResponse)
async def create_tip(
    tip: TipCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new tip."""
    new_tip = TipPrompt(
        tip_type=tip.category,  # Map API's category to DB's tip_type
        title=tip.title,
        message=tip.content,  # Map API's content to DB's message
        priority=tip.priority,
        enabled=tip.enabled,
    )
    db.add(new_tip)
    db.commit()
    db.refresh(new_tip)

    # Log the action
    db.add(AuditLog(
        action="create_tip",
        resource_type="tip_prompt",
        resource_id=new_tip.id,
        user_id=current_user.id,
        new_value={"title": tip.title, "category": tip.category},
        success=True,
    ))
    db.commit()

    logger.info("Tip created", tip_id=new_tip.id, title=tip.title)

    return new_tip.to_dict()


@tips_router.patch("/tips/{tip_id}", response_model=TipResponse)
async def update_tip(
    tip_id: int,
    tip_update: TipUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a tip."""
    tip = db.query(TipPrompt).filter(TipPrompt.id == tip_id).first()

    if not tip:
        raise HTTPException(status_code=404, detail="Tip not found")

    update_data = tip_update.model_dump(exclude_unset=True)
    # Map API field names to DB column names
    field_mapping = {'category': 'tip_type', 'content': 'message'}
    for field, value in update_data.items():
        db_field = field_mapping.get(field, field)
        setattr(tip, db_field, value)

    db.commit()
    db.refresh(tip)

    # Log the action
    db.add(AuditLog(
        action="update_tip",
        resource_type="tip_prompt",
        resource_id=tip.id,
        user_id=current_user.id,
        new_value=update_data,
        success=True,
    ))
    db.commit()

    logger.info("Tip updated", tip_id=tip.id, changes=update_data)

    return tip.to_dict()


@tips_router.delete("/tips/{tip_id}")
async def delete_tip(
    tip_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a tip."""
    tip = db.query(TipPrompt).filter(TipPrompt.id == tip_id).first()

    if not tip:
        raise HTTPException(status_code=404, detail="Tip not found")

    tip_title = tip.title
    db.delete(tip)
    db.commit()

    # Log the action
    db.add(AuditLog(
        action="delete_tip",
        resource_type="tip_prompt",
        resource_id=tip_id,
        user_id=current_user.id,
        old_value={"title": tip_title},
        success=True,
    ))
    db.commit()

    logger.info("Tip deleted", tip_id=tip_id, title=tip_title)

    return {"status": "deleted", "id": tip_id}
