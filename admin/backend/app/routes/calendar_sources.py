"""
Calendar Sources management API routes.

Provides endpoints for managing iCal feed sources for guest mode.
Users can add, update, enable/disable, and test calendar sources.
Supports Airbnb, VRBO, Lodgify, and generic iCal feeds.

Lodgify API Integration:
- Lodgify iCal exports mask guest names for privacy
- When a Lodgify API key is available, we fetch full guest details via API
- API returns type: "Booking" for real guests, "ClosedPeriod" for manual blocks
"""
from typing import List, Optional, Tuple
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks
from sqlalchemy.orm import Session
from pydantic import BaseModel, HttpUrl
import structlog
import httpx
import os

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, CalendarSource, CalendarEvent, ExternalAPIKey

logger = structlog.get_logger()

router = APIRouter(prefix="/api/calendar-sources", tags=["calendar-sources"])

# Lodgify API endpoint
LODGIFY_API_BASE = "https://api.lodgify.com"


# ============================================================================
# Pydantic Schemas
# ============================================================================

class CalendarSourceCreate(BaseModel):
    """Schema for creating a new calendar source."""
    name: str
    source_type: str  # 'airbnb', 'vrbo', 'lodgify', 'generic_ical'
    ical_url: str
    enabled: bool = True
    sync_interval_minutes: int = 30
    priority: int = 1
    default_checkin_time: str = '16:00'  # 4:00 PM
    default_checkout_time: str = '11:00'  # 11:00 AM
    description: Optional[str] = None


class CalendarSourceUpdate(BaseModel):
    """Schema for updating a calendar source."""
    name: Optional[str] = None
    source_type: Optional[str] = None
    ical_url: Optional[str] = None
    enabled: Optional[bool] = None
    sync_interval_minutes: Optional[int] = None
    priority: Optional[int] = None
    default_checkin_time: Optional[str] = None
    default_checkout_time: Optional[str] = None
    description: Optional[str] = None


class CalendarSourceResponse(BaseModel):
    """Schema for calendar source response."""
    id: int
    name: str
    source_type: str
    ical_url: str
    ical_url_masked: Optional[str] = None
    enabled: bool
    sync_interval_minutes: int
    priority: int
    last_sync_at: Optional[str]
    last_sync_status: Optional[str]
    last_sync_error: Optional[str]
    last_event_count: int
    default_checkin_time: Optional[str] = '16:00'
    default_checkout_time: Optional[str] = '11:00'
    description: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class TestConnectionResponse(BaseModel):
    """Response for testing iCal URL connectivity."""
    success: bool
    message: str
    event_count: Optional[int] = None
    sample_events: Optional[List[dict]] = None


class SyncResponse(BaseModel):
    """Response for manual sync trigger."""
    success: bool
    message: str
    events_synced: int = 0
    events_added: int = 0
    events_updated: int = 0
    events_removed: int = 0


# ============================================================================
# Helper Functions
# ============================================================================

def get_lodgify_api_key(db: Session) -> Optional[str]:
    """
    Get Lodgify API key from external_api_keys table.

    Returns decrypted API key or None if not configured.
    """
    try:
        from app.utils.encryption import decrypt_value

        api_key_record = db.query(ExternalAPIKey).filter(
            ExternalAPIKey.service_name == 'lodgify',
            ExternalAPIKey.enabled == True
        ).first()

        if api_key_record and api_key_record.api_key_encrypted:
            return decrypt_value(api_key_record.api_key_encrypted)
        return None
    except Exception as e:
        logger.warning("failed_to_get_lodgify_api_key", error=str(e))
        return None


def parse_time_string(time_str: str) -> Tuple[int, int]:
    """Parse 'HH:MM' time string to (hour, minute) tuple."""
    try:
        parts = time_str.split(':')
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return 16, 0  # Default to 4:00 PM


async def fetch_lodgify_reservations(
    api_key: str,
    timeout: float = 30.0,
    checkin_time: str = '16:00',
    checkout_time: str = '11:00'
) -> List[dict]:
    """
    Fetch reservations from Lodgify API with pagination support.

    Returns list of reservations with full guest details.
    Filters out ClosedPeriod entries (manual blocks).

    Args:
        api_key: Lodgify API key
        timeout: Request timeout in seconds
        checkin_time: Default check-in time in 'HH:MM' format (e.g., '16:00')
        checkout_time: Default check-out time in 'HH:MM' format (e.g., '11:00')

    API Response structure:
    - type: "Booking" = real guest reservation
    - type: "ClosedPeriod" = manual block by owner
    """
    reservations = []
    offset = 0
    limit = 50  # Fetch 50 at a time
    max_pages = 10  # Safety limit

    # Parse check-in/check-out times
    checkin_hour, checkin_min = parse_time_string(checkin_time)
    checkout_hour, checkout_min = parse_time_string(checkout_time)

    async with httpx.AsyncClient() as client:
        for page in range(max_pages):
            response = await client.get(
                f"{LODGIFY_API_BASE}/v1/reservation",
                timeout=timeout,
                params={"offset": offset, "limit": limit},
                headers={
                    "X-ApiKey": api_key,
                    "Accept": "application/json"
                }
            )
            response.raise_for_status()
            data = response.json()

            items = data.get('items', [])
            if not items:
                break  # No more items

            for item in items:
                # Skip ClosedPeriod entries (manual blocks)
                if item.get('type') == 'ClosedPeriod':
                    logger.debug("skipping_closed_period",
                               arrival=item.get('arrival'),
                               departure=item.get('departure'))
                    continue

                # Extract guest info
                guest = item.get('guest', {})
                guest_name = guest.get('name', '')
                guest_email = guest.get('email', '')
                guest_phone = guest.get('phone', '')

                # Parse dates
                arrival = item.get('arrival')
                departure = item.get('departure')

                if not arrival or not departure:
                    continue

                # Convert to datetime using configurable check-in/out times
                try:
                    checkin = datetime.strptime(arrival, '%Y-%m-%d').replace(
                        hour=checkin_hour, minute=checkin_min, second=0, tzinfo=timezone.utc)
                    checkout = datetime.strptime(departure, '%Y-%m-%d').replace(
                        hour=checkout_hour, minute=checkout_min, second=0, tzinfo=timezone.utc)
                except ValueError:
                    logger.warning("invalid_date_format", arrival=arrival, departure=departure)
                    continue

                reservations.append({
                    'external_id': f"lodgify_{item.get('id', '')}",
                    'title': f"Lodgify Booking - {guest_name}" if guest_name else "Lodgify Booking",
                    'checkin': checkin,
                    'checkout': checkout,
                    'guest_name': guest_name or None,
                    'guest_email': guest_email or None,
                    'guest_phone': guest_phone or None,
                    'notes': f"Source: {item.get('source', 'Lodgify')}",
                    'source': 'lodgify',
                    'status': 'confirmed',  # Lodgify "Booked" = confirmed for guest mode
                    'is_manual_block': False
                })

            # Check if we've fetched all items
            total = data.get('total', 0)
            if offset + limit >= total:
                break  # All items fetched
            offset += limit

    return reservations


async def fetch_ical_data(url: str, timeout: float = 30.0) -> str:
    """Fetch iCal data from a URL."""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": "Athena-Calendar-Sync/1.0"
            }
        )
        response.raise_for_status()
        return response.text


def parse_ical_events(ical_data: str, source_type: str) -> List[dict]:
    """
    Parse iCal data and extract events.

    Returns a list of event dictionaries with:
    - external_id (UID)
    - title (SUMMARY)
    - checkin (DTSTART)
    - checkout (DTEND)
    - guest_name (extracted from SUMMARY/DESCRIPTION)
    - notes (DESCRIPTION)
    """
    try:
        from icalendar import Calendar
    except ImportError:
        logger.error("icalendar_not_installed")
        raise HTTPException(
            status_code=500,
            detail="icalendar library not installed. Run: pip install icalendar"
        )

    events = []
    cal = Calendar.from_ical(ical_data)

    for component in cal.walk():
        if component.name == "VEVENT":
            uid = str(component.get('uid', ''))
            summary = str(component.get('summary', ''))
            description = str(component.get('description', '') or '')

            # Parse dates
            dtstart = component.get('dtstart')
            dtend = component.get('dtend')

            if not dtstart or not dtend:
                continue

            # Handle date vs datetime
            start_dt = dtstart.dt
            end_dt = dtend.dt

            # Convert date to datetime if needed
            if hasattr(start_dt, 'hour'):
                checkin = start_dt
            else:
                checkin = datetime.combine(start_dt, datetime.min.time())

            if hasattr(end_dt, 'hour'):
                checkout = end_dt
            else:
                checkout = datetime.combine(end_dt, datetime.min.time())

            # Make timezone aware if not already
            if checkin.tzinfo is None:
                checkin = checkin.replace(tzinfo=timezone.utc)
            if checkout.tzinfo is None:
                checkout = checkout.replace(tzinfo=timezone.utc)

            # Extract guest name based on source type
            guest_name = None
            if source_type == 'airbnb':
                # Airbnb shows "Reserved" or "Not available"
                if 'reserved' in summary.lower():
                    guest_name = 'Airbnb Guest'
            elif source_type == 'lodgify':
                # Lodgify often has guest name in summary
                if summary and summary not in ['Blocked', 'Closed Period', 'Reserved']:
                    guest_name = summary
            elif source_type == 'vrbo':
                # VRBO often shows "Blocked" for external syncs
                if 'blocked' not in summary.lower():
                    guest_name = summary or 'VRBO Guest'
            else:
                # Generic - use summary if it looks like a name
                if summary and summary not in ['Blocked', 'Reserved', 'Not available']:
                    guest_name = summary

            # Extract phone from description if present
            guest_phone = None
            if description:
                import re
                phone_match = re.search(r'Phone[:\s]+[\d\-\(\)\s]+(\d{4})', description)
                if phone_match:
                    guest_phone = phone_match.group(0)

            events.append({
                'external_id': uid,
                'title': summary,
                'checkin': checkin,
                'checkout': checkout,
                'guest_name': guest_name,
                'guest_phone': guest_phone,
                'notes': description if description else None,
                'source': source_type
            })

    return events


# ============================================================================
# CRUD Endpoints
# ============================================================================

@router.get("", response_model=List[CalendarSourceResponse])
async def list_calendar_sources(
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    db: Session = Depends(get_db)
):
    """
    List all calendar sources.

    NOTE: This endpoint is public for internal service access.
    """
    try:
        query = db.query(CalendarSource)

        if enabled is not None:
            query = query.filter(CalendarSource.enabled == enabled)

        query = query.order_by(CalendarSource.priority.desc(), CalendarSource.name)
        sources = query.all()

        logger.info("calendar_sources_listed", count=len(sources), enabled=enabled)

        # Use safe dict that masks URL
        return [source.to_dict_safe() for source in sources]

    except Exception as e:
        logger.error("failed_to_list_calendar_sources", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve calendar sources")


@router.get("/types")
async def get_source_types():
    """Get available calendar source types with descriptions."""
    return [
        {
            "type": "airbnb",
            "name": "Airbnb",
            "description": "Airbnb iCal export feed",
            "url_pattern": "https://www.airbnb.com/calendar/ical/*.ics"
        },
        {
            "type": "vrbo",
            "name": "VRBO / HomeAway",
            "description": "VRBO or HomeAway iCal feed",
            "url_pattern": "https://www.vrbo.com/icalendar/*.ics"
        },
        {
            "type": "lodgify",
            "name": "Lodgify",
            "description": "Lodgify property management iCal export",
            "url_pattern": "https://www.lodgify.com/*.ics"
        },
        {
            "type": "generic_ical",
            "name": "Generic iCal",
            "description": "Any standard iCal/ICS feed URL",
            "url_pattern": "*.ics"
        }
    ]


@router.get("/{source_id}", response_model=CalendarSourceResponse)
async def get_calendar_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific calendar source by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()

        if not source:
            raise HTTPException(status_code=404, detail="Calendar source not found")

        logger.info("calendar_source_retrieved",
                   user=current_user.username,
                   source_id=source_id)

        # Return full URL for authenticated users
        return source.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_calendar_source", error=str(e), source_id=source_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve calendar source")


@router.post("", response_model=CalendarSourceResponse, status_code=201)
async def create_calendar_source(
    source_data: CalendarSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new calendar source."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Validate source type
        valid_types = ['airbnb', 'vrbo', 'lodgify', 'generic_ical']
        if source_data.source_type not in valid_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid source_type. Must be one of: {', '.join(valid_types)}"
            )

        # Validate URL format
        if not source_data.ical_url.startswith(('http://', 'https://')):
            raise HTTPException(status_code=400, detail="iCal URL must start with http:// or https://")

        # Check for duplicate URL
        existing = db.query(CalendarSource).filter(
            CalendarSource.ical_url == source_data.ical_url
        ).first()
        if existing:
            raise HTTPException(
                status_code=409,
                detail="A calendar source with this URL already exists"
            )

        # Create the source
        new_source = CalendarSource(
            name=source_data.name,
            source_type=source_data.source_type,
            ical_url=source_data.ical_url,
            enabled=source_data.enabled,
            sync_interval_minutes=source_data.sync_interval_minutes,
            priority=source_data.priority,
            description=source_data.description,
            last_sync_status='pending'
        )
        db.add(new_source)
        db.commit()
        db.refresh(new_source)

        logger.info("calendar_source_created",
                   user=current_user.username,
                   source_id=new_source.id,
                   name=new_source.name,
                   source_type=new_source.source_type)

        return new_source.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_create_calendar_source", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create calendar source")


@router.put("/{source_id}", response_model=CalendarSourceResponse)
async def update_calendar_source(
    source_id: int,
    update_data: CalendarSourceUpdate,
    db: Session = Depends(get_db)
):
    """Update a calendar source.

    Note: This endpoint does not require authentication to allow
    automated management and CLI access.
    """

    try:
        source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()

        if not source:
            raise HTTPException(status_code=404, detail="Calendar source not found")

        # Update fields
        if update_data.name is not None:
            source.name = update_data.name
        if update_data.source_type is not None:
            valid_types = ['airbnb', 'vrbo', 'lodgify', 'generic_ical']
            if update_data.source_type not in valid_types:
                raise HTTPException(status_code=400, detail=f"Invalid source_type")
            source.source_type = update_data.source_type
        if update_data.ical_url is not None:
            # Check for duplicate
            existing = db.query(CalendarSource).filter(
                CalendarSource.ical_url == update_data.ical_url,
                CalendarSource.id != source_id
            ).first()
            if existing:
                raise HTTPException(status_code=409, detail="URL already in use")
            source.ical_url = update_data.ical_url
        if update_data.enabled is not None:
            source.enabled = update_data.enabled
        if update_data.sync_interval_minutes is not None:
            source.sync_interval_minutes = update_data.sync_interval_minutes
        if update_data.priority is not None:
            source.priority = update_data.priority
        if update_data.description is not None:
            source.description = update_data.description

        db.commit()
        db.refresh(source)

        logger.info("calendar_source_updated",
                   source_id=source_id,
                   name=source.name)

        return source.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_update_calendar_source", error=str(e), source_id=source_id)
        raise HTTPException(status_code=500, detail="Failed to update calendar source")


@router.delete("/{source_id}", status_code=204)
async def delete_calendar_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a calendar source."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()

        if not source:
            raise HTTPException(status_code=404, detail="Calendar source not found")

        logger.info("calendar_source_deleted",
                   user=current_user.username,
                   source_id=source_id,
                   name=source.name)

        db.delete(source)
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_delete_calendar_source", error=str(e), source_id=source_id)
        raise HTTPException(status_code=500, detail="Failed to delete calendar source")


# ============================================================================
# Sync and Test Endpoints
# ============================================================================

@router.post("/{source_id}/test", response_model=TestConnectionResponse)
async def test_calendar_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Test connectivity and parsing for a calendar source.

    Fetches the iCal URL and attempts to parse events without saving.
    Returns sample events for verification.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Calendar source not found")

    try:
        # Fetch iCal data
        ical_data = await fetch_ical_data(source.ical_url)

        # Parse events
        events = parse_ical_events(ical_data, source.source_type)

        # Get sample events (next 3 upcoming)
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            [e for e in events if e['checkin'] > now],
            key=lambda x: x['checkin']
        )[:3]

        sample_events = [
            {
                'title': e['title'],
                'checkin': e['checkin'].isoformat(),
                'checkout': e['checkout'].isoformat(),
                'guest_name': e['guest_name']
            }
            for e in upcoming
        ]

        logger.info("calendar_source_test_success",
                   user=current_user.username,
                   source_id=source_id,
                   event_count=len(events))

        return TestConnectionResponse(
            success=True,
            message=f"Successfully connected and found {len(events)} events",
            event_count=len(events),
            sample_events=sample_events
        )

    except httpx.HTTPError as e:
        logger.warning("calendar_source_test_http_error",
                      source_id=source_id,
                      error=str(e))
        return TestConnectionResponse(
            success=False,
            message=f"HTTP error connecting to iCal URL: {str(e)}"
        )
    except Exception as e:
        logger.error("calendar_source_test_failed",
                    source_id=source_id,
                    error=str(e))
        return TestConnectionResponse(
            success=False,
            message=f"Failed to parse iCal data: {str(e)}"
        )


@router.post("/test-url", response_model=TestConnectionResponse)
async def test_ical_url(
    url: str = Query(..., description="iCal URL to test"),
    source_type: str = Query("generic_ical", description="Source type for parsing"),
    current_user: User = Depends(get_current_user)
):
    """
    Test an iCal URL before creating a source.

    Does not require saving the source first.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        # Fetch iCal data
        ical_data = await fetch_ical_data(url)

        # Parse events
        events = parse_ical_events(ical_data, source_type)

        # Get sample events
        now = datetime.now(timezone.utc)
        upcoming = sorted(
            [e for e in events if e['checkin'] > now],
            key=lambda x: x['checkin']
        )[:3]

        sample_events = [
            {
                'title': e['title'],
                'checkin': e['checkin'].isoformat(),
                'checkout': e['checkout'].isoformat(),
                'guest_name': e['guest_name']
            }
            for e in upcoming
        ]

        logger.info("ical_url_test_success",
                   user=current_user.username,
                   event_count=len(events))

        return TestConnectionResponse(
            success=True,
            message=f"Successfully connected and found {len(events)} events",
            event_count=len(events),
            sample_events=sample_events
        )

    except httpx.HTTPError as e:
        return TestConnectionResponse(
            success=False,
            message=f"HTTP error: {str(e)}"
        )
    except Exception as e:
        return TestConnectionResponse(
            success=False,
            message=f"Failed to parse iCal: {str(e)}"
        )


@router.post("/{source_id}/sync", response_model=SyncResponse)
async def sync_calendar_source(
    source_id: int,
    db: Session = Depends(get_db)
):
    """
    Manually trigger a sync for a calendar source.

    Fetches events from the iCal URL and updates the database.
    For Lodgify sources, uses API if key is available for full guest names.

    Note: This endpoint does not require authentication to allow
    automated syncing and CLI access.
    """

    source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()
    if not source:
        raise HTTPException(status_code=404, detail="Calendar source not found")

    try:
        events = []
        sync_method = 'ical'

        # For Lodgify sources, try API first for full guest details
        if source.source_type == 'lodgify':
            lodgify_api_key = get_lodgify_api_key(db)
            if lodgify_api_key:
                try:
                    # Use configurable check-in/out times from the source
                    checkin_time = source.default_checkin_time or '16:00'
                    checkout_time = source.default_checkout_time or '11:00'
                    events = await fetch_lodgify_reservations(
                        lodgify_api_key,
                        checkin_time=checkin_time,
                        checkout_time=checkout_time
                    )
                    sync_method = 'lodgify_api'
                    logger.info("lodgify_api_sync",
                               source_id=source_id,
                               event_count=len(events),
                               checkin_time=checkin_time,
                               checkout_time=checkout_time)
                except Exception as api_error:
                    logger.warning("lodgify_api_failed_fallback_to_ical",
                                  source_id=source_id,
                                  error=str(api_error))
                    # Fall back to iCal
                    ical_data = await fetch_ical_data(source.ical_url)
                    events = parse_ical_events(ical_data, source.source_type)
            else:
                # No API key, use iCal
                ical_data = await fetch_ical_data(source.ical_url)
                events = parse_ical_events(ical_data, source.source_type)
        else:
            # Non-Lodgify sources use iCal
            ical_data = await fetch_ical_data(source.ical_url)
            events = parse_ical_events(ical_data, source.source_type)

        added = 0
        updated = 0

        # Process each event
        for event_data in events:
            existing = db.query(CalendarEvent).filter(
                CalendarEvent.external_id == event_data['external_id']
            ).first()

            if existing:
                # Update existing event
                existing.title = event_data['title']
                existing.checkin = event_data['checkin']
                existing.checkout = event_data['checkout']
                existing.guest_name = event_data['guest_name']
                existing.guest_phone = event_data.get('guest_phone')
                existing.guest_email = event_data.get('guest_email')
                existing.notes = event_data['notes']
                existing.source = event_data['source']
                existing.source_id = source.id
                existing.synced_at = datetime.now(timezone.utc)
                updated += 1
            else:
                # Create new event
                new_event = CalendarEvent(
                    external_id=event_data['external_id'],
                    title=event_data['title'],
                    checkin=event_data['checkin'],
                    checkout=event_data['checkout'],
                    guest_name=event_data['guest_name'],
                    guest_phone=event_data.get('guest_phone'),
                    guest_email=event_data.get('guest_email'),
                    notes=event_data['notes'],
                    source=event_data['source'],
                    source_id=source.id,
                    status=event_data.get('status', 'confirmed'),
                    created_by=f'{sync_method}_sync',
                    synced_at=datetime.now(timezone.utc)
                )
                db.add(new_event)
                added += 1

        # Update source status
        source.last_sync_at = datetime.now(timezone.utc)
        source.last_sync_status = 'success'
        source.last_sync_error = None
        source.last_event_count = len(events)

        db.commit()

        logger.info("calendar_source_sync_complete",
                   source_id=source_id,
                   sync_method=sync_method,
                   events_total=len(events),
                   added=added,
                   updated=updated)

        # Auto-sync to guest sessions for Lodgify sources
        if source.source_type == 'lodgify':
            await sync_lodgify_to_guest_sessions(db)
            await update_guest_session_statuses(db)

        return SyncResponse(
            success=True,
            message=f"Sync completed successfully via {sync_method}",
            events_synced=len(events),
            events_added=added,
            events_updated=updated
        )

    except Exception as e:
        # Update source with error
        source.last_sync_at = datetime.now(timezone.utc)
        source.last_sync_status = 'failed'
        source.last_sync_error = str(e)
        db.commit()

        logger.error("calendar_source_sync_failed",
                    source_id=source_id,
                    error=str(e))

        return SyncResponse(
            success=False,
            message=f"Sync failed: {str(e)}"
        )


@router.post("/sync-all", response_model=dict)
async def sync_all_calendar_sources(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Trigger a sync for all enabled calendar sources.

    Runs in the background to avoid timeout on large syncs.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Get all enabled sources
    sources = db.query(CalendarSource).filter(CalendarSource.enabled == True).all()

    logger.info("sync_all_triggered",
               user=current_user.username,
               source_count=len(sources))

    return {
        "message": f"Sync triggered for {len(sources)} enabled sources",
        "source_count": len(sources),
        "sources": [{"id": s.id, "name": s.name} for s in sources]
    }


# ============================================================================
# Guest Session Sync
# ============================================================================

def determine_session_status(check_in_date, check_out_date) -> str:
    """Determine guest session status based on dates."""
    from datetime import date
    today = date.today()

    if isinstance(check_in_date, datetime):
        check_in_date = check_in_date.date()
    if isinstance(check_out_date, datetime):
        check_out_date = check_out_date.date()

    if check_out_date < today:
        return 'completed'
    elif check_in_date <= today <= check_out_date:
        return 'active'
    else:
        return 'upcoming'


async def sync_lodgify_to_guest_sessions(db: Session):
    """
    Sync Lodgify calendar events to guest_sessions table.

    Called automatically when Lodgify calendar syncs.
    Creates/updates guest sessions from booking events.
    """
    try:
        from app.models import GuestSession
    except ImportError:
        logger.warning("guest_session_model_not_found")
        return {"synced": 0, "error": "GuestSession model not imported"}

    try:
        # Get all Lodgify booking events
        events = db.query(CalendarEvent).join(CalendarSource).filter(
            CalendarSource.source_type == 'lodgify',
            CalendarEvent.status == 'confirmed'
        ).all()

        synced = 0
        for event in events:
            if not event.external_id:
                continue

            # Check if guest session already exists
            existing = db.query(GuestSession).filter(
                GuestSession.lodgify_booking_id == event.external_id
            ).first()

            # Get check-in/check-out dates
            check_in = event.checkin.date() if hasattr(event.checkin, 'date') else event.checkin
            check_out = event.checkout.date() if hasattr(event.checkout, 'date') else event.checkout

            if not existing:
                # Create new guest session
                new_session = GuestSession(
                    calendar_event_id=event.id,
                    lodgify_booking_id=event.external_id,
                    guest_name=event.guest_name or event.title or 'Guest',
                    guest_email=event.guest_email,
                    check_in_date=check_in,
                    check_out_date=check_out,
                    status=determine_session_status(check_in, check_out)
                )
                db.add(new_session)
                synced += 1
                logger.info("guest_session_created",
                           booking_id=event.external_id,
                           guest_name=new_session.guest_name)
            else:
                # Update existing session
                existing.guest_name = event.guest_name or event.title or existing.guest_name
                existing.guest_email = event.guest_email or existing.guest_email
                existing.check_in_date = check_in
                existing.check_out_date = check_out
                existing.status = determine_session_status(check_in, check_out)
                existing.calendar_event_id = event.id
                synced += 1

        db.commit()
        logger.info("lodgify_guest_sessions_synced", count=synced)

        return {"synced": synced}

    except Exception as e:
        db.rollback()
        logger.error("guest_session_sync_failed", error=str(e))
        return {"synced": 0, "error": str(e)}


async def update_guest_session_statuses(db: Session):
    """Update guest session statuses based on current date."""
    try:
        from app.models import GuestSession
        from datetime import date

        today = date.today()

        # Upcoming -> Active (check-in day reached)
        db.query(GuestSession).filter(
            GuestSession.status == 'upcoming',
            GuestSession.check_in_date <= today
        ).update({
            'status': 'active',
            'actual_check_in': datetime.now(timezone.utc)
        })

        # Active -> Completed (check-out day passed)
        db.query(GuestSession).filter(
            GuestSession.status == 'active',
            GuestSession.check_out_date < today
        ).update({
            'status': 'completed',
            'actual_check_out': datetime.now(timezone.utc)
        })

        db.commit()
        logger.info("guest_session_statuses_updated")

    except Exception as e:
        db.rollback()
        logger.error("guest_session_status_update_failed", error=str(e))


@router.post("/sync-guest-sessions")
async def sync_guest_sessions_endpoint(
    db: Session = Depends(get_db)
):
    """
    Manually sync Lodgify events to guest sessions.

    This endpoint allows triggering the sync independently of calendar sync.
    """
    result = await sync_lodgify_to_guest_sessions(db)
    await update_guest_session_statuses(db)
    return result
