"""
Model Configuration API Routes.

Provides CRUD operations for dynamic LLM model configurations with Ollama/MLX options.
Works alongside llm_backends table - this handles execution options (num_ctx, mirostat, etc.)
while llm_backends handles routing decisions (which backend to use).
"""
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, ModelConfiguration

logger = structlog.get_logger()

router = APIRouter(prefix="/api/model-configs", tags=["model-configs"])


# Pydantic models for request/response

class OllamaOptions(BaseModel):
    """Ollama-specific options for model execution."""
    num_ctx: Optional[int] = Field(None, description="Context window size (2048-32768)")
    num_batch: Optional[int] = Field(None, description="Batch size (128-1024)")
    num_gpu: Optional[int] = Field(None, description="Number of GPU layers")
    num_thread: Optional[int] = Field(None, description="Number of CPU threads")
    top_k: Optional[int] = Field(None, description="Top-K sampling (10-100)")
    top_p: Optional[float] = Field(None, description="Nucleus sampling (0.5-1.0)")
    repeat_penalty: Optional[float] = Field(None, description="Repetition penalty (0.9-1.5)")
    mirostat: Optional[int] = Field(None, description="Mirostat mode (0=off, 1, 2)")
    mirostat_tau: Optional[float] = Field(None, description="Mirostat target entropy")
    mirostat_eta: Optional[float] = Field(None, description="Mirostat learning rate")
    presence_penalty: Optional[float] = Field(None, description="Presence penalty")
    frequency_penalty: Optional[float] = Field(None, description="Frequency penalty")
    num_predict: Optional[int] = Field(None, description="Max tokens to predict")

    class Config:
        extra = "allow"  # Allow additional options


class MLXOptions(BaseModel):
    """MLX-specific options for model execution."""
    max_kv_size: Optional[int] = Field(None, description="Maximum KV cache size")
    quantization: Optional[str] = Field(None, description="Quantization type (4bit, 8bit, bf16)")

    class Config:
        extra = "allow"


class ModelConfigCreate(BaseModel):
    """Request model for creating model configuration."""
    model_name: str = Field(..., description="Model identifier (e.g., 'qwen3:8b', '_default')")
    display_name: Optional[str] = Field(None, description="Human-readable name")
    backend_type: str = Field(default="ollama", description="Backend: 'ollama', 'mlx', or 'auto'")
    enabled: bool = Field(default=True, description="Whether this config is active")
    temperature: float = Field(default=0.7, description="Generation temperature (0.0-2.0)")
    max_tokens: int = Field(default=2048, description="Maximum tokens to generate")
    timeout_seconds: int = Field(default=60, description="Request timeout")
    keep_alive_seconds: int = Field(default=-1, description="Model keep-alive (-1=forever)")
    ollama_options: Optional[Dict[str, Any]] = Field(default={}, description="Ollama options")
    mlx_options: Optional[Dict[str, Any]] = Field(default={}, description="MLX options")
    description: Optional[str] = Field(None, description="Configuration description")
    priority: int = Field(default=0, description="Priority for ordering")

    class Config:
        json_schema_extra = {
            "example": {
                "model_name": "qwen3:8b",
                "display_name": "Qwen3 8B (Mirostat)",
                "backend_type": "ollama",
                "enabled": True,
                "temperature": 0.7,
                "max_tokens": 2048,
                "ollama_options": {
                    "num_ctx": 4096,
                    "num_batch": 256,
                    "mirostat": 2,
                    "mirostat_tau": 5.0,
                    "mirostat_eta": 0.1
                },
                "description": "Optimized Qwen3 8B with Mirostat 2.0 sampling"
            }
        }


class ModelConfigUpdate(BaseModel):
    """Request model for updating model configuration."""
    display_name: Optional[str] = None
    backend_type: Optional[str] = None
    enabled: Optional[bool] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout_seconds: Optional[int] = None
    keep_alive_seconds: Optional[int] = None
    ollama_options: Optional[Dict[str, Any]] = None
    mlx_options: Optional[Dict[str, Any]] = None
    description: Optional[str] = None
    priority: Optional[int] = None


class ModelConfigResponse(BaseModel):
    """Response model for model configuration."""
    id: int
    model_name: str
    display_name: Optional[str] = None
    backend_type: str
    enabled: bool
    temperature: float
    max_tokens: int
    timeout_seconds: int
    keep_alive_seconds: int
    ollama_options: Dict[str, Any]
    mlx_options: Dict[str, Any]
    description: Optional[str] = None
    priority: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


# Preset configurations for quick setup
PRESET_CONFIGS = {
    "speed": {
        "name": "Speed Optimized",
        "description": "Optimized for lowest latency",
        "temperature": 0.3,
        "ollama_options": {
            "num_ctx": 2048,
            "num_batch": 128,
            "top_k": 20,
            "top_p": 0.8,
            "repeat_penalty": 1.05
        }
    },
    "balanced": {
        "name": "Balanced",
        "description": "Good balance of speed and quality",
        "temperature": 0.5,
        "ollama_options": {
            "num_ctx": 4096,
            "num_batch": 256,
            "top_k": 30,
            "top_p": 0.85,
            "repeat_penalty": 1.08
        }
    },
    "quality": {
        "name": "Quality Optimized",
        "description": "Optimized for best quality",
        "temperature": 0.6,
        "ollama_options": {
            "num_ctx": 8192,
            "num_batch": 512,
            "top_k": 40,
            "top_p": 0.9,
            "repeat_penalty": 1.1
        }
    },
    "mirostat": {
        "name": "Mirostat 2.0",
        "description": "Adaptive sampling for consistent quality (best benchmark results)",
        "temperature": 0.7,
        "ollama_options": {
            "num_ctx": 4096,
            "num_batch": 256,
            "mirostat": 2,
            "mirostat_tau": 5.0,
            "mirostat_eta": 0.1
        }
    },
    "deterministic": {
        "name": "Deterministic",
        "description": "Low temperature for consistent, predictable responses",
        "temperature": 0.1,
        "ollama_options": {
            "num_ctx": 4096,
            "num_batch": 256,
            "top_k": 10,
            "top_p": 0.7,
            "repeat_penalty": 1.02
        }
    }
}


# ============================================================================
# Public endpoints (no auth) - for services to fetch configurations
# ============================================================================

@router.get("/public", response_model=List[ModelConfigResponse])
async def list_configs_public(
    enabled_only: bool = True,
    db: Session = Depends(get_db)
):
    """
    List all model configurations (public endpoint, no auth required).

    This endpoint is used by services (Gateway, Orchestrator, LLM Router) to fetch
    model configurations without requiring authentication.

    Query params:
    - enabled_only: If true (default), only return enabled configurations

    Returns:
        List of model configurations sorted by priority
    """
    logger.info("list_model_configs_public", enabled_only=enabled_only)

    query = db.query(ModelConfiguration)
    if enabled_only:
        query = query.filter(ModelConfiguration.enabled == True)

    configs = query.order_by(ModelConfiguration.priority, ModelConfiguration.model_name).all()

    return [ModelConfigResponse(**config.to_dict()) for config in configs]


@router.get("/public/{model_name}", response_model=ModelConfigResponse)
async def get_config_public(
    model_name: str,
    db: Session = Depends(get_db)
):
    """
    Get model configuration for a specific model (public endpoint).

    If no configuration exists for the model, returns the _default configuration.
    If no _default exists, returns 404.

    This endpoint is used by the LLM Router to fetch options for each model.
    """
    # Try to find specific config
    config = db.query(ModelConfiguration).filter(
        ModelConfiguration.model_name == model_name,
        ModelConfiguration.enabled == True
    ).first()

    # Fall back to _default if not found
    if not config:
        config = db.query(ModelConfiguration).filter(
            ModelConfiguration.model_name == "_default",
            ModelConfiguration.enabled == True
        ).first()

    if not config:
        logger.warning("model_config_not_found", model_name=model_name)
        raise HTTPException(
            status_code=404,
            detail=f"No configuration found for model '{model_name}' and no _default exists"
        )

    logger.debug("get_model_config_public", model_name=model_name, found=config.model_name)

    return ModelConfigResponse(**config.to_dict())


@router.get("/presets")
async def get_presets():
    """
    Get available preset configurations (public endpoint).

    Returns preset configurations that can be applied to any model.
    """
    return PRESET_CONFIGS


# ============================================================================
# Authenticated endpoints - for Admin UI
# ============================================================================

@router.get("", response_model=List[ModelConfigResponse])
async def list_configs(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all model configurations (authenticated endpoint).

    Query params:
    - enabled_only: If true, only return enabled configurations
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    logger.info("list_model_configs", user=current_user.username, enabled_only=enabled_only)

    query = db.query(ModelConfiguration)
    if enabled_only:
        query = query.filter(ModelConfiguration.enabled == True)

    configs = query.order_by(ModelConfiguration.model_name).all()

    return [ModelConfigResponse(**config.to_dict()) for config in configs]


@router.get("/{config_id}", response_model=ModelConfigResponse)
async def get_config(
    config_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get specific model configuration by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(ModelConfiguration).filter(ModelConfiguration.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    logger.info("get_model_config", config_id=config_id, user=current_user.username)

    return ModelConfigResponse(**config.to_dict())


@router.post("", response_model=ModelConfigResponse, status_code=201)
async def create_config(
    config_data: ModelConfigCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create new model configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate backend_type
    valid_types = ['ollama', 'mlx', 'auto']
    if config_data.backend_type not in valid_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid backend_type. Must be one of: {', '.join(valid_types)}"
        )

    # Check if model already configured
    existing = db.query(ModelConfiguration).filter(
        ModelConfiguration.model_name == config_data.model_name
    ).first()

    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Configuration for model '{config_data.model_name}' already exists"
        )

    config = ModelConfiguration(
        model_name=config_data.model_name,
        display_name=config_data.display_name,
        backend_type=config_data.backend_type,
        enabled=config_data.enabled,
        temperature=config_data.temperature,
        max_tokens=config_data.max_tokens,
        timeout_seconds=config_data.timeout_seconds,
        keep_alive_seconds=config_data.keep_alive_seconds,
        ollama_options=config_data.ollama_options or {},
        mlx_options=config_data.mlx_options or {},
        description=config_data.description,
        priority=config_data.priority
    )

    db.add(config)
    db.commit()
    db.refresh(config)

    logger.info(
        "created_model_config",
        config_id=config.id,
        model_name=config.model_name,
        user=current_user.username
    )

    return ModelConfigResponse(**config.to_dict())


@router.put("/{config_id}", response_model=ModelConfigResponse)
async def update_config(
    config_id: int,
    config_data: ModelConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update model configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(ModelConfiguration).filter(ModelConfiguration.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    # Validate backend_type if provided
    if config_data.backend_type is not None:
        valid_types = ['ollama', 'mlx', 'auto']
        if config_data.backend_type not in valid_types:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid backend_type. Must be one of: {', '.join(valid_types)}"
            )

    # Update fields
    update_data = config_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    db.commit()
    db.refresh(config)

    logger.info(
        "updated_model_config",
        config_id=config_id,
        model_name=config.model_name,
        updated_fields=list(update_data.keys()),
        user=current_user.username
    )

    return ModelConfigResponse(**config.to_dict())


@router.delete("/{config_id}", status_code=204)
async def delete_config(
    config_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete model configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(ModelConfiguration).filter(ModelConfiguration.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    # Prevent deletion of _default
    if config.model_name == "_default":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete _default configuration. Disable it instead."
        )

    model_name = config.model_name
    db.delete(config)
    db.commit()

    logger.info(
        "deleted_model_config",
        config_id=config_id,
        model_name=model_name,
        user=current_user.username
    )

    return None


@router.post("/{config_id}/toggle", response_model=ModelConfigResponse)
async def toggle_config(
    config_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Toggle enabled/disabled status of a model configuration."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(ModelConfiguration).filter(ModelConfiguration.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    config.enabled = not config.enabled
    db.commit()
    db.refresh(config)

    logger.info(
        "toggled_model_config",
        config_id=config_id,
        model_name=config.model_name,
        enabled=config.enabled,
        user=current_user.username
    )

    return ModelConfigResponse(**config.to_dict())


@router.post("/{config_id}/apply-preset", response_model=ModelConfigResponse)
async def apply_preset(
    config_id: int,
    preset_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Apply a preset configuration to an existing model config.

    Query params:
    - preset_name: One of 'speed', 'balanced', 'quality', 'mirostat', 'deterministic'
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    if preset_name not in PRESET_CONFIGS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid preset. Must be one of: {', '.join(PRESET_CONFIGS.keys())}"
        )

    config = db.query(ModelConfiguration).filter(ModelConfiguration.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Configuration not found")

    preset = PRESET_CONFIGS[preset_name]

    # Apply preset values
    config.temperature = preset.get("temperature", 0.7)
    config.ollama_options = preset.get("ollama_options", {})
    config.description = f"{preset['name']}: {preset['description']}"

    db.commit()
    db.refresh(config)

    logger.info(
        "applied_preset_to_model_config",
        config_id=config_id,
        model_name=config.model_name,
        preset=preset_name,
        user=current_user.username
    )

    return ModelConfigResponse(**config.to_dict())
