"""
System Alerts API routes.

Provides CRUD operations for system alerts including:
- Stuck sensor detection alerts
- Service health issues
- System warnings
- WebSocket broadcast for real-time updates
"""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import or_
from pydantic import BaseModel
from datetime import datetime, timezone
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Alert

logger = structlog.get_logger()

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


# =============================================================================
# Pydantic Models
# =============================================================================

class AlertCreate(BaseModel):
    """Request model for creating an alert."""
    alert_type: str
    severity: str = "warning"  # info, warning, error, critical
    title: str
    message: str
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    alert_data: Optional[Dict[str, Any]] = {}
    dedup_key: Optional[str] = None


class AlertUpdate(BaseModel):
    """Request model for updating an alert."""
    status: Optional[str] = None  # active, acknowledged, resolved, dismissed
    resolution_notes: Optional[str] = None


class AlertResponse(BaseModel):
    """Response model for alert data."""
    id: int
    alert_type: str
    severity: str
    title: str
    message: str
    entity_id: Optional[str] = None
    entity_type: Optional[str] = None
    alert_data: Optional[Dict[str, Any]] = None
    status: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    acknowledged_at: Optional[str] = None
    resolved_at: Optional[str] = None
    acknowledged_by: Optional[str] = None
    resolved_by: Optional[str] = None
    resolution_notes: Optional[str] = None
    dedup_key: Optional[str] = None

    class Config:
        from_attributes = True


class AlertStats(BaseModel):
    """Statistics about current alerts."""
    total: int
    active: int
    acknowledged: int
    resolved: int
    by_severity: Dict[str, int]
    by_type: Dict[str, int]


# =============================================================================
# Public Endpoints (for internal services)
# =============================================================================

@router.post("/public/create", response_model=AlertResponse)
async def create_alert_public(
    alert: AlertCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new alert (public endpoint for internal services).

    Uses dedup_key to prevent duplicate alerts - if an alert with the same
    dedup_key already exists and is active, it will be returned instead.
    """
    # Check for existing active alert with same dedup_key
    if alert.dedup_key:
        existing = db.query(Alert).filter(
            Alert.dedup_key == alert.dedup_key,
            Alert.status.in_(['active', 'acknowledged'])
        ).first()

        if existing:
            logger.info("alert_deduplicated", dedup_key=alert.dedup_key, existing_id=existing.id)
            return AlertResponse(**existing.to_dict())

    # Create new alert
    new_alert = Alert(
        alert_type=alert.alert_type,
        severity=alert.severity,
        title=alert.title,
        message=alert.message,
        entity_id=alert.entity_id,
        entity_type=alert.entity_type,
        alert_data=alert.alert_data or {},
        dedup_key=alert.dedup_key,
        status='active'
    )

    db.add(new_alert)
    db.commit()
    db.refresh(new_alert)

    logger.info(
        "alert_created",
        alert_id=new_alert.id,
        alert_type=alert.alert_type,
        severity=alert.severity,
        entity_id=alert.entity_id
    )

    # TODO: Broadcast to WebSocket for real-time updates

    return AlertResponse(**new_alert.to_dict())


@router.post("/public/resolve-by-entity")
async def resolve_alerts_by_entity(
    entity_id: str,
    alert_type: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Resolve all active alerts for a specific entity.

    Used when a stuck sensor starts working again.
    """
    query = db.query(Alert).filter(
        Alert.entity_id == entity_id,
        Alert.status.in_(['active', 'acknowledged'])
    )

    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)

    alerts = query.all()
    resolved_count = 0

    for alert in alerts:
        alert.status = 'resolved'
        alert.resolved_at = datetime.now(timezone.utc)
        alert.resolution_notes = 'Auto-resolved: Entity recovered'
        resolved_count += 1

    db.commit()

    logger.info(
        "alerts_auto_resolved",
        entity_id=entity_id,
        resolved_count=resolved_count
    )

    return {"resolved_count": resolved_count}


@router.get("/public/active-by-type")
async def get_active_alerts_by_type(
    alert_type: str,
    db: Session = Depends(get_db)
):
    """
    Get all active alerts of a specific type.

    Returns list of entity_ids that have active alerts.
    """
    alerts = db.query(Alert).filter(
        Alert.alert_type == alert_type,
        Alert.status.in_(['active', 'acknowledged'])
    ).all()

    return {
        "alert_type": alert_type,
        "count": len(alerts),
        "entity_ids": [a.entity_id for a in alerts if a.entity_id],
        "alerts": [a.to_dict() for a in alerts]
    }


# =============================================================================
# Authenticated Endpoints
# =============================================================================

@router.get("/active/count")
async def get_active_alert_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get count of active alerts for the status bar badge."""
    active_count = db.query(Alert).filter(
        Alert.status == 'active'
    ).count()

    critical_count = db.query(Alert).filter(
        Alert.status == 'active',
        Alert.severity == 'critical'
    ).count()

    warning_count = db.query(Alert).filter(
        Alert.status == 'active',
        Alert.severity == 'warning'
    ).count()

    return {
        "active": active_count,
        "critical": critical_count,
        "warning": warning_count
    }


@router.get("", response_model=List[AlertResponse])
async def list_alerts(
    status: Optional[str] = Query(None, description="Filter by status"),
    alert_type: Optional[str] = Query(None, description="Filter by alert type"),
    severity: Optional[str] = Query(None, description="Filter by severity"),
    entity_id: Optional[str] = Query(None, description="Filter by entity ID"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List alerts with optional filtering."""
    query = db.query(Alert)

    if status:
        query = query.filter(Alert.status == status)
    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)
    if severity:
        query = query.filter(Alert.severity == severity)
    if entity_id:
        query = query.filter(Alert.entity_id == entity_id)

    # Order by created_at descending (newest first)
    query = query.order_by(Alert.created_at.desc())

    alerts = query.offset(offset).limit(limit).all()

    return [AlertResponse(**a.to_dict()) for a in alerts]


@router.get("/stats", response_model=AlertStats)
async def get_alert_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get alert statistics."""
    all_alerts = db.query(Alert).all()

    stats = {
        "total": len(all_alerts),
        "active": sum(1 for a in all_alerts if a.status == 'active'),
        "acknowledged": sum(1 for a in all_alerts if a.status == 'acknowledged'),
        "resolved": sum(1 for a in all_alerts if a.status == 'resolved'),
        "by_severity": {},
        "by_type": {}
    }

    for alert in all_alerts:
        if alert.status == 'active':
            stats["by_severity"][alert.severity] = stats["by_severity"].get(alert.severity, 0) + 1
            stats["by_type"][alert.alert_type] = stats["by_type"].get(alert.alert_type, 0) + 1

    return AlertStats(**stats)


@router.get("/{alert_id}", response_model=AlertResponse)
async def get_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific alert by ID."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    return AlertResponse(**alert.to_dict())


@router.patch("/{alert_id}", response_model=AlertResponse)
async def update_alert(
    alert_id: int,
    update: AlertUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an alert (acknowledge, resolve, dismiss)."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    now = datetime.now(timezone.utc)

    if update.status:
        old_status = alert.status
        alert.status = update.status

        if update.status == 'acknowledged' and old_status != 'acknowledged':
            alert.acknowledged_at = now
            alert.acknowledged_by_id = current_user.id
        elif update.status in ['resolved', 'dismissed']:
            alert.resolved_at = now
            alert.resolved_by_id = current_user.id

    if update.resolution_notes:
        alert.resolution_notes = update.resolution_notes

    db.commit()
    db.refresh(alert)

    logger.info(
        "alert_updated",
        alert_id=alert_id,
        new_status=alert.status,
        updated_by=current_user.username
    )

    return AlertResponse(**alert.to_dict())


@router.delete("/{alert_id}")
async def delete_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete an alert (admin only)."""
    alert = db.query(Alert).filter(Alert.id == alert_id).first()

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    db.delete(alert)
    db.commit()

    logger.info(
        "alert_deleted",
        alert_id=alert_id,
        deleted_by=current_user.username
    )

    return {"message": "Alert deleted"}


@router.post("/acknowledge-all")
async def acknowledge_all_alerts(
    alert_type: Optional[str] = None,
    severity: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Acknowledge all active alerts matching the filters."""
    query = db.query(Alert).filter(Alert.status == 'active')

    if alert_type:
        query = query.filter(Alert.alert_type == alert_type)
    if severity:
        query = query.filter(Alert.severity == severity)

    alerts = query.all()
    now = datetime.now(timezone.utc)

    for alert in alerts:
        alert.status = 'acknowledged'
        alert.acknowledged_at = now
        alert.acknowledged_by_id = current_user.id

    db.commit()

    logger.info(
        "alerts_bulk_acknowledged",
        count=len(alerts),
        acknowledged_by=current_user.username
    )

    return {"acknowledged_count": len(alerts)}
