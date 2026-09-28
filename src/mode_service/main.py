"""
Mode Service - Guest Mode Detection and Management

Polls Airbnb iCal calendar, detects active stays, and determines current mode (guest/owner).
Provides API for orchestrator to query current mode and permissions.

API Endpoints:
- GET /health - Health check (no auth)
- GET /mode - Get current mode (guest/owner/degraded)
- GET /mode/permissions - Get current permissions for mode (?mode=guest to force guest)
- POST /mode/override - Manually override mode (voice PIN, verified by the admin backend)
- GET /mode/events - Get current calendar events

Authentication (ATHENA-69 D15): every route below except /health requires
X-Service-Key, gated by `mode_service_ingress_auth`
(MODE_SERVICE_INGRESS_AUTH, default "enforce"; "warn" for rollout).

Owner PIN authority (ATHENA-69 D16/D25): this service holds no PIN state.
`POST /mode/override` for mode="owner" forwards the caller's PIN and trust
tier to the admin backend's `POST /api/internal/guest-mode/verify-pin`,
which owns the hash, the per-tier lockout counter, and the verdict. This
service never hashes or compares a PIN itself.

Cold start (ATHENA-69 D38): until the first successful admin-config load,
`/mode` reports mode="degraded" (never "owner") and `/health` reports
config_source="none", ready=false.
"""
import os
import time
import asyncio
from typing import Dict, Any, Optional, List, Literal
from datetime import datetime, timedelta, timezone
from contextlib import asynccontextmanager

import httpx
from icalendar import Calendar
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

# Add to Python path for imports
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from shared.logging_config import configure_logging
from shared.cache import CacheClient
from shared.admin_url import get_admin_url
from shared.config import get_config
from shared.guest_policy import apply_guest_baseline, guest_baseline
from shared.service_ingress_auth import make_require_service_caller

# Configure logging
logger = configure_logging("mode-service")

# Environment variables
ADMIN_API_URL = get_admin_url()
REDIS_URL = get_config().redis_url
SERVICE_PORT = int(os.getenv("MODE_SERVICE_PORT", "8021"))
POLL_INTERVAL_SECONDS = int(os.getenv("CALENDAR_POLL_INTERVAL_SECONDS", "600"))  # 10 minutes

_POSTURE_REMINDER_INTERVAL_SECONDS = 3600
_CONFIG_STALE_AFTER_SECONDS = 15 * 60
_CONFIG_STALE_LOG_INTERVAL_SECONDS = 10 * 60

# Ingress auth dependency (D15).
_require_caller = make_require_service_caller("mode_service_ingress_auth", "mode_service")

# Global state
cache: Optional[CacheClient] = None
current_config: Dict[str, Any] = {}
current_events: List[Dict[str, Any]] = []
current_mode = "degraded"  # Safe default until the first config load succeeds (D38)
active_override: Optional[Dict[str, Any]] = None

# Last-good config tracking (D26/D37/D38).
_config_loaded = False  # sticky True once any load has ever succeeded
_last_load_ok = False  # whether the MOST RECENT load attempt succeeded
_config_loaded_at: Optional[float] = None  # time.monotonic() of the last successful load
_service_key_warned = False  # log the unauthenticated-config-fetch warning once
_last_stale_log_at: Optional[float] = None

# Lazily-created admin HTTP client for PIN verification (module-level so tests
# can inject an httpx.MockTransport before calling override_mode).
_admin_http_client: Optional[httpx.AsyncClient] = None


def _default_config() -> Dict[str, Any]:
    """The built-in-defaults config used before any admin load has ever
    succeeded (D26 "using=defaults"). Guest lists come from the same
    shared floor/baseline the orchestrator uses (D8/D22), so a
    never-configured deployment and a mode-service-can't-reach-admin
    deployment converge on identical guest restrictions.
    """
    baseline = guest_baseline()
    return {
        "enabled": False,
        "buffer_before_checkin_hours": 2,
        "buffer_after_checkout_hours": 1,
        "guest_allowed_intents": baseline["allowed_intents"],
        "guest_restricted_entities": baseline["restricted_entities"],
        "guest_allowed_domains": baseline["allowed_domains"],
        "guest_restricted_intents": list(baseline.get("restricted_intents") or []),
        "max_queries_per_minute_guest": 10,
        "max_queries_per_minute_owner": 100,
        "owner_pin_configured": False,
    }


def _get_admin_http_client() -> httpx.AsyncClient:
    global _admin_http_client
    if _admin_http_client is None:
        _admin_http_client = httpx.AsyncClient(timeout=3.0)
    return _admin_http_client


# Pydantic models
class ModeResponse(BaseModel):
    """Response for current mode query."""
    mode: str  # 'guest', 'owner', or 'degraded'
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
    restricted_intents: List[str] = []
    max_queries_per_minute: int


class ModeOverrideRequest(BaseModel):
    """Request to override mode."""
    mode: str  # 'owner' or 'guest'
    voice_pin: Optional[str] = None
    timeout_minutes: Optional[int] = None
    voice_device_id: Optional[str] = None
    caller_tier: Optional[Literal["household", "sms", "web_authenticated"]] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache

    # Startup
    logger.info("mode_service.startup", msg="Starting Mode Service")
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    if not get_config().service_api_key and not get_config().dev_mode:
        logger.error("mode_service_service_api_key_unset")

    # Load initial config
    await load_config()

    # Start background tasks
    asyncio.create_task(calendar_polling_loop())
    asyncio.create_task(config_refresh_loop())
    asyncio.create_task(_posture_reminder_loop(_POSTURE_REMINDER_INTERVAL_SECONDS))

    logger.info("mode_service.startup.complete", msg="Mode Service ready")

    yield

    # Shutdown
    logger.info("mode_service.shutdown", msg="Shutting down Mode Service")
    if cache:
        await cache.disconnect()
    if _admin_http_client is not None:
        await _admin_http_client.aclose()


app = FastAPI(
    title="Mode Service",
    description="Guest mode detection and management via iCal calendar integration",
    version="1.0.0",
    lifespan=lifespan
)


def _config_source() -> str:
    if not _config_loaded:
        return "none"
    return "admin" if _last_load_ok else "last_good"


def _config_age_seconds() -> Optional[float]:
    if _config_loaded_at is None:
        return None
    return time.monotonic() - _config_loaded_at


@app.get("/health")
async def health_check():
    """Health check endpoint. Never behind ingress auth (D15)."""
    _refresh_mode()
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "mode-service",
            "version": "1.0.0",
            "current_mode": current_mode,
            "events_loaded": len(current_events),
            "config_enabled": current_config.get('enabled', False),
            "config_source": _config_source(),
            "config_age_seconds": _config_age_seconds(),
            "pin_authority": "admin",
            "ready": _config_loaded,
        }
    )


def _refresh_mode() -> None:
    """Recompute `current_mode` from live state (D26: cheap, in-memory, run
    on every read). Cold start (D38): never owner/guest until the first
    admin-config load has succeeded at least once.
    """
    global current_mode
    if not _config_loaded:
        current_mode = "degraded"
        return
    current_mode = determine_mode()


@app.get("/mode", response_model=ModeResponse, dependencies=[Depends(_require_caller)])
async def get_current_mode():
    """
    Get the current operating mode (guest, owner, or degraded).

    Returns:
        ModeResponse with mode, reason, and current event details
    """
    _refresh_mode()
    current_event = get_current_event()

    return ModeResponse(
        mode=current_mode,
        reason=determine_mode_reason(),
        override_active=active_override is not None,
        events_count=len(current_events),
        current_event=current_event
    )


def _degraded_permissions_response() -> PermissionsResponse:
    """Physical-security domains denied, conversation allowed (D4/D38):
    reuses the same floor/baseline shape as a guest, but reported under
    mode="degraded" so a caller can't mistake this for an intentional
    guest-mode grant.
    """
    baseline = guest_baseline()
    return PermissionsResponse(
        mode="degraded",
        allowed_intents=baseline["allowed_intents"],
        restricted_entities=baseline["restricted_entities"],
        allowed_domains=baseline["allowed_domains"],
        restricted_intents=[],
        max_queries_per_minute=current_config.get('max_queries_per_minute_guest', 10),
    )


@app.get("/mode/permissions", response_model=PermissionsResponse, dependencies=[Depends(_require_caller)])
async def get_permissions(mode: Optional[Literal["guest"]] = Query(None)):
    """
    Get permissions for the current mode, or force the guest set via
    ?mode=guest regardless of the server's current mode (D6). Any other
    explicit value (e.g. ?mode=owner) is rejected by FastAPI/pydantic as a
    422 before this body runs.

    Returns:
        PermissionsResponse with allowed intents, entities, and rate limits
    """
    _refresh_mode()
    effective_mode = "guest" if mode == "guest" else current_mode

    if effective_mode == "degraded":
        return _degraded_permissions_response()

    if effective_mode == "guest":
        guest_dict = apply_guest_baseline({
            "mode": "guest",
            "allowed_intents": current_config.get('guest_allowed_intents', []),
            "restricted_entities": current_config.get('guest_restricted_entities', []),
            "allowed_domains": current_config.get('guest_allowed_domains', []),
            "restricted_intents": current_config.get('guest_restricted_intents', ['tesla']),
        })
        return PermissionsResponse(
            mode="guest",
            allowed_intents=guest_dict["allowed_intents"],
            restricted_entities=guest_dict["restricted_entities"],
            allowed_domains=guest_dict["allowed_domains"],
            restricted_intents=guest_dict["restricted_intents"],
            max_queries_per_minute=current_config.get('max_queries_per_minute_guest', 10),
        )

    # Owner mode - unrestricted
    return PermissionsResponse(
        mode="owner",
        allowed_intents=[],  # Empty = all allowed
        restricted_entities=[],  # Empty = none restricted
        allowed_domains=[],  # Empty = all allowed
        restricted_intents=[],
        max_queries_per_minute=current_config.get('max_queries_per_minute_owner', 100)
    )


async def _verify_owner_pin(pin: str, tier: str) -> Dict[str, Any]:
    """POST the PIN + caller tier to the admin backend's verify-pin endpoint
    and return its verdict body. Raises on any transport/parsing failure;
    the caller maps that to 503 owner_pin_verification_unavailable.
    """
    client = _get_admin_http_client()
    response = await client.post(
        f"{ADMIN_API_URL}/api/internal/guest-mode/verify-pin",
        json={"pin": pin, "tier": tier},
        headers={"X-Service-Key": get_config().service_api_key},
    )
    response.raise_for_status()
    return response.json()


@app.post("/mode/override", dependencies=[Depends(_require_caller)])
async def override_mode(request: ModeOverrideRequest):
    """
    Manually override the current mode (e.g., owner returning home during guest stay).

    Switching to owner mode requires a PIN, verified by the admin backend
    (D16/D25) -- this service holds no PIN state.

    Args:
        request: ModeOverrideRequest with mode and optional PIN

    Returns:
        Success message with new mode

    Raises:
        HTTPException 401: PIN required but not provided
        HTTPException 400: PIN not exactly 6 digits
        HTTPException 403: Invalid PIN, or no PIN configured at all
        HTTPException 429: Tier locked after too many failed attempts
        HTTPException 503: The admin backend couldn't verify the PIN
    """
    global current_mode, active_override

    if request.mode == "owner":
        pin_configured = bool(current_config.get('owner_pin_configured'))

        if not request.voice_pin:
            if not pin_configured:
                logger.warning(
                    "owner_override_refused_no_pin",
                    device=request.voice_device_id,
                )
                raise HTTPException(status_code=403, detail="owner_pin_not_configured")
            logger.warning(
                "mode_service.override.pin_required",
                device=request.voice_device_id
            )
            raise HTTPException(
                status_code=401,
                detail="PIN required for owner mode override"
            )

        tier = request.caller_tier or "unknown"
        try:
            verdict = await _verify_owner_pin(request.voice_pin, tier)
            verdict_status = verdict.get("status")
        except Exception as e:
            logger.error("owner_pin_verification_unavailable", error=str(e))
            raise HTTPException(status_code=503, detail="owner_pin_verification_unavailable")

        if verdict_status == "invalid":
            logger.warning("mode_service.override.pin_verification_failed", device=request.voice_device_id)
            raise HTTPException(status_code=403, detail="Invalid PIN")
        if verdict_status == "malformed":
            raise HTTPException(status_code=400, detail="PIN must be exactly 6 digits")
        if verdict_status == "not_configured":
            raise HTTPException(status_code=403, detail="owner_pin_not_configured")
        if verdict_status == "locked":
            raise HTTPException(status_code=429, detail="owner_override_locked")
        if verdict_status != "verified":
            logger.error("owner_pin_verification_unavailable", status=verdict_status)
            raise HTTPException(status_code=503, detail="owner_pin_verification_unavailable")

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


@app.get("/mode/events", dependencies=[Depends(_require_caller)])
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
    """Load guest mode configuration from admin API (D26).

    Sends X-Service-Key. On success, replaces current_config and marks the
    load loaded/ok. On failure: keeps the last-good config if one has ever
    loaded (an admin blip during an active booking must not flip the house
    guest -> owner); falls back to the built-in defaults only if nothing has
    ever loaded.
    """
    global current_config, _config_loaded, _last_load_ok, _config_loaded_at
    global _service_key_warned

    headers = {}
    key = get_config().service_api_key
    if key:
        headers["X-Service-Key"] = key
    elif not _service_key_warned:
        logger.warning("mode_service_admin_config_unauthenticated")
        _service_key_warned = True

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ADMIN_API_URL}/api/guest-mode/config", headers=headers)
            response.raise_for_status()
            current_config = response.json()
            _config_loaded = True
            _last_load_ok = True
            _config_loaded_at = time.monotonic()
            logger.info("mode_service.config.loaded", enabled=current_config.get('enabled'))
    except Exception as e:
        _last_load_ok = False
        if _config_loaded:
            logger.error("mode_service.config.load_failed", error=str(e), using="last_good")
        else:
            current_config = _default_config()
            logger.warning("mode_service.config.load_failed", error=str(e), using="defaults")


def _check_config_staleness() -> None:
    """After a load attempt, log a throttled ERROR if the config hasn't
    refreshed successfully in over `_CONFIG_STALE_AFTER_SECONDS` (D37).

    Split out of config_refresh_loop so the throttling logic is directly
    testable without waiting on the loop's real 60 s outer sleep.
    """
    global _last_stale_log_at

    age = _config_age_seconds()
    if (
        _config_loaded
        and age is not None
        and age > _CONFIG_STALE_AFTER_SECONDS
        and (
            _last_stale_log_at is None
            or (time.monotonic() - _last_stale_log_at) >= _CONFIG_STALE_LOG_INTERVAL_SECONDS
        )
    ):
        logger.error("mode_service_config_stale", config_age_seconds=age)
        _last_stale_log_at = time.monotonic()


async def config_refresh_loop():
    """Periodically refresh configuration from admin API, and log a stale
    warning if a successful load hasn't happened in a while (D37)."""
    while True:
        await asyncio.sleep(60)  # Check every 60 seconds
        await load_config()
        _check_config_staleness()


async def _posture_reminder_loop(interval: float) -> None:
    """Log a WARNING immediately (startup) and every `interval` seconds
    thereafter while MODE_SERVICE_INGRESS_AUTH=warn (D28/xander r2 New-M1)."""
    while True:
        if get_config().mode_service_ingress_auth == "warn":
            logger.warning("mode_service_ingress_auth_warn_active")
        await asyncio.sleep(interval)


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
                    _refresh_mode()

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

    if not _config_loaded:
        return "Mode service starting up (config not yet loaded)"

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
