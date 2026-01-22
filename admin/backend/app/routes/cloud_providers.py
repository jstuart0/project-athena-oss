"""
Cloud LLM Provider Management API Routes.

Provides CRUD operations for cloud LLM providers (OpenAI, Anthropic, Google)
including API key management, health checks, and model pricing.

Open Source Compatible - Uses standard FastAPI patterns.
"""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from sqlalchemy import func
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone
import structlog
import httpx

from app.database import get_db
from app.auth.oidc import get_current_user, get_optional_user
from app.models import (
    User, CloudLLMProvider, CloudLLMModelPricing, CloudLLMUsage, ExternalAPIKey
)
from app.utils.encryption import encrypt_value, decrypt_value

logger = structlog.get_logger()

router = APIRouter(prefix="/api/cloud-providers", tags=["cloud-providers"])


# =============================================================================
# Pydantic Models
# =============================================================================

class CloudProviderCreate(BaseModel):
    """Request model for creating/updating cloud provider config."""
    provider: str = Field(..., description="Provider ID (openai, anthropic, google)")
    display_name: str = Field(..., description="Display name for the provider")
    enabled: bool = Field(default=False, description="Whether provider is enabled")
    default_model: Optional[str] = Field(None, description="Default model for this provider")
    max_tokens_default: int = Field(default=2048, description="Default max tokens")
    temperature_default: float = Field(default=0.7, description="Default temperature")
    rate_limit_rpm: int = Field(default=60, description="Rate limit (requests per minute)")
    description: Optional[str] = Field(None, description="Provider description")


class CloudProviderResponse(BaseModel):
    """Response model for cloud provider config."""
    id: int
    provider: str
    display_name: str
    enabled: bool
    default_model: Optional[str]
    max_tokens_default: int
    temperature_default: float
    rate_limit_rpm: int
    input_cost_per_1m: Optional[float]
    output_cost_per_1m: Optional[float]
    last_health_check: Optional[str]
    health_status: str
    consecutive_failures: int
    description: Optional[str]
    has_api_key: bool = False

    class Config:
        from_attributes = True


class ModelPricingCreate(BaseModel):
    """Request model for creating model pricing."""
    provider: str
    model_id: str
    model_name: Optional[str]
    input_cost_per_1m: float
    output_cost_per_1m: float
    max_context_length: Optional[int]
    supports_vision: bool = False
    supports_tools: bool = True
    supports_streaming: bool = True


class ModelPricingResponse(BaseModel):
    """Response model for model pricing."""
    id: int
    provider: str
    model_id: str
    model_name: Optional[str]
    input_cost_per_1m: float
    output_cost_per_1m: float
    max_context_length: Optional[int]
    supports_vision: bool
    supports_tools: bool
    supports_streaming: bool
    deprecated: bool

    class Config:
        from_attributes = True


class ProviderSetupRequest(BaseModel):
    """Request model for provider setup (API key configuration)."""
    api_key: str = Field(..., description="API key for the provider")
    endpoint_url: Optional[str] = Field(None, description="Custom endpoint URL (optional)")
    organization_id: Optional[str] = Field(None, description="Organization ID (optional)")


class ProviderUpdateRequest(BaseModel):
    """Request model for partial provider updates."""
    enabled: Optional[bool] = Field(None, description="Whether provider is enabled")
    default_model: Optional[str] = Field(None, description="Default model for this provider")
    max_tokens_default: Optional[int] = Field(None, description="Default max tokens")
    temperature_default: Optional[float] = Field(None, description="Default temperature")
    rate_limit_rpm: Optional[int] = Field(None, description="Rate limit (requests per minute)")
    rate_limit_tpm: Optional[int] = Field(None, description="Rate limit (tokens per minute)")


class HealthCheckResponse(BaseModel):
    """Response model for health check."""
    provider: str
    status: str  # healthy, degraded, unavailable
    latency_ms: Optional[int]
    last_check: str
    error: Optional[str]


# =============================================================================
# Provider Management Routes
# =============================================================================

@router.get("", response_model=List[CloudProviderResponse])
async def list_providers(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """List all cloud LLM providers."""
    query = db.query(CloudLLMProvider)
    if enabled_only:
        query = query.filter(CloudLLMProvider.enabled == True)

    providers = query.all()

    # Check if API keys exist for each provider
    result = []
    for p in providers:
        provider_dict = p.to_dict()

        # Check if API key exists
        api_key = db.query(ExternalAPIKey).filter(
            ExternalAPIKey.service_name == p.provider,
            ExternalAPIKey.enabled == True
        ).first()
        provider_dict["has_api_key"] = api_key is not None

        result.append(provider_dict)

    return result


@router.get("/{provider}", response_model=CloudProviderResponse)
async def get_provider(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get specific cloud provider configuration."""
    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail=f"Provider {provider} not found")

    # Check if API key exists
    result = config.to_dict()
    api_key = db.query(ExternalAPIKey).filter(
        ExternalAPIKey.service_name == provider,
        ExternalAPIKey.enabled == True
    ).first()
    result["has_api_key"] = api_key is not None

    return result


@router.put("/{provider}")
async def update_provider(
    provider: str,
    data: CloudProviderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update cloud provider configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if not config:
        # Create new provider config
        config = CloudLLMProvider(provider=provider)
        db.add(config)

    # Update fields
    config.display_name = data.display_name
    config.enabled = data.enabled
    config.default_model = data.default_model
    config.max_tokens_default = data.max_tokens_default
    config.temperature_default = data.temperature_default
    config.rate_limit_rpm = data.rate_limit_rpm
    config.description = data.description

    db.commit()
    db.refresh(config)

    logger.info("cloud_provider_updated", provider=provider, enabled=data.enabled)
    return config.to_dict()


@router.patch("/{provider}")
async def patch_provider(
    provider: str,
    data: ProviderUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Partially update cloud provider configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail=f"Provider {provider} not found")

    # Only update fields that were provided
    if data.enabled is not None:
        config.enabled = data.enabled
    if data.default_model is not None:
        config.default_model = data.default_model
    if data.max_tokens_default is not None:
        config.max_tokens_default = data.max_tokens_default
    if data.temperature_default is not None:
        config.temperature_default = data.temperature_default
    if data.rate_limit_rpm is not None:
        config.rate_limit_rpm = data.rate_limit_rpm
    if data.rate_limit_tpm is not None:
        config.rate_limit_tpm = data.rate_limit_tpm

    db.commit()
    db.refresh(config)

    logger.info("cloud_provider_patched", provider=provider, updates=data.model_dump(exclude_none=True))
    return config.to_dict()


@router.post("/{provider}/setup")
async def setup_provider(
    provider: str,
    data: ProviderSetupRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Set up a cloud provider with API key.

    This creates or updates the API key in external_api_keys table
    and enables the provider.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate provider
    valid_providers = ["openai", "anthropic", "google"]
    if provider not in valid_providers:
        raise HTTPException(status_code=400, detail=f"Invalid provider. Must be one of: {valid_providers}")

    # Get provider config
    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail=f"Provider {provider} not configured")

    # Encrypt and store API key
    encrypted_key = encrypt_value(data.api_key)

    # Default endpoint URLs
    default_endpoints = {
        "openai": "https://api.openai.com/v1",
        "anthropic": "https://api.anthropic.com",
        "google": "https://generativelanguage.googleapis.com"
    }
    endpoint_url = data.endpoint_url or default_endpoints.get(provider, "")

    # Check if API key record exists
    api_key_record = db.query(ExternalAPIKey).filter(
        ExternalAPIKey.service_name == provider
    ).first()

    if api_key_record:
        # Update existing
        api_key_record.api_key_encrypted = encrypted_key
        api_key_record.endpoint_url = endpoint_url
        api_key_record.enabled = True
    else:
        # Create new
        api_key_record = ExternalAPIKey(
            service_name=provider,
            api_name=f"{config.display_name} API",
            api_key_encrypted=encrypted_key,
            endpoint_url=endpoint_url,
            enabled=True,
            description=f"API key for {config.display_name} cloud LLM provider",
            created_by_id=current_user.id
        )
        db.add(api_key_record)

    # Enable the provider
    config.enabled = True

    db.commit()

    logger.info("cloud_provider_setup", provider=provider, user=current_user.username)

    # Schedule health check
    background_tasks.add_task(check_provider_health, provider, db)

    return {
        "provider": provider,
        "status": "configured",
        "enabled": True,
        "message": f"{config.display_name} provider configured successfully"
    }


@router.delete("/{provider}/api-key")
async def remove_api_key(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Remove API key for a provider (disables the provider)."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    api_key_record = db.query(ExternalAPIKey).filter(
        ExternalAPIKey.service_name == provider
    ).first()

    if api_key_record:
        api_key_record.enabled = False

    # Disable provider
    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if config:
        config.enabled = False

    db.commit()

    logger.info("cloud_provider_disabled", provider=provider, user=current_user.username)
    return {"provider": provider, "status": "disabled"}


# =============================================================================
# Health Check Routes
# =============================================================================

@router.get("/health/all")
async def check_all_providers_health(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Check health of all enabled cloud providers."""
    providers = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.enabled == True
    ).all()

    results = {}
    for provider in providers:
        results[provider.provider] = {
            "status": provider.health_status,
            "last_check": provider.last_health_check.isoformat() if provider.last_health_check else None,
            "consecutive_failures": provider.consecutive_failures
        }

    return results


@router.get("/{provider}/health")
async def get_provider_health(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get health status for a specific provider and trigger a new health check."""
    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail=f"Provider {provider} not found")

    result = await check_provider_health(provider, db)

    # Map status to expected response format
    return {
        "healthy": result.get("status") == "healthy",
        "status": result.get("status"),
        "latency_ms": result.get("latency_ms"),
        "last_check": result.get("last_check"),
        "error": result.get("error")
    }


@router.post("/health/{provider}")
async def trigger_health_check(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Trigger a health check for a specific provider."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail=f"Provider {provider} not found")

    result = await check_provider_health(provider, db)
    return result


async def check_provider_health(provider: str, db: Session) -> Dict[str, Any]:
    """
    Perform health check for a cloud provider.

    Tests the API endpoint with a minimal request.
    """
    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider
    ).first()

    if not config:
        return {"provider": provider, "status": "unavailable", "error": "Not configured"}

    # Get API key
    api_key_record = db.query(ExternalAPIKey).filter(
        ExternalAPIKey.service_name == provider,
        ExternalAPIKey.enabled == True
    ).first()

    if not api_key_record:
        config.health_status = "unavailable"
        config.last_health_check = datetime.now(timezone.utc)
        db.commit()
        return {"provider": provider, "status": "unavailable", "error": "No API key configured"}

    api_key = decrypt_value(api_key_record.api_key_encrypted)

    start_time = datetime.now(timezone.utc)
    error_msg = None
    latency_ms = None

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if provider == "openai":
                # Test OpenAI by listing models
                response = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"}
                )
                response.raise_for_status()

            elif provider == "anthropic":
                # Test Anthropic with a minimal request
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json"
                    },
                    json={
                        "model": "claude-3-5-haiku-20241022",
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "Hi"}]
                    }
                )
                # Even rate limit errors mean the key is valid
                if response.status_code not in (200, 429):
                    response.raise_for_status()

            elif provider == "google":
                # Test Google by listing models
                response = await client.get(
                    f"https://generativelanguage.googleapis.com/v1/models?key={api_key}"
                )
                response.raise_for_status()

        latency_ms = int((datetime.now(timezone.utc) - start_time).total_seconds() * 1000)
        config.health_status = "healthy"
        config.consecutive_failures = 0

    except httpx.HTTPStatusError as e:
        error_msg = f"HTTP {e.response.status_code}: {e.response.text[:100]}"
        config.consecutive_failures += 1
        if config.consecutive_failures >= 3:
            config.health_status = "unavailable"
        else:
            config.health_status = "degraded"

    except Exception as e:
        error_msg = str(e)[:100]
        config.consecutive_failures += 1
        if config.consecutive_failures >= 3:
            config.health_status = "unavailable"
        else:
            config.health_status = "degraded"

    config.last_health_check = datetime.now(timezone.utc)
    db.commit()

    logger.info(
        "cloud_provider_health_check",
        provider=provider,
        status=config.health_status,
        latency_ms=latency_ms,
        error=error_msg
    )

    return {
        "provider": provider,
        "status": config.health_status,
        "latency_ms": latency_ms,
        "last_check": config.last_health_check.isoformat(),
        "error": error_msg
    }


# =============================================================================
# Model Pricing Routes
# =============================================================================

@router.get("/pricing/{provider}")
async def list_model_pricing(
    provider: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """List all model pricing for a provider."""
    pricing = db.query(CloudLLMModelPricing).filter(
        CloudLLMModelPricing.provider == provider,
        CloudLLMModelPricing.deprecated == False
    ).all()

    return [p.to_dict() for p in pricing]


@router.get("/pricing/{provider}/{model_id}")
async def get_model_pricing(
    provider: str,
    model_id: str,
    db: Session = Depends(get_db)
):
    """Get pricing for a specific model."""
    pricing = db.query(CloudLLMModelPricing).filter(
        CloudLLMModelPricing.provider == provider,
        CloudLLMModelPricing.model_id == model_id
    ).first()

    if not pricing:
        raise HTTPException(status_code=404, detail=f"Pricing not found for {provider}/{model_id}")

    return pricing.to_dict()


@router.post("/pricing")
async def create_model_pricing(
    data: ModelPricingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create or update model pricing."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if pricing exists
    existing = db.query(CloudLLMModelPricing).filter(
        CloudLLMModelPricing.provider == data.provider,
        CloudLLMModelPricing.model_id == data.model_id
    ).first()

    if existing:
        # Update
        existing.model_name = data.model_name
        existing.input_cost_per_1m = data.input_cost_per_1m
        existing.output_cost_per_1m = data.output_cost_per_1m
        existing.max_context_length = data.max_context_length
        existing.supports_vision = data.supports_vision
        existing.supports_tools = data.supports_tools
        existing.supports_streaming = data.supports_streaming
        pricing = existing
    else:
        # Create
        pricing = CloudLLMModelPricing(**data.model_dump())
        db.add(pricing)

    db.commit()
    db.refresh(pricing)

    return pricing.to_dict()


# =============================================================================
# Public Endpoints (No Auth Required)
# =============================================================================

@router.get("/public/enabled")
async def get_enabled_providers(db: Session = Depends(get_db)):
    """
    Public endpoint to get list of enabled providers.

    Used by LLMRouter to check available cloud backends.
    """
    providers = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.enabled == True
    ).all()

    return [{"provider": p.provider, "default_model": p.default_model} for p in providers]


@router.get("/public/{provider}/config")
async def get_public_provider_config(
    provider: str,
    db: Session = Depends(get_db)
):
    """
    Public endpoint to get provider configuration.

    Used by services to get provider settings without full auth.
    """
    config = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.provider == provider,
        CloudLLMProvider.enabled == True
    ).first()

    if not config:
        return {"enabled": False}

    return {
        "enabled": True,
        "default_model": config.default_model,
        "max_tokens_default": config.max_tokens_default,
        "temperature_default": float(config.temperature_default) if config.temperature_default else 0.7,
        "rate_limit_rpm": config.rate_limit_rpm
    }
