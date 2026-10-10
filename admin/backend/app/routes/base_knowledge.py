"""
Base Knowledge management API routes.

Provides endpoints for managing context-aware knowledge entries for voice assistant.
Supports property information, user mode context, and temporal data.
"""
import json
import re
from typing import Annotated, Any, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import AuditLog, User, BaseKnowledge, SystemSetting
from app.utils.service_auth import require_user_permission, verify_service_or_oidc
from shared.config import get_config
from shared.knowledge_tiers import (
    KNOWLEDGE_TIERS,
    OWNER_CATEGORY,
    OWNER_TIER,
    WRITABLE_TIERS,
    validate_entry_fields,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/base-knowledge", tags=["base-knowledge"])

# D5/D9 (ATHENA-91): the settings facade's persisted key, and the eight
# fields it round-trips. city/state are also derived from (and, on write,
# fanned out to) every location/default_location BaseKnowledge entry --
# see D6/D8 in _resolve_city_state / put_base_knowledge_settings.
_SETTINGS_KEY = "base_knowledge_settings"
_VALID_TEMP_UNITS = {"F", "C"}
_VALID_DISTANCE_UNITS = {"mi", "km"}
_VALID_DATE_FORMATS = {"MM/DD/YYYY", "DD/MM/YYYY", "YYYY-MM-DD"}

_AUDIT_RESOURCE = "base_knowledge"
_BULK_TIER_MAX_IDS = 500
# A bulk import is a seed bundle or a hand-built list; 500 matches bulk-tier.
_BULK_CREATE_MAX_ENTRIES = 500
# Generous for an instruction paragraph, small enough that one row can't
# flood a prompt. Newlines and tabs stay legal (multi-line instructions);
# every other control character is refused.
_VALUE_MAX_LENGTH = 4000
_VALUE_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
# Moving a row out of Owner only, or deleting one, is owner role only.
_DEMOTE_PERMISSION = "manage_infrastructure"
_INT32_MAX = 2**31 - 1
_CREATE_COLLISION_DETAIL = "An entry with this category, key and audience already exists"
_TIER_COLLISION_DETAIL = (
    "applies_to: another entry with this category and key already has that audience"
)


def _entry_audit_fields(entry: BaseKnowledge) -> Dict[str, Any]:
    """The only fields an audit row carries about an entry: ids, tier and
    state, never category, key, value or description."""
    return {
        "id": entry.id,
        "applies_to": entry.applies_to,
        "enabled": entry.enabled,
    }


def _audit(db: Session, user: User, request: Optional[Request], action: str,
           resource_id: Optional[int], old_value: Optional[dict], new_value: Optional[dict],
           success: bool = True, error_message: Optional[str] = None) -> None:
    """Stage an audit row in the caller's transaction, so it commits with the
    change it records and is rolled back with it."""
    db.add(AuditLog(
        user_id=user.id,
        action=action,
        resource_type=_AUDIT_RESOURCE,
        resource_id=resource_id,
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        success=success,
        error_message=error_message,
    ))


def _value_problem(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if len(value) > _VALUE_MAX_LENGTH:
        return f"value: must be at most {_VALUE_MAX_LENGTH} characters"
    if _VALUE_CONTROL_CHAR_RE.search(value):
        return "value: must not contain control characters"
    return None


def _tier_rule_violation(entry: BaseKnowledge, new_tier: str) -> Optional[str]:
    """Judge only the tier change on a stored row. A legacy row keeps whatever
    category/key shape it has, so the category is normalised and a malformed
    key neutralised before the shared validator sees them."""
    category = (entry.category or "").strip().lower()
    if category != OWNER_CATEGORY and validate_entry_fields(category, "k", new_tier) is not None:
        category = "c"
    key = entry.key if validate_entry_fields("x", entry.key, new_tier) is None else "k"
    return validate_entry_fields(category, key, new_tier)


def _refuse_demotion(db: Session, user: User, request: Request, action: str,
                     resource_id: Optional[int]) -> HTTPException:
    """Record the refusal (and only it), then return the 403 to raise."""
    db.rollback()
    _audit(db, user, request, action, resource_id, None, None,
           success=False, error_message="insufficient_role")
    db.commit()
    return HTTPException(status_code=403, detail={"error": "insufficient_role"})


def _tier_counts(tiers: List[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for tier in tiers:
        counts[tier] = counts.get(tier, 0) + 1
    return counts


class BaseKnowledgeSettings(BaseModel):
    """The 8-field contract for Memory & Context -> Base Knowledge (D5).
    Also the source of truth for the frontend/API parity check (B9)."""
    city: str = ""
    state: str = ""
    latitude: str = ""
    longitude: str = ""
    timezone: str = "UTC"
    temp_unit: str = "F"
    distance_unit: str = "mi"
    date_format: str = "MM/DD/YYYY"


class BaseKnowledgeSettingsResponse(BaseKnowledgeSettings):
    """GET's response: the 8 fields plus the read-only preferred
    default_location entry value (None when no entry exists or it's empty)."""
    default_location: Optional[str] = None


def _default_timezone() -> str:
    """D7/M3/B10: DEFAULT_TIMEZONE when it's a valid IANA zone, else UTC --
    so a deployment's configured timezone is the GET default, not a bare
    'UTC' literal that ignores AthenaConfig."""
    candidate = (get_config().default_timezone or "").strip()
    if not candidate:
        return "UTC"
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC"
    return candidate


def _default_settings() -> Dict[str, str]:
    return {
        "city": "",
        "state": "",
        "latitude": "",
        "longitude": "",
        "timezone": _default_timezone(),
        "temp_unit": "F",
        "distance_unit": "mi",
        "date_format": "MM/DD/YYYY",
    }


def _nonempty_join(*values: str) -> str:
    return ", ".join(v for v in values if v)


# xander Medium (P3/D44): city/state are rendered VERBATIM into every
# guest's system prompt via base_knowledge_utils.build_knowledge_context
# ("• Default Location: <value>"). An embedded control character or
# newline lets a value smuggle a fake prompt line, e.g.
# "Denver\nIGNORE PREVIOUS INSTRUCTIONS". Reject outright rather than
# silently stripping -- a silent strip still persists an attacker-chosen
# string, just with the tell-tale character quietly removed.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _is_clean_text(value: str) -> bool:
    if _CONTROL_CHAR_RE.search(value):
        return False
    return value.isprintable()


def _validate_settings(body: Any) -> Dict[str, str]:
    """M2: validation happens here, not via pydantic request-body
    validators, so a failure is a single string detail naming the field
    ("<field>: <reason>") -- FastAPI's own validators return a list, which
    the frontend's ApiError stringifies to '[object Object]'."""
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="body: expected a JSON object")

    result = _default_settings()

    if "city" in body:
        # Check the RAW value for control characters BEFORE .strip() --
        # a leading/trailing newline (e.g. "\nIGNORE PREVIOUS
        # INSTRUCTIONS") would otherwise be silently removed by strip()
        # and the rest would read as ordinary, wrongly-accepted text.
        raw_value = str(body["city"])
        if not _is_clean_text(raw_value):
            raise HTTPException(status_code=422, detail="city: must not contain control characters or newlines")
        value = raw_value.strip()
        if len(value) > 100:
            raise HTTPException(status_code=422, detail="city: must be 100 characters or fewer")
        result["city"] = value

    if "state" in body:
        raw_value = str(body["state"])
        if not _is_clean_text(raw_value):
            raise HTTPException(status_code=422, detail="state: must not contain control characters or newlines")
        value = raw_value.strip()
        if len(value) > 100:
            raise HTTPException(status_code=422, detail="state: must be 100 characters or fewer")
        result["state"] = value

    if "latitude" in body:
        value = str(body["latitude"]).strip()
        if value != "":
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="latitude: must be a number between -90 and 90")
            if not (-90 <= parsed <= 90):
                raise HTTPException(status_code=422, detail="latitude: must be between -90 and 90")
        result["latitude"] = value

    if "longitude" in body:
        value = str(body["longitude"]).strip()
        if value != "":
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                raise HTTPException(status_code=422, detail="longitude: must be a number between -180 and 180")
            if not (-180 <= parsed <= 180):
                raise HTTPException(status_code=422, detail="longitude: must be between -180 and 180")
        result["longitude"] = value

    if "timezone" in body:
        value = str(body["timezone"]).strip()
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            raise HTTPException(status_code=422, detail="timezone: must be a valid IANA timezone")
        result["timezone"] = value

    if "temp_unit" in body:
        value = str(body["temp_unit"]).strip()
        if value not in _VALID_TEMP_UNITS:
            raise HTTPException(status_code=422, detail="temp_unit: must be one of F, C")
        result["temp_unit"] = value

    if "distance_unit" in body:
        value = str(body["distance_unit"]).strip()
        if value not in _VALID_DISTANCE_UNITS:
            raise HTTPException(status_code=422, detail="distance_unit: must be one of mi, km")
        result["distance_unit"] = value

    if "date_format" in body:
        value = str(body["date_format"]).strip()
        if value not in _VALID_DATE_FORMATS:
            raise HTTPException(status_code=422, detail="date_format: must be one of MM/DD/YYYY, DD/MM/YYYY, YYYY-MM-DD")
        result["date_format"] = value

    return result


def _coerce_stored_settings(data: Dict[str, Any]) -> Dict[str, str]:
    """codex P3 FIX: a persisted system_settings blob can drift from the
    current schema (a hand-edited row, a since-narrowed enum, a bad
    migration writing the wrong type -- e.g. timezone: [] or
    latitude: "abc"). Re-validate each field independently against the same
    rules _validate_settings enforces on write; a field that fails falls
    back to its own default with one WARNING. This must never raise or
    500 -- B11's "corrupt JSON survives" guarantee extends to "corrupt
    field" too, not just "corrupt JSON entirely"."""
    result = _default_settings()

    def _warn(field: str, reason: str) -> None:
        logger.warning("base_knowledge_settings_field_invalid", field=field, reason=reason)

    for field in ("city", "state"):
        if field not in data:
            continue
        value = data[field]
        if isinstance(value, str) and len(value) <= 100 and _is_clean_text(value):
            result[field] = value
        else:
            _warn(field, "not a valid string")

    for field, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180)):
        if field not in data:
            continue
        value = data[field]
        if not isinstance(value, str):
            _warn(field, "not a string")
            continue
        if value == "":
            result[field] = value
            continue
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            _warn(field, "not numeric")
            continue
        if lo <= parsed <= hi:
            result[field] = value
        else:
            _warn(field, "out of range")

    if "timezone" in data:
        value = data["timezone"]
        if isinstance(value, str):
            try:
                ZoneInfo(value)
                result["timezone"] = value
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                _warn("timezone", "invalid IANA zone")
        else:
            _warn("timezone", "not a string")

    for field, valid_set in (
        ("temp_unit", _VALID_TEMP_UNITS),
        ("distance_unit", _VALID_DISTANCE_UNITS),
        ("date_format", _VALID_DATE_FORMATS),
    ):
        if field not in data:
            continue
        value = data[field]
        if isinstance(value, str) and value in valid_set:
            result[field] = value
        else:
            _warn(field, "not a valid value")

    return result


def _load_settings_blob(db: Session) -> Dict[str, str]:
    defaults = _default_settings()
    setting = db.query(SystemSetting).filter(SystemSetting.key == _SETTINGS_KEY).first()
    if setting is None:
        return defaults

    try:
        data = json.loads(setting.value)
        if not isinstance(data, dict):
            raise ValueError("base_knowledge_settings value is not a JSON object")
    except (ValueError, TypeError) as e:
        logger.warning("base_knowledge_settings_corrupt", error=str(e))
        return defaults

    return _coerce_stored_settings(data)


def _preferred_location_entries(db: Session) -> List[BaseKnowledge]:
    return (
        db.query(BaseKnowledge)
        .filter(BaseKnowledge.category == "location", BaseKnowledge.key == "default_location")
        .all()
    )


def _preferred_location_entry(entries: List[BaseKnowledge]) -> Optional[BaseKnowledge]:
    """D6: the applies_to=='both' row is authoritative; else the lowest id."""
    if not entries:
        return None
    both = [e for e in entries if e.applies_to == "both"]
    if both:
        return both[0]
    return min(entries, key=lambda e: e.id)


def _build_settings_response(db: Session) -> Dict[str, Any]:
    settings = _load_settings_blob(db)
    entries = _preferred_location_entries(db)
    entry = _preferred_location_entry(entries)

    if entry is None or not entry.value:
        city, state = "", ""
    elif _nonempty_join(settings["city"], settings["state"]) == entry.value:
        city, state = settings["city"], settings["state"]
    else:
        city, state = entry.value, ""

    default_location = entry.value if (entry is not None and entry.value) else None

    return {**settings, "city": city, "state": state, "default_location": default_location}


class BaseKnowledgeCreate(BaseModel):
    """Schema for creating a new base knowledge entry."""
    category: str  # 'property', 'location', 'user', 'temporal', 'general'
    key: str
    value: str
    # One of both/guest/household/owner (shared.knowledge_tiers). 'owner' means
    # owner only. The default stays 'both' for API compatibility; the UI makes
    # the caller choose.
    applies_to: str = 'both'
    priority: int = 0
    extra_metadata: Optional[dict] = None
    enabled: bool = True
    description: Optional[str] = None


class BaseKnowledgeUpdate(BaseModel):
    """Schema for updating an existing base knowledge entry.

    category and key are not editable. applies_to is one of both/guest/
    household/owner; 'owner' means owner only.
    """
    value: Optional[str] = None
    applies_to: Optional[str] = None
    priority: Optional[int] = None
    extra_metadata: Optional[dict] = None
    enabled: Optional[bool] = None
    description: Optional[str] = None


class BaseKnowledgeResponse(BaseModel):
    """Schema for base knowledge response."""
    id: int
    category: str
    key: str
    value: str
    applies_to: str
    priority: int
    extra_metadata: Optional[dict]
    enabled: bool
    description: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class BaseKnowledgeBulkCreate(BaseModel):
    """Schema for bulk creating knowledge entries."""
    entries: Annotated[List[BaseKnowledgeCreate], Field(max_length=_BULK_CREATE_MAX_ENTRIES)]


@router.get("", response_model=List[BaseKnowledgeResponse])
async def list_base_knowledge(
    category: Optional[str] = Query(None, description="Filter by category"),
    applies_to: Optional[str] = Query(None, description="Filter by applies_to (both/guest/household/owner)"),
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all base knowledge entries with optional filters.

    Supports filtering by category, applies_to, and enabled status.
    Returns entries sorted by priority (descending).

    Requires read permission for admin access.
    """
    if not current_user.has_permission('read:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        query = db.query(BaseKnowledge)

        # Apply filters
        if category:
            query = query.filter(BaseKnowledge.category == category)
        if applies_to:
            query = query.filter(BaseKnowledge.applies_to == applies_to)
        if enabled is not None:
            query = query.filter(BaseKnowledge.enabled == enabled)

        # Order by priority (highest first)
        query = query.order_by(BaseKnowledge.priority.desc(), BaseKnowledge.created_at)

        entries = query.all()

        logger.info("base_knowledge_listed",
                   count=len(entries),
                   category=category,
                   applies_to=applies_to,
                   enabled=enabled)

        return [entry.to_dict() for entry in entries]

    except Exception as e:
        logger.error("failed_to_list_base_knowledge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve base knowledge entries")


@router.get("/public", response_model=List[BaseKnowledgeResponse])
async def list_base_knowledge_public(
    category: Optional[str] = Query(None, description="Filter by category"),
    applies_to: Optional[str] = Query(None, description="Filter by applies_to (both/guest/household/owner)"),
    enabled: Optional[bool] = Query(None, description="Filter by enabled status"),
    db: Session = Depends(get_db),
    _authorized: bool = Depends(verify_service_or_oidc),
):
    """
    Read-only endpoint for internal service-to-service calls (D44/P3).

    Gated by verify_service_or_oidc: an X-Service-Key matching
    SERVICE_API_KEY, OR an authenticated admin session (Bearer JWT /
    X-API-Key) -- any role, since this dependency authenticates, it doesn't
    authorize by permission. Previously ungated: any unauthenticated caller
    could read every base_knowledge row, including a home street address
    stored under category='property' (xander FIX). Path and response shape
    unchanged for the two known callers (shared.admin_config.AdminConfigClient
    .get_base_knowledge, src/rag/directions/main.py) -- both now send
    X-Service-Key.
    """
    try:
        query = db.query(BaseKnowledge)

        if category:
            query = query.filter(BaseKnowledge.category == category)
        if applies_to:
            query = query.filter(BaseKnowledge.applies_to == applies_to)
        if enabled is not None:
            query = query.filter(BaseKnowledge.enabled == enabled)

        query = query.order_by(BaseKnowledge.priority.desc(), BaseKnowledge.created_at)
        entries = query.all()

        logger.info("base_knowledge_listed_public",
                   count=len(entries),
                   category=category,
                   applies_to=applies_to,
                   enabled=enabled)

        return [entry.to_dict() for entry in entries]
    except Exception as e:
        logger.error("failed_to_list_base_knowledge_public", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve base knowledge entries")


@router.get("/settings", response_model=BaseKnowledgeSettingsResponse)
async def get_base_knowledge_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get the Memory & Context -> Base Knowledge settings (ATHENA-91/D4-c).

    A typed 8-field facade over the two-store split (D5): city/state come
    from the location/default_location entry (D6 authority), the other six
    fields from system_settings. Declared before /{knowledge_id} so this
    literal path isn't swallowed by that int-typed path param (B8).
    """
    if not current_user.has_permission('read:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    return _build_settings_response(db)


@router.put("/settings", response_model=BaseKnowledgeSettingsResponse)
async def put_base_knowledge_settings(
    # Any, not Dict[str, Any]: a typed dict body makes FastAPI/pydantic
    # reject a non-object payload with its OWN list-shaped 422 detail
    # before _validate_settings ever runs -- defeating the single-string
    # detail this route promises (M2). The isinstance check below is the
    # one and only body-shape gate.
    request: Request,
    body: Any = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Save the Memory & Context -> Base Knowledge settings (ATHENA-91/D4-c).

    One session, one commit: the system_settings blob is fully reassigned,
    and every location/default_location row is updated to match (D8) --
    creating one applies_to='both' row only when none exists. Any
    exception rolls back both writes together.
    """
    if not current_user.has_permission('write:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    validated = _validate_settings(body)

    try:
        prior = _load_settings_blob(db)
        setting = db.query(SystemSetting).filter(SystemSetting.key == _SETTINGS_KEY).first()
        if setting is None:
            setting = SystemSetting(
                key=_SETTINGS_KEY,
                value=json.dumps(validated),
                category="base_knowledge",
                description="Memory & Context -> Base Knowledge settings",
            )
            db.add(setting)
        else:
            setting.value = json.dumps(validated)

        location_value = _nonempty_join(validated["city"], validated["state"])
        entries = _preferred_location_entries(db)
        if entries:
            for entry in entries:
                entry.value = location_value
                entry.enabled = bool(location_value)
        else:
            invalid = validate_entry_fields("location", "default_location", "both")
            if invalid:
                raise HTTPException(status_code=422, detail=invalid)
            db.add(BaseKnowledge(
                category="location",
                key="default_location",
                value=location_value,
                applies_to="both",
                priority=0,
                enabled=bool(location_value),
                description="Set from Memory & Context -> Base Knowledge",
            ))

        _audit(db, current_user, request, "settings_update", None, None, {
            "changed_fields": sorted(k for k in validated if validated[k] != prior.get(k)),
            "location_rows": len(entries) or 1,
        })
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_save_base_knowledge_settings", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to save base knowledge settings")

    return _build_settings_response(db)


@router.get("/{knowledge_id}", response_model=BaseKnowledgeResponse)
async def get_base_knowledge(
    knowledge_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get a specific base knowledge entry by ID.
    """
    if not current_user.has_permission('read:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        entry = db.query(BaseKnowledge).filter(BaseKnowledge.id == knowledge_id).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Base knowledge entry not found")

        logger.info("base_knowledge_retrieved",
                   user=current_user.username,
                   knowledge_id=knowledge_id,
                   category=entry.category,
                   key=entry.key)

        return entry.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        logger.error("failed_to_get_base_knowledge", error=str(e), knowledge_id=knowledge_id)
        raise HTTPException(status_code=500, detail="Failed to retrieve base knowledge entry")


def _find_existing(db: Session, category: str, key: str, applies_to: str) -> Optional[BaseKnowledge]:
    """Matches the table's unique key (category, key, applies_to): the same
    fact may exist once per audience."""
    return db.query(BaseKnowledge).filter(
        BaseKnowledge.category == category,
        BaseKnowledge.key == key,
        BaseKnowledge.applies_to == applies_to,
    ).first()


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


@router.post("", response_model=BaseKnowledgeResponse, status_code=201)
async def create_base_knowledge(
    entry: BaseKnowledgeCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new base knowledge entry.

    Requires write permission.
    Category and key combination must be unique.
    """
    if not current_user.has_permission('write:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    invalid = validate_entry_fields(entry.category, entry.key, entry.applies_to) or _value_problem(entry.value)
    if invalid:
        raise _unprocessable(invalid)

    try:
        if _find_existing(db, entry.category, entry.key, entry.applies_to):
            raise HTTPException(status_code=409, detail=_CREATE_COLLISION_DETAIL)

        new_entry = BaseKnowledge(
            category=entry.category,
            key=entry.key,
            value=entry.value,
            applies_to=entry.applies_to,
            priority=entry.priority,
            extra_metadata=entry.extra_metadata,
            enabled=entry.enabled,
            description=entry.description
        )

        db.add(new_entry)
        db.flush()
        _audit(db, current_user, request, "create", new_entry.id, None, _entry_audit_fields(new_entry))
        db.commit()
        db.refresh(new_entry)

        logger.info("base_knowledge_created",
                   user=current_user.username,
                   knowledge_id=new_entry.id,
                   category=new_entry.category,
                   key=new_entry.key)

        return new_entry.to_dict()

    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=_CREATE_COLLISION_DETAIL)
    except Exception as e:
        db.rollback()
        logger.error("failed_to_create_base_knowledge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to create base knowledge entry")


@router.put("/{knowledge_id}", response_model=BaseKnowledgeResponse)
async def update_base_knowledge(
    knowledge_id: int,
    update_data: BaseKnowledgeUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update an existing base knowledge entry.

    Requires write permission.
    Only provided fields will be updated. A changed applies_to is checked
    against the row's own (normalised) category and key, so an owner-category
    entry can only be moved to Owner only (owner_name and name excepted); an
    edit that leaves the audience as it is is not re-checked. Moving a row out
    of Owner only needs the owner role (403 insufficient_role, audited).
    """
    if not current_user.has_permission('write:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        entry = db.query(BaseKnowledge).filter(BaseKnowledge.id == knowledge_id).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Base knowledge entry not found")

        tier_changes = update_data.applies_to is not None and update_data.applies_to != entry.applies_to
        if tier_changes and entry.applies_to == OWNER_TIER and not current_user.has_permission(_DEMOTE_PERMISSION):
            raise _refuse_demotion(db, current_user, request, "update", entry.id)
        invalid = _value_problem(update_data.value)
        if not invalid and tier_changes:
            invalid = _tier_rule_violation(entry, update_data.applies_to)
        if invalid:
            raise _unprocessable(invalid)

        old_audit = _entry_audit_fields(entry)
        changed_fields = [
            name for name in ("value", "applies_to", "priority", "extra_metadata", "enabled", "description")
            if getattr(update_data, name) is not None and getattr(update_data, name) != getattr(entry, name)
        ]

        if update_data.value is not None:
            entry.value = update_data.value
        if update_data.applies_to is not None:
            entry.applies_to = update_data.applies_to
        if update_data.priority is not None:
            entry.priority = update_data.priority
        if update_data.extra_metadata is not None:
            entry.extra_metadata = update_data.extra_metadata
        if update_data.enabled is not None:
            entry.enabled = update_data.enabled
        if update_data.description is not None:
            entry.description = update_data.description

        _audit(db, current_user, request, "update", entry.id, old_audit,
               {**_entry_audit_fields(entry), "changed_fields": changed_fields})
        db.commit()
        db.refresh(entry)

        logger.info("base_knowledge_updated",
                   user=current_user.username,
                   knowledge_id=knowledge_id,
                   category=entry.category,
                   key=entry.key)

        return entry.to_dict()

    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=_TIER_COLLISION_DETAIL)
    except Exception as e:
        db.rollback()
        logger.error("failed_to_update_base_knowledge", error=str(e), knowledge_id=knowledge_id)
        raise HTTPException(status_code=500, detail="Failed to update base knowledge entry")


@router.delete("/{knowledge_id}", status_code=204)
async def delete_base_knowledge(
    knowledge_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Delete a base knowledge entry.

    Requires write permission; deleting an Owner only entry needs the owner
    role (403 insufficient_role, audited as a refusal).
    """
    if not current_user.has_permission('write:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        entry = db.query(BaseKnowledge).filter(BaseKnowledge.id == knowledge_id).first()

        if not entry:
            raise HTTPException(status_code=404, detail="Base knowledge entry not found")

        logger.info("base_knowledge_deleted",
                   user=current_user.username,
                   knowledge_id=knowledge_id,
                   category=entry.category,
                   key=entry.key)

        if entry.applies_to == OWNER_TIER and not current_user.has_permission(_DEMOTE_PERMISSION):
            raise _refuse_demotion(db, current_user, request, "delete", entry.id)

        _audit(db, current_user, request, "delete", entry.id, _entry_audit_fields(entry), None)
        db.delete(entry)
        db.commit()

        return None

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_delete_base_knowledge", error=str(e), knowledge_id=knowledge_id)
        raise HTTPException(status_code=500, detail="Failed to delete base knowledge entry")


@router.post("/bulk", response_model=dict, status_code=201)
async def bulk_create_base_knowledge(
    data: BaseKnowledgeBulkCreate,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Bulk create base knowledge entries.

    Requires write permission.
    Every entry is validated before any row is written, then all are created
    in a single transaction. Skips entries that already exist (by category +
    key). A database-level collision rolls the whole request back (409).
    """
    if not current_user.has_permission('write:base_knowledge'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    for index, entry_data in enumerate(data.entries):
        invalid = (
            validate_entry_fields(entry_data.category, entry_data.key, entry_data.applies_to)
            or _value_problem(entry_data.value)
        )
        if invalid:
            raise _unprocessable(f"entries[{index}].{invalid}")

    try:
        created_count = 0
        skipped_count = 0
        created_ids = []
        created_tiers = []

        for entry_data in data.entries:
            if _find_existing(db, entry_data.category, entry_data.key, entry_data.applies_to):
                skipped_count += 1
                continue

            new_entry = BaseKnowledge(
                category=entry_data.category,
                key=entry_data.key,
                value=entry_data.value,
                applies_to=entry_data.applies_to,
                priority=entry_data.priority,
                extra_metadata=entry_data.extra_metadata,
                enabled=entry_data.enabled,
                description=entry_data.description
            )

            db.add(new_entry)
            db.flush()  # Get the ID without committing
            created_ids.append(new_entry.id)
            created_tiers.append(new_entry.applies_to)
            created_count += 1

        _audit(db, current_user, request, "bulk_create", None, None, {
            "created_ids": created_ids,
            "created_count": created_count,
            "skipped_count": skipped_count,
            "tiers": _tier_counts(created_tiers),
        })
        db.commit()

        logger.info("base_knowledge_bulk_created",
                   user=current_user.username,
                   created_count=created_count,
                   skipped_count=skipped_count)

        return {
            "created_count": created_count,
            "skipped_count": skipped_count,
            "created_ids": created_ids
        }

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="entries: an entry with the same category and key already exists"
        )
    except Exception as e:
        db.rollback()
        logger.error("failed_to_bulk_create_base_knowledge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to bulk create base knowledge entries")


class BaseKnowledgeBulkTier(BaseModel):
    """Schema for moving several entries to one audience."""
    # Bounded by pydantic before any handler work. An out-of-range id or more
    # than 500 ids gets pydantic's list-shaped 422, so the UI caps selection.
    ids: Annotated[List[Annotated[int, Field(ge=1, le=_INT32_MAX)]], Field(max_length=_BULK_TIER_MAX_IDS)]
    applies_to: str


@router.post("/bulk-tier", response_model=dict)
async def bulk_tier_base_knowledge(
    data: BaseKnowledgeBulkTier,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_user_permission("write:base_knowledge")),
):
    """
    Move the selected entries to one audience, all or nothing.

    Signed-in users with write:base_knowledge only (a service key is refused).
    Moving any Owner only row to another audience needs the owner role (403
    insufficient_role, audited as a refusal; nothing changes). One entry that
    breaks the owner-category rule (422) or collides with an
    existing (category, key, audience) row (409) fails the whole request and
    changes nothing, including the audit row.
    """
    if data.applies_to not in WRITABLE_TIERS:
        raise _unprocessable("applies_to: must be one of " + ", ".join(KNOWLEDGE_TIERS))
    if not 1 <= len(data.ids) <= _BULK_TIER_MAX_IDS:
        raise _unprocessable(f"ids: select between 1 and {_BULK_TIER_MAX_IDS} entries")
    if len(set(data.ids)) != len(data.ids):
        raise _unprocessable("ids: each entry may be selected once")

    try:
        entries = db.query(BaseKnowledge).filter(BaseKnowledge.id.in_(data.ids)).all()
        if len(entries) != len(data.ids):
            raise HTTPException(status_code=404, detail="One or more entries were not found")

        if (
            data.applies_to != OWNER_TIER
            and any(entry.applies_to == OWNER_TIER for entry in entries)
            and not current_user.has_permission(_DEMOTE_PERMISSION)
        ):
            raise _refuse_demotion(db, current_user, request, "bulk_tier", None)

        violating = sum(1 for entry in entries if _tier_rule_violation(entry, data.applies_to))
        if violating:
            raise _unprocessable(
                f"applies_to: {violating} selected entries are in the owner category and must stay Owner only"
            )

        old_tiers = _tier_counts([entry.applies_to for entry in entries])
        for entry in entries:
            entry.applies_to = data.applies_to
        db.flush()

        _audit(db, current_user, request, "bulk_tier", None, None, {
            "ids": sorted(data.ids),
            "old_tiers": old_tiers,
            "new_tier": data.applies_to,
        })
        db.commit()
        return {"updated": len(entries)}

    except HTTPException:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=_TIER_COLLISION_DETAIL)
    except Exception as e:
        db.rollback()
        logger.error("failed_to_bulk_tier_base_knowledge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to change the audience of the selected entries")
