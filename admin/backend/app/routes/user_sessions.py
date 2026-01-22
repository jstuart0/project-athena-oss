"""
User session management API routes.

Provides endpoints for device-to-guest mapping.
Enables device fingerprint-based user identification across web app and voice.
"""
from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import desc
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, UserSession, Guest

logger = structlog.get_logger()

router = APIRouter(prefix="/api/user-sessions", tags=["user-sessions"])


# ============================================================================
# Pydantic Schemas
# ============================================================================

class UserSessionCreate(BaseModel):
    """Schema for creating or updating a user session."""
    session_id: str
    guest_id: int
    device_id: str
    device_type: str = 'web'  # 'web', 'mobile', 'voice'
    room: Optional[str] = None
    preferences: Optional[dict] = {}


class UserSessionResponse(BaseModel):
    """Schema for user session response."""
    id: int
    session_id: str
    guest_id: Optional[int]
    guest_name: Optional[str]
    device_id: str
    device_type: str
    room: Optional[str]
    last_seen: Optional[str]
    preferences: dict
    created_at: Optional[str]

    class Config:
        from_attributes = True


# ============================================================================
# Session Endpoints
# ============================================================================

@router.post("", response_model=UserSessionResponse)
async def create_or_update_session(
    session_data: UserSessionCreate,
    db: Session = Depends(get_db)
):
    """
    Create or update a user session.

    If a session with the same session_id exists, it will be updated.
    Otherwise, a new session is created.

    NOTE: This endpoint is public to allow web app guest self-identification.
    """
    try:
        # Check if session exists
        existing = db.query(UserSession).filter(
            UserSession.session_id == session_data.session_id
        ).first()

        if existing:
            # Update existing session
            existing.guest_id = session_data.guest_id
            existing.device_id = session_data.device_id
            existing.device_type = session_data.device_type
            existing.room = session_data.room
            existing.preferences = session_data.preferences or {}
            existing.last_seen = datetime.now(timezone.utc)
            db.commit()
            db.refresh(existing)

            logger.info("user_session_updated",
                       session_id=session_data.session_id,
                       guest_id=session_data.guest_id,
                       device_id=session_data.device_id[:16] + "...")

            return existing.to_dict()

        # Verify guest exists
        guest = db.query(Guest).filter(Guest.id == session_data.guest_id).first()
        if not guest:
            raise HTTPException(status_code=404, detail="Guest not found")

        # Create new session
        new_session = UserSession(
            session_id=session_data.session_id,
            guest_id=session_data.guest_id,
            device_id=session_data.device_id,
            device_type=session_data.device_type,
            room=session_data.room,
            preferences=session_data.preferences or {}
        )
        db.add(new_session)
        db.commit()
        db.refresh(new_session)

        logger.info("user_session_created",
                   session_id=session_data.session_id,
                   guest_id=session_data.guest_id,
                   guest_name=guest.name,
                   device_id=session_data.device_id[:16] + "...")

        return new_session.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_create_session", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create session")


@router.get("/device/{device_id}", response_model=UserSessionResponse)
async def get_session_by_device(
    device_id: str,
    db: Session = Depends(get_db)
):
    """
    Get the most recent session for a device.

    This is the primary endpoint used by the orchestrator to identify users
    by their device fingerprint.

    Updates last_seen timestamp on access.

    NOTE: This endpoint is public for orchestrator access.
    """
    try:
        session = db.query(UserSession).filter(
            UserSession.device_id == device_id
        ).order_by(desc(UserSession.last_seen)).first()

        if not session:
            raise HTTPException(
                status_code=404,
                detail="No session found for device"
            )

        # Update last_seen
        session.last_seen = datetime.now(timezone.utc)
        db.commit()

        logger.info("user_session_retrieved_by_device",
                   device_id=device_id[:16] + "...",
                   guest_id=session.guest_id,
                   guest_name=session.guest.name if session.guest else None)

        return session.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_session_by_device",
                    error=str(e),
                    device_id=device_id[:16] + "...")
        raise HTTPException(status_code=500, detail="Failed to retrieve session")


@router.get("/{session_id}", response_model=UserSessionResponse)
async def get_session(
    session_id: str,
    db: Session = Depends(get_db)
):
    """
    Get a specific session by ID.

    NOTE: This endpoint is public for orchestrator access.
    """
    try:
        session = db.query(UserSession).filter(
            UserSession.session_id == session_id
        ).first()

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        logger.info("user_session_retrieved",
                   session_id=session_id,
                   guest_id=session.guest_id)

        return session.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_session", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve session")


@router.patch("/{session_id}/last-seen")
async def update_last_seen(
    session_id: str,
    db: Session = Depends(get_db)
):
    """
    Update session last_seen timestamp.

    Call this periodically to keep session active.

    NOTE: This endpoint is public.
    """
    try:
        session = db.query(UserSession).filter(
            UserSession.session_id == session_id
        ).first()

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        session.last_seen = datetime.now(timezone.utc)
        db.commit()

        return {"status": "updated", "session_id": session_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_update_last_seen", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to update session")


@router.delete("/device/{device_id}")
async def clear_device_sessions(
    device_id: str,
    db: Session = Depends(get_db)
):
    """
    Clear all sessions for a device (logout).

    NOTE: This endpoint is public to allow users to clear their own sessions.
    """
    try:
        sessions = db.query(UserSession).filter(
            UserSession.device_id == device_id
        ).all()

        count = len(sessions)
        for session in sessions:
            db.delete(session)
        db.commit()

        logger.info("device_sessions_cleared",
                   device_id=device_id[:16] + "...",
                   count=count)

        return {"status": "cleared", "count": count}

    except Exception as e:
        db.rollback()
        logger.error("failed_to_clear_device_sessions",
                    error=str(e),
                    device_id=device_id[:16] + "...")
        raise HTTPException(status_code=500, detail="Failed to clear sessions")


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Delete a specific session.

    Requires authentication with write permissions.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        session = db.query(UserSession).filter(
            UserSession.session_id == session_id
        ).first()

        if not session:
            raise HTTPException(status_code=404, detail="Session not found")

        logger.info("user_session_deleted",
                   user=current_user.username,
                   session_id=session_id)

        db.delete(session)
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_delete_session", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail="Failed to delete session")
