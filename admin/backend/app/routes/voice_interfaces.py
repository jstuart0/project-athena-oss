"""
Voice Interfaces API routes.

Provides CRUD operations for voice interface configuration:
- Per-interface STT/TTS engine selection
- Wake word settings
- Behavior configuration (continuous conversation, debug mode)
- Rate limiting configuration

Internal endpoints for Gateway/Orchestrator to fetch interface configs.
"""
from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.models import VoiceInterface, STTEngine, TTSEngine
from app.auth.oidc import get_current_user, User

logger = structlog.get_logger()
router = APIRouter(prefix="/api/voice-interfaces", tags=["voice-interfaces"])


# =============================================================================
# Pydantic Models
# =============================================================================

class STTConfigModel(BaseModel):
    """STT engine-specific configuration."""
    model: Optional[str] = "base.en"
    language: Optional[str] = "en"
    beam_size: Optional[int] = 5


class TTSConfigModel(BaseModel):
    """TTS engine-specific configuration."""
    voice: Optional[str] = "en_US-amy-medium"
    speed: Optional[float] = 1.0
    noise_scale: Optional[float] = 0.667


class VoiceInterfaceCreate(BaseModel):
    """Request model for creating a voice interface."""
    interface_name: str = Field(..., min_length=1, max_length=100)
    display_name: Optional[str] = None
    description: Optional[str] = None
    enabled: bool = True

    # STT Configuration
    stt_engine: str = "faster-whisper"
    stt_config: Optional[Dict[str, Any]] = None

    # TTS Configuration
    tts_engine: str = "piper"
    tts_config: Optional[Dict[str, Any]] = None

    # Behavior
    wake_word_enabled: bool = False
    wake_word: Optional[str] = None
    continuous_conversation: bool = True
    debug_mode: bool = False

    # Rate limiting
    max_requests_per_minute: int = 30


class VoiceInterfaceUpdate(BaseModel):
    """Request model for updating a voice interface."""
    display_name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None

    stt_engine: Optional[str] = None
    stt_config: Optional[Dict[str, Any]] = None

    tts_engine: Optional[str] = None
    tts_config: Optional[Dict[str, Any]] = None

    wake_word_enabled: Optional[bool] = None
    wake_word: Optional[str] = None
    continuous_conversation: Optional[bool] = None
    debug_mode: Optional[bool] = None

    max_requests_per_minute: Optional[int] = None


class VoiceInterfaceResponse(BaseModel):
    """Response model for voice interface."""
    id: int
    interface_name: str
    display_name: Optional[str]
    description: Optional[str]
    enabled: bool
    stt_engine: str
    stt_config: Dict[str, Any]
    tts_engine: str
    tts_config: Dict[str, Any]
    wake_word_enabled: bool
    wake_word: Optional[str]
    continuous_conversation: bool
    debug_mode: bool
    max_requests_per_minute: int
    created_at: Optional[str]
    updated_at: Optional[str]

    class Config:
        from_attributes = True


class EngineResponse(BaseModel):
    """Response model for STT/TTS engine."""
    id: int
    engine_name: str
    display_name: Optional[str]
    description: Optional[str]
    endpoint_url: Optional[str]
    enabled: bool
    requires_gpu: bool
    is_cloud: bool
    default_config: Dict[str, Any]

    class Config:
        from_attributes = True


# =============================================================================
# Voice Interface CRUD Endpoints (Authenticated)
# =============================================================================

@router.get("", response_model=List[VoiceInterfaceResponse])
async def list_voice_interfaces(
    enabled_only: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    List all voice interfaces.

    Query params:
    - enabled_only: If true, only return enabled interfaces
    """
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(VoiceInterface)
    if enabled_only:
        query = query.filter(VoiceInterface.enabled == True)

    interfaces = query.order_by(VoiceInterface.interface_name).all()
    logger.info("list_voice_interfaces", user=current_user.username, count=len(interfaces))

    return [VoiceInterfaceResponse(**vi.to_dict()) for vi in interfaces]


@router.get("/{interface_name}", response_model=VoiceInterfaceResponse)
async def get_voice_interface(
    interface_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific voice interface by name."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    interface = db.query(VoiceInterface).filter(
        VoiceInterface.interface_name == interface_name
    ).first()

    if not interface:
        raise HTTPException(status_code=404, detail=f"Voice interface '{interface_name}' not found")

    logger.info("get_voice_interface", interface_name=interface_name, user=current_user.username)
    return VoiceInterfaceResponse(**interface.to_dict())


@router.post("", response_model=VoiceInterfaceResponse)
async def create_voice_interface(
    data: VoiceInterfaceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new voice interface."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Check if interface already exists
    existing = db.query(VoiceInterface).filter(
        VoiceInterface.interface_name == data.interface_name
    ).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Voice interface '{data.interface_name}' already exists"
        )

    interface = VoiceInterface(
        interface_name=data.interface_name,
        display_name=data.display_name or data.interface_name.replace('_', ' ').title(),
        description=data.description,
        enabled=data.enabled,
        stt_engine=data.stt_engine,
        stt_config=data.stt_config or {},
        tts_engine=data.tts_engine,
        tts_config=data.tts_config or {},
        wake_word_enabled=data.wake_word_enabled,
        wake_word=data.wake_word,
        continuous_conversation=data.continuous_conversation,
        debug_mode=data.debug_mode,
        max_requests_per_minute=data.max_requests_per_minute,
    )

    db.add(interface)
    db.commit()
    db.refresh(interface)

    logger.info(
        "create_voice_interface",
        interface_name=interface.interface_name,
        user=current_user.username
    )

    return VoiceInterfaceResponse(**interface.to_dict())


@router.put("/{interface_name}", response_model=VoiceInterfaceResponse)
async def update_voice_interface(
    interface_name: str,
    data: VoiceInterfaceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing voice interface."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    interface = db.query(VoiceInterface).filter(
        VoiceInterface.interface_name == interface_name
    ).first()

    if not interface:
        raise HTTPException(status_code=404, detail=f"Voice interface '{interface_name}' not found")

    # Update fields
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(interface, field, value)

    db.commit()
    db.refresh(interface)

    logger.info(
        "update_voice_interface",
        interface_name=interface_name,
        updated_fields=list(update_data.keys()),
        user=current_user.username
    )

    return VoiceInterfaceResponse(**interface.to_dict())


@router.delete("/{interface_name}")
async def delete_voice_interface(
    interface_name: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Delete a voice interface."""
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    interface = db.query(VoiceInterface).filter(
        VoiceInterface.interface_name == interface_name
    ).first()

    if not interface:
        raise HTTPException(status_code=404, detail=f"Voice interface '{interface_name}' not found")

    # Prevent deletion of core interfaces
    core_interfaces = ['web_jarvis', 'home_assistant', 'admin_jarvis']
    if interface_name in core_interfaces:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot delete core interface '{interface_name}'. Disable it instead."
        )

    db.delete(interface)
    db.commit()

    logger.info(
        "delete_voice_interface",
        interface_name=interface_name,
        user=current_user.username
    )

    return {"status": "deleted", "interface_name": interface_name}


# =============================================================================
# Engine Endpoints (List available engines)
# =============================================================================

@router.get("/engines/stt", response_model=List[EngineResponse])
async def list_stt_engines(
    enabled_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List available STT engines."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(STTEngine)
    if enabled_only:
        query = query.filter(STTEngine.enabled == True)

    engines = query.order_by(STTEngine.engine_name).all()
    return [EngineResponse(**e.to_dict()) for e in engines]


@router.get("/engines/tts", response_model=List[EngineResponse])
async def list_tts_engines(
    enabled_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List available TTS engines."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    query = db.query(TTSEngine)
    if enabled_only:
        query = query.filter(TTSEngine.enabled == True)

    engines = query.order_by(TTSEngine.engine_name).all()
    return [EngineResponse(**e.to_dict()) for e in engines]


# =============================================================================
# Internal/Public Endpoints (for Gateway/Orchestrator)
# =============================================================================

@router.get("/public", response_model=List[VoiceInterfaceResponse])
async def list_voice_interfaces_public(
    enabled_only: bool = True,
    db: Session = Depends(get_db)
):
    """
    List voice interfaces (public endpoint, no auth).

    Used by Gateway/Orchestrator to fetch interface configurations.
    """
    query = db.query(VoiceInterface)
    if enabled_only:
        query = query.filter(VoiceInterface.enabled == True)

    interfaces = query.order_by(VoiceInterface.interface_name).all()
    logger.debug("list_voice_interfaces_public", count=len(interfaces))

    return [VoiceInterfaceResponse(**vi.to_dict()) for vi in interfaces]


@router.get("/public/{interface_name}", response_model=VoiceInterfaceResponse)
async def get_voice_interface_public(
    interface_name: str,
    db: Session = Depends(get_db)
):
    """
    Get a specific voice interface (public endpoint, no auth).

    Used by Gateway/Orchestrator to fetch a specific interface config.
    """
    interface = db.query(VoiceInterface).filter(
        VoiceInterface.interface_name == interface_name
    ).first()

    if not interface:
        raise HTTPException(status_code=404, detail=f"Voice interface '{interface_name}' not found")

    logger.debug("get_voice_interface_public", interface_name=interface_name)
    return VoiceInterfaceResponse(**interface.to_dict())


@router.get("/internal/config/{interface_name}")
async def get_interface_full_config(
    interface_name: str,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get complete voice interface configuration for services.

    Returns interface config plus resolved engine endpoints.
    Used by Gateway to set up STT/TTS connections.
    """
    interface = db.query(VoiceInterface).filter(
        VoiceInterface.interface_name == interface_name
    ).first()

    if not interface:
        return {"configured": False, "enabled": False}

    # Get engine details
    stt_engine = db.query(STTEngine).filter(
        STTEngine.engine_name == interface.stt_engine
    ).first()

    tts_engine = db.query(TTSEngine).filter(
        TTSEngine.engine_name == interface.tts_engine
    ).first()

    return {
        "configured": True,
        "enabled": interface.enabled,
        "interface": interface.to_dict(),
        "stt": {
            "engine": stt_engine.to_dict() if stt_engine else None,
            "config": interface.stt_config,
        },
        "tts": {
            "engine": tts_engine.to_dict() if tts_engine else None,
            "config": interface.tts_config,
        },
        "behavior": {
            "wake_word_enabled": interface.wake_word_enabled,
            "wake_word": interface.wake_word,
            "continuous_conversation": interface.continuous_conversation,
            "debug_mode": interface.debug_mode,
        }
    }


@router.get("/engines/public/stt", response_model=List[EngineResponse])
async def list_stt_engines_public(
    enabled_only: bool = True,
    db: Session = Depends(get_db)
):
    """List available STT engines (public endpoint)."""
    query = db.query(STTEngine)
    if enabled_only:
        query = query.filter(STTEngine.enabled == True)

    engines = query.order_by(STTEngine.engine_name).all()
    return [EngineResponse(**e.to_dict()) for e in engines]


@router.get("/engines/public/tts", response_model=List[EngineResponse])
async def list_tts_engines_public(
    enabled_only: bool = True,
    db: Session = Depends(get_db)
):
    """List available TTS engines (public endpoint)."""
    query = db.query(TTSEngine)
    if enabled_only:
        query = query.filter(TTSEngine.enabled == True)

    engines = query.order_by(TTSEngine.engine_name).all()
    return [EngineResponse(**e.to_dict()) for e in engines]
