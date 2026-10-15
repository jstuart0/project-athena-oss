"""Household settings: GET /api/settings/household and PUT /api/settings/household/{section}.

One generic pair of routes over the typed registry in ``shared.household_settings``
(each section is one ``system_settings`` row, ``household.<name>``). Services read
with the service key and get whole values; a signed-in user reads URLs as scheme
and host only. A save is a compare-and-swap on the row's text, so a stale
revision is a 409 on SQLite and Postgres alike.
"""
import json
import time
from itertools import islice
from typing import Any, Dict, List, Optional, Tuple

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import AuditLog, SystemSetting, User
from app.routes.base_knowledge import location_for_services
from app.utils.service_auth import require_service_or_user_permission, require_user_permission
from shared.household_settings import (
    SETTING_CATEGORY,
    SECTIONS,
    SectionSpec,
    SettingsValidationError,
    audit_payload,
    dump,
    mask_for_user,
    merge_masked,
    parse_stored,
    parse_value,
    stored_flags,
    stored_value,
    validate_update,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/api/settings/household", tags=["household-settings"])

# At most one refusal audit row per user, section and reason per minute (per process).
REFUSAL_AUDIT_INTERVAL_S = 60.0
_REFUSAL_TRACKED_MAX = 1000
_refusal_audited_at: Dict[Tuple[int, str, str], float] = {}

_OWNER_ONLY = "manage_infrastructure"
MAX_BODY_BYTES = 1024 * 1024  # sections that hold lists (feeds, sources, globs)
MAX_SCALAR_BODY_BYTES = 64 * 1024  # every other section is a handful of short fields
MAX_PROBLEMS = 20
MAX_LOC_LENGTH = 64


def _body_cap(spec: SectionSpec) -> int:
    return MAX_BODY_BYTES if spec.list_fields else MAX_SCALAR_BODY_BYTES


async def _read_capped_body(request: Request, cap: int) -> bytes:
    """The request body, refused with 413 as soon as it is known to exceed
    ``cap``: by its declared Content-Length before a byte is read, and by a
    running count while it streams (a chunked or understated body)."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_content_length")
        if declared_size > cap:
            raise HTTPException(status_code=413, detail="payload_too_large")
    chunks: List[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > cap:
            raise HTTPException(status_code=413, detail="payload_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _entry(spec: SectionSpec, model, revision: int, *, masked: bool, restricted: bool = False) -> Dict[str, Any]:
    if restricted:
        value = None
    else:
        value = mask_for_user(spec.name, model) if masked else dump(spec.name, model)
    entry = {
        "value": value,
        "stored": stored_flags(spec.name, model),
        "revision": revision,
        "required_permission": spec.required_permission,
        "writable": spec.writable,
    }
    if restricted:
        entry["restricted"] = True
    return entry


def _hidden_from(spec: SectionSpec, user: Optional[User]) -> bool:
    """An owner-only section's values go to the owner role and to a service,
    not to every signed-in user who may read the page."""
    if spec.required_permission != _OWNER_ONLY:
        return False
    return user is None or not user.has_permission(spec.required_permission)


@router.get("", dependencies=[Depends(require_service_or_user_permission("read"))])
async def get_household_settings(
    request: Request,
    sections: Optional[List[str]] = Query(None),
    db: Session = Depends(get_db),
):
    """The requested sections (all known ones when none are named; unknown names
    are dropped). A section with nothing stored has ``value: null`` and
    revision 0. Anything but a verified service key gets URLs masked, and an
    owner-only section's value is withheld (``restricted: true``) from a user
    without that permission."""
    names = [n for n in SECTIONS if sections is None or n in sections]
    is_service = getattr(request.state, "auth_kind", None) == "service"
    masked = not is_service
    reader = getattr(request.state, "auth_user", None)
    try:
        rows = {
            r.key: r.value
            for r in db.query(SystemSetting)
            .filter(SystemSetting.key.in_([SECTIONS[n].key for n in names if SECTIONS[n].writable]))
            .all()
        }
        located = location_for_services(db) if "location" in names else None
    except Exception as exc:
        logger.error("household_settings_read_failed", error_type=type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to read household settings")

    body: Dict[str, Any] = {}
    for name in names:
        spec = SECTIONS[name]
        if name == "location":
            model, revision = (parse_value(name, located) if located else None), 0
        else:
            model, revision = parse_stored(name, rows.get(spec.key))
        body[name] = _entry(spec, model, revision, masked=masked,
                            restricted=masked and _hidden_from(spec, reader))
    return body


def _audit_refusal(db: Session, request: Request, user: User, spec: SectionSpec, reason: str) -> None:
    now = time.monotonic()
    key = (user.id, spec.name, reason)
    last = _refusal_audited_at.get(key)
    if last is not None and now - last < REFUSAL_AUDIT_INTERVAL_S:
        return
    if len(_refusal_audited_at) >= _REFUSAL_TRACKED_MAX:
        _refusal_audited_at.clear()
    try:
        db.add(AuditLog(
            user_id=user.id,
            action="household_settings_refused",
            resource_type="system_setting",
            new_value={"section": spec.name, "reason": reason},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            success=False,
            error_message=reason,
        ))
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("household_settings_refusal_audit_failed", section=spec.name, error_type=type(exc).__name__)
        return
    _refusal_audited_at[key] = now


def _refuse_if_owner_only(db: Session, request: Request, user: User, spec: SectionSpec, reason: str) -> None:
    if spec.required_permission == _OWNER_ONLY:
        db.rollback()
        _audit_refusal(db, request, user, spec, reason)


def _422(errors: List[Dict[str, Any]]) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=[
            {"loc": [p[:MAX_LOC_LENGTH] if isinstance(p, str) else p for p in e["loc"]], "type": e["type"]}
            for e in islice(errors, MAX_PROBLEMS)
        ],
    )


@router.put("/{section}")
async def put_household_section(
    section: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user_permission("write")),
):
    """Replace one section. The body is ``{"value": {...}, "revision": n}``, where
    ``revision`` is the one the caller loaded; a mismatch is 409
    ``stale_revision``. The body is read here rather than declared, so a
    malformed one gets the same ``loc``/``type`` 422 as any other and the
    framework's error (which carries ``input``) is never built. Errors name
    fields and rules, never the submitted input."""
    spec = SECTIONS.get(section)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown_section")
    if not spec.writable:
        raise HTTPException(status_code=405, detail="section_read_only")

    if not user.has_permission(spec.required_permission):
        _audit_refusal(db, request, user, spec, "insufficient_role")
        raise HTTPException(status_code=403, detail={"error": "insufficient_role"})

    raw = await _read_capped_body(request, _body_cap(spec))
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        raise _422([{"loc": ("body",), "type": "json_invalid"}])
    if not isinstance(body, dict):
        raise _422([{"loc": ("body",), "type": "dict_type"}])
    problems = [
        {"loc": (k,), "type": "extra_forbidden"}
        for k in islice((k for k in body if k not in ("value", "revision")), MAX_PROBLEMS)
    ]
    revision = body.get("revision")
    if "revision" not in body:
        problems.append({"loc": ("revision",), "type": "missing"})
    elif not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        problems.append({"loc": ("revision",), "type": "int_type"})
    if "value" not in body:
        problems.append({"loc": ("value",), "type": "missing"})
    if problems:
        _refuse_if_owner_only(db, request, user, spec, "validation_failed")
        raise _422(problems)

    try:
        row = db.query(SystemSetting).filter(SystemSetting.key == spec.key).first()
    except Exception as exc:
        logger.error("household_settings_read_failed", section=section, error_type=type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to read household settings")
    old_text = row.value if row else None
    old_model, current_revision = parse_stored(section, old_text)

    try:
        merged = merge_masked(section, body["value"], old_model)
        model = validate_update(section, merged)
    except SettingsValidationError as exc:
        _refuse_if_owner_only(db, request, user, spec, "validation_failed")
        raise _422(exc.errors)

    if revision != current_revision:
        _refuse_if_owner_only(db, request, user, spec, "stale_revision")
        raise HTTPException(status_code=409, detail="stale_revision")

    new_revision = current_revision + 1
    new_text = stored_value(section, model, new_revision)
    old_value, new_value = audit_payload(section, old_model, model)
    try:
        if row is not None:
            changed = (
                db.query(SystemSetting)
                .filter(SystemSetting.key == spec.key, SystemSetting.value == old_text)
                .update({SystemSetting.value: new_text}, synchronize_session=False)
            )
            if changed == 0:
                db.rollback()
                _refuse_if_owner_only(db, request, user, spec, "stale_revision")
                raise HTTPException(status_code=409, detail="stale_revision")
        else:
            db.add(SystemSetting(
                key=spec.key, value=new_text,
                description=f"Household setting: {section}", category=SETTING_CATEGORY,
            ))
        db.add(AuditLog(
            user_id=user.id,
            action="household_settings_update",
            resource_type="system_setting",
            old_value=old_value,
            new_value={**new_value, "revision": new_revision},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            success=True,
        ))
        db.flush()
        db.commit()
    except HTTPException:
        raise
    except IntegrityError:
        db.rollback()
        _refuse_if_owner_only(db, request, user, spec, "stale_revision")
        raise HTTPException(status_code=409, detail="stale_revision")
    except Exception as exc:
        db.rollback()
        logger.error("household_settings_save_failed", section=section, error_type=type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to save household settings")

    logger.info("household_settings_saved", section=section, revision=new_revision)
    return _entry(spec, model, new_revision, masked=True)
