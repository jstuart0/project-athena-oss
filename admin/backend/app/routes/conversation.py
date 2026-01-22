"""
Conversation context and clarification API routes.

Provides configuration management for conversation context tracking,
clarifying questions, and disambiguation rules.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from sqlalchemy import func, text
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import (
    User, AuditLog,
    ConversationSettings, ClarificationSettings, ClarificationType,
    SportsTeamDisambiguation, DeviceDisambiguationRule, ConversationAnalytics
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/conversation", tags=["conversation"])


# ============================================================================
# Pydantic Models
# ============================================================================

class ConversationSettingsUpdate(BaseModel):
    """Request model for updating conversation settings."""
    enabled: Optional[bool] = None
    use_context: Optional[bool] = None
    max_messages: Optional[int] = Field(None, ge=1, le=100)
    timeout_seconds: Optional[int] = Field(None, ge=60, le=7200)
    cleanup_interval_seconds: Optional[int] = Field(None, ge=10, le=600)
    session_ttl_seconds: Optional[int] = Field(None, ge=300, le=86400)
    max_llm_history_messages: Optional[int] = Field(None, ge=2, le=50)
    history_mode: Optional[str] = Field(None, pattern='^(none|summarized|full)$')


class ConversationSettingsResponse(BaseModel):
    """Response model for conversation settings."""
    id: int
    enabled: bool
    use_context: bool
    max_messages: int
    timeout_seconds: int
    cleanup_interval_seconds: int
    session_ttl_seconds: int
    max_llm_history_messages: int
    history_mode: str
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class ClarificationSettingsUpdate(BaseModel):
    """Request model for updating clarification settings."""
    enabled: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(None, ge=30, le=600)


class ClarificationSettingsResponse(BaseModel):
    """Response model for clarification settings."""
    id: int
    enabled: bool
    timeout_seconds: int
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class ClarificationTypeUpdate(BaseModel):
    """Request model for updating a clarification type."""
    enabled: Optional[bool] = None
    timeout_seconds: Optional[int] = Field(None, ge=30, le=600)
    priority: Optional[int] = Field(None, ge=0, le=1000)
    description: Optional[str] = None


class ClarificationTypeResponse(BaseModel):
    """Response model for clarification type."""
    id: int
    type: str
    enabled: bool
    timeout_seconds: Optional[int]
    priority: int
    description: Optional[str]
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class SportsTeamOption(BaseModel):
    """Sports team option within disambiguation data."""
    id: str
    label: str
    sport: str


class SportsTeamCreate(BaseModel):
    """Request model for creating a sports team disambiguation rule."""
    team_name: str = Field(..., min_length=1, max_length=100)
    requires_disambiguation: bool = True
    options: List[dict]


class SportsTeamUpdate(BaseModel):
    """Request model for updating a sports team disambiguation rule."""
    requires_disambiguation: Optional[bool] = None
    options: Optional[List[dict]] = None


class SportsTeamResponse(BaseModel):
    """Response model for sports team disambiguation."""
    id: int
    team_name: str
    requires_disambiguation: bool
    options: List[dict]
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class DeviceRuleUpdate(BaseModel):
    """Request model for updating a device disambiguation rule."""
    requires_disambiguation: Optional[bool] = None
    min_entities_for_clarification: Optional[int] = Field(None, ge=1, le=20)
    include_all_option: Optional[bool] = None


class DeviceRuleResponse(BaseModel):
    """Response model for device disambiguation rule."""
    id: int
    device_type: str
    requires_disambiguation: bool
    min_entities_for_clarification: int
    include_all_option: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


class AnalyticsResponse(BaseModel):
    """Response model for conversation analytics."""
    id: int
    session_id: str
    event_type: str
    metadata: Optional[dict]
    timestamp: str

    class Config:
        from_attributes = True


class AnalyticsSummaryResponse(BaseModel):
    """Response model for analytics summary."""
    total_events: int
    events_by_type: dict
    recent_sessions: int
    avg_session_length: Optional[float]


# ============================================================================
# Helper Functions
# ============================================================================

def create_audit_log(
    db: Session,
    user: User,
    action: str,
    resource_type: str,
    resource_id: int,
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


# ============================================================================
# Conversation Settings Endpoints
# ============================================================================

@router.get("/settings", response_model=ConversationSettingsResponse)
async def get_conversation_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get conversation context settings."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    settings = db.query(ConversationSettings).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")

    return settings.to_dict()


@router.put("/settings", response_model=ConversationSettingsResponse)
async def update_conversation_settings(
    settings_data: ConversationSettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update conversation context settings."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    settings = db.query(ConversationSettings).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")

    # Store old values for audit
    old_value = settings.to_dict()

    # Update fields
    if settings_data.enabled is not None:
        settings.enabled = settings_data.enabled
    if settings_data.use_context is not None:
        settings.use_context = settings_data.use_context
    if settings_data.max_messages is not None:
        settings.max_messages = settings_data.max_messages
    if settings_data.timeout_seconds is not None:
        settings.timeout_seconds = settings_data.timeout_seconds
    if settings_data.cleanup_interval_seconds is not None:
        settings.cleanup_interval_seconds = settings_data.cleanup_interval_seconds
    if settings_data.session_ttl_seconds is not None:
        settings.session_ttl_seconds = settings_data.session_ttl_seconds
    if settings_data.max_llm_history_messages is not None:
        settings.max_llm_history_messages = settings_data.max_llm_history_messages
    if settings_data.history_mode is not None:
        settings.history_mode = settings_data.history_mode

    db.commit()
    db.refresh(settings)

    # Audit log
    create_audit_log(
        db, current_user, 'update', 'conversation_settings', settings.id,
        old_value=old_value, new_value=settings.to_dict(), request=request
    )

    logger.info("conversation_settings_updated", user=current_user.username)

    return settings.to_dict()


# ============================================================================
# Clarification Settings Endpoints
# ============================================================================

@router.get("/clarification", response_model=ClarificationSettingsResponse)
async def get_clarification_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get global clarification settings."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    settings = db.query(ClarificationSettings).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")

    return settings.to_dict()


@router.put("/clarification", response_model=ClarificationSettingsResponse)
async def update_clarification_settings(
    settings_data: ClarificationSettingsUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update global clarification settings."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    settings = db.query(ClarificationSettings).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Settings not found")

    # Store old values for audit
    old_value = settings.to_dict()

    # Update fields
    if settings_data.enabled is not None:
        settings.enabled = settings_data.enabled
    if settings_data.timeout_seconds is not None:
        settings.timeout_seconds = settings_data.timeout_seconds

    db.commit()
    db.refresh(settings)

    # Audit log
    create_audit_log(
        db, current_user, 'update', 'clarification_settings', settings.id,
        old_value=old_value, new_value=settings.to_dict(), request=request
    )

    logger.info("clarification_settings_updated", user=current_user.username)

    return settings.to_dict()


# ============================================================================
# Clarification Types Endpoints
# ============================================================================

@router.get("/clarification/types", response_model=List[ClarificationTypeResponse])
async def list_clarification_types(
    enabled: Optional[bool] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all clarification types."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(ClarificationType)

    if enabled is not None:
        query = query.filter(ClarificationType.enabled == enabled)

    types = query.order_by(ClarificationType.priority.desc()).all()

    return [t.to_dict() for t in types]


@router.get("/clarification/types/{type_name}", response_model=ClarificationTypeResponse)
async def get_clarification_type(
    type_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific clarification type by name."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    clar_type = db.query(ClarificationType).filter(ClarificationType.type == type_name).first()
    if not clar_type:
        raise HTTPException(status_code=404, detail="Clarification type not found")

    return clar_type.to_dict()


@router.put("/clarification/types/{type_name}", response_model=ClarificationTypeResponse)
async def update_clarification_type(
    type_name: str,
    type_data: ClarificationTypeUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a clarification type configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    clar_type = db.query(ClarificationType).filter(ClarificationType.type == type_name).first()
    if not clar_type:
        raise HTTPException(status_code=404, detail="Clarification type not found")

    # Store old values for audit
    old_value = clar_type.to_dict()

    # Update fields
    if type_data.enabled is not None:
        clar_type.enabled = type_data.enabled
    if type_data.timeout_seconds is not None:
        clar_type.timeout_seconds = type_data.timeout_seconds
    if type_data.priority is not None:
        clar_type.priority = type_data.priority
    if type_data.description is not None:
        clar_type.description = type_data.description

    db.commit()
    db.refresh(clar_type)

    # Audit log
    create_audit_log(
        db, current_user, 'update', 'clarification_type', clar_type.id,
        old_value=old_value, new_value=clar_type.to_dict(), request=request
    )

    logger.info("clarification_type_updated", type=type_name, user=current_user.username)

    return clar_type.to_dict()


# ============================================================================
# Sports Team Disambiguation Endpoints
# ============================================================================

@router.get("/sports-teams", response_model=List[SportsTeamResponse])
async def list_sports_teams(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all sports team disambiguation rules."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    teams = db.query(SportsTeamDisambiguation).order_by(SportsTeamDisambiguation.team_name).all()

    return [team.to_dict() for team in teams]


@router.post("/sports-teams", response_model=SportsTeamResponse, status_code=201)
async def create_sports_team(
    team_data: SportsTeamCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new sports team disambiguation rule."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if team already exists
    existing = db.query(SportsTeamDisambiguation).filter(
        SportsTeamDisambiguation.team_name == team_data.team_name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"Team '{team_data.team_name}' already exists")

    # Validate options format
    if not team_data.options or len(team_data.options) < 2:
        raise HTTPException(status_code=400, detail="At least 2 options required for disambiguation")

    team = SportsTeamDisambiguation(
        team_name=team_data.team_name,
        requires_disambiguation=team_data.requires_disambiguation,
        options=team_data.options
    )
    db.add(team)
    db.commit()
    db.refresh(team)

    # Audit log
    create_audit_log(
        db, current_user, 'create', 'sports_team_disambiguation', team.id,
        new_value={'team_name': team.team_name, 'options': team.options}, request=request
    )

    logger.info("sports_team_created", team_name=team.team_name, user=current_user.username)

    return team.to_dict()


@router.put("/sports-teams/{team_id}", response_model=SportsTeamResponse)
async def update_sports_team(
    team_id: int,
    team_data: SportsTeamUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a sports team disambiguation rule."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    team = db.query(SportsTeamDisambiguation).filter(SportsTeamDisambiguation.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Sports team not found")

    # Store old values for audit
    old_value = team.to_dict()

    # Update fields
    if team_data.requires_disambiguation is not None:
        team.requires_disambiguation = team_data.requires_disambiguation
    if team_data.options is not None:
        if len(team_data.options) < 2:
            raise HTTPException(status_code=400, detail="At least 2 options required for disambiguation")
        team.options = team_data.options

    db.commit()
    db.refresh(team)

    # Audit log
    create_audit_log(
        db, current_user, 'update', 'sports_team_disambiguation', team.id,
        old_value=old_value, new_value=team.to_dict(), request=request
    )

    logger.info("sports_team_updated", team_id=team_id, team_name=team.team_name, user=current_user.username)

    return team.to_dict()


@router.delete("/sports-teams/{team_id}", status_code=204)
async def delete_sports_team(
    team_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a sports team disambiguation rule."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    team = db.query(SportsTeamDisambiguation).filter(SportsTeamDisambiguation.id == team_id).first()
    if not team:
        raise HTTPException(status_code=404, detail="Sports team not found")

    team_name = team.team_name

    # Audit log before deletion
    create_audit_log(
        db, current_user, 'delete', 'sports_team_disambiguation', team.id,
        old_value={'team_name': team.team_name}, request=request
    )

    # Delete
    db.delete(team)
    db.commit()

    logger.info("sports_team_deleted", team_id=team_id, team_name=team_name, user=current_user.username)

    return None


# ============================================================================
# Device Disambiguation Rules Endpoints
# ============================================================================

@router.get("/device-rules", response_model=List[DeviceRuleResponse])
async def list_device_rules(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all device disambiguation rules."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    rules = db.query(DeviceDisambiguationRule).order_by(DeviceDisambiguationRule.device_type).all()

    return [rule.to_dict() for rule in rules]


@router.get("/device-rules/{device_type}", response_model=DeviceRuleResponse)
async def get_device_rule(
    device_type: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get device disambiguation rule for a specific device type."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    rule = db.query(DeviceDisambiguationRule).filter(
        DeviceDisambiguationRule.device_type == device_type
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Device rule not found")

    return rule.to_dict()


@router.put("/device-rules/{device_type}", response_model=DeviceRuleResponse)
async def update_device_rule(
    device_type: str,
    rule_data: DeviceRuleUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update device disambiguation rule."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    rule = db.query(DeviceDisambiguationRule).filter(
        DeviceDisambiguationRule.device_type == device_type
    ).first()
    if not rule:
        raise HTTPException(status_code=404, detail="Device rule not found")

    # Store old values for audit
    old_value = rule.to_dict()

    # Update fields
    if rule_data.requires_disambiguation is not None:
        rule.requires_disambiguation = rule_data.requires_disambiguation
    if rule_data.min_entities_for_clarification is not None:
        rule.min_entities_for_clarification = rule_data.min_entities_for_clarification
    if rule_data.include_all_option is not None:
        rule.include_all_option = rule_data.include_all_option

    db.commit()
    db.refresh(rule)

    # Audit log
    create_audit_log(
        db, current_user, 'update', 'device_disambiguation_rule', rule.id,
        old_value=old_value, new_value=rule.to_dict(), request=request
    )

    logger.info("device_rule_updated", device_type=device_type, user=current_user.username)

    return rule.to_dict()


# ============================================================================
# Analytics Endpoints
# ============================================================================

@router.get("/analytics", response_model=List[AnalyticsResponse])
async def get_analytics(
    event_type: Optional[str] = None,
    session_id: Optional[str] = None,
    hours: int = 24,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get conversation analytics events."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Calculate time threshold
    time_threshold = datetime.utcnow() - timedelta(hours=hours)

    query = db.query(ConversationAnalytics).filter(
        ConversationAnalytics.timestamp >= time_threshold
    )

    if event_type:
        query = query.filter(ConversationAnalytics.event_type == event_type)
    if session_id:
        query = query.filter(ConversationAnalytics.session_id == session_id)

    events = query.order_by(ConversationAnalytics.timestamp.desc()).limit(limit).all()

    return [event.to_dict() for event in events]


@router.get("/analytics/summary", response_model=AnalyticsSummaryResponse)
async def get_analytics_summary(
    hours: int = 24,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get conversation analytics summary."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Calculate time threshold
    time_threshold = datetime.utcnow() - timedelta(hours=hours)

    # Total events
    total_events = db.query(ConversationAnalytics).filter(
        ConversationAnalytics.timestamp >= time_threshold
    ).count()

    # Events by type
    events_by_type_query = db.query(
        ConversationAnalytics.event_type,
        func.count(ConversationAnalytics.id).label('count')
    ).filter(
        ConversationAnalytics.timestamp >= time_threshold
    ).group_by(ConversationAnalytics.event_type).all()

    events_by_type = {row[0]: row[1] for row in events_by_type_query}

    # Recent sessions count
    recent_sessions = db.query(ConversationAnalytics.session_id).filter(
        ConversationAnalytics.timestamp >= time_threshold
    ).distinct().count()

    # Calculate average session length (in seconds) from min/max timestamps per session
    # Sessions with only one event will have 0 duration
    session_length_query = text("""
        SELECT AVG(session_duration) as avg_duration
        FROM (
            SELECT
                session_id,
                EXTRACT(EPOCH FROM (MAX(timestamp) - MIN(timestamp))) as session_duration
            FROM conversation_analytics
            WHERE timestamp >= :time_threshold
            GROUP BY session_id
            HAVING COUNT(*) > 1
        ) session_durations
    """)

    try:
        result = db.execute(session_length_query, {"time_threshold": time_threshold}).fetchone()
        avg_session_length = round(result.avg_duration, 2) if result and result.avg_duration else None
    except Exception as e:
        logger.warning("session_length_calculation_failed", error=str(e))
        avg_session_length = None

    return {
        'total_events': total_events,
        'events_by_type': events_by_type,
        'recent_sessions': recent_sessions,
        'avg_session_length': avg_session_length  # Average session duration in seconds
    }
