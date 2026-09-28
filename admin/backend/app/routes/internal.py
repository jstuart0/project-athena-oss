"""
Internal Service-to-Service API Routes

These endpoints are designed for internal services to fetch configuration
without user authentication. They should only be accessible from the
internal network (not exposed publicly).

Two databases are used:
- athena: RAG services registry, base_knowledge, hallucination checks, validation
- athena_admin: Conversation settings, clarification, admin UI config

Endpoints:
- /api/internal/config/conversation - Conversation settings (athena_admin)
- /api/internal/config/clarification - Clarification settings (athena_admin)
- /api/internal/config/clarification-types - Clarification types (athena_admin)
- /api/internal/config/sports-teams - Sports team disambiguation (athena_admin)
- /api/internal/config/device-rules - Device disambiguation rules (athena_admin)
- /api/internal/config/multi-intent - Multi-intent config (athena)
- /api/internal/config/intent-chains - Intent chain rules (athena)
- /api/internal/config/hallucination-checks - Hallucination checks (athena)
- /api/internal/config/validation-models - Cross-validation models (athena)
- /api/internal/config/validation-scenarios - Validation test scenarios (athena)
"""
from typing import Dict, Any, List, Literal, Optional
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, Header, HTTPException, Query
import asyncpg
import hmac
import os
import re
import structlog
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.utils.service_auth import verify_service_api_key
from app.utils.passwords import verify_password
from app.database import get_db
from app.models import GuestModeConfig, OwnerPinAttempt, RagService, CalendarEvent
from shared.config import get_config
from shared.booking_window import booking_key, db_value_to_utc, resolve_property_tz

logger = structlog.get_logger()

router = APIRouter(
    prefix="/api/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_service_api_key)],
)


async def require_service_key_401(
    x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
) -> bool:
    """Service-key-only auth that returns 401 (not 422) for a missing/wrong
    key, and 503 when SERVICE_API_KEY itself is unset (ATHENA-69 D40).

    `verify_service_api_key` declares its header parameter as required
    (`Header(...)`), so FastAPI raises a 422 validation error before that
    dependency's body ever runs when the header is absent -- fine for the
    existing `/api/internal/*` routes (their tests expect 422-on-missing),
    wrong for a route whose contract is "401 when the caller sends nothing".
    This dependency declares the header optional at the FastAPI layer and
    does the presence/match check itself, so a missing key gets 401 instead
    of 422. Used by the guest-mode PIN-verification and current-guest
    routes, which are registered on a *separate* router/route (not the
    `/api/internal` router above) so its router-level `verify_service_api_key`
    dependency never intercepts them first.
    """
    key = get_config().service_api_key
    if not key:
        raise HTTPException(status_code=503, detail="Service authentication not configured")
    if not x_service_key or not hmac.compare_digest(x_service_key, key):
        raise HTTPException(status_code=401, detail="Invalid or missing service key")
    return True


# Separate router (ATHENA-69 D40): same URL prefix as `router` above, but
# deliberately without its `Depends(verify_service_api_key)` router-level
# dependency, so a missing X-Service-Key on this route surfaces this
# dependency's 401 instead of a 422 from the other router's required-header
# parameter.
guest_mode_pin_router = APIRouter(
    prefix="/api/internal/guest-mode",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(require_service_key_401)],
)


class VerifyPinRequest(BaseModel):
    pin: str = Field(max_length=16)
    tier: Literal["household", "sms", "web_authenticated", "unknown"]


class VerifyPinResponse(BaseModel):
    status: Literal["verified", "invalid", "malformed", "not_configured", "locked"]
    locked_until: Optional[str] = None


def _record_pin_failure(db: Session, attempt: OwnerPinAttempt, tier: str, now: datetime) -> None:
    """Increment the tier's failure counter; lock it once the threshold is
    reached, resetting the counter so the next window starts fresh (D25)."""
    cfg = get_config()
    attempt.failed_count += 1
    if attempt.failed_count >= cfg.mode_override_lockout_threshold:
        attempt.locked_until = now + timedelta(minutes=cfg.mode_override_lockout_minutes)
        attempt.failed_count = 0
        logger.warning(
            "owner_pin_lockout_started",
            tier=tier,
            locked_until=attempt.locked_until.isoformat(),
        )
    attempt.updated_at = now
    db.commit()


@guest_mode_pin_router.post("/verify-pin", response_model=VerifyPinResponse)
async def verify_owner_pin(payload: VerifyPinRequest, db: Session = Depends(get_db)):
    """Verify an owner-override PIN on behalf of the mode service (D16/D25).

    This service is the sole holder of PIN state: the hash, the per-tier
    lockout counter, and the verdict. The mode service forwards whatever PIN
    and caller_tier it received and never evaluates either itself.

    Order (D25): (1) no config row or no PIN configured, or a legacy
    unsalted-SHA256 hash that predates ATHENA-69 D30 -> not_configured, never
    counted; (2) tier locked -> locked, the hash is NOT evaluated; (3) PIN
    not exactly 6 ASCII digits -> counted, malformed; (4) hash mismatch ->
    counted, invalid; (5) match -> tier row reset, verified.
    """
    now = datetime.now(timezone.utc)
    tier = payload.tier

    config = db.query(GuestModeConfig).first()
    if not config or not config.owner_pin:
        return VerifyPinResponse(status="not_configured", locked_until=None)

    if not config.owner_pin.startswith("pbkdf2_sha256$"):
        # Legacy unsalted-SHA256 PIN (pre-ATHENA-69 D30) -- can't be verified
        # against the new hash scheme; the admin UI prompts to re-set it.
        logger.info("owner_pin_verify", tier=tier, status="not_configured")
        return VerifyPinResponse(status="not_configured", locked_until=None)

    # D35: lock the tier row before check+increment. SQLite serializes on a
    # single writer connection, so with_for_update() is a documented no-op
    # there rather than an error; PostgreSQL takes a real row lock, which is
    # what makes concurrent wrong PINs across replicas produce exactly one
    # lockout instead of a lost-update race.
    attempt = (
        db.query(OwnerPinAttempt)
        .filter(OwnerPinAttempt.tier == tier)
        .with_for_update()
        .first()
    )
    if attempt is None:
        # Known race (valerie r1, Low -- accepted, not fixed here): two
        # concurrent first-ever attempts for the same tier can both reach
        # this branch and both try to INSERT a new row. Whichever loses
        # raises an IntegrityError on the tier's unique constraint and the
        # request 500s rather than silently double-inserting or granting
        # an unlocked verification -- the failure mode is closed, not
        # open. Not an upsert because SQLAlchemy's ORM-level upsert isn't
        # portable across SQLite (tests) and PostgreSQL (production)
        # without dialect-specific statements; the race window is a single
        # tier's very first verify-pin call ever, not a steady-state path.
        attempt = OwnerPinAttempt(tier=tier, failed_count=0, locked_until=None)
        db.add(attempt)
        db.flush()

    # SQLite stores DateTime(timezone=True) as a naive ISO string and reloads
    # it without tzinfo; PostgreSQL returns tz-aware values. Normalise to UTC
    # before comparing (copied from local_auth.py:92-128).
    locked_until_utc = attempt.locked_until
    if locked_until_utc is not None and locked_until_utc.tzinfo is None:
        locked_until_utc = locked_until_utc.replace(tzinfo=timezone.utc)

    if locked_until_utc and locked_until_utc > now:
        db.commit()
        logger.info("owner_pin_verify", tier=tier, status="locked")
        return VerifyPinResponse(status="locked", locked_until=locked_until_utc.isoformat())

    # ATHENA-69 Pass H (valerie r1, Low): str.isdigit() accepts many
    # non-ASCII Unicode digit characters (superscripts, Devanagari, etc.)
    # -- a "PIN" built from those would pass this check but never match
    # what verify_password compares against (the hash was derived from an
    # actual 6-ASCII-digit PIN when it was set). An explicit ASCII-digit
    # regex is the only string this can ever legitimately match.
    if not re.fullmatch(r"[0-9]{6}", payload.pin):
        _record_pin_failure(db, attempt, tier, now)
        logger.info("owner_pin_verify", tier=tier, status="malformed")
        return VerifyPinResponse(status="malformed", locked_until=None)

    if not verify_password(payload.pin, config.owner_pin):
        _record_pin_failure(db, attempt, tier, now)
        logger.info("owner_pin_verify", tier=tier, status="invalid")
        return VerifyPinResponse(status="invalid", locked_until=None)

    attempt.failed_count = 0
    attempt.locked_until = None
    attempt.updated_at = now
    db.commit()
    logger.info("owner_pin_verify", tier=tier, status="verified")
    return VerifyPinResponse(status="verified", locked_until=None)


# ATHENA-127 D2/D3: this router now hosts more than PIN verification -- the
# mode service's booking source read (guest_mode_pin_router's name predates
# this addition; kept as-is rather than renamed, to avoid an unrelated diff
# across every other route already mounted on it).

_MAX_BOOKINGS_WINDOW = timedelta(days=62)


class BookingRow(BaseModel):
    id: int
    key: str
    source: str
    checkin: str
    checkout: str
    is_test: bool


class SuppressedRow(BaseModel):
    checkin: str
    checkout: str


class BookingsResponse(BaseModel):
    generated_at: str
    property_timezone: str
    property_timezone_valid: bool
    bookings: List[BookingRow]
    suppressed: List[SuppressedRow]


def _utc_query_bound(value: datetime, dialect_name: str) -> datetime:
    """An aware bound -> the UTC instant to compare stored checkin/checkout
    against. The columns are `DateTime(timezone=True)`: timestamptz on
    Postgres, where the bound must stay aware (a naive one is read in the
    session TimeZone). SQLite stores naive UTC, so only there is tzinfo
    stripped."""
    bound = value.astimezone(timezone.utc)
    if dialect_name == "sqlite":
        return bound.replace(tzinfo=None)
    return bound


@guest_mode_pin_router.get("/bookings", response_model=BookingsResponse)
async def get_internal_bookings(
    start: datetime = Query(...),
    end: datetime = Query(...),
    db: Session = Depends(get_db),
):
    """D2/D3: the mode service's booking source. Returns confirmed,
    non-deleted rows overlapping [start, end), plus a `suppressed` list of
    soft-deleted/cancelled day pairs (D13) the mode service uses to drop a
    matching legacy-iCal duplicate. Payload is minimised: no title, name,
    email, phone, or raw external_id -- only an opaque hashed `key` (D3).
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(
            status_code=422, detail="start and end must be ISO-8601 with a UTC offset or 'Z'"
        )
    if end <= start:
        raise HTTPException(status_code=422, detail="end must be after start")
    if end - start > _MAX_BOOKINGS_WINDOW:
        raise HTTPException(status_code=422, detail="window must be at most 62 days")

    dialect_name = db.get_bind().dialect.name
    start_bound = _utc_query_bound(start, dialect_name)
    end_bound = _utc_query_bound(end, dialect_name)

    rows = (
        db.query(CalendarEvent)
        .filter(
            CalendarEvent.deleted_at.is_(None),
            CalendarEvent.status == "confirmed",
            CalendarEvent.checkout > start_bound,
            CalendarEvent.checkin < end_bound,
        )
        .order_by(CalendarEvent.checkin)
        .all()
    )

    suppressed_rows = (
        db.query(CalendarEvent)
        .filter(
            or_(CalendarEvent.deleted_at.isnot(None), CalendarEvent.status == "cancelled"),
            CalendarEvent.checkout > start_bound,
            CalendarEvent.checkin < end_bound,
        )
        .all()
    )

    property_tz_name = get_config().default_timezone
    _, tz_valid = resolve_property_tz(property_tz_name)

    bookings = [
        BookingRow(
            id=r.id,
            key=booking_key(r.source, r.external_id),
            source=r.source,
            checkin=db_value_to_utc(r.checkin).isoformat(),
            checkout=db_value_to_utc(r.checkout).isoformat(),
            is_test=r.is_test,
        )
        for r in rows
    ]
    suppressed = [
        SuppressedRow(
            checkin=db_value_to_utc(r.checkin).isoformat(),
            checkout=db_value_to_utc(r.checkout).isoformat(),
        )
        for r in suppressed_rows
    ]

    logger.info("internal_bookings_served", bookings=len(bookings), suppressed=len(suppressed))

    return BookingsResponse(
        generated_at=datetime.now(timezone.utc).isoformat(),
        property_timezone=property_tz_name or "UTC",
        property_timezone_valid=tz_valid,
        bookings=bookings,
        suppressed=suppressed,
    )


async def get_athena_db_connection():
    """Get connection to the Athena database (rag_services, validation tables)."""
    password = os.getenv('ATHENA_DB_PASSWORD')
    if not password:
        logger.error("db_password_not_configured", db="athena")
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        return await asyncpg.connect(
            host=os.getenv('ATHENA_DB_HOST', 'localhost'),
            port=int(os.getenv('ATHENA_DB_PORT', '5432')),
            user=os.getenv('ATHENA_DB_USER', 'psadmin'),
            password=password,
            database=os.getenv('ATHENA_DB_NAME', 'athena')
        )
    except Exception as e:
        logger.error("athena_db_connect_failed", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Database not configured")


async def get_admin_db_connection():
    """Get connection to the Admin database (conversation settings, clarification)."""
    password = os.getenv('ATHENA_DB_PASSWORD')
    if not password:
        logger.error("db_password_not_configured", db="admin")
        raise HTTPException(status_code=500, detail="Database not configured")
    try:
        return await asyncpg.connect(
            host=os.getenv('ADMIN_DB_HOST', os.getenv('ATHENA_DB_HOST', 'localhost')),
            port=int(os.getenv('ADMIN_DB_PORT', os.getenv('ATHENA_DB_PORT', '5432'))),
            user=os.getenv('ADMIN_DB_USER', os.getenv('ATHENA_DB_USER', 'psadmin')),
            password=password,
            database=os.getenv('ADMIN_DB_NAME', 'athena_admin')
        )
    except Exception as e:
        logger.error("admin_db_connect_failed", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Database not configured")


# =============================================================================
# Conversation Settings (athena_admin database)
# =============================================================================

@router.get("/config/conversation")
async def get_conversation_settings() -> Dict[str, Any]:
    """Get conversation settings for orchestrator."""
    conn = None
    try:
        conn = await get_admin_db_connection()
        row = await conn.fetchrow("SELECT * FROM conversation_settings LIMIT 1")
        if row:
            return dict(row)
        # Return defaults if not found
        return {
            "enabled": True,
            "use_context": True,
            "max_messages": 20,
            "timeout_seconds": 1800,
            "cleanup_interval_seconds": 60,
            "session_ttl_seconds": 3600,
            "max_llm_history_messages": 10,
            "history_mode": "full"
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_conversation_settings_failed", route="get_conversation_settings", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/config/clarification")
async def get_clarification_settings() -> Dict[str, Any]:
    """Get clarification settings for orchestrator."""
    conn = None
    try:
        conn = await get_admin_db_connection()
        row = await conn.fetchrow("SELECT * FROM clarification_settings LIMIT 1")
        if row:
            return dict(row)
        # Return defaults if not found
        return {
            "enabled": True,
            "timeout_seconds": 300
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_clarification_settings_failed", route="get_clarification_settings", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/config/clarification-types")
async def get_clarification_types() -> List[Dict[str, Any]]:
    """Get all clarification types for orchestrator."""
    conn = None
    try:
        conn = await get_admin_db_connection()
        rows = await conn.fetch("""
            SELECT * FROM clarification_types
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        return [dict(row) for row in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_clarification_types_failed", route="get_clarification_types", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


# =============================================================================
# Disambiguation Rules (athena_admin database)
# =============================================================================

@router.get("/config/sports-teams")
async def get_sports_teams() -> List[Dict[str, Any]]:
    """Get sports team disambiguation rules."""
    conn = None
    try:
        conn = await get_admin_db_connection()
        rows = await conn.fetch("""
            SELECT * FROM sports_team_disambiguation
            WHERE requires_disambiguation = true
            ORDER BY team_name
        """)
        return [dict(row) for row in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_sports_teams_failed", route="get_sports_teams", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/config/device-rules")
async def get_device_rules() -> List[Dict[str, Any]]:
    """Get device disambiguation rules."""
    conn = None
    try:
        conn = await get_admin_db_connection()
        rows = await conn.fetch("""
            SELECT * FROM device_disambiguation_rules
            WHERE requires_disambiguation = true
            ORDER BY device_type
        """)
        return [dict(row) for row in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_device_rules_failed", route="get_device_rules", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


# =============================================================================
# Multi-Intent Configuration
# =============================================================================

@router.get("/config/multi-intent")
async def get_multi_intent_config() -> Dict[str, Any]:
    """Get multi-intent configuration."""
    conn = None
    try:
        conn = await get_athena_db_connection()
        row = await conn.fetchrow("SELECT * FROM multi_intent_config LIMIT 1")
        if row:
            return dict(row)
        return {}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_multi_intent_config_failed", route="get_multi_intent_config", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/config/intent-chains")
async def get_intent_chain_rules() -> List[Dict[str, Any]]:
    """Get intent chain rules."""
    conn = None
    try:
        conn = await get_athena_db_connection()
        rows = await conn.fetch("""
            SELECT * FROM intent_chain_rules
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        return [dict(row) for row in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_intent_chain_rules_failed", route="get_intent_chain_rules", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


# =============================================================================
# Validation Configuration
# =============================================================================

@router.get("/config/hallucination-checks")
async def get_hallucination_checks() -> List[Dict[str, Any]]:
    """Get hallucination check patterns."""
    conn = None
    try:
        conn = await get_athena_db_connection()
        rows = await conn.fetch("""
            SELECT * FROM hallucination_checks
            WHERE enabled = true
            ORDER BY priority DESC, category
        """)
        return [dict(row) for row in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_hallucination_checks_failed", route="get_hallucination_checks", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/config/validation-models")
async def get_validation_models() -> List[Dict[str, Any]]:
    """Get cross-validation models."""
    conn = None
    try:
        conn = await get_athena_db_connection()
        rows = await conn.fetch("""
            SELECT * FROM cross_validation_models
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        return [dict(row) for row in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_validation_models_failed", route="get_validation_models", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/config/validation-scenarios")
async def get_validation_scenarios() -> List[Dict[str, Any]]:
    """Get validation test scenarios."""
    conn = None
    try:
        conn = await get_athena_db_connection()
        rows = await conn.fetch("""
            SELECT * FROM validation_test_scenarios
            WHERE enabled = true
            ORDER BY category, name
        """)
        return [dict(row) for row in rows]
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_validation_scenarios_failed", route="get_validation_scenarios", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


# =============================================================================
# Base Knowledge
# =============================================================================

@router.get("/config/base-knowledge")
async def get_base_knowledge() -> Dict[str, Any]:
    """Get base knowledge configuration (default location, user context)."""
    conn = None
    try:
        conn = await get_athena_db_connection()
        row = await conn.fetchrow("SELECT * FROM base_knowledge LIMIT 1")
        if row:
            return dict(row)
        # Return default values if no config exists
        return {
            "default_location": None,
            "user_name": None,
            "preferences": {}
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_base_knowledge_failed", route="get_base_knowledge", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


# =============================================================================
# Bundled Config (fetch all at once for efficiency)
# =============================================================================

@router.get("/config/all")
async def get_all_config() -> Dict[str, Any]:
    """
    Get all configuration in a single request.
    More efficient for orchestrator startup than multiple requests.

    Queries from both databases:
    - athena_admin: conversation settings, clarification, disambiguation
    - athena: multi-intent, intent chains, base knowledge
    """
    result = {}
    admin_conn = None
    athena_conn = None

    try:
        # Get connections to both databases
        admin_conn = await get_admin_db_connection()
        athena_conn = await get_athena_db_connection()

        # ===== athena_admin database =====

        # Conversation settings
        row = await admin_conn.fetchrow("SELECT * FROM conversation_settings LIMIT 1")
        result['conversation_settings'] = dict(row) if row else {
            "enabled": True,
            "use_context": True,
            "max_messages": 20,
            "timeout_seconds": 1800
        }

        # Clarification settings
        row = await admin_conn.fetchrow("SELECT * FROM clarification_settings LIMIT 1")
        result['clarification_settings'] = dict(row) if row else {
            "enabled": True,
            "timeout_seconds": 300
        }

        # Clarification types
        rows = await admin_conn.fetch("""
            SELECT * FROM clarification_types
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        result['clarification_types'] = [dict(row) for row in rows]

        # Sports teams
        rows = await admin_conn.fetch("""
            SELECT * FROM sports_team_disambiguation
            WHERE requires_disambiguation = true
            ORDER BY team_name
        """)
        result['sports_teams'] = [dict(row) for row in rows]

        # Device rules
        rows = await admin_conn.fetch("""
            SELECT * FROM device_disambiguation_rules
            WHERE requires_disambiguation = true
            ORDER BY device_type
        """)
        result['device_rules'] = [dict(row) for row in rows]

        # ===== athena database =====

        # Multi-intent config
        row = await athena_conn.fetchrow("SELECT * FROM multi_intent_config LIMIT 1")
        result['multi_intent_config'] = dict(row) if row else {}

        # Intent chains
        rows = await athena_conn.fetch("""
            SELECT * FROM intent_chain_rules
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        result['intent_chains'] = [dict(row) for row in rows]

        # Base knowledge
        row = await athena_conn.fetchrow("SELECT * FROM base_knowledge LIMIT 1")
        result['base_knowledge'] = dict(row) if row else {
            "default_location": None,
            "user_name": None,
            "preferences": {}
        }

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_all_config_failed", route="get_all_config", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if admin_conn:
            await admin_conn.close()
        if athena_conn:
            await athena_conn.close()


@router.get("/config/rag-services")
async def get_rag_service_urls(db: Session = Depends(get_db)) -> Dict[str, str]:
    """Return enabled service URL map for orchestrator startup.

    Response shape: {name: url} — matches rag_client.py:65-91 consumer.
    Reads from the admin DB's athena_service_registry table (ORM) instead of the legacy
    asyncpg connection to the athena DB.  (ian I-C1 / ATHENA-1 Phase 2)
    """
    try:
        services = db.query(RagService).filter(RagService.enabled.is_(True)).all()
        return {
            svc.name: (
                svc.endpoint_url
                or f"{svc.protocol or 'http'}://{svc.host}:{svc.port}"
            )
            for svc in services
        }
    except Exception as e:
        logger.error("fetch_rag_service_urls_failed", route="get_rag_service_urls", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


# =============================================================================
# Analytics Logging (for orchestrator to send events)
# =============================================================================

from pydantic import BaseModel
from datetime import datetime
import json

class AnalyticsEventRequest(BaseModel):
    """Request body for logging an analytics event."""
    session_id: str
    event_type: str
    metadata: Optional[Dict[str, Any]] = None


@router.post("/analytics/log")
async def log_analytics_event(event: AnalyticsEventRequest) -> Dict[str, Any]:
    """
    Log an analytics event from the orchestrator.

    This endpoint is used by the orchestrator to log intent classification
    and other analytics events to the database for later analysis.

    No authentication required - internal service-to-service call.
    """
    from app.routes.websocket import broadcast_to_admin_jarvis
    import time

    conn = None
    try:
        conn = await get_admin_db_connection()
        # Insert into conversation_analytics table
        await conn.execute("""
            INSERT INTO conversation_analytics (session_id, event_type, metadata, timestamp)
            VALUES ($1, $2, $3, $4)
        """, event.session_id, event.event_type, json.dumps(event.metadata) if event.metadata else None, datetime.utcnow())

        logger.info(
            "analytics_event_logged",
            session_id=event.session_id,
            event_type=event.event_type
        )

        # Broadcast to Admin Jarvis WebSocket clients
        try:
            await broadcast_to_admin_jarvis({
                "event_type": event.event_type,
                "session_id": event.session_id,
                "data": event.metadata or {},
                "timestamp": time.time()
            })
            logger.debug("analytics_event_broadcast", event_type=event.event_type)
        except Exception as broadcast_error:
            logger.warning("analytics_broadcast_failed", error=str(broadcast_error))

        return {"status": "logged", "event_type": event.event_type}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("log_analytics_event_failed", route="log_analytics_event", error_type=type(e).__name__, error=str(e), event_type=event.event_type)
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/config/validation-all")
async def get_all_validation_config() -> Dict[str, Any]:
    """
    Get all validation configuration in a single request.
    For db_validator.py startup.
    """
    conn = None
    try:
        conn = await get_athena_db_connection()
        result = {}

        # Hallucination checks
        rows = await conn.fetch("""
            SELECT * FROM hallucination_checks
            WHERE enabled = true
            ORDER BY priority DESC, category
        """)
        result['hallucination_checks'] = [dict(row) for row in rows]

        # Validation models
        rows = await conn.fetch("""
            SELECT * FROM cross_validation_models
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        result['validation_models'] = [dict(row) for row in rows]

        # Test scenarios
        rows = await conn.fetch("""
            SELECT * FROM validation_test_scenarios
            WHERE enabled = true
            ORDER BY category, name
        """)
        result['validation_scenarios'] = [dict(row) for row in rows]

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error("fetch_validation_config_failed", route="get_all_validation_config", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


# =============================================================================
# Service Usage Tracking (for budget management)
# =============================================================================

@router.get("/service-usage/{service_name}")
async def get_service_usage(service_name: str) -> Dict[str, Any]:
    """
    Get current month's usage for a service.

    Used by RAG services (like Bright Data) to check budget before making requests.
    Returns monthly count and limit (if set).
    """
    conn = None
    try:
        conn = await get_admin_db_connection()
        current_month = datetime.now().strftime("%Y-%m")

        row = await conn.fetchrow("""
            SELECT service_name, month, request_count, monthly_limit
            FROM service_usage
            WHERE service_name = $1 AND month = $2
        """, service_name, current_month)

        if row:
            return {
                "service_name": row['service_name'],
                "month": row['month'],
                "monthly_count": row['request_count'],
                "monthly_limit": row['monthly_limit'],
                "remaining": (row['monthly_limit'] - row['request_count']) if row['monthly_limit'] else None
            }

        # No record for this month yet
        return {
            "service_name": service_name,
            "month": current_month,
            "monthly_count": 0,
            "monthly_limit": None,
            "remaining": None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_service_usage_failed", route="get_service_usage", error_type=type(e).__name__, service=service_name, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.post("/service-usage/{service_name}/increment")
async def record_service_usage(service_name: str, count: int = 1) -> Dict[str, Any]:
    """
    Increment usage counter for a service.

    Called by RAG services after each API request to track usage.
    Creates a new record if one doesn't exist for the current month.
    """
    conn = None
    try:
        conn = await get_admin_db_connection()
        current_month = datetime.now().strftime("%Y-%m")

        # Upsert: increment if exists, insert if not
        row = await conn.fetchrow("""
            INSERT INTO service_usage (service_name, month, request_count)
            VALUES ($1, $2, $3)
            ON CONFLICT (service_name, month)
            DO UPDATE SET
                request_count = service_usage.request_count + $3,
                last_updated = CURRENT_TIMESTAMP
            RETURNING request_count, monthly_limit
        """, service_name, current_month, count)

        return {
            "service_name": service_name,
            "month": current_month,
            "monthly_count": row['request_count'],
            "monthly_limit": row['monthly_limit'],
            "remaining": (row['monthly_limit'] - row['request_count']) if row['monthly_limit'] else None
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("record_service_usage_failed", route="record_service_usage", error_type=type(e).__name__, service=service_name, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()


@router.get("/service-usage")
async def get_all_service_usage() -> List[Dict[str, Any]]:
    """
    Get usage for all services for the current month.

    Used by Admin UI to display budget status across all tracked services.
    """
    conn = None
    try:
        conn = await get_admin_db_connection()
        current_month = datetime.now().strftime("%Y-%m")

        rows = await conn.fetch("""
            SELECT service_name, month, request_count, monthly_limit, last_updated
            FROM service_usage
            WHERE month = $1
            ORDER BY service_name
        """, current_month)

        return [{
            "service_name": row['service_name'],
            "month": row['month'],
            "monthly_count": row['request_count'],
            "monthly_limit": row['monthly_limit'],
            "remaining": (row['monthly_limit'] - row['request_count']) if row['monthly_limit'] else None,
            "last_updated": row['last_updated'].isoformat() if row['last_updated'] else None
        } for row in rows]

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_all_service_usage_failed", route="get_all_service_usage", error_type=type(e).__name__, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn is not None:
            await conn.close()
