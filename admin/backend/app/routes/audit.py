"""
Audit log API routes.

Provides read-only access to audit logs for compliance and security monitoring.
Includes undo functionality for reversible operations.
"""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel
from datetime import datetime, timedelta
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, AuditLog, Feature

logger = structlog.get_logger()

router = APIRouter(prefix="/api/audit", tags=["audit"])

# Actions that can be undone (have reversible old_value)
REVERSIBLE_ACTIONS = {'toggle', 'update'}
# Actions that cannot be undone
IRREVERSIBLE_ACTIONS = {'create', 'delete', 'restart', 'start', 'stop'}


def is_action_reversible(action: str, old_value: Optional[Dict]) -> bool:
    """Determine if an audit action can be undone."""
    if action not in REVERSIBLE_ACTIONS:
        return False
    # Must have old_value to restore
    if not old_value:
        return False
    return True


class AuditLogResponse(BaseModel):
    """Response model for audit log data."""
    id: int
    timestamp: str
    user: Optional[str] = None  # Keep for backwards compatibility
    username: Optional[str] = None  # More intuitive field name
    user_id: Optional[int] = None
    action: str
    resource_type: str
    resource_id: Optional[int] = None
    old_value: Optional[Dict[str, Any]] = None
    new_value: Optional[Dict[str, Any]] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    success: bool
    error_message: Optional[str] = None
    reversible: bool = False  # Whether this action can be undone

    class Config:
        from_attributes = True


class AuditLogWithReversibility(AuditLogResponse):
    """Extended response model with reversibility information for activity sidebar."""
    undo_description: Optional[str] = None  # Human-readable description of what undo would do


@router.get("", response_model=List[AuditLogResponse])
async def list_audit_logs(
    resource_type: str = None,
    resource_id: int = None,
    user_id: int = None,
    action: str = None,
    start_date: datetime = Query(None, description="Start date for filtering (ISO format)"),
    end_date: datetime = Query(None, description="End date for filtering (ISO format)"),
    limit: int = Query(100, ge=1, le=1000, description="Number of logs to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List audit logs with optional filtering.

    Requires view_audit permission.
    """
    if not current_user.has_permission('view_audit'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to view audit logs")

    query = db.query(AuditLog).options(joinedload(AuditLog.user))

    # Apply filters
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if resource_id is not None:
        query = query.filter(AuditLog.resource_id == resource_id)
    if user_id is not None:
        query = query.filter(AuditLog.user_id == user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if start_date:
        query = query.filter(AuditLog.timestamp >= start_date)
    if end_date:
        query = query.filter(AuditLog.timestamp <= end_date)

    # Order by timestamp descending (most recent first)
    query = query.order_by(AuditLog.timestamp.desc())

    # Pagination
    total_count = query.count()
    logs = query.offset(offset).limit(limit).all()

    logger.info("audit_logs_queried", user=current_user.username, count=len(logs),
                total=total_count, filters={
                    'resource_type': resource_type,
                    'resource_id': resource_id,
                    'action': action
                })

    return [log.to_dict() for log in logs]


@router.get("/recent")
async def get_recent_audit_logs(
    limit: int = Query(10, ge=1, le=100, description="Number of recent logs to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get most recent audit logs.

    Convenience endpoint for dashboards and activity feeds.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logs = (
        db.query(AuditLog)
        .options(joinedload(AuditLog.user))
        .order_by(AuditLog.timestamp.desc())
        .limit(limit)
        .all()
    )

    return {"entries": [log.to_dict() for log in logs]}


@router.get("/stats")
async def get_audit_stats(
    days: int = Query(7, ge=1, le=90, description="Number of days to include in stats"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get audit log statistics.

    Returns counts by action type, resource type, and user for the specified time period.
    """
    if not current_user.has_permission('view_audit'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to view audit logs")

    start_date = datetime.utcnow() - timedelta(days=days)

    # Total count
    total = db.query(AuditLog).filter(AuditLog.timestamp >= start_date).count()

    # Count by action
    actions = db.query(AuditLog.action, db.func.count(AuditLog.id))\
        .filter(AuditLog.timestamp >= start_date)\
        .group_by(AuditLog.action)\
        .all()

    # Count by resource type
    resources = db.query(AuditLog.resource_type, db.func.count(AuditLog.id))\
        .filter(AuditLog.timestamp >= start_date)\
        .group_by(AuditLog.resource_type)\
        .all()

    # Count by user (top 10)
    users = db.query(User.username, db.func.count(AuditLog.id))\
        .join(AuditLog, User.id == AuditLog.user_id)\
        .filter(AuditLog.timestamp >= start_date)\
        .group_by(User.username)\
        .order_by(db.func.count(AuditLog.id).desc())\
        .limit(10)\
        .all()

    # Failed operations
    failures = db.query(AuditLog).filter(
        AuditLog.timestamp >= start_date,
        AuditLog.success == False
    ).count()

    return {
        "period_days": days,
        "start_date": start_date.isoformat(),
        "total_logs": total,
        "by_action": {action: count for action, count in actions},
        "by_resource_type": {resource: count for resource, count in resources},
        "top_users": {username: count for username, count in users},
        "failed_operations": failures
    }


@router.get("/recent")
async def get_recent_audit_logs(
    limit: int = Query(20, ge=1, le=100, description="Number of recent logs to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get most recent audit logs with reversibility information.

    Each entry includes a `reversible` flag indicating if the action can be undone,
    and an `undo_description` explaining what the undo would do.
    """
    if not current_user.has_permission('view_audit'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to view audit logs")

    logs = db.query(AuditLog)\
        .options(joinedload(AuditLog.user))\
        .order_by(AuditLog.timestamp.desc())\
        .limit(limit)\
        .all()

    result = []
    for log in logs:
        log_dict = log.to_dict()
        # Check if action is reversible
        reversible = is_action_reversible(log.action, log.old_value)
        log_dict['reversible'] = reversible

        # Generate undo description
        if reversible:
            if log.action == 'toggle' and log.resource_type == 'feature':
                old_enabled = log.old_value.get('enabled', False) if log.old_value else False
                log_dict['undo_description'] = f"Restore to {'enabled' if old_enabled else 'disabled'}"
            elif log.action == 'update':
                log_dict['undo_description'] = "Restore previous value"
            else:
                log_dict['undo_description'] = "Undo this action"
        else:
            log_dict['undo_description'] = None

        result.append(log_dict)

    return result


@router.get("/resource/{resource_type}/{resource_id}")
async def get_resource_audit_trail(
    resource_type: str,
    resource_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get complete audit trail for a specific resource."""
    if not current_user.has_permission('view_audit'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to view audit logs")

    logs = db.query(AuditLog)\
        .options(joinedload(AuditLog.user))\
        .filter(
            AuditLog.resource_type == resource_type,
            AuditLog.resource_id == resource_id
        )\
        .order_by(AuditLog.timestamp.desc())\
        .all()

    if not logs:
        raise HTTPException(
            status_code=404,
            detail=f"No audit logs found for {resource_type} {resource_id}"
        )

    return {
        "resource_type": resource_type,
        "resource_id": resource_id,
        "log_count": len(logs),
        "first_seen": logs[-1].timestamp.isoformat() if logs else None,
        "last_modified": logs[0].timestamp.isoformat() if logs else None,
        "logs": [log.to_dict() for log in logs]
    }


@router.get("/user/{user_id}")
async def get_user_activity(
    user_id: int,
    days: int = Query(30, ge=1, le=90, description="Number of days to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get activity history for a specific user."""
    if not current_user.has_permission('view_audit'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to view audit logs")

    # Check if user exists
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    start_date = datetime.utcnow() - timedelta(days=days)

    logs = db.query(AuditLog)\
        .options(joinedload(AuditLog.user))\
        .filter(
            AuditLog.user_id == user_id,
            AuditLog.timestamp >= start_date
        )\
        .order_by(AuditLog.timestamp.desc())\
        .all()

    # Count by action type
    actions = db.query(AuditLog.action, db.func.count(AuditLog.id))\
        .filter(
            AuditLog.user_id == user_id,
            AuditLog.timestamp >= start_date
        )\
        .group_by(AuditLog.action)\
        .all()

    return {
        "user_id": user_id,
        "username": user.username,
        "period_days": days,
        "total_actions": len(logs),
        "by_action": {action: count for action, count in actions},
        "recent_activity": [log.to_dict() for log in logs[:50]]  # Last 50 actions
    }


@router.post("/{audit_id}/undo")
async def undo_audit_action(
    audit_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Undo a reversible audit action.

    Only supports reversible actions (toggle, update) that have old_value stored.
    Creates a new audit log entry recording the undo operation.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions to undo actions")

    # Get the audit log entry
    audit_entry = db.query(AuditLog).filter(AuditLog.id == audit_id).first()
    if not audit_entry:
        raise HTTPException(status_code=404, detail="Audit log entry not found")

    # Check if action is reversible
    if not is_action_reversible(audit_entry.action, audit_entry.old_value):
        raise HTTPException(
            status_code=400,
            detail=f"Action '{audit_entry.action}' cannot be undone"
        )

    # Perform the undo based on resource type
    try:
        if audit_entry.resource_type == 'feature' and audit_entry.action == 'toggle':
            # Undo feature toggle
            feature = db.query(Feature).filter(Feature.id == audit_entry.resource_id).first()
            if not feature:
                raise HTTPException(status_code=404, detail="Feature not found")

            old_enabled = audit_entry.old_value.get('enabled', False)
            current_enabled = feature.enabled

            # Restore old value
            feature.enabled = old_enabled
            db.commit()

            # Create audit entry for the undo
            undo_audit = AuditLog(
                user_id=current_user.id,
                action='undo',
                resource_type='feature',
                resource_id=feature.id,
                old_value={'enabled': current_enabled},
                new_value={'enabled': old_enabled},
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get('user-agent'),
                success=True
            )
            db.add(undo_audit)
            db.commit()

            logger.info("audit_action_undone", audit_id=audit_id, user=current_user.username,
                        resource_type='feature', resource_id=feature.id,
                        old_enabled=current_enabled, new_enabled=old_enabled)

            return {
                "success": True,
                "message": f"Feature '{feature.name}' restored to {'enabled' if old_enabled else 'disabled'}",
                "resource_type": "feature",
                "resource_id": feature.id,
                "old_value": {"enabled": current_enabled},
                "new_value": {"enabled": old_enabled}
            }

        elif audit_entry.action == 'update':
            # Generic update undo - restore old_value
            # This would need to be extended for each resource type
            raise HTTPException(
                status_code=501,
                detail=f"Undo for '{audit_entry.resource_type}' updates not yet implemented"
            )

        else:
            raise HTTPException(
                status_code=400,
                detail=f"Undo not supported for action '{audit_entry.action}' on '{audit_entry.resource_type}'"
            )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("audit_undo_failed", audit_id=audit_id, error=str(e))
        db.rollback()

        # Log the failed undo attempt
        failed_audit = AuditLog(
            user_id=current_user.id,
            action='undo',
            resource_type=audit_entry.resource_type,
            resource_id=audit_entry.resource_id,
            old_value=audit_entry.new_value,
            new_value=audit_entry.old_value,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get('user-agent'),
            success=False,
            error_message=str(e)
        )
        db.add(failed_audit)
        db.commit()

        raise HTTPException(status_code=500, detail=f"Failed to undo action: {str(e)}")
