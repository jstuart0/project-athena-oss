"""
Mode Service - Guest Mode Detection and Management

Reads bookings from the admin backend (required, `calendar_events` fed by
`calendar_sources`), optionally supplemented by a legacy iCal URL, detects
active stays, and determines current mode (guest/owner/degraded). Provides
API for the orchestrator/gateway to query current mode and permissions.

ATHENA-127: booking source selection, fetch/merge/freshness, and the D5
stay-window math live in `mode_service.bookings` (BookingSources) and
`shared.booking_window`. See docs/CONFIGURATION.md's "Guest-mode booking
source" section for the full model (MODE_BOOKINGS_SOURCE, freshness
classification, the stale-lookahead residual, precedence).

API Endpoints:
- GET /health - Health check (no auth)
- GET /mode - Get current mode (guest/owner/degraded)
- GET /mode/permissions - Get current permissions for mode (?mode=guest to force guest)
- POST /mode/override - Manually override mode (voice PIN, verified by the admin backend)
- GET /mode/events - Get current merged bookings

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
from shared.guest_policy import (
    GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT,
    apply_guest_baseline,
    guest_baseline,
    parse_json_array_env,
)
from shared.service_ingress_auth import make_require_service_caller
from shared.booking_window import active_booking as bw_active_booking, clamp_buffer_hours

# ATHENA-127 D10: imported as `mode_service.bookings` -- the image copies
# this package to /app/mode_service/ and runs uvicorn from /app, so any
# unqualified or dot-relative sibling-module import fails in the real
# container even though it'd resolve in a test process with `src`
# manually inserted onto sys.path. Verified by the Phase 3 real-image boot
# gate and by tests/unit/test_mode_service_bookings.py's import-form check.
from mode_service.bookings import BookingSources, BookingSnapshot

# Configure logging
logger = configure_logging("mode-service")

# Environment variables
ADMIN_API_URL = get_admin_url()
REDIS_URL = get_config().redis_url
SERVICE_PORT = int(os.getenv("MODE_SERVICE_PORT", "8021"))

_POSTURE_REMINDER_INTERVAL_SECONDS = 3600
_BOOKINGS_REFRESH_INTERVAL_SECONDS = 60
# uvicorn serves nothing (not even /health) until lifespan startup returns,
# and the liveness probe starts 10 s in: the initial booking refresh may
# hold startup for at most this long. The admin fetch's own timeout is 3 s;
# a slow iCal feed (30 s timeout) finishes in the background.
_STARTUP_BOOKINGS_REFRESH_TIMEOUT_SECONDS = 5.0
_CONFIG_STALE_AFTER_SECONDS = 15 * 60
_CONFIG_STALE_LOG_INTERVAL_SECONDS = 10 * 60

# Ingress auth dependency (D15).
_require_caller = make_require_service_caller("mode_service_ingress_auth", "mode_service")

# Global state
cache: Optional[CacheClient] = None
current_config: Dict[str, Any] = {}
current_mode = "degraded"  # Safe default until the first config load succeeds (D38)
active_override: Optional[Dict[str, Any]] = None

# ATHENA-127: the merged/suppressed booking snapshot lives behind
# BookingSources (mode_service.bookings) -- the admin fetch (required) and
# the legacy iCal fetch (advisory-additive in "auto") with their own
# freshness state (D6).
booking_sources = BookingSources()
_startup_bookings_refresh: Optional[asyncio.Task] = None

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
    # ATHENA-127: booking-source visibility (bob L1 -- property_timezone_valid
    # included alongside the rest, not just on the mode-status proxy).
    bookings_source: Optional[str] = None
    bookings_status: Optional[str] = None
    bookings_age_seconds: Optional[float] = None
    property_timezone: Optional[str] = None
    property_timezone_valid: Optional[bool] = None


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
    # ATHENA-69 Pass H2 (xander delta review, High): was a bare `str` --
    # any non-"owner" value (a typo, "Owner", "admin") silently skipped
    # the entire PIN-required branch below and got stored verbatim, and
    # get_permissions()'s fallthrough "else: unrestricted" branch then
    # granted owner permissions to it with zero PIN attempts. A Literal
    # rejects anything else with 422 before this body ever runs.
    mode: Literal["owner", "guest"]
    voice_pin: Optional[str] = None
    timeout_minutes: Optional[int] = None
    voice_device_id: Optional[str] = None
    caller_tier: Optional[Literal["household", "sms", "web_authenticated"]] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache, _startup_bookings_refresh

    # Startup
    logger.info("mode_service.startup", msg="Starting Mode Service")
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    if not get_config().service_api_key and not get_config().dev_mode:
        logger.error("mode_service_service_api_key_unset")

    # Load initial config
    await load_config()

    # ATHENA-127 D9: one booking refresh before serving traffic, so a fresh
    # pod never answers /mode from an empty snapshot when admin is already
    # reachable -- bounded, and shielded so a slow fetch keeps running in
    # the background instead of being cancelled (a cancelled iCal attempt
    # would not be retried until its poll interval elapsed).
    _startup_bookings_refresh = asyncio.create_task(
        booking_sources.refresh(
            current_config,
            now=datetime.now(timezone.utc),
            admin_client=_get_admin_http_client(),
        )
    )
    try:
        await asyncio.wait_for(
            asyncio.shield(_startup_bookings_refresh),
            timeout=_STARTUP_BOOKINGS_REFRESH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "mode_bookings_startup_refresh_pending",
            timeout_seconds=_STARTUP_BOOKINGS_REFRESH_TIMEOUT_SECONDS,
        )

    # Start background tasks
    asyncio.create_task(bookings_refresh_loop())
    asyncio.create_task(config_refresh_loop())
    asyncio.create_task(_posture_reminder_loop(_POSTURE_REMINDER_INTERVAL_SECONDS))

    logger.info("mode_service.startup.complete", msg="Mode Service ready")

    yield

    # Shutdown
    logger.info("mode_service.shutdown", msg="Shutting down Mode Service")
    if cache:
        await cache.disconnect()
    if _startup_bookings_refresh is not None and not _startup_bookings_refresh.done():
        _startup_bookings_refresh.cancel()
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


def _bookings_snapshot(now: datetime, now_monotonic: float) -> Optional[BookingSnapshot]:
    """`booking_sources.snapshot()` needs a loaded config to read buffers/
    calendar_url/poll-interval from; before the first config load there's
    nothing meaningful to report."""
    if not _config_loaded:
        return None
    return booking_sources.snapshot(current_config, now=now, now_monotonic=now_monotonic)


def _bookings_status_for_response(snapshot: Optional[BookingSnapshot]) -> Optional[str]:
    """The required source's freshness status, or "not_required" while
    guest mode is disabled (bookings are still fetched per D9, but not
    consulted for mode) -- distinct from "never_loaded", which means
    disabled or not, nothing has ever been fetched."""
    if snapshot is None:
        return None
    if not current_config.get('enabled'):
        return "not_required"
    return snapshot.statuses.get(snapshot.required, "never_loaded")


@app.get("/health")
async def health_check():
    """Health check endpoint. Never behind ingress auth (D15)."""
    now = datetime.now(timezone.utc)
    now_monotonic = time.monotonic()
    _refresh_mode(now=now, now_monotonic=now_monotonic)

    snapshot = _bookings_snapshot(now, now_monotonic)
    bookings_by_source: Dict[str, Any] = {}
    bookings_window = None
    if snapshot is not None:
        bookings_by_source = {
            name: {
                "status": status,
                "count": snapshot.counts.get(name, 0),
                "required": name == snapshot.required,
            }
            for name, status in snapshot.statuses.items()
        }
        if snapshot.window is not None:
            bookings_window = {
                "start": snapshot.window[0].isoformat(),
                "end": snapshot.window[1].isoformat(),
            }

    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "mode-service",
            "version": "1.0.0",
            "current_mode": current_mode,
            "events_loaded": len(snapshot.bookings) if snapshot else 0,
            "config_enabled": current_config.get('enabled', False),
            "config_source": _config_source(),
            "config_age_seconds": _config_age_seconds(),
            "pin_authority": "admin",
            "ready": _config_loaded,
            "bookings_source": snapshot.label if snapshot else None,
            "bookings_status": _bookings_status_for_response(snapshot),
            "bookings_age_seconds": snapshot.age_seconds if snapshot else None,
            "bookings_by_source": bookings_by_source,
            "bookings_window": bookings_window,
            "property_timezone_valid": snapshot.property_timezone_valid if snapshot else None,
        }
    )


def _refresh_mode(now: Optional[datetime] = None, now_monotonic: Optional[float] = None) -> None:
    """Recompute `current_mode` from live state (D26: cheap, in-memory, run
    on every read). Cold start (D38): never owner/guest until the first
    admin-config load has succeeded at least once.
    """
    global current_mode
    if not _config_loaded:
        current_mode = "degraded"
        return
    current_mode = determine_mode(now=now, now_monotonic=now_monotonic)


@app.get("/mode", response_model=ModeResponse, dependencies=[Depends(_require_caller)])
async def get_current_mode():
    """
    Get the current operating mode (guest, owner, or degraded).

    Returns:
        ModeResponse with mode, reason, and current event details
    """
    now = datetime.now(timezone.utc)
    now_monotonic = time.monotonic()
    _refresh_mode(now=now, now_monotonic=now_monotonic)
    current_event = get_current_event(now=now, now_monotonic=now_monotonic)
    snapshot = _bookings_snapshot(now, now_monotonic)

    return ModeResponse(
        mode=current_mode,
        reason=determine_mode_reason(now=now, now_monotonic=now_monotonic),
        override_active=active_override is not None,
        events_count=len(snapshot.bookings) if snapshot else 0,
        current_event=current_event,
        bookings_source=snapshot.label if snapshot else None,
        bookings_status=_bookings_status_for_response(snapshot),
        bookings_age_seconds=snapshot.age_seconds if snapshot else None,
        property_timezone=snapshot.property_timezone if snapshot else None,
        property_timezone_valid=snapshot.property_timezone_valid if snapshot else None,
    )


def _degraded_permissions_response() -> PermissionsResponse:
    """Physical-security domains denied, everything else unrestricted
    (D4/D38). ATHENA-69 Pass H2 (xander delta review, Info->fix): must
    match orchestrator.mode_permission.degraded_permissions() exactly --
    the entity floor alone (HA_PERMISSION_FALLBACK_RESTRICTED_ENTITIES),
    no intent narrowing, no domain restriction. This previously reused the
    full guest baseline (GUEST_BASELINE_ALLOWED_INTENTS/_DOMAINS, a
    DIFFERENT, deliberately narrower allowlist), so an owner hitting a
    cold mode service (this endpoint reachable, config not yet loaded) was
    denied lights/media/climate that the orchestrator's own outage
    fallback (an unreachable mode service) would have kept -- the same
    failure class produced two different outcomes depending on which
    layer detected it.
    """
    fallback_entities = parse_json_array_env(
        get_config().ha_permission_fallback_restricted_entities,
        GUEST_BASELINE_RESTRICTED_ENTITIES_DEFAULT,
    )
    return PermissionsResponse(
        mode="degraded",
        allowed_intents=[],
        restricted_entities=fallback_entities,
        allowed_domains=[],
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

    if effective_mode == "owner":
        # Owner mode - unrestricted
        return PermissionsResponse(
            mode="owner",
            allowed_intents=[],  # Empty = all allowed
            restricted_entities=[],  # Empty = none restricted
            allowed_domains=[],  # Empty = all allowed
            restricted_intents=[],
            max_queries_per_minute=current_config.get('max_queries_per_minute_owner', 100)
        )

    # ATHENA-69 Pass H2 (xander delta review, High): the unrestricted
    # branch above used to be an unconditional else -- any effective_mode
    # value that wasn't literally "degraded" or "guest" fell through to it
    # and got owner permissions. Explicit "owner" check above; anything
    # else (should be unreachable now that determine_mode()/_refresh_mode()
    # validate the stored value, and ModeOverrideRequest.mode is a Literal)
    # fails closed to degraded rather than defaulting open.
    logger.error("get_permissions_unknown_effective_mode", effective_mode=effective_mode)
    return _degraded_permissions_response()


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

    # Set override. ATHENA-69 Pass H2 (xander delta review, High): the
    # requested/configured duration is capped server-side regardless of
    # how the PIN check went above -- a caller-supplied timeout_minutes
    # was previously unbounded (e.g. 999999), letting a single successful
    # override (or, before the Literal fix above, none at all) grant
    # effectively-permanent owner mode.
    requested_timeout_minutes = request.timeout_minutes or current_config.get('override_timeout_minutes', 60)
    max_timeout_minutes = get_config().override_max_timeout_minutes
    timeout_minutes = min(requested_timeout_minutes, max_timeout_minutes)
    if timeout_minutes != requested_timeout_minutes:
        logger.warning(
            "mode_service.override.timeout_clamped",
            requested=requested_timeout_minutes,
            clamped_to=timeout_minutes,
        )
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
    Get the current merged/suppressed booking snapshot (ATHENA-127).

    Returns:
        List of bookings with checkin/checkout times
    """
    now = datetime.now(timezone.utc)
    now_monotonic = time.monotonic()
    snapshot = _bookings_snapshot(now, now_monotonic)
    bookings = snapshot.bookings if snapshot else []
    events = [
        {
            'uid': b.key,
            'summary': b.label,
            'dtstart': b.start.isoformat(),
            'dtend': b.end.isoformat(),
            'is_test': b.is_test,
        }
        for b in bookings
    ]
    return {
        "events": events,
        "count": len(events),
        "current_event": get_current_event(now=now, now_monotonic=now_monotonic)
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


async def bookings_refresh_loop():
    """ATHENA-127 D9: ticks every 60 s and fetches regardless of `enabled`
    -- enabling guest mode must never start from a `never_loaded` snapshot
    (bob M2-i)."""
    while True:
        await asyncio.sleep(_BOOKINGS_REFRESH_INTERVAL_SECONDS)
        try:
            await booking_sources.refresh(
                current_config,
                now=datetime.now(timezone.utc),
                admin_client=_get_admin_http_client(),
            )
        except Exception as e:
            logger.error("mode_bookings_refresh_loop_error", error=str(e), exc_info=True)


def _clamped_buffers() -> tuple[timedelta, timedelta]:
    before = timedelta(hours=clamp_buffer_hours(current_config.get('buffer_before_checkin_hours', 2)))
    after = timedelta(hours=clamp_buffer_hours(current_config.get('buffer_after_checkout_hours', 1)))
    return before, after


def determine_mode(now: Optional[datetime] = None, now_monotonic: Optional[float] = None) -> str:
    """
    Determine current mode from bookings and overrides (ATHENA-127).

    Normative precedence:
      1. (handled by _refresh_mode) config never loaded -> degraded.
      2. an active, unexpired override -> that mode.
      3. enabled == false -> owner.
      4. any considered booking active (D5/D6 rule 1) -> guest.
      5. the required booking source is fresh or stale -> owner.
      6. otherwise -> degraded.

    Returns:
        'guest', 'owner', or 'degraded'
    """
    global active_override

    now = now if now is not None else datetime.now(timezone.utc)
    now_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()

    # Check for active override
    if active_override:
        if now < active_override['expires_at']:
            stored_mode = active_override['mode']
            if stored_mode not in ("owner", "guest"):
                # ATHENA-69 Pass H2 (xander delta review, High): belt and
                # suspenders alongside ModeOverrideRequest.mode's Literal --
                # a stored value that predates this fix, or that somehow
                # reached this dict outside override_mode(), must not be
                # returned verbatim. Treat as if there were no override at
                # all (fall through to booking-based determination) rather
                # than propagating an unvalidated mode string.
                logger.error("active_override_invalid_mode_discarded", stored_mode=stored_mode)
                active_override = None
            else:
                return stored_mode
        else:
            # Override expired
            active_override = None

    # If guest mode disabled, always owner mode -- bookings are still
    # fetched (D9), just not consulted.
    if not current_config.get('enabled'):
        return "owner"

    snapshot = booking_sources.snapshot(current_config, now=now, now_monotonic=now_monotonic)
    buffer_before, buffer_after = _clamped_buffers()

    if bw_active_booking(snapshot.bookings, now, buffer_before, buffer_after):
        return "guest"

    if snapshot.statuses.get(snapshot.required) in ("fresh", "stale"):
        return "owner"

    return "degraded"


def determine_mode_reason(now: Optional[datetime] = None, now_monotonic: Optional[float] = None) -> str:
    """Get human-readable reason for current mode."""
    global active_override

    now = now if now is not None else datetime.now(timezone.utc)
    now_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()

    if not _config_loaded:
        return "Mode service starting up (config not yet loaded)"

    if active_override and now < active_override['expires_at']:
        return "Manual override via voice PIN"

    if not current_config.get('enabled'):
        return "Guest mode disabled"

    event = get_current_event(now=now, now_monotonic=now_monotonic)
    if event:
        prefix = "[TEST] " if event.get('is_test') else ""
        return f"{prefix}Active booking: {event['summary']} (until {event['checkout']})"

    snapshot = booking_sources.snapshot(current_config, now=now, now_monotonic=now_monotonic)
    required_status = snapshot.statuses.get(snapshot.required)
    if required_status in ("fresh", "stale"):
        return "No active bookings"

    return f"Booking data unavailable ({snapshot.required}: {required_status})"


def get_current_event(now: Optional[datetime] = None, now_monotonic: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Get the currently active booking, if any (D3 label as `summary`,
    `uid` = the opaque booking key)."""
    now = now if now is not None else datetime.now(timezone.utc)
    now_monotonic = now_monotonic if now_monotonic is not None else time.monotonic()

    snapshot = booking_sources.snapshot(current_config, now=now, now_monotonic=now_monotonic)
    buffer_before, buffer_after = _clamped_buffers()

    active = bw_active_booking(snapshot.bookings, now, buffer_before, buffer_after)
    if not active:
        return None

    return {
        'summary': active.label,
        'checkin': active.start.isoformat(),
        'checkout': active.end.isoformat(),
        'uid': active.key,
        'is_test': active.is_test,
    }


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
