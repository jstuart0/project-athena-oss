"""
Mode Service - Guest Mode Detection and Management

Polls Airbnb iCal calendar, detects active stays, and determines current mode (guest/owner).
Provides API for orchestrator to query current mode and permissions.

API Endpoints:
- GET /health - Health check
- GET /mode - Get current mode (guest/owner)
- GET /mode/permissions - Get current permissions for mode
- POST /mode/override - Manually override mode (voice PIN)
- GET /mode/events - Get current calendar events
"""
import os
import asyncio
import hashlib
import secrets
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

import httpx
from icalendar import Calendar
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Add to Python path for imports
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.logging_config import configure_logging
from shared.cache import CacheClient

# Configure logging
logger = configure_logging("mode-service")

# Environment variables
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "http://localhost:5000")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("MODE_SERVICE_PORT", "8021"))
POLL_INTERVAL_SECONDS = int(os.getenv("CALENDAR_POLL_INTERVAL_SECONDS", "600"))  # 10 minutes

# Global state
cache: Optional[CacheClient] = None
current_config: Dict[str, Any] = {}
current_events: List[Dict[str, Any]] = []
current_mode = "owner"  # Safe default - owner mode
active_override: Optional[Dict[str, Any]] = None


# Pydantic models
class ModeResponse(BaseModel):
    """Response for current mode query."""
    mode: str  # 'guest' or 'owner'
    reason: str
    override_active: bool
    events_count: int
    current_event: Optional[Dict[str, Any]] = None


class PermissionsResponse(BaseModel):
    """Response for current permissions query."""
    mode: str
    allowed_intents: List[str]
    restricted_entities: List[str]
    allowed_domains: List[str]
    max_queries_per_minute: int


class ModeOverrideRequest(BaseModel):
    """Request to override mode."""
    mode: str  # 'owner' or 'guest'
    voice_pin: Optional[str] = None
    timeout_minutes: Optional[int] = None
    voice_device_id: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache

    # Startup
    logger.info("mode_service.startup", msg="Starting Mode Service")
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # Load initial config
    await load_config()

    # Start background tasks
    asyncio.create_task(calendar_polling_loop())
    asyncio.create_task(config_refresh_loop())

    logger.info("mode_service.startup.complete", msg="Mode Service ready")

    yield

    # Shutdown
    logger.info("mode_service.shutdown", msg="Shutting down Mode Service")
    if cache:
        await cache.disconnect()


app = FastAPI(
    title="Mode Service",
    description="Guest mode detection and management via iCal calendar integration",
    version="1.0.0",
    lifespan=lifespan
)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "mode-service",
            "version": "1.0.0",
            "current_mode": current_mode,
            "events_loaded": len(current_events),
            "config_enabled": current_config.get('enabled', False)
        }
    )


@app.get("/mode", response_model=ModeResponse)
async def get_current_mode():
    """
    Get the current operating mode (guest or owner).

    Returns:
        ModeResponse with mode, reason, and current event details
    """
    current_event = get_current_event()

    return ModeResponse(
        mode=current_mode,
        reason=determine_mode_reason(),
        override_active=active_override is not None,
        events_count=len(current_events),
        current_event=current_event
    )


@app.get("/mode/permissions", response_model=PermissionsResponse)
async def get_permissions():
    """
    Get permissions for the current mode.

    Returns:
        PermissionsResponse with allowed intents, entities, and rate limits
    """
    if current_mode == "guest":
        return PermissionsResponse(
            mode="guest",
            allowed_intents=current_config.get('guest_allowed_intents', [
                'weather', 'time', 'general_info', 'news', 'recipes', 'streaming'
            ]),
            restricted_entities=current_config.get('guest_restricted_entities', [
                'lock.*', 'garage.*', 'alarm.*', 'camera.*'
            ]),
            allowed_domains=current_config.get('guest_allowed_domains', [
                'light', 'media_player', 'switch', 'scene', 'climate'
            ]),
            max_queries_per_minute=current_config.get('max_queries_per_minute_guest', 10)
        )
    else:
        # Owner mode - unrestricted
        return PermissionsResponse(
            mode="owner",
            allowed_intents=[],  # Empty = all allowed
            restricted_entities=[],  # Empty = none restricted
            allowed_domains=[],  # Empty = all allowed
            max_queries_per_minute=current_config.get('max_queries_per_minute_owner', 100)
        )


def verify_pin(input_pin: str) -> bool:
    """
    Verify a PIN against the stored hash from admin backend config.

    Args:
        input_pin: The 6-digit PIN provided by the user

    Returns:
        True if PIN matches, False otherwise
    """
    stored_hash = current_config.get('owner_pin')
    if not stored_hash:
        logger.warning("mode_service.pin.not_configured", msg="No owner PIN configured")
        return False

    # Hash the input PIN with SHA256 (same as admin backend)
    input_hash = hashlib.sha256(input_pin.encode()).hexdigest()

    # Use timing-safe comparison to prevent timing attacks
    return secrets.compare_digest(input_hash, stored_hash)


@app.post("/mode/override")
async def override_mode(request: ModeOverrideRequest):
    """
    Manually override the current mode (e.g., owner returning home during guest stay).

    Requires voice PIN verification for switching to owner mode.

    Args:
        request: ModeOverrideRequest with mode and optional PIN

    Returns:
        Success message with new mode

    Raises:
        HTTPException 401: If PIN is required but not provided
        HTTPException 403: If PIN verification fails
    """
    global current_mode, active_override

    # Verify PIN if switching to owner mode
    if request.mode == "owner":
        # Check if PIN is configured
        pin_configured = bool(current_config.get('owner_pin'))

        if pin_configured:
            # PIN is required
            if not request.voice_pin:
                logger.warning(
                    "mode_service.override.pin_required",
                    device=request.voice_device_id
                )
                raise HTTPException(
                    status_code=401,
                    detail="PIN required for owner mode override"
                )

            # Validate PIN format (6 digits)
            if not request.voice_pin.isdigit() or len(request.voice_pin) != 6:
                logger.warning(
                    "mode_service.override.invalid_pin_format",
                    device=request.voice_device_id
                )
                raise HTTPException(
                    status_code=400,
                    detail="PIN must be exactly 6 digits"
                )

            # Verify PIN against stored hash
            if not verify_pin(request.voice_pin):
                logger.warning(
                    "mode_service.override.pin_verification_failed",
                    device=request.voice_device_id
                )
                raise HTTPException(
                    status_code=403,
                    detail="Invalid PIN"
                )

            logger.info(
                "mode_service.override.pin_verified",
                device=request.voice_device_id
            )

    # Set override
    timeout_minutes = request.timeout_minutes or current_config.get('override_timeout_minutes', 60)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=timeout_minutes)

    active_override = {
        'mode': request.mode,
        'activated_at': datetime.now(timezone.utc),
        'expires_at': expires_at,
        'voice_device_id': request.voice_device_id
    }

    current_mode = request.mode

    logger.info(
        "mode_service.override.activated",
        mode=request.mode,
        expires_at=expires_at.isoformat(),
        device=request.voice_device_id
    )

    return {
        "success": True,
        "mode": current_mode,
        "expires_at": expires_at.isoformat(),
        "message": f"Mode override activated. Switching to {request.mode} mode for {timeout_minutes} minutes."
    }


@app.get("/mode/events")
async def get_events():
    """
    Get current calendar events.

    Returns:
        List of calendar events with checkin/checkout times
    """
    return {
        "events": current_events,
        "count": len(current_events),
        "current_event": get_current_event()
    }


async def load_config():
    """Load guest mode configuration from admin API."""
    global current_config

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ADMIN_API_URL}/api/guest-mode/config")
            response.raise_for_status()
            current_config = response.json()
            logger.info("mode_service.config.loaded", enabled=current_config.get('enabled'))
    except Exception as e:
        logger.warning(
            "mode_service.config.load_failed",
            error=str(e),
            msg="Using safe defaults"
        )
        # Use safe defaults
        current_config = {
            'enabled': False,
            'buffer_before_checkin_hours': 2,
            'buffer_after_checkout_hours': 1,
            'guest_allowed_intents': ['weather', 'time', 'general_info', 'news', 'recipes'],
            'guest_restricted_entities': ['lock.*', 'garage.*', 'alarm.*', 'camera.*'],
            'guest_allowed_domains': ['light', 'media_player', 'switch', 'scene', 'climate'],
            'max_queries_per_minute_guest': 10,
            'max_queries_per_minute_owner': 100,
        }


async def config_refresh_loop():
    """Periodically refresh configuration from admin API."""
    while True:
        await asyncio.sleep(60)  # Check every 60 seconds
        await load_config()


async def calendar_polling_loop():
    """Periodically poll iCal calendar for events."""
    global current_events, current_mode

    while True:
        try:
            if current_config.get('enabled') and current_config.get('calendar_url'):
                # Fetch iCal feed
                calendar_url = current_config['calendar_url']
                logger.info("mode_service.calendar.fetching", url=calendar_url[:50] + "...")

                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(calendar_url)
                    response.raise_for_status()

                    # Parse iCal
                    cal = Calendar.from_ical(response.content)
                    events = []

                    for component in cal.walk():
                        if component.name == "VEVENT":
                            dtstart = component.get('dtstart').dt
                            dtend = component.get('dtend').dt

                            # Convert to datetime with timezone if needed
                            if not isinstance(dtstart, datetime):
                                dtstart = datetime.combine(dtstart, datetime.min.time()).replace(tzinfo=timezone.utc)
                            if not isinstance(dtend, datetime):
                                dtend = datetime.combine(dtend, datetime.min.time()).replace(tzinfo=timezone.utc)

                            events.append({
                                'uid': str(component.get('uid')),
                                'summary': str(component.get('summary', '')),
                                'dtstart': dtstart,
                                'dtend': dtend,
                            })

                    current_events = events
                    logger.info("mode_service.calendar.loaded", count=len(events))

                    # Update current mode
                    current_mode = determine_mode()

        except Exception as e:
            logger.error("mode_service.calendar.fetch_failed", error=str(e), exc_info=True)

        # Wait for next poll
        poll_interval = current_config.get('calendar_poll_interval_minutes', 10) * 60
        await asyncio.sleep(poll_interval)


def determine_mode() -> str:
    """
    Determine current mode based on calendar events and overrides.

    Returns:
        'guest' or 'owner'
    """
    global active_override

    # Check for active override
    if active_override:
        if datetime.now(timezone.utc) < active_override['expires_at']:
            return active_override['mode']
        else:
            # Override expired
            active_override = None

    # If guest mode disabled, always owner mode
    if not current_config.get('enabled'):
        return "owner"

    # Check for active stay
    now = datetime.now(timezone.utc)
    buffer_before = timedelta(hours=current_config.get('buffer_before_checkin_hours', 2))
    buffer_after = timedelta(hours=current_config.get('buffer_after_checkout_hours', 1))

    for event in current_events:
        checkin = event['dtstart'] - buffer_before
        checkout = event['dtend'] + buffer_after

        if checkin <= now <= checkout:
            return "guest"

    return "owner"


def determine_mode_reason() -> str:
    """Get human-readable reason for current mode."""
    global active_override

    if active_override:
        return "Manual override via voice PIN"

    if not current_config.get('enabled'):
        return "Guest mode disabled"

    event = get_current_event()
    if event:
        return f"Active booking: {event['summary']}"

    return "No active bookings"


def get_current_event() -> Optional[Dict[str, Any]]:
    """Get the currently active calendar event, if any."""
    now = datetime.now(timezone.utc)
    buffer_before = timedelta(hours=current_config.get('buffer_before_checkin_hours', 2))
    buffer_after = timedelta(hours=current_config.get('buffer_after_checkout_hours', 1))

    for event in current_events:
        checkin = event['dtstart'] - buffer_before
        checkout = event['dtend'] + buffer_after

        if checkin <= now <= checkout:
            return {
                'summary': event['summary'],
                'checkin': event['dtstart'].isoformat(),
                'checkout': event['dtend'].isoformat(),
                'uid': event['uid']
            }

    return None


if __name__ == "__main__":
    import uvicorn

    port = SERVICE_PORT
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=True,
        log_config=None  # Use structlog configuration
    )
