"""
RAG Service Bypass Configuration Routes.

Allows specific RAG services to be bypassed and routed directly to cloud LLMs
with custom system prompts and configurations.

Open Source Compatible - Uses standard FastAPI patterns.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional, List
import structlog

from ..database import get_db
from ..models import RAGServiceBypass
from ..auth.oidc import get_current_user

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/rag-service-bypass", tags=["RAG Service Bypass"])


@router.get("")
async def list_bypass_configs(
    enabled_only: bool = Query(False, description="Only return enabled configurations"),
    db: Session = Depends(get_db)
) -> List[dict]:
    """
    List all RAG service bypass configurations.

    Returns all configured services that can be bypassed to cloud LLMs,
    optionally filtered to only those currently enabled.
    """
    query = db.query(RAGServiceBypass)
    if enabled_only:
        query = query.filter(RAGServiceBypass.bypass_enabled == True)

    configs = query.order_by(RAGServiceBypass.service_name).all()
    return [c.to_dict() for c in configs]


@router.get("/{service_name}")
async def get_bypass_config(
    service_name: str,
    db: Session = Depends(get_db)
) -> dict:
    """
    Get bypass configuration for a specific service.

    Returns the full configuration including system prompt and settings.
    """
    config = db.query(RAGServiceBypass).filter(
        RAGServiceBypass.service_name == service_name
    ).first()

    if not config:
        return {"service_name": service_name, "bypass_enabled": False}

    return config.to_dict()


@router.put("/{service_name}")
async def update_bypass_config(
    service_name: str,
    config_data: dict,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    """
    Update bypass configuration for a service.

    Creates the configuration if it doesn't exist.
    Requires write permissions.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(RAGServiceBypass).filter(
        RAGServiceBypass.service_name == service_name
    ).first()

    if not config:
        # Create new config
        config = RAGServiceBypass(
            service_name=service_name,
            created_by_id=current_user.id
        )
        db.add(config)
        logger.info("bypass_config_created", service=service_name, user=current_user.username)

    # Update fields
    for field in ['bypass_enabled', 'cloud_provider', 'cloud_model',
                  'system_prompt', 'bypass_conditions', 'temperature',
                  'max_tokens', 'display_name', 'description']:
        if field in config_data:
            setattr(config, field, config_data[field])

    db.commit()
    db.refresh(config)

    logger.info("bypass_config_updated", service=service_name, user=current_user.username,
                enabled=config.bypass_enabled)
    return config.to_dict()


@router.post("/{service_name}/toggle")
async def toggle_bypass(
    service_name: str,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    """
    Toggle bypass on/off for a service.

    Quick action to enable or disable bypass without changing other settings.
    Requires write permissions.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(RAGServiceBypass).filter(
        RAGServiceBypass.service_name == service_name
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Service not found")

    config.bypass_enabled = not config.bypass_enabled
    db.commit()

    logger.info("bypass_toggled", service=service_name, enabled=config.bypass_enabled,
                user=current_user.username)

    return {
        "service_name": service_name,
        "bypass_enabled": config.bypass_enabled,
        "message": f"Bypass {'enabled' if config.bypass_enabled else 'disabled'} for {service_name}"
    }


@router.delete("/{service_name}")
async def delete_bypass_config(
    service_name: str,
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> dict:
    """
    Delete a bypass configuration.

    Removes the service from bypass configuration entirely.
    Requires delete permissions.
    """
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(RAGServiceBypass).filter(
        RAGServiceBypass.service_name == service_name
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Service not found")

    db.delete(config)
    db.commit()

    logger.info("bypass_config_deleted", service=service_name, user=current_user.username)

    return {
        "service_name": service_name,
        "message": f"Bypass configuration deleted for {service_name}"
    }


# Public endpoint for orchestrator to check bypass status
@router.get("/public/{service_name}/config")
async def get_public_bypass_config(
    service_name: str,
    db: Session = Depends(get_db)
) -> dict:
    """
    Public endpoint for orchestrator to get bypass configuration.

    This endpoint does not require authentication and is meant to be called
    by the orchestrator service to check if a service should be bypassed.
    Only returns configuration if bypass is enabled.
    """
    config = db.query(RAGServiceBypass).filter(
        RAGServiceBypass.service_name == service_name,
        RAGServiceBypass.bypass_enabled == True
    ).first()

    if not config:
        return {"bypass_enabled": False}

    return {
        "bypass_enabled": True,
        "cloud_provider": config.cloud_provider,
        "cloud_model": config.cloud_model,
        "system_prompt": config.system_prompt,
        "temperature": float(config.temperature) if config.temperature else 0.7,
        "max_tokens": config.max_tokens,
    }


@router.get("/public/enabled")
async def get_enabled_bypasses(
    db: Session = Depends(get_db)
) -> List[str]:
    """
    Get list of services with bypass enabled.

    Public endpoint for orchestrator to quickly check which services
    have bypass enabled without fetching full configurations.
    """
    configs = db.query(RAGServiceBypass.service_name).filter(
        RAGServiceBypass.bypass_enabled == True
    ).all()

    return [c.service_name for c in configs]
