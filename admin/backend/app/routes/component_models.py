import os
"""
Component Model Assignment Routes

Manages which LLM model is assigned to each system component.
Enables hot-swapping of models without service restart via cache TTL.
"""

from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import structlog
import httpx

from app.database import get_db
from app.models import ComponentModelAssignment, User, LLMBackend, CloudLLMProvider, ExternalAPIKey
from app.auth.oidc import get_current_user

logger = structlog.get_logger()
router = APIRouter(prefix="/api/component-models", tags=["component-models"])


# Pydantic Models
class ComponentModelResponse(BaseModel):
    id: int
    component_name: str
    display_name: str
    description: Optional[str]
    category: str
    model_name: str
    backend_type: str
    temperature: Optional[float]
    max_tokens: Optional[int]
    timeout_seconds: Optional[int]
    enabled: bool
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class ComponentModelUpdate(BaseModel):
    model_name: Optional[str] = None
    backend_type: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_seconds: Optional[int] = None
    enabled: Optional[bool] = None


class AvailableModel(BaseModel):
    name: str
    size: int = 0
    modified_at: str = ""
    family: Optional[str] = None
    parameter_size: Optional[str] = None
    quantization: Optional[str] = None
    backend_type: str = "ollama"  # ollama, openai, anthropic, google
    provider: Optional[str] = None  # For cloud models


class AvailableModelsResponse(BaseModel):
    models: List[AvailableModel]
    total: int
    endpoint_url: str
    fetched_at: str
    cloud_models_count: int = 0


# Routes
@router.get("", response_model=List[ComponentModelResponse])
async def list_component_models(
    category: Optional[str] = None,
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all component model assignments."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(ComponentModelAssignment)

    if category:
        query = query.filter(ComponentModelAssignment.category == category)
    if enabled_only:
        query = query.filter(ComponentModelAssignment.enabled == True)

    assignments = query.order_by(ComponentModelAssignment.category, ComponentModelAssignment.display_name).all()

    return [ComponentModelResponse(**a.to_dict()) for a in assignments]


@router.get("/public", response_model=List[ComponentModelResponse])
async def list_component_models_public(
    db: Session = Depends(get_db)
):
    """
    List component model assignments (public endpoint for services).
    No authentication required for service-to-service communication.
    """
    assignments = db.query(ComponentModelAssignment).filter(
        ComponentModelAssignment.enabled == True
    ).all()

    return [ComponentModelResponse(**a.to_dict()) for a in assignments]


@router.get("/component/{component_name}", response_model=ComponentModelResponse)
async def get_component_model(
    component_name: str,
    db: Session = Depends(get_db)
):
    """
    Get model assignment for a specific component (public endpoint).
    Called by services to fetch their model configuration.
    """
    assignment = db.query(ComponentModelAssignment).filter(
        ComponentModelAssignment.component_name == component_name,
        ComponentModelAssignment.enabled == True
    ).first()

    if not assignment:
        raise HTTPException(
            status_code=404,
            detail=f"No enabled model assignment for component '{component_name}'"
        )

    return ComponentModelResponse(**assignment.to_dict())


@router.put("/{component_name}", response_model=ComponentModelResponse)
async def update_component_model(
    component_name: str,
    update_data: ComponentModelUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update model assignment for a component."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    assignment = db.query(ComponentModelAssignment).filter(
        ComponentModelAssignment.component_name == component_name
    ).first()

    if not assignment:
        raise HTTPException(status_code=404, detail=f"Component '{component_name}' not found")

    # If model_name is being updated, validate it exists in Ollama
    if update_data.model_name:
        is_valid = await validate_model_exists(update_data.model_name, db)
        if not is_valid:
            raise HTTPException(
                status_code=400,
                detail=f"Model '{update_data.model_name}' not found in Ollama. Please pull the model first."
            )

    # Update fields
    update_dict = update_data.model_dump(exclude_unset=True)
    for field, value in update_dict.items():
        setattr(assignment, field, value)

    db.commit()
    db.refresh(assignment)

    logger.info(
        "updated_component_model",
        component_name=component_name,
        updated_fields=list(update_dict.keys()),
        user=current_user.username
    )

    # Trigger cache invalidation on orchestrator
    await _invalidate_orchestrator_cache()

    return ComponentModelResponse(**assignment.to_dict())


@router.post("/{component_name}/toggle", response_model=ComponentModelResponse)
async def toggle_component_model(
    component_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Toggle enabled/disabled status of a component model assignment."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    assignment = db.query(ComponentModelAssignment).filter(
        ComponentModelAssignment.component_name == component_name
    ).first()

    if not assignment:
        raise HTTPException(status_code=404, detail=f"Component '{component_name}' not found")

    assignment.enabled = not assignment.enabled
    db.commit()
    db.refresh(assignment)

    logger.info(
        "toggled_component_model",
        component_name=component_name,
        enabled=assignment.enabled,
        user=current_user.username
    )

    # Trigger cache invalidation on orchestrator
    await _invalidate_orchestrator_cache()

    return ComponentModelResponse(**assignment.to_dict())


@router.get("/available-models", response_model=AvailableModelsResponse)
async def get_available_models(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Discover available models from Ollama and enabled cloud providers.
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    models = []
    cloud_models_count = 0

    # Get Ollama endpoint from first enabled backend
    backend = db.query(LLMBackend).filter(
        LLMBackend.backend_type == 'ollama',
        LLMBackend.enabled == True
    ).first()

    ollama_url = backend.endpoint_url if backend else os.getenv("OLLAMA_URL", "http://localhost:11434")

    # Fetch Ollama models
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(f"{ollama_url}/api/tags")
            response.raise_for_status()
            data = response.json()

        for model in data.get("models", []):
            details = model.get("details", {})
            models.append(AvailableModel(
                name=model["name"],
                size=model.get("size", 0),
                modified_at=model.get("modified_at", ""),
                family=details.get("family"),
                parameter_size=details.get("parameter_size"),
                quantization=details.get("quantization_level"),
                backend_type="ollama"
            ))

    except Exception as e:
        logger.warning("ollama_model_discovery_failed", error=str(e))
        # Continue to cloud models even if Ollama fails

    # Fetch cloud models from enabled providers
    cloud_providers = db.query(CloudLLMProvider).filter(
        CloudLLMProvider.enabled == True
    ).all()

    # Cloud model definitions by provider
    cloud_model_catalog = {
        "openai": [
            {"name": "gpt-5", "family": "GPT-5"},
            {"name": "gpt-4.5-preview", "family": "GPT-4.5"},
            {"name": "gpt-4o", "family": "GPT-4o"},
            {"name": "gpt-4o-mini", "family": "GPT-4o"},
            {"name": "o3", "family": "O3"},
            {"name": "o3-mini", "family": "O3"},
            {"name": "o1", "family": "O1"},
            {"name": "o1-mini", "family": "O1"},
            {"name": "o1-preview", "family": "O1"},
            {"name": "gpt-4-turbo", "family": "GPT-4"},
            {"name": "gpt-4", "family": "GPT-4"},
            {"name": "gpt-3.5-turbo", "family": "GPT-3.5"},
        ],
        "anthropic": [
            {"name": "claude-sonnet-4-20250514", "family": "Claude 4"},
            {"name": "claude-3-5-sonnet-20241022", "family": "Claude 3.5"},
            {"name": "claude-3-5-haiku-20241022", "family": "Claude 3.5"},
            {"name": "claude-3-opus-20240229", "family": "Claude 3"},
            {"name": "claude-3-sonnet-20240229", "family": "Claude 3"},
            {"name": "claude-3-haiku-20240307", "family": "Claude 3"},
        ],
        "google": [
            {"name": "gemini-2.0-flash", "family": "Gemini 2.0"},
            {"name": "gemini-2.0-flash-exp", "family": "Gemini 2.0"},
            {"name": "gemini-1.5-pro", "family": "Gemini 1.5"},
            {"name": "gemini-1.5-flash", "family": "Gemini 1.5"},
            {"name": "gemini-1.5-flash-8b", "family": "Gemini 1.5"},
        ],
    }

    for provider in cloud_providers:
        # Check if API key exists for this provider
        api_key = db.query(ExternalAPIKey).filter(
            ExternalAPIKey.service_name == provider.provider,
            ExternalAPIKey.enabled == True
        ).first()

        if api_key and provider.provider in cloud_model_catalog:
            provider_models = cloud_model_catalog[provider.provider]
            for model_info in provider_models:
                models.append(AvailableModel(
                    name=f"{provider.provider}/{model_info['name']}",
                    family=model_info.get("family"),
                    backend_type=provider.provider,
                    provider=provider.display_name
                ))
                cloud_models_count += 1

    logger.info("discovered_available_models",
                ollama_count=len(models) - cloud_models_count,
                cloud_count=cloud_models_count,
                endpoint=ollama_url)

    return AvailableModelsResponse(
        models=models,
        total=len(models),
        endpoint_url=ollama_url,
        fetched_at=datetime.utcnow().isoformat(),
        cloud_models_count=cloud_models_count
    )


@router.post("/invalidate-cache")
async def invalidate_cache(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Notify orchestrator to invalidate its component model cache.
    Called automatically when a model assignment is changed.
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    result = await _invalidate_orchestrator_cache()
    return result


async def _invalidate_orchestrator_cache() -> Dict[str, Any]:
    """Internal function to call orchestrator cache invalidation."""
    orchestrator_url = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001") + "/admin/invalidate-model-cache"

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(orchestrator_url, timeout=5.0)

            if response.status_code == 200:
                logger.info("orchestrator_cache_invalidated")
                return {"status": "success", "message": "Cache invalidated"}
            else:
                logger.warning("orchestrator_cache_invalidation_failed", status=response.status_code)
                return {"status": "warning", "message": "Cache invalidation may have failed"}

    except Exception as e:
        logger.error("orchestrator_cache_invalidation_error", error=str(e))
        # Don't fail the request - cache will expire naturally
        return {"status": "warning", "message": f"Could not reach orchestrator: {str(e)}"}


async def validate_model_exists(model_name: str, db: Session) -> bool:
    """Check if a model exists in Ollama or is a valid cloud model."""
    # Check if it's a cloud model (format: provider/model_name)
    if "/" in model_name:
        provider = model_name.split("/")[0]
        if provider in ["openai", "anthropic", "google"]:
            # Verify the provider is enabled and has an API key
            cloud_provider = db.query(CloudLLMProvider).filter(
                CloudLLMProvider.provider == provider,
                CloudLLMProvider.enabled == True
            ).first()

            if cloud_provider:
                api_key = db.query(ExternalAPIKey).filter(
                    ExternalAPIKey.service_name == provider,
                    ExternalAPIKey.enabled == True
                ).first()
                return api_key is not None

            return False

    # Check Ollama for local models
    backend = db.query(LLMBackend).filter(
        LLMBackend.backend_type == 'ollama',
        LLMBackend.enabled == True
    ).first()

    ollama_url = backend.endpoint_url if backend else os.getenv("OLLAMA_URL", "http://localhost:11434")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ollama_url}/api/tags")
            response.raise_for_status()
            data = response.json()

        available_models = [m["name"] for m in data.get("models", [])]
        return model_name in available_models

    except Exception as e:
        logger.warning("model_validation_failed", model=model_name, error=str(e))
        return False
