"""
Performance Presets Routes

CRUD operations for performance presets and preset activation.
Presets bundle all performance-related settings for easy A/B testing.
"""

import asyncio
from datetime import datetime
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
import structlog
import httpx

from app.database import get_db
from app.models import (
    PerformancePreset, User, Feature, ConversationSettings,
    GatewayConfig, ComponentModelAssignment
)
from app.auth.oidc import get_current_user

import os

logger = structlog.get_logger()
router = APIRouter(prefix="/api/presets", tags=["performance-presets"])

# Service URLs from environment
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")

# Service endpoints for cache invalidation
CACHE_INVALIDATION_ENDPOINTS = [
    f"{GATEWAY_URL}/admin/invalidate-feature-cache",
    f"{ORCHESTRATOR_URL}/admin/invalidate-feature-cache",
]


# Pydantic Models
class PresetSettings(BaseModel):
    """Settings stored in a preset."""
    # Gateway intent classification (fast binary routing)
    gateway_intent_model: str = "phi3:mini"
    gateway_intent_temperature: float = Field(0.1, ge=0.0, le=2.0)
    gateway_intent_max_tokens: int = Field(10, ge=1, le=100)

    # Orchestrator component models (complexity-based routing)
    intent_classifier_model: str = "qwen3:4b"  # Fast classification
    tool_calling_simple_model: str = "qwen3:4b-instruct-2507-q4_K_M"  # Simple queries
    tool_calling_complex_model: str = "qwen3:4b-instruct-2507-q4_K_M"  # Complex queries
    tool_calling_super_complex_model: str = "qwen3:8b"  # Deep reasoning
    response_synthesis_model: str = "qwen3:4b-instruct-2507-q4_K_M"  # Final response generation
    conversation_summarizer_model: str = "qwen3:4b"  # Conversation history summarization

    # LLM generation settings
    llm_temperature: float = Field(0.5, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(512, ge=64, le=4096)
    llm_keep_alive_seconds: int = -1

    # Conversation history
    history_mode: str = Field("summarized", pattern="^(none|summarized|full)$")
    max_llm_history_messages: int = Field(5, ge=0, le=50)

    # HA optimization flags
    feature_flags: Dict[str, bool] = {}


class PresetResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    is_system: bool
    is_active: bool
    settings: Dict[str, Any]
    estimated_latency_ms: Optional[int]
    icon: Optional[str]
    created_by_id: Optional[int]
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class PresetCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    settings: PresetSettings
    estimated_latency_ms: Optional[int] = None
    icon: Optional[str] = None


class PresetUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    settings: Optional[PresetSettings] = None
    estimated_latency_ms: Optional[int] = None
    icon: Optional[str] = None


# Routes
@router.get("", response_model=List[PresetResponse])
async def list_presets(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List all presets (system + user's own)."""
    presets = db.query(PerformancePreset).filter(
        (PerformancePreset.is_system == True) |
        (PerformancePreset.created_by_id == current_user.id)
    ).order_by(PerformancePreset.is_system.desc(), PerformancePreset.name).all()

    return [PresetResponse(**p.to_dict()) for p in presets]


@router.get("/active", response_model=PresetResponse)
async def get_active_preset(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get the currently active preset."""
    preset = db.query(PerformancePreset).filter(
        PerformancePreset.is_active == True
    ).first()

    if not preset:
        raise HTTPException(status_code=404, detail="No active preset found")

    return PresetResponse(**preset.to_dict())


@router.get("/public/active")
async def get_active_preset_public(db: Session = Depends(get_db)):
    """Get active preset settings (public endpoint for services)."""
    preset = db.query(PerformancePreset).filter(
        PerformancePreset.is_active == True
    ).first()

    if not preset:
        return {"active": False, "settings": None}

    return {
        "active": True,
        "name": preset.name,
        "settings": preset.settings
    }


@router.get("/{preset_id}", response_model=PresetResponse)
async def get_preset(
    preset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific preset."""
    preset = db.query(PerformancePreset).filter(
        PerformancePreset.id == preset_id
    ).first()

    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    return PresetResponse(**preset.to_dict())


@router.post("", response_model=PresetResponse, status_code=201)
async def create_preset(
    data: PresetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new user preset."""
    # Check for duplicate name
    existing = db.query(PerformancePreset).filter(
        PerformancePreset.name == data.name
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Preset name already exists")

    preset = PerformancePreset(
        name=data.name,
        description=data.description,
        is_system=False,
        is_active=False,
        settings=data.settings.model_dump(),
        estimated_latency_ms=data.estimated_latency_ms,
        icon=data.icon,
        created_by_id=current_user.id
    )

    db.add(preset)
    db.commit()
    db.refresh(preset)

    logger.info("preset_created", preset_id=preset.id, name=preset.name, user=current_user.username)

    return PresetResponse(**preset.to_dict())


@router.post("/{preset_id}/duplicate", response_model=PresetResponse, status_code=201)
async def duplicate_preset(
    preset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Duplicate a preset (useful for customizing system presets)."""
    original = db.query(PerformancePreset).filter(
        PerformancePreset.id == preset_id
    ).first()

    if not original:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Generate unique name
    base_name = f"{original.name} (Copy)"
    name = base_name
    counter = 1
    while db.query(PerformancePreset).filter(PerformancePreset.name == name).first():
        counter += 1
        name = f"{base_name} {counter}"

    preset = PerformancePreset(
        name=name,
        description=original.description,
        is_system=False,
        is_active=False,
        settings=original.settings.copy() if original.settings else {},
        estimated_latency_ms=original.estimated_latency_ms,
        icon=original.icon,
        created_by_id=current_user.id
    )

    db.add(preset)
    db.commit()
    db.refresh(preset)

    logger.info("preset_duplicated", original_id=preset_id, new_id=preset.id, user=current_user.username)

    return PresetResponse(**preset.to_dict())


@router.put("/{preset_id}", response_model=PresetResponse)
async def update_preset(
    preset_id: int,
    data: PresetUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a user preset (system presets are read-only)."""
    preset = db.query(PerformancePreset).filter(
        PerformancePreset.id == preset_id
    ).first()

    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    if preset.is_system:
        raise HTTPException(status_code=403, detail="System presets are read-only. Duplicate to customize.")

    if preset.created_by_id != current_user.id and current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Can only edit your own presets")

    # Check for duplicate name
    if data.name and data.name != preset.name:
        existing = db.query(PerformancePreset).filter(
            PerformancePreset.name == data.name
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Preset name already exists")

    # Update fields
    if data.name is not None:
        preset.name = data.name
    if data.description is not None:
        preset.description = data.description
    if data.settings is not None:
        preset.settings = data.settings.model_dump()
    if data.estimated_latency_ms is not None:
        preset.estimated_latency_ms = data.estimated_latency_ms
    if data.icon is not None:
        preset.icon = data.icon

    db.commit()
    db.refresh(preset)

    # If this is the active preset, apply changes
    if preset.is_active:
        await apply_preset_settings(preset, db)

    logger.info("preset_updated", preset_id=preset.id, user=current_user.username)

    return PresetResponse(**preset.to_dict())


@router.delete("/{preset_id}")
async def delete_preset(
    preset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a user preset (system presets cannot be deleted)."""
    preset = db.query(PerformancePreset).filter(
        PerformancePreset.id == preset_id
    ).first()

    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    if preset.is_system:
        raise HTTPException(status_code=403, detail="System presets cannot be deleted")

    if preset.created_by_id != current_user.id and current_user.role != 'admin':
        raise HTTPException(status_code=403, detail="Can only delete your own presets")

    if preset.is_active:
        raise HTTPException(status_code=400, detail="Cannot delete active preset. Activate another preset first.")

    db.delete(preset)
    db.commit()

    logger.info("preset_deleted", preset_id=preset_id, user=current_user.username)

    return {"success": True, "message": "Preset deleted"}


@router.post("/capture-current", response_model=PresetResponse, status_code=201)
async def capture_current_settings(
    name: str = Query(..., min_length=1, max_length=100),
    description: Optional[str] = Query(None),
    icon: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Capture current system settings as a new preset.

    Reads the current state from all settings tables and creates
    a new user preset with those values.
    """
    # Check for duplicate name
    existing = db.query(PerformancePreset).filter(
        PerformancePreset.name == name
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Preset name already exists")

    # Capture ALL current feature flags
    feature_flags = {}
    all_features = db.query(Feature).all()
    for feature in all_features:
        feature_flags[feature.name] = feature.enabled

    # Capture conversation settings
    conv_settings = db.query(ConversationSettings).first()

    # Capture gateway config
    gateway_config = db.query(GatewayConfig).filter(GatewayConfig.id == 1).first()

    # Capture component model assignments
    component_models = {}
    component_names = [
        'intent_classifier', 'tool_calling_simple', 'tool_calling_complex',
        'tool_calling_super_complex', 'response_synthesis', 'conversation_summarizer'
    ]
    for comp_name in component_names:
        comp = db.query(ComponentModelAssignment).filter(ComponentModelAssignment.component_name == comp_name).first()
        if comp and comp.enabled:
            component_models[comp_name] = comp.model_name

    # Build settings snapshot with all 5 component models
    settings = {
        # Gateway intent classification
        "gateway_intent_model": gateway_config.intent_model if gateway_config else "phi3:mini",
        "gateway_intent_temperature": gateway_config.intent_temperature if gateway_config else 0.1,
        "gateway_intent_max_tokens": gateway_config.intent_max_tokens if gateway_config else 10,

        # Orchestrator component models
        "intent_classifier_model": component_models.get('intent_classifier', 'qwen3:4b'),
        "tool_calling_simple_model": component_models.get('tool_calling_simple', 'qwen3:4b-instruct-2507-q4_K_M'),
        "tool_calling_complex_model": component_models.get('tool_calling_complex', 'qwen3:4b-instruct-2507-q4_K_M'),
        "tool_calling_super_complex_model": component_models.get('tool_calling_super_complex', 'qwen3:8b'),
        "response_synthesis_model": component_models.get('response_synthesis', 'qwen3:4b-instruct-2507-q4_K_M'),
        "conversation_summarizer_model": component_models.get('conversation_summarizer', 'qwen3:4b'),

        # General LLM settings
        "llm_temperature": 0.5,
        "llm_max_tokens": 512,
        "llm_keep_alive_seconds": -1,

        # Conversation history
        "history_mode": conv_settings.history_mode if conv_settings else "full",
        "max_llm_history_messages": conv_settings.max_llm_history_messages if conv_settings else 10,

        # Feature flags
        "feature_flags": feature_flags
    }

    # Create the preset
    preset = PerformancePreset(
        name=name,
        description=description or f"Captured from current settings on {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        is_system=False,
        is_active=False,
        settings=settings,
        icon=icon,
        created_by_id=current_user.id
    )

    db.add(preset)
    db.commit()
    db.refresh(preset)

    logger.info("preset_captured", preset_id=preset.id, name=preset.name, user=current_user.username)

    return PresetResponse(**preset.to_dict())


@router.post("/{preset_id}/activate", response_model=PresetResponse)
async def activate_preset(
    preset_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Activate a preset and apply all its settings."""
    preset = db.query(PerformancePreset).filter(
        PerformancePreset.id == preset_id
    ).first()

    if not preset:
        raise HTTPException(status_code=404, detail="Preset not found")

    # Deactivate all other presets
    db.query(PerformancePreset).filter(
        PerformancePreset.is_active == True
    ).update({"is_active": False})

    # Activate this preset
    preset.is_active = True
    db.commit()

    # Apply all settings from the preset
    await apply_preset_settings(preset, db)

    logger.info("preset_activated", preset_id=preset.id, name=preset.name, user=current_user.username)

    return PresetResponse(**preset.to_dict())


async def apply_preset_settings(preset: PerformancePreset, db: Session):
    """Apply all settings from a preset to the actual configuration tables."""
    settings = preset.settings

    # 1. Update feature flags
    feature_flags = settings.get('feature_flags', {})
    for flag_name, enabled in feature_flags.items():
        feature = db.query(Feature).filter(Feature.name == flag_name).first()
        if feature:
            feature.enabled = enabled

    # 2. Update conversation settings
    conv_settings = db.query(ConversationSettings).first()
    if conv_settings:
        if 'history_mode' in settings:
            conv_settings.history_mode = settings['history_mode']
        if 'max_llm_history_messages' in settings:
            conv_settings.max_llm_history_messages = settings['max_llm_history_messages']

    # 3. Update gateway config (intent classification)
    gateway_config = db.query(GatewayConfig).filter(GatewayConfig.id == 1).first()
    if gateway_config:
        if 'gateway_intent_model' in settings:
            gateway_config.intent_model = settings['gateway_intent_model']
        if 'gateway_intent_temperature' in settings:
            gateway_config.intent_temperature = settings['gateway_intent_temperature']
        if 'gateway_intent_max_tokens' in settings:
            gateway_config.intent_max_tokens = settings['gateway_intent_max_tokens']

    # 4. Update component model assignments (6 components)
    component_mappings = {
        'intent_classifier': settings.get('intent_classifier_model'),
        'tool_calling_simple': settings.get('tool_calling_simple_model'),
        'tool_calling_complex': settings.get('tool_calling_complex_model'),
        'tool_calling_super_complex': settings.get('tool_calling_super_complex_model'),
        'response_synthesis': settings.get('response_synthesis_model'),
        'conversation_summarizer': settings.get('conversation_summarizer_model'),
    }

    for comp_name, model_name in component_mappings.items():
        if model_name:
            comp = db.query(ComponentModelAssignment).filter(
                ComponentModelAssignment.component_name == comp_name
            ).first()
            if comp:
                comp.model_name = model_name
                comp.enabled = True
            else:
                # Create if doesn't exist
                new_comp = ComponentModelAssignment(
                    component_name=comp_name,
                    model_name=model_name,
                    enabled=True
                )
                db.add(new_comp)

    db.commit()

    # 5. Invalidate caches on Gateway and Orchestrator
    await invalidate_service_caches()

    logger.info("preset_settings_applied", preset_name=preset.name)


async def invalidate_service_caches():
    """Notify Gateway and Orchestrator to invalidate their caches."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        tasks = []
        for endpoint in CACHE_INVALIDATION_ENDPOINTS:
            tasks.append(client.post(endpoint))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for endpoint, result in zip(CACHE_INVALIDATION_ENDPOINTS, results):
            if isinstance(result, Exception):
                logger.warning("cache_invalidation_failed", endpoint=endpoint, error=str(result))
            else:
                logger.info("cache_invalidated", endpoint=endpoint, status_code=result.status_code)
