"""
OSS profile initialization and diagnostics API.
"""

from typing import Any, Optional

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.oidc import get_current_user
from app.database import get_db
from app.models import User
from app.routes.component_models import _invalidate_orchestrator_cache
from app.services.oss_profiles import (
    apply_profile,
    compute_effective_config,
    compute_profile_status,
    detach_profile_field,
    detach_profile_record,
    install_ollama_model,
    preview_profile,
    reset_profile_field,
    reset_profile_record,
    retry_runtime_sync,
)

logger = structlog.get_logger()
router = APIRouter(prefix="/api/oss-profiles", tags=["oss-profiles"])


class ApplyProfileRequest(BaseModel):
    profile_name: str = Field(..., description="Profile identifier to apply")
    mode: str = Field("fill_missing_only", pattern="^(fill_missing_only|reconcile_profile|overwrite_all)$")


class InstallModelRequest(BaseModel):
    model_name: str


class PreviewProfileRequest(BaseModel):
    profile_name: str = Field(..., description="Profile identifier to preview")
    mode: str = Field("fill_missing_only", pattern="^(fill_missing_only|reconcile_profile|overwrite_all)$")


class FieldMutationRequest(BaseModel):
    record_type: str = Field(..., pattern="^(GatewayConfig|ComponentModelAssignment|ModelConfiguration)$")
    identifier: str
    field_name: str


class RecordMutationRequest(BaseModel):
    record_type: str = Field(..., pattern="^(GatewayConfig|ComponentModelAssignment|ModelConfiguration)$")
    identifier: str


@router.get("/status")
async def get_oss_profile_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("read"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return await compute_profile_status(db)


@router.get("/effective-config")
async def get_oss_effective_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("read"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return await compute_effective_config(db)


@router.post("/apply")
async def apply_oss_profile(
    request: ApplyProfileRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = apply_profile(db, request.profile_name, request.mode, current_user=current_user)
        cache_result = await _invalidate_orchestrator_cache()
        return {
            **result,
            "cache_invalidation": cache_result,
        }
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("oss_profile_apply_failed", error=str(exc), profile_name=request.profile_name, mode=request.mode)
        raise HTTPException(status_code=500, detail="Failed to apply profile") from exc


@router.post("/preview")
async def preview_oss_profile(
    request: PreviewProfileRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("read"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        return await preview_profile(db, request.profile_name, request.mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("oss_profile_preview_failed", error=str(exc), profile_name=request.profile_name, mode=request.mode)
        raise HTTPException(status_code=500, detail="Failed to preview profile") from exc


@router.post("/install-model")
async def install_model_for_profile(
    request: InstallModelRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = await install_ollama_model(db, request.model_name, current_user=current_user)
        return result
    except httpx.HTTPStatusError as exc:
        detail: Optional[Any] = None
        try:
            detail = exc.response.json()
        except Exception:
            detail = exc.response.text
        raise HTTPException(status_code=502, detail=detail or "Model installation failed") from exc
    except Exception as exc:
        logger.error("oss_profile_model_install_failed", error=str(exc), model_name=request.model_name)
        raise HTTPException(status_code=500, detail="Failed to install model") from exc


@router.get("/runtime-sync")
async def get_runtime_sync(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("read"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    status = await compute_profile_status(db)
    return status.get("runtime_sync")


@router.post("/runtime-sync/retry")
async def retry_oss_runtime_sync(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = await retry_runtime_sync(db, current_user=current_user)
        cache_result = await _invalidate_orchestrator_cache()
        return {**result, "cache_invalidation": cache_result}
    except Exception as exc:
        logger.error("oss_runtime_sync_retry_failed", error=str(exc))
        raise HTTPException(status_code=500, detail="Failed to retry runtime sync") from exc


@router.post("/fields/reset")
async def reset_oss_profile_field(
    request: FieldMutationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = reset_profile_field(db, request.record_type, request.identifier, request.field_name, current_user=current_user)
        cache_result = await _invalidate_orchestrator_cache()
        return {**result, "cache_invalidation": cache_result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("oss_profile_field_reset_failed", error=str(exc), record_type=request.record_type, identifier=request.identifier, field_name=request.field_name)
        raise HTTPException(status_code=500, detail="Failed to reset field") from exc


@router.post("/fields/detach")
async def detach_oss_profile_field(
    request: FieldMutationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = detach_profile_field(db, request.record_type, request.identifier, request.field_name, current_user=current_user)
        cache_result = await _invalidate_orchestrator_cache()
        return {**result, "cache_invalidation": cache_result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("oss_profile_field_detach_failed", error=str(exc), record_type=request.record_type, identifier=request.identifier, field_name=request.field_name)
        raise HTTPException(status_code=500, detail="Failed to detach field") from exc


@router.post("/records/reset")
async def reset_oss_profile_record(
    request: RecordMutationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = reset_profile_record(db, request.record_type, request.identifier, current_user=current_user)
        cache_result = await _invalidate_orchestrator_cache()
        return {**result, "cache_invalidation": cache_result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("oss_profile_record_reset_failed", error=str(exc), record_type=request.record_type, identifier=request.identifier)
        raise HTTPException(status_code=500, detail="Failed to reset record") from exc


@router.post("/records/detach")
async def detach_oss_profile_record(
    request: RecordMutationRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not current_user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    try:
        result = detach_profile_record(db, request.record_type, request.identifier, current_user=current_user)
        cache_result = await _invalidate_orchestrator_cache()
        return {**result, "cache_invalidation": cache_result}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("oss_profile_record_detach_failed", error=str(exc), record_type=request.record_type, identifier=request.identifier)
        raise HTTPException(status_code=500, detail="Failed to detach record") from exc
