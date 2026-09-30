"""Admin API for install telemetry: what's sent, and the owner's controls.

Status is readable with `read`; changing anything needs
`manage_infrastructure` (owner). The environment opt-outs are env-locked:
the admin switch can't override them. The install key is never returned.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.auth.oidc import get_current_user
from app.database import get_db
from app.models import AuditLog, User
from app.services import settings_lease
from app.services.telemetry import sender

logger = structlog.get_logger()

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])



class TelemetrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool


def _require(user: User, permission: str) -> None:
    if not user.has_permission(permission):
        raise HTTPException(status_code=403, detail={"error": "forbidden", "permission": permission})


def _conflict(error: str, **extra: Any) -> HTTPException:
    return HTTPException(status_code=409, detail={"error": error, **extra})


def create_audit_log(db: Session, user: User, action: str, request: Request,
                     old_value: Optional[Dict[str, Any]], new_value: Optional[Dict[str, Any]]) -> None:
    db.add(AuditLog(
        user_id=user.id,
        action=action,
        resource_type="telemetry",
        old_value=old_value,
        new_value=new_value,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        success=True,
    ))
    db.commit()


@router.get("/status")
async def telemetry_status(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """What telemetry would send and where, why it's on or off, and the exact
    last payload. Never creates an identity."""
    _require(current_user, "read")
    return sender.get_status(db, current_user)


@router.put("/settings")
async def update_telemetry_settings(body: TelemetrySettings, request: Request, db: Session = Depends(get_db),
                                    current_user: User = Depends(get_current_user)):
    _require(current_user, "manage_infrastructure")
    state = sender.current_state(db)
    if state["env_locked"]:
        raise _conflict("env_locked", reason=state["reason"])
    sender.set_admin_disabled(db, not body.enabled)
    create_audit_log(db, current_user, "update_settings", request,
                     old_value={"enabled": state["enabled"]}, new_value={"enabled": body.enabled})
    logger.info("telemetry_admin_setting_changed", enabled=body.enabled, user_id=current_user.id)
    return sender.get_status(db, current_user)


@router.post("/send", status_code=202)
async def send_telemetry_now(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Queue one send now (it still takes the lease and re-checks the
    switches). At most one manual send per 10 minutes."""
    _require(current_user, "manage_infrastructure")
    state = sender.current_state(db)
    if not state["enabled"]:
        raise _conflict("disabled", reason=state["reason"])
    if not sender.reserve_manual_send(db, sender.CLOCK()):
        raise HTTPException(status_code=429, detail={"error": "too_soon"})
    sender.request_send()
    return {"status": "scheduled"}


@router.post("/reset-identity")
async def reset_telemetry_identity(request: Request, db: Session = Depends(get_db),
                                   current_user: User = Depends(get_current_user)):
    """Start a new installation ID (the next cycle sends first_boot). The old
    ID's rows stay at the collector until retention removes them."""
    _require(current_user, "manage_infrastructure")
    state = sender.current_state(db)
    if not state["enabled"]:
        raise _conflict("disabled", reason=state["reason"])
    try:
        old_id = await asyncio.to_thread(sender.reset_identity)
    except settings_lease.LeaseBusy:
        raise _conflict("send_in_progress")
    create_audit_log(db, current_user, "reset_identity", request,
                     old_value={"installation_id": old_id}, new_value=None)
    logger.info("telemetry_identity_reset", user_id=current_user.id)
    return {"status": "reset"}
