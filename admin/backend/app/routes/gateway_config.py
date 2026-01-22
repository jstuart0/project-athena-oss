"""
Gateway Configuration API routes.

Provides endpoints for managing gateway service configuration.
Gateway config is a singleton table (id=1) for hot-reconfiguration
without service restart.
"""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, GatewayConfig

logger = structlog.get_logger()

router = APIRouter(prefix="/api/gateway-config", tags=["gateway-config"])


# =============================================================================
# Pydantic Schemas
# =============================================================================

class GatewayConfigResponse(BaseModel):
    """Gateway configuration response schema."""
    id: int
    orchestrator_url: str
    ollama_fallback_url: str
    intent_model: str
    intent_temperature: float
    intent_max_tokens: int
    intent_timeout_seconds: int
    orchestrator_timeout_seconds: int
    session_timeout_seconds: int
    session_max_age_seconds: int
    session_cleanup_interval_seconds: int
    cache_ttl_seconds: int
    rate_limit_enabled: bool
    rate_limit_requests_per_minute: int
    circuit_breaker_enabled: bool
    circuit_breaker_failure_threshold: int
    circuit_breaker_recovery_timeout_seconds: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class GatewayConfigUpdate(BaseModel):
    """Gateway configuration update schema (all fields optional)."""
    orchestrator_url: Optional[str] = Field(None, description="URL of the orchestrator service")
    ollama_fallback_url: Optional[str] = Field(None, description="URL of Ollama fallback service")
    intent_model: Optional[str] = Field(None, description="Model used for intent classification")
    intent_temperature: Optional[float] = Field(None, ge=0.0, le=2.0, description="Temperature for intent classification")
    intent_max_tokens: Optional[int] = Field(None, ge=1, le=100, description="Max tokens for intent classification")
    intent_timeout_seconds: Optional[int] = Field(None, ge=1, le=30, description="Timeout for intent classification")
    orchestrator_timeout_seconds: Optional[int] = Field(None, ge=5, le=300, description="Timeout for orchestrator calls")
    session_timeout_seconds: Optional[int] = Field(None, ge=60, le=3600, description="Session inactivity timeout")
    session_max_age_seconds: Optional[int] = Field(None, ge=3600, le=604800, description="Maximum session age")
    session_cleanup_interval_seconds: Optional[int] = Field(None, ge=30, le=600, description="Session cleanup interval")
    cache_ttl_seconds: Optional[int] = Field(None, ge=10, le=600, description="Cache TTL in seconds")
    rate_limit_enabled: Optional[bool] = Field(None, description="Enable rate limiting")
    rate_limit_requests_per_minute: Optional[int] = Field(None, ge=1, le=1000, description="Rate limit threshold")
    circuit_breaker_enabled: Optional[bool] = Field(None, description="Enable circuit breaker")
    circuit_breaker_failure_threshold: Optional[int] = Field(None, ge=1, le=100, description="Failures before opening circuit")
    circuit_breaker_recovery_timeout_seconds: Optional[int] = Field(None, ge=5, le=300, description="Recovery timeout after circuit opens")


# =============================================================================
# Helper Functions
# =============================================================================

def ensure_singleton_config(db: Session) -> GatewayConfig:
    """Ensure the singleton gateway config row exists and return it."""
    config = db.query(GatewayConfig).filter(GatewayConfig.id == 1).first()
    if not config:
        # Create default config with id=1
        config = GatewayConfig(id=1)
        db.add(config)
        db.commit()
        db.refresh(config)
        logger.info("gateway_config_initialized", message="Created default gateway configuration")
    return config


# =============================================================================
# API Endpoints
# =============================================================================

@router.get("", response_model=GatewayConfigResponse)
async def get_gateway_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get current gateway configuration.

    Returns the singleton gateway configuration row.
    Requires read permission.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        config = ensure_singleton_config(db)
        return config.to_dict()

    except Exception as e:
        logger.error("failed_to_get_gateway_config", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve gateway configuration")


@router.get("/public", response_model=GatewayConfigResponse)
async def get_gateway_config_public(
    db: Session = Depends(get_db)
):
    """
    Get gateway configuration (public endpoint).

    Used by gateway service to fetch its configuration without authentication.
    This is a service-to-service endpoint.
    """
    try:
        config = ensure_singleton_config(db)
        return config.to_dict()

    except Exception as e:
        logger.error("failed_to_get_gateway_config_public", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to retrieve gateway configuration")


@router.patch("", response_model=GatewayConfigResponse)
async def update_gateway_config(
    update: GatewayConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Update gateway configuration.

    Partial update - only provided fields are updated.
    Changes take effect on next gateway refresh (cache expiry or restart).
    Requires write permission.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    try:
        config = ensure_singleton_config(db)

        # Update only provided fields
        update_data = update.model_dump(exclude_unset=True)

        if not update_data:
            raise HTTPException(status_code=400, detail="No fields to update")

        for field, value in update_data.items():
            if hasattr(config, field):
                setattr(config, field, value)

        db.commit()
        db.refresh(config)

        logger.info(
            "gateway_config_updated",
            user=current_user.username,
            updated_fields=list(update_data.keys())
        )

        return config.to_dict()

    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error("failed_to_update_gateway_config", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to update gateway configuration: {str(e)}")


@router.post("/reset")
async def reset_gateway_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Reset gateway configuration to defaults.

    Deletes current configuration and creates a new one with default values.
    Requires admin role.
    """
    if current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Admin role required")

    try:
        # Delete existing config
        config = db.query(GatewayConfig).filter(GatewayConfig.id == 1).first()
        if config:
            db.delete(config)
            db.commit()

        # Create new default config
        new_config = ensure_singleton_config(db)

        logger.info(
            "gateway_config_reset",
            user=current_user.username
        )

        return {
            "status": "success",
            "message": "Gateway configuration reset to defaults",
            "config": new_config.to_dict()
        }

    except Exception as e:
        db.rollback()
        logger.error("failed_to_reset_gateway_config", error=str(e), user=current_user.username)
        raise HTTPException(status_code=500, detail=f"Failed to reset gateway configuration: {str(e)}")
