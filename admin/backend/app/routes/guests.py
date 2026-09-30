"""
Guest management API routes.

Provides endpoints for managing multiple guests per reservation.
Supports device-based identification for personalized interactions.
"""
from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.utils.service_auth import require_user_permission
from app.auth.oidc import get_current_user
from app.models import User, Guest, CalendarEvent

logger = structlog.get_logger()

router = APIRouter(prefix="/api/guests", tags=["guests"])


def _active_event(db: Session, now: datetime) -> Optional[CalendarEvent]:
    """The current stay: confirmed, not deleted, covering ``now``. On a
    changeover day two stays can cover the same instant; the one checking
    out first (the departing guest's) is current until it checks out."""
    return db.query(CalendarEvent).filter(
        CalendarEvent.deleted_at.is_(None),
        CalendarEvent.status == 'confirmed',
        CalendarEvent.checkin <= now,
        CalendarEvent.checkout >= now,
    ).order_by(CalendarEvent.checkout.asc()).first()


# ============================================================================
# Pydantic Schemas
# ============================================================================

class GuestCreate(BaseModel):
    """Schema for creating a new guest."""
    calendar_event_id: int
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    is_primary: bool = False
    is_test: bool = False


class GuestUpdate(BaseModel):
    """Schema for updating an existing guest."""
    name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    is_primary: Optional[bool] = None


class GuestResponse(BaseModel):
    """Schema for guest response."""
    id: int
    calendar_event_id: Optional[int]
    name: str
    email: Optional[str]
    phone: Optional[str]
    is_primary: bool
    voice_profile_id: Optional[str]
    is_test: bool = False
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class AddGuestToCurrentRequest(BaseModel):
    """Schema for adding guest to current reservation."""
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None


# ============================================================================
# Guest CRUD Endpoints
# ============================================================================

@router.get("", response_model=List[GuestResponse], dependencies=[Depends(require_user_permission("read"))])
async def list_guests(
    calendar_event_id: Optional[int] = Query(None, description="Filter by calendar event"),
    db: Session = Depends(get_db)
):
    """
    List all guests, optionally filtered by calendar event.

    Requires a signed-in user with read permission.
    """
    try:
        query = db.query(Guest)

        if calendar_event_id:
            query = query.filter(Guest.calendar_event_id == calendar_event_id)

        guests = query.order_by(Guest.is_primary.desc(), Guest.created_at).all()

        logger.info("guests_listed", count=len(guests), calendar_event_id=calendar_event_id)

        return [g.to_dict() for g in guests]

    except Exception as e:
        logger.error("failed_to_list_guests", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve guests")


@router.get("/current", response_model=List[GuestResponse], dependencies=[Depends(require_user_permission("read"))])
async def get_current_guests(db: Session = Depends(get_db)):
    """
    Get guests for the currently active reservation.

    The current stay is the confirmed, non-deleted reservation covering now
    (the departing one on a changeover day; see _active_event). Returns
    guests ordered by is_primary (primary first), then by created_at.

    Requires a signed-in user with read permission.
    """
    try:
        now = datetime.now(timezone.utc)

        event = _active_event(db, now)

        if not event:
            logger.debug("no_active_reservation_for_guests")
            return []

        guests = db.query(Guest).filter(
            Guest.calendar_event_id == event.id
        ).order_by(Guest.is_primary.desc(), Guest.created_at).all()

        logger.info("current_guests_retrieved",
                   event_id=event.id,
                   guest_count=len(guests))

        return [g.to_dict() for g in guests]

    except Exception as e:
        logger.error("failed_to_get_current_guests", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve current guests")


@router.get("/by-events", dependencies=[Depends(require_user_permission("read"))])
async def get_guests_by_events(
    event_ids: str = Query(..., description="Comma-separated event IDs"),
    db: Session = Depends(get_db)
):
    """
    Get guests grouped by calendar event ID.

    Used by admin UI to efficiently load guests for multiple reservations.
    Returns a dictionary mapping event_id to list of guests.

    Requires a signed-in user with read permission.
    """
    try:
        # Parse comma-separated IDs
        try:
            ids = [int(id.strip()) for id in event_ids.split(",") if id.strip()]
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid event IDs format")

        if not ids:
            return {}

        guests = db.query(Guest).filter(
            Guest.calendar_event_id.in_(ids)
        ).order_by(Guest.calendar_event_id, Guest.is_primary.desc(), Guest.created_at).all()

        # Group by event_id
        result = {}
        for guest in guests:
            event_id = guest.calendar_event_id
            if event_id not in result:
                result[event_id] = []
            result[event_id].append(guest.to_dict())

        logger.info("guests_by_events_retrieved", event_count=len(ids), total_guests=len(guests))

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_guests_by_events", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve guests")


@router.get("/{guest_id}", response_model=GuestResponse, dependencies=[Depends(require_user_permission("read"))])
async def get_guest(
    guest_id: int,
    db: Session = Depends(get_db)
):
    """
    Get a specific guest by ID.

    Requires a signed-in user with read permission.
    """
    try:
        guest = db.query(Guest).filter(Guest.id == guest_id).first()

        if not guest:
            raise HTTPException(status_code=404, detail="Guest not found")

        logger.info("guest_retrieved", guest_id=guest_id)

        return guest.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_guest", error=str(e), guest_id=guest_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve guest")


@router.post("", response_model=GuestResponse, status_code=201)
async def create_guest(
    guest_data: GuestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new guest for a calendar event (reservation).

    Requires authentication with write permissions.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Verify calendar event exists
        event = db.query(CalendarEvent).filter(
            CalendarEvent.id == guest_data.calendar_event_id
        ).first()

        if not event:
            raise HTTPException(status_code=404, detail="Calendar event not found")

        # Check for duplicate name on same event
        existing = db.query(Guest).filter(
            Guest.calendar_event_id == guest_data.calendar_event_id,
            Guest.name == guest_data.name
        ).first()

        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Guest '{guest_data.name}' already exists for this reservation"
            )

        # Create guest
        new_guest = Guest(
            calendar_event_id=guest_data.calendar_event_id,
            name=guest_data.name,
            email=guest_data.email,
            phone=guest_data.phone,
            is_primary=guest_data.is_primary,
            is_test=guest_data.is_test
        )
        db.add(new_guest)
        db.commit()
        db.refresh(new_guest)

        logger.info("guest_created",
                   user=current_user.username,
                   guest_id=new_guest.id,
                   event_id=guest_data.calendar_event_id)

        return new_guest.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_create_guest", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create guest")


@router.post("/current/add", response_model=dict, dependencies=[Depends(require_user_permission("write"))])
async def add_guest_to_current_reservation(
    guest_data: AddGuestToCurrentRequest,
    db: Session = Depends(get_db)
):
    """
    Add a guest to the currently active reservation.

    Creates a new guest record linked to the current stay (see
    _active_event). Requires a signed-in user with write permission.
    """
    try:
        now = datetime.now(timezone.utc)

        event = _active_event(db, now)

        if not event:
            raise HTTPException(status_code=404, detail="No active reservation found")

        # Check for duplicate
        existing = db.query(Guest).filter(
            Guest.calendar_event_id == event.id,
            Guest.name == guest_data.name
        ).first()

        if existing:
            raise HTTPException(
                status_code=409,
                detail=f"Guest '{guest_data.name}' already exists"
            )

        # Create guest (not primary - primary comes from iCal)
        new_guest = Guest(
            calendar_event_id=event.id,
            name=guest_data.name,
            email=guest_data.email,
            phone=guest_data.phone,
            is_primary=False
        )
        db.add(new_guest)
        db.commit()
        db.refresh(new_guest)

        logger.info("guest_added_to_current_reservation",
                   guest_id=new_guest.id,
                   event_id=event.id)

        return {"success": True, "guest": new_guest.to_dict()}

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_add_guest_to_current", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to add guest")


@router.put("/{guest_id}", response_model=GuestResponse)
async def update_guest(
    guest_id: int,
    update_data: GuestUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing guest."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        guest = db.query(Guest).filter(Guest.id == guest_id).first()

        if not guest:
            raise HTTPException(status_code=404, detail="Guest not found")

        # Update fields
        if update_data.name is not None:
            # Check for duplicate name on same event
            existing = db.query(Guest).filter(
                Guest.calendar_event_id == guest.calendar_event_id,
                Guest.name == update_data.name,
                Guest.id != guest_id
            ).first()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f"Guest '{update_data.name}' already exists"
                )
            guest.name = update_data.name

        if update_data.email is not None:
            guest.email = update_data.email
        if update_data.phone is not None:
            guest.phone = update_data.phone
        if update_data.is_primary is not None:
            guest.is_primary = update_data.is_primary

        db.commit()
        db.refresh(guest)

        logger.info("guest_updated",
                   user=current_user.username,
                   guest_id=guest_id)

        return guest.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_update_guest", error=str(e), guest_id=guest_id)
        raise HTTPException(status_code=500, detail="Failed to update guest")


@router.delete("/{guest_id}", status_code=204)
async def delete_guest(
    guest_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a guest."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        guest = db.query(Guest).filter(Guest.id == guest_id).first()

        if not guest:
            raise HTTPException(status_code=404, detail="Guest not found")

        logger.info("guest_deleted",
                   user=current_user.username,
                   guest_id=guest_id)

        db.delete(guest)  # Cascades to sessions
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_delete_guest", error=str(e), guest_id=guest_id)
        raise HTTPException(status_code=500, detail="Failed to delete guest")