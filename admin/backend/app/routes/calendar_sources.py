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
from typing import List, Literal, Optional
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, BackgroundTasks, Request
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import structlog
import httpx

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import AuditLog, User, CalendarSource, CalendarEvent, ExternalAPIKey, mask_feed_url
from app.utils.service_auth import require_user_permission
from shared.config import get_config
from shared.booking_window import (
    DEFAULT_CHECKIN_TIME,
    DEFAULT_CHECKOUT_TIME,
    classify_summary,
    feed_value_to_utc,
    localize_stay,
    resolve_property_tz,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/calendar-sources", tags=["calendar-sources"])

# Lodgify API endpoint
LODGIFY_API_BASE = "https://api.lodgify.com"

# A sync interval below this can't give the deleted/cancelled-entry match
# rule a real "previous sync" to compare against (see calendar_sync).
MIN_SYNC_INTERVAL_MINUTES = 5

VALID_SOURCE_TYPES = ('airbnb', 'vrbo', 'lodgify', 'generic_ical')


# ============================================================================
# Pydantic Schemas
# ============================================================================

class CalendarSourceCreate(BaseModel):
    """Schema for creating a new calendar source."""
    name: str
    source_type: str  # 'airbnb', 'vrbo', 'lodgify', 'generic_ical'
    ical_url: str
    enabled: bool = True
    sync_interval_minutes: int = Field(default=30, ge=MIN_SYNC_INTERVAL_MINUTES)
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
    sync_interval_minutes: Optional[int] = Field(default=None, ge=MIN_SYNC_INTERVAL_MINUTES)
    priority: Optional[int] = None
    default_checkin_time: Optional[str] = None
    default_checkout_time: Optional[str] = None
    description: Optional[str] = None


class CalendarSourceResponse(BaseModel):
    """Schema for calendar source response."""
    id: int
    name: str
    source_type: str
    # Only GET /{id} sets this; list/create/update omit the key entirely
    # (response_model_exclude_unset), so the feed token never leaves there.
    ical_url: Optional[str] = None
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
    events_matched_deleted: int = 0
    events_rekeyed: int = 0


# ============================================================================
# Helper Functions
# ============================================================================

def is_lodgify_host(url: Optional[str]) -> bool:
    """True when the URL's host is lodgify.com or a subdomain of it."""
    try:
        host = (urlsplit(url or '').hostname or '').lower()
    except ValueError:
        return False
    return host == 'lodgify.com' or host.endswith('.lodgify.com')


def lodgify_key_enabled(db: Session) -> bool:
    return db.query(ExternalAPIKey.id).filter(
        ExternalAPIKey.service_name == 'lodgify',
        ExternalAPIKey.enabled == True,  # noqa: E712
    ).first() is not None


def _validate_feed_url(url: str, current: Optional[str] = None) -> None:
    """400 unless ``url`` is a full https feed URL. Rejects the masked value
    the source card shows (anything containing the mask's ellipsis), so a
    form that posts the display value back can't overwrite the real URL."""
    if not url or not url.strip():
        raise HTTPException(status_code=400, detail="iCal URL is required")
    if '…' in url or (current and url == mask_feed_url(current)):
        raise HTTPException(
            status_code=400,
            detail="That is the masked URL shown on the card. Paste the full iCal URL instead.",
        )
    try:
        scheme = urlsplit(url).scheme.lower()
    except ValueError:
        scheme = ''
    if scheme != 'https':
        raise HTTPException(status_code=400, detail="iCal URL must use https://")


def _enforce_lodgify_type_lock(db: Session, *, current_type: Optional[str], new_type: str, url: str) -> None:
    """While a Lodgify API key is enabled, a Lodgify source can't be turned
    into another type, and a lodgify.com feed can't be filed under one: the
    sync treats both as API-authoritative, and a type change would silently
    switch it back to writing from the iCal export."""
    if not lodgify_key_enabled(db):
        return
    moving_off = current_type == 'lodgify' and new_type != 'lodgify'
    mispaired = new_type != 'lodgify' and is_lodgify_host(url)
    if moving_off or mispaired:
        raise HTTPException(
            status_code=409,
            detail=(
                "lodgify_source_type_locked: a Lodgify API key is enabled, so a "
                "Lodgify feed must stay a Lodgify source. Disable the key first."
            ),
        )


def _audit(db: Session, user: User, request: Optional[Request], action: str,
           source_id: Optional[int], old_value: Optional[dict], new_value: Optional[dict]) -> None:
    """Stage an audit row in the caller's transaction (committed with the
    change it records). Values come from ``to_dict_safe``, so no feed URL."""
    db.add(AuditLog(
        user_id=user.id,
        action=action,
        resource_type='calendar_source',
        resource_id=source_id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get('user-agent') if request else None,
        success=True,
    ))

def safe_error(exc: BaseException) -> dict:
    """The only form in which an exception reaches a log line or a status
    string in the calendar modules: its class and, when it has one, its
    HTTP status. Never the text -- httpx messages embed the feed URL (and
    its token), SQLAlchemy errors embed bound parameters."""
    http_status = None
    if isinstance(exc, httpx.HTTPStatusError):
        http_status = exc.response.status_code
    elif isinstance(exc, HTTPException):
        http_status = exc.status_code
    return {"error_class": type(exc).__name__, "http_status": http_status}


def describe_error(exc: BaseException) -> str:
    """`<Class>` or `<Class> HTTP <n>`, for user-facing status text."""
    info = safe_error(exc)
    suffix = f" HTTP {info['http_status']}" if info['http_status'] is not None else ""
    return f"{info['error_class']}{suffix}"


KeyStatus = Literal["absent", "ok", "unreadable"]
_multiple_keys_logged: set = set()


def resolve_lodgify_api_key(db: Session) -> tuple:
    """(status, key) for the Lodgify API key. Fails closed:

    - no enabled `lodgify` row -> ("absent", None);
    - an empty ciphertext, a decrypt error, or a decrypted None/blank value
      on ANY enabled row -> ("unreadable", None);
    - a query error -> ("unreadable", None);
    - otherwise ("ok", key of the lowest-id enabled row).

    "unreadable" means a key is configured but can't be used: the caller
    must write nothing rather than fall back to the iCal export. No key
    material is ever logged.
    """
    from app.utils import encryption

    try:
        rows = (
            db.query(ExternalAPIKey)
            .filter(ExternalAPIKey.service_name == 'lodgify', ExternalAPIKey.enabled == True)  # noqa: E712
            .order_by(ExternalAPIKey.id)
            .all()
        )
    except Exception as exc:
        logger.error("lodgify_api_key_query_failed", **safe_error(exc))
        db.rollback()
        return "unreadable", None

    if not rows:
        return "absent", None

    keys = []
    for row in rows:
        if not row.api_key_encrypted:
            logger.error("lodgify_api_key_unreadable", key_id=row.id, reason="empty_ciphertext")
            return "unreadable", None
        try:
            plaintext = encryption.decrypt_value(row.api_key_encrypted)
        except Exception as exc:
            logger.error("lodgify_api_key_unreadable", key_id=row.id, reason="decrypt_failed", **safe_error(exc))
            return "unreadable", None
        if plaintext is None or not str(plaintext).strip():
            logger.error("lodgify_api_key_unreadable", key_id=row.id, reason="blank")
            return "unreadable", None
        keys.append(plaintext)

    if len(rows) > 1:
        ids = tuple(r.id for r in rows)
        if ids not in _multiple_keys_logged:
            _multiple_keys_logged.add(ids)
            logger.warning("lodgify_api_key_multiple_enabled", key_ids=list(ids), using=ids[0])
    return "ok", keys[0]


async def fetch_lodgify_reservations(
    api_key: str,
    timeout: float = 30.0,
    checkin_time: str = DEFAULT_CHECKIN_TIME,
    checkout_time: str = DEFAULT_CHECKOUT_TIME
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

    # ATHENA-127 D4/D5: house-local check-in/out times, localised in the
    # property timezone rather than stamped as UTC (the pre-fix defect).
    property_tz, _ = resolve_property_tz(get_config().default_timezone)

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

                # Convert to datetime using configurable check-in/out times,
                # localised in the property timezone (ATHENA-127 D4/D5).
                try:
                    arrival_date = datetime.strptime(arrival, '%Y-%m-%d').date()
                    departure_date = datetime.strptime(departure, '%Y-%m-%d').date()
                    checkin, checkout = localize_stay(
                        arrival_date, departure_date, checkin_time, checkout_time, property_tz
                    )
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
    """Fetch iCal data from a URL.

    Security (ATHENA-59 Phase 0 / 0.3a):
    - HTTPS-only at intake (D4) and on every redirect hop (per-hop ``allowed_schemes``).
    - Per-hop SSRF re-validation via ``safe_get`` (xander Blocker 1).
    - ~10MB response-size cap via safe_get's max_bytes.
    - Caller-owned allowlist passed in; validator never reads env (D9).
    """
    import sys as _sys
    import os as _os
    # Resolve shared/ for both in-tree and installed layouts.
    _shared = _os.path.join(_os.path.dirname(__file__), '..', '..', '..', '..', 'src', 'shared')
    if _os.path.isdir(_shared) and _shared not in _sys.path:
        _sys.path.insert(0, _os.path.dirname(_shared))
    from shared.url_safety import safe_get, SsrfBlockedError
    from shared.config import get_config
    from urllib.parse import urlparse as _urlparse

    # D4: Reject non-HTTPS URLs at intake before any network activity.
    if _urlparse(url).scheme.lower() != "https":
        raise HTTPException(status_code=400, detail="iCal URL must use https://")

    cfg = get_config()
    allowlist = (
        [h for h in cfg.sitescraper_allowed_private_hosts.split(",") if h.strip()]
        if cfg.sitescraper_allowed_private_hosts
        else []
    )

    try:
        response = await safe_get(
            url,
            allowed_schemes=frozenset({"https"}),  # enforce HTTPS on every hop
            allowed_private_hosts=allowlist,
            timeout=timeout,
            headers={"User-Agent": "Athena-Calendar-Sync/1.0"},
        )
    except SsrfBlockedError:
        raise HTTPException(status_code=400, detail="iCal URL host is not allowed")

    response.raise_for_status()
    return response.text


def parse_ical_events(
    ical_data: str,
    source_type: str,
    checkin_time: str = DEFAULT_CHECKIN_TIME,
    checkout_time: str = DEFAULT_CHECKOUT_TIME,
) -> List[dict]:
    """
    Parse iCal data and extract events.

    Returns a list of event dictionaries with:
    - external_id (UID)
    - title (SUMMARY)
    - checkin (DTSTART)
    - checkout (DTEND)
    - guest_name (extracted from SUMMARY/DESCRIPTION)
    - notes (DESCRIPTION)
    - status ('confirmed' or 'blocked' -- ATHENA-127 D11)

    Date-only values and floating (no Z/TZID) DATE-TIMEs are localised in
    DEFAULT_TIMEZONE (ATHENA-127 D4/D5) via shared.booking_window, using
    checkin_time/checkout_time for date-only values.
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
    property_tz, _ = resolve_property_tz(get_config().default_timezone)

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

            # ATHENA-127 D4/D5: date-only and floating values are localised
            # in the property timezone rather than stamped as UTC.
            checkin = feed_value_to_utc(
                dtstart.dt, default_hhmm=checkin_time, tz=property_tz
            )
            checkout = feed_value_to_utc(
                dtend.dt, default_hhmm=checkout_time, tz=property_tz
            )

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
                'source': source_type,
                'status': classify_summary(summary, source_type=source_type),
            })

    return events


# ============================================================================
# CRUD Endpoints
# ============================================================================

@router.get("", response_model=List[CalendarSourceResponse], response_model_exclude_unset=True)
async def list_calendar_sources(
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_permission('read')),
):
    """List all calendar sources. The feed URL is masked; GET /{id} has it."""
    try:
        query = db.query(CalendarSource)

        if enabled is not None:
            query = query.filter(CalendarSource.enabled == enabled)

        query = query.order_by(CalendarSource.priority.desc(), CalendarSource.name)
        sources = query.all()

        logger.info("calendar_sources_listed", count=len(sources), enabled=enabled)

        return [source.to_dict_safe() for source in sources]

    except Exception as e:
        logger.error("failed_to_list_calendar_sources", **safe_error(e))
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
        logger.error("failed_to_get_calendar_source", source_id=source_id, **safe_error(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve calendar source")


@router.post("", response_model=CalendarSourceResponse, status_code=201, response_model_exclude_unset=True)
async def create_calendar_source(
    source_data: CalendarSourceCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new calendar source."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        if source_data.source_type not in VALID_SOURCE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid source_type. Must be one of: {', '.join(VALID_SOURCE_TYPES)}"
            )

        _validate_feed_url(source_data.ical_url)
        _enforce_lodgify_type_lock(
            db, current_type=None, new_type=source_data.source_type, url=source_data.ical_url,
        )

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
        db.flush()
        _audit(db, current_user, request, 'calendar_source_created', new_source.id,
               None, new_source.to_dict_safe())
        db.commit()
        db.refresh(new_source)

        logger.info("calendar_source_created",
                   user=current_user.username,
                   source_id=new_source.id,
                   name=new_source.name,
                   source_type=new_source.source_type)

        return new_source.to_dict_safe()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_create_calendar_source", **safe_error(e))
        raise HTTPException(status_code=500, detail="Failed to create calendar source")


@router.put("/{source_id}", response_model=CalendarSourceResponse, response_model_exclude_unset=True)
async def update_calendar_source(
    source_id: int,
    update_data: CalendarSourceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_permission('write')),
):
    """Update a calendar source. Only fields present in the body change."""

    try:
        source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()

        if not source:
            raise HTTPException(status_code=404, detail="Calendar source not found")

        old_value = source.to_dict_safe()

        if update_data.source_type is not None and update_data.source_type not in VALID_SOURCE_TYPES:
            raise HTTPException(status_code=400, detail="Invalid source_type")
        if update_data.ical_url is not None:
            _validate_feed_url(update_data.ical_url, current=source.ical_url)
        if update_data.source_type is not None or update_data.ical_url is not None:
            _enforce_lodgify_type_lock(
                db,
                current_type=source.source_type,
                new_type=update_data.source_type if update_data.source_type is not None else source.source_type,
                url=update_data.ical_url if update_data.ical_url is not None else source.ical_url,
            )

        # Update fields
        if update_data.name is not None:
            source.name = update_data.name
        if update_data.source_type is not None:
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

        _audit(db, current_user, request, 'calendar_source_updated', source.id,
               old_value, source.to_dict_safe())
        db.commit()
        db.refresh(source)

        logger.info("calendar_source_updated",
                   user=current_user.username,
                   source_id=source_id,
                   name=source.name)

        return source.to_dict_safe()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_update_calendar_source", source_id=source_id, **safe_error(e))
        raise HTTPException(status_code=500, detail="Failed to update calendar source")


@router.delete("/{source_id}", status_code=204)
async def delete_calendar_source(
    source_id: int,
    request: Request,
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

        _audit(db, current_user, request, 'calendar_source_deleted', source.id,
               source.to_dict_safe(), None)
        db.delete(source)
        db.commit()

        return None

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_delete_calendar_source", source_id=source_id, **safe_error(e))
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
        events = parse_ical_events(
            ical_data,
            source.source_type,
            checkin_time=source.default_checkin_time or DEFAULT_CHECKIN_TIME,
            checkout_time=source.default_checkout_time or DEFAULT_CHECKOUT_TIME,
        )

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
        logger.warning("calendar_source_test_http_error", source_id=source_id, **safe_error(e))
        return TestConnectionResponse(
            success=False,
            message=f"HTTP error connecting to iCal URL ({describe_error(e)})"
        )
    except Exception as e:
        logger.error("calendar_source_test_failed", source_id=source_id, **safe_error(e))
        return TestConnectionResponse(
            success=False,
            message=f"Failed to fetch or parse iCal data ({describe_error(e)})"
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
        logger.warning("ical_url_test_http_error", **safe_error(e))
        return TestConnectionResponse(
            success=False,
            message=f"HTTP error ({describe_error(e)})"
        )
    except Exception as e:
        logger.warning("ical_url_test_failed", **safe_error(e))
        return TestConnectionResponse(
            success=False,
            message=f"Failed to fetch or parse iCal ({describe_error(e)})"
        )


@router.post("/{source_id}/sync", response_model=SyncResponse)
async def sync_calendar_source(
    source_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_permission('write')),
):
    """
    Manually trigger a sync for a calendar source.

    Fetches events from the iCal URL and updates the database.
    For Lodgify sources, uses API if key is available for full guest names.
    """

    from app.services.calendar_sync import run_source_sync

    outcome = await run_source_sync(source_id, db, trigger="manual")
    if outcome.status == "not_found":
        raise HTTPException(status_code=404, detail="Calendar source not found")

    return SyncResponse(
        success=outcome.status == "success",
        message=_sync_message(outcome),
        events_synced=outcome.events_total,
        events_added=outcome.added,
        events_updated=outcome.updated,
        events_matched_deleted=outcome.matched_deleted,
        events_rekeyed=outcome.rekeyed,
    )


SYNC_BUSY_MESSAGE = "A sync for this source is already running"


def _sync_message(outcome) -> str:
    """User-facing sync result. Never starts with "Sync failed" (the admin
    UI adds its own prefix) and never carries exception text."""
    if outcome.status == "success":
        message = f"Synced via {outcome.method}"
        return f"{message}. {outcome.warning}" if outcome.warning else message
    if outcome.status == "busy":
        return SYNC_BUSY_MESSAGE
    return outcome.error or "The sync did not complete; no changes written"


@router.post("/sync-all", response_model=dict)
async def sync_all_calendar_sources(
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Trigger a sync for all enabled calendar sources.

    Runs in the background to avoid timeout on large syncs. Each source is
    synced in its own fresh DB session (ATHENA-127 bob H3d): the request
    session in `db` above is gone by the time these background tasks run.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    from app.services.calendar_sync import sync_source_in_new_session

    # Get all enabled sources
    sources = db.query(CalendarSource).filter(CalendarSource.enabled == True).all()

    for s in sources:
        background_tasks.add_task(sync_source_in_new_session, s.id)

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

def _today() -> date:
    """The house's calendar date for session status. A seam for tests."""
    return date.today()


def determine_session_status(check_in_date, check_out_date) -> str:
    """Determine guest session status based on dates."""
    today = _today()

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
        # Live Lodgify bookings only: a deleted event never gets a session.
        events = db.query(CalendarEvent).join(CalendarSource).filter(
            CalendarSource.source_type == 'lodgify',
            CalendarEvent.status == 'confirmed',
            CalendarEvent.deleted_at.is_(None),
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
                # Load-bearing: re-deriving the status here is what restores a
                # session the cancel pass below cancelled once its event is
                # live again. It also revives a session someone cancelled
                # directly while its event stays live (pre-existing).
                existing.status = determine_session_status(check_in, check_out)
                existing.calendar_event_id = event.id
                synced += 1

        cancelled = _cancel_sessions_for_gone_events(db)

        db.commit()
        logger.info("lodgify_guest_sessions_synced", count=synced, cancelled=cancelled)

        return {"synced": synced, "cancelled": cancelled}

    except Exception as e:
        db.rollback()
        logger.error("guest_session_sync_failed", **safe_error(e))
        return {"synced": 0, "error": describe_error(e)}


def _cancel_sessions_for_gone_events(db: Session) -> int:
    """Cancel upcoming/active sessions whose Lodgify event is gone (deleted,
    or no longer confirmed). Only sessions linked to an event are touched:
    manual sessions have no event, and completed sessions stay completed.
    Memories are left alone. Staged in the caller's transaction."""
    from sqlalchemy import or_
    from app.models import GuestSession

    sessions = (
        db.query(GuestSession)
        .join(CalendarEvent, GuestSession.calendar_event_id == CalendarEvent.id)
        .join(CalendarSource, CalendarEvent.source_id == CalendarSource.id)
        .filter(
            CalendarSource.source_type == 'lodgify',
            GuestSession.status.in_(('upcoming', 'active')),
            or_(CalendarEvent.deleted_at.isnot(None), CalendarEvent.status != 'confirmed'),
        )
        .all()
    )
    for session in sessions:
        session.status = 'cancelled'
        logger.info("guest_session_cancelled_event_gone", session_id=session.id, event_id=session.calendar_event_id)
    return len(sessions)


async def update_guest_session_statuses(db: Session):
    """Update guest session statuses based on current date."""
    try:
        from app.models import GuestSession

        today = _today()

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
        logger.error("guest_session_status_update_failed", **safe_error(e))


@router.post("/sync-guest-sessions")
async def sync_guest_sessions_endpoint(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_permission('write')),
):
    """
    Manually sync Lodgify events to guest sessions.

    This endpoint allows triggering the sync independently of calendar sync.
    """
    result = await sync_lodgify_to_guest_sessions(db)
    await update_guest_session_statuses(db)
    return result
