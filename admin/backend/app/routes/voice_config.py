"""
Voice Configuration API routes.

Provides endpoints for managing STT (Speech-to-Text) and TTS (Text-to-Speech) configuration:
- STT model selection (Whisper models)
- TTS voice selection (Piper voices)
- Service host/port configuration
- Internal endpoints for orchestrator/gateway
"""
import os
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import structlog

from app.database import get_db
from app.models import STTModel, TTSVoice, VoiceServiceConfig, User
from app.auth.oidc import get_current_user

logger = structlog.get_logger()
router = APIRouter(prefix="/api/voice-config", tags=["voice-config"])


# Pydantic models
class SetActiveRequest(BaseModel):
    id: int


class ServiceConfigUpdate(BaseModel):
    host: Optional[str] = None
    wyoming_port: Optional[int] = None
    rest_port: Optional[int] = None
    enabled: Optional[bool] = None


# ============================================================================
# STT Model Configuration
# ============================================================================

@router.get("/stt/models")
async def list_stt_models(
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all available STT models (no auth - public config)."""
    models = db.query(STTModel).order_by(STTModel.size_mb).all()
    return [model.to_dict() for model in models]


@router.get("/stt/active")
async def get_active_stt_model(
    db: Session = Depends(get_db)
) -> Optional[Dict[str, Any]]:
    """Get the currently active STT model (no auth - public config)."""
    model = db.query(STTModel).filter(STTModel.is_active == True).first()
    return model.to_dict() if model else None


@router.post("/stt/set-active")
async def set_active_stt_model(
    request: SetActiveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Set the active STT model.

    Note: Container restart required to apply changes.
    """
    # Deactivate all models
    db.query(STTModel).update({STTModel.is_active: False})

    # Activate selected model
    model = db.query(STTModel).filter(STTModel.id == request.id).first()
    if not model:
        raise HTTPException(status_code=404, detail="STT model not found")

    model.is_active = True
    db.commit()
    db.refresh(model)

    logger.info("stt_model_activated", model=model.name, user=current_user.username)
    return {
        "status": "success",
        "model": model.name,
        "model_name": model.model_name,
        "restart_required": True,
        "message": f"STT model set to {model.display_name}. Restart voice services to apply."
    }


# ============================================================================
# TTS Voice Configuration
# ============================================================================

@router.get("/tts/voices")
async def list_tts_voices(
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List all available TTS voices (no auth - public config)."""
    voices = db.query(TTSVoice).order_by(TTSVoice.display_name).all()
    return [voice.to_dict() for voice in voices]


@router.get("/tts/active")
async def get_active_tts_voice(
    db: Session = Depends(get_db)
) -> Optional[Dict[str, Any]]:
    """Get the currently active TTS voice (no auth - public config)."""
    voice = db.query(TTSVoice).filter(TTSVoice.is_active == True).first()
    return voice.to_dict() if voice else None


@router.post("/tts/set-active")
async def set_active_tts_voice(
    request: SetActiveRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Set the active TTS voice.

    Note: Container restart required to apply changes.
    """
    # Deactivate all voices
    db.query(TTSVoice).update({TTSVoice.is_active: False})

    # Activate selected voice
    voice = db.query(TTSVoice).filter(TTSVoice.id == request.id).first()
    if not voice:
        raise HTTPException(status_code=404, detail="TTS voice not found")

    voice.is_active = True
    db.commit()
    db.refresh(voice)

    logger.info("tts_voice_activated", voice=voice.name, user=current_user.username)
    return {
        "status": "success",
        "voice": voice.name,
        "voice_id": voice.voice_id,
        "restart_required": True,
        "message": f"TTS voice set to {voice.display_name}. Restart voice services to apply."
    }


# ============================================================================
# Service Configuration
# ============================================================================

@router.get("/services")
async def list_voice_services(
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List voice service configurations (no auth - public config)."""
    services = db.query(VoiceServiceConfig).all()
    return [service.to_dict() for service in services]


@router.get("/services/{service_type}")
async def get_voice_service(
    service_type: str,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get configuration for a specific service type (stt or tts)."""
    if service_type not in ("stt", "tts"):
        raise HTTPException(status_code=400, detail="Service type must be 'stt' or 'tts'")

    service = db.query(VoiceServiceConfig).filter(
        VoiceServiceConfig.service_type == service_type
    ).first()

    if not service:
        raise HTTPException(status_code=404, detail=f"{service_type.upper()} service not configured")

    return service.to_dict()


@router.put("/services/{service_type}")
async def update_voice_service(
    service_type: str,
    data: ServiceConfigUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update voice service configuration."""
    if service_type not in ("stt", "tts"):
        raise HTTPException(status_code=400, detail="Service type must be 'stt' or 'tts'")

    service = db.query(VoiceServiceConfig).filter(
        VoiceServiceConfig.service_type == service_type
    ).first()

    if not service:
        raise HTTPException(status_code=404, detail=f"{service_type.upper()} service not configured")

    update_data = data.dict(exclude_unset=True)
    for key, value in update_data.items():
        setattr(service, key, value)

    db.commit()
    db.refresh(service)

    logger.info("voice_service_updated", service_type=service_type, user=current_user.get("username"))
    return service.to_dict()


# ============================================================================
# Internal Endpoints (for orchestrator/gateway - no auth)
# ============================================================================

@router.get("/internal/stt")
async def internal_get_stt_config(
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get complete STT configuration for services.

    Returns model info and connection details for orchestrator/gateway.
    """
    model = db.query(STTModel).filter(STTModel.is_active == True).first()
    service = db.query(VoiceServiceConfig).filter(
        VoiceServiceConfig.service_type == "stt"
    ).first()

    if not model or not service:
        return {"configured": False, "enabled": False}

    return {
        "configured": True,
        "enabled": service.enabled,
        "model": {
            "name": model.name,
            "model_name": model.model_name,
            "engine": model.engine,
            "display_name": model.display_name,
        },
        "service": {
            "host": service.host,
            "wyoming_port": service.wyoming_port,
            "rest_port": service.rest_port,
            "wyoming_url": f"tcp://{service.host}:{service.wyoming_port}",
            "rest_url": f"http://{service.host}:{service.rest_port}" if service.rest_port else None,
        }
    }


@router.get("/internal/tts")
async def internal_get_tts_config(
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get complete TTS configuration for services.

    Returns voice info and connection details for orchestrator/gateway.
    """
    voice = db.query(TTSVoice).filter(TTSVoice.is_active == True).first()
    service = db.query(VoiceServiceConfig).filter(
        VoiceServiceConfig.service_type == "tts"
    ).first()

    if not voice or not service:
        return {"configured": False, "enabled": False}

    return {
        "configured": True,
        "enabled": service.enabled,
        "voice": {
            "name": voice.name,
            "voice_id": voice.voice_id,
            "engine": voice.engine,
            "quality": voice.quality,
            "display_name": voice.display_name,
        },
        "service": {
            "host": service.host,
            "wyoming_port": service.wyoming_port,
            "rest_port": service.rest_port,
            "wyoming_url": f"tcp://{service.host}:{service.wyoming_port}",
            "rest_url": f"http://{service.host}:{service.rest_port}" if service.rest_port else None,
        }
    }


@router.get("/internal/all")
async def internal_get_all_voice_config(
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get complete voice configuration (STT + TTS) for services.

    Single endpoint to fetch all voice config at once.
    """
    stt_model = db.query(STTModel).filter(STTModel.is_active == True).first()
    tts_voice = db.query(TTSVoice).filter(TTSVoice.is_active == True).first()
    stt_service = db.query(VoiceServiceConfig).filter(
        VoiceServiceConfig.service_type == "stt"
    ).first()
    tts_service = db.query(VoiceServiceConfig).filter(
        VoiceServiceConfig.service_type == "tts"
    ).first()

    return {
        "stt": {
            "configured": stt_model is not None and stt_service is not None,
            "enabled": stt_service.enabled if stt_service else False,
            "model_name": stt_model.model_name if stt_model else None,
            "wyoming_url": f"tcp://{stt_service.host}:{stt_service.wyoming_port}" if stt_service else None,
            "rest_url": f"http://{stt_service.host}:{stt_service.rest_port}" if stt_service and stt_service.rest_port else None,
        },
        "tts": {
            "configured": tts_voice is not None and tts_service is not None,
            "enabled": tts_service.enabled if tts_service else False,
            "voice_id": tts_voice.voice_id if tts_voice else None,
            "wyoming_url": f"tcp://{tts_service.host}:{tts_service.wyoming_port}" if tts_service else None,
            "rest_url": f"http://{tts_service.host}:{tts_service.rest_port}" if tts_service and tts_service.rest_port else None,
        }
    }


# ============================================================================
# Health Check for Voice Services
# ============================================================================

@router.get("/health")
async def check_voice_services_health(
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Check health of voice services.

    Attempts to connect to STT and TTS REST endpoints.
    """
    import httpx

    services = db.query(VoiceServiceConfig).all()
    health = {}

    for service in services:
        service_type = service.service_type
        if not service.enabled:
            health[service_type] = {"status": "disabled"}
            continue

        if not service.rest_port:
            health[service_type] = {"status": "no_rest_endpoint"}
            continue

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                url = f"http://{service.host}:{service.rest_port}/health"
                response = await client.get(url)

                if response.status_code == 200:
                    health[service_type] = {
                        "status": "healthy",
                        "url": url,
                        "response": response.json() if response.headers.get("content-type", "").startswith("application/json") else None
                    }
                else:
                    health[service_type] = {
                        "status": "unhealthy",
                        "url": url,
                        "status_code": response.status_code
                    }
        except httpx.ConnectError:
            health[service_type] = {
                "status": "unreachable",
                "url": f"http://{service.host}:{service.rest_port}/health",
                "error": "Connection refused"
            }
        except Exception as e:
            health[service_type] = {
                "status": "error",
                "error": str(e)
            }

    return health


# ============================================================================
# Voice Control Proxy (forwards to Mac mini voice-control API)
# ============================================================================

VOICE_CONTROL_URL = os.getenv("VOICE_CONTROL_URL", "http://localhost:8098")


@router.get("/running-config")
async def get_running_voice_config() -> Dict[str, Any]:
    """
    Proxy to get running configuration from Mac mini containers.

    This endpoint forwards to the voice-control API on Mac mini to get
    the actual running Whisper model and Piper voice from containers.
    """
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{VOICE_CONTROL_URL}/running-config")
            if response.status_code == 200:
                return response.json()
            return {"error": f"Voice control returned {response.status_code}"}
    except httpx.ConnectError:
        return {"error": "Voice control API unreachable", "loaded": False}
    except Exception as e:
        logger.warning("running_config_proxy_error", error=str(e))
        return {"error": str(e), "loaded": False}


@router.post("/restart-services")
async def restart_voice_services_proxy(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Proxy to restart voice services on Mac mini.

    Also updates the .env config on Mac mini with current admin settings.
    """
    import httpx

    # Get current active settings from database
    stt_model = db.query(STTModel).filter(STTModel.is_active == True).first()
    tts_voice = db.query(TTSVoice).filter(TTSVoice.is_active == True).first()

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # First update config
            config_update = {}
            if stt_model:
                config_update["whisper_model"] = stt_model.model_name
            if tts_voice:
                config_update["piper_voice"] = tts_voice.voice_id

            if config_update:
                await client.post(
                    f"{VOICE_CONTROL_URL}/config",
                    json=config_update
                )

            # Then restart services
            response = await client.post(f"{VOICE_CONTROL_URL}/restart-voice-services")
            if response.status_code == 200:
                logger.info(
                    "voice_services_restarted",
                    user=current_user.get("username"),
                    whisper_model=stt_model.model_name if stt_model else None,
                    piper_voice=tts_voice.voice_id if tts_voice else None
                )
                return response.json()
            return {"error": f"Restart failed with status {response.status_code}"}
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="Voice control API unreachable on Mac mini")
    except Exception as e:
        logger.error("restart_services_proxy_error", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
