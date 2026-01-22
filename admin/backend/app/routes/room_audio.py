"""
Room Audio Configuration API routes.

Provides endpoints for managing per-room audio output configuration:
- Single speaker, stereo pair, and group configurations
- Entity discovery from Home Assistant
- Audio testing
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
from datetime import datetime
import structlog
import httpx
import os
import re

from app.database import get_db
from app.models import RoomAudioConfig
from app.auth.oidc import get_current_user

logger = structlog.get_logger()
router = APIRouter(prefix="/api/room-audio", tags=["room-audio"])

# Home Assistant configuration - defaults empty, should be configured
HA_URL = os.getenv("HA_URL", "")
HA_TOKEN = os.getenv("HA_TOKEN", "")


# Pydantic models
class RoomAudioCreate(BaseModel):
    room_name: str
    display_name: Optional[str] = None
    speaker_type: str = "single"  # single, stereo_pair, group
    primary_entity_id: Optional[str] = None  # Required for single/stereo_pair, optional for group
    secondary_entity_id: Optional[str] = None
    group_entity_ids: Optional[List[str]] = None
    default_volume: float = 0.5
    preferred_provider: Optional[str] = "music_assistant"
    use_radio_mode: bool = True
    enabled: bool = True


class RoomAudioUpdate(BaseModel):
    display_name: Optional[str] = None
    speaker_type: Optional[str] = None
    primary_entity_id: Optional[str] = None
    secondary_entity_id: Optional[str] = None
    group_entity_ids: Optional[List[str]] = None
    default_volume: Optional[float] = None
    preferred_provider: Optional[str] = None
    use_radio_mode: Optional[bool] = None
    enabled: Optional[bool] = None


# ============================================================================
# Room Audio Configuration CRUD
# ============================================================================

@router.get("")
async def list_room_configs(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, Any]]:
    """List all room audio configurations."""
    configs = db.query(RoomAudioConfig).order_by(RoomAudioConfig.room_name).all()
    return [config.to_dict() for config in configs]


@router.get("/internal")
async def list_room_configs_internal(
    db: Session = Depends(get_db)
) -> List[Dict[str, Any]]:
    """List enabled room configs for orchestrator (no auth)."""
    configs = db.query(RoomAudioConfig).filter(
        RoomAudioConfig.enabled == True
    ).order_by(RoomAudioConfig.room_name).all()
    return [config.to_dict() for config in configs]


@router.get("/internal/{room_name}")
async def get_room_config_internal(
    room_name: str,
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get room config by name for orchestrator (no auth)."""
    config = db.query(RoomAudioConfig).filter(
        RoomAudioConfig.room_name == room_name.lower(),
        RoomAudioConfig.enabled == True
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room not found")

    return config.to_dict()


@router.get("/{room_name}")
async def get_room_config(
    room_name: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get a specific room configuration by name."""
    config = db.query(RoomAudioConfig).filter(
        RoomAudioConfig.room_name == room_name.lower()
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room not found")

    return config.to_dict()


@router.post("")
async def create_room_config(
    data: RoomAudioCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Create a new room audio configuration."""
    # Check for duplicate
    existing = db.query(RoomAudioConfig).filter(
        RoomAudioConfig.room_name == data.room_name.lower()
    ).first()

    if existing:
        raise HTTPException(status_code=400, detail="Room already configured")

    # Validate speaker type configuration
    if data.speaker_type in ("single", "stereo_pair") and not data.primary_entity_id:
        raise HTTPException(status_code=400, detail="Primary entity is required for single/stereo configurations")

    if data.speaker_type == "stereo_pair" and not data.secondary_entity_id:
        raise HTTPException(status_code=400, detail="Stereo pair requires secondary entity")

    if data.speaker_type == "group" and not data.group_entity_ids:
        raise HTTPException(status_code=400, detail="Group requires at least one group entity")

    config = RoomAudioConfig(
        room_name=data.room_name.lower(),
        display_name=data.display_name or data.room_name.title(),
        speaker_type=data.speaker_type,
        primary_entity_id=data.primary_entity_id,
        secondary_entity_id=data.secondary_entity_id,
        group_entity_ids=data.group_entity_ids or [],
        default_volume=data.default_volume,
        preferred_provider=data.preferred_provider,
        use_radio_mode=data.use_radio_mode,
        enabled=data.enabled
    )

    db.add(config)
    db.commit()
    db.refresh(config)

    logger.info("room_audio_config_created", room=data.room_name, type=data.speaker_type)

    return config.to_dict()


@router.put("/{config_id}")
async def update_room_config(
    config_id: int,
    data: RoomAudioUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update a room audio configuration."""
    config = db.query(RoomAudioConfig).filter(
        RoomAudioConfig.id == config_id
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room config not found")

    # Update fields that were provided
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    db.commit()
    db.refresh(config)

    logger.info("room_audio_config_updated", room=config.room_name, fields=list(update_data.keys()))

    return config.to_dict()


@router.delete("/{config_id}")
async def delete_room_config(
    config_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, str]:
    """Delete a room audio configuration."""
    config = db.query(RoomAudioConfig).filter(
        RoomAudioConfig.id == config_id
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room config not found")

    room_name = config.room_name
    db.delete(config)
    db.commit()

    logger.info("room_audio_config_deleted", room=room_name)

    return {"message": f"Room '{room_name}' deleted"}


# ============================================================================
# Entity Discovery
# ============================================================================

@router.get("/discover/entities")
async def discover_media_players(
    filter_type: Optional[str] = "music_assistant",
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, Any]]:
    """
    Discover available media player entities from Home Assistant.

    Args:
        filter_type: Filter entities by type. Options:
            - "music_assistant" (default): Only Music Assistant virtual players
            - "all": All media players
            - "native": Only native/hardware players (excludes Music Assistant)

    Returns list of media_player entities with their current state.
    """
    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.get(
                f"{HA_URL}/api/states",
                headers={"Authorization": f"Bearer {HA_TOKEN}"}
            )

            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Home Assistant returned status {response.status_code}"
                )

            states = response.json()

            # Filter to media_player entities
            media_players = []
            for state in states:
                entity_id = state.get("entity_id", "")
                if entity_id.startswith("media_player."):
                    attrs = state.get("attributes", {})
                    friendly_name = attrs.get("friendly_name", entity_id)

                    # Detect integration type
                    attrs_str = str(attrs).lower()
                    if "mass_" in entity_id or "music_assistant" in attrs_str:
                        integration = "music_assistant"
                    elif "sonos" in entity_id.lower() or "sonos" in friendly_name.lower():
                        integration = "sonos"
                    elif "cast" in entity_id.lower() or "chromecast" in friendly_name.lower():
                        integration = "chromecast"
                    elif "airplay" in entity_id.lower():
                        integration = "airplay"
                    else:
                        integration = "native"

                    # Apply filter
                    if filter_type == "music_assistant" and integration != "music_assistant":
                        continue
                    elif filter_type == "native" and integration == "music_assistant":
                        continue

                    # Skip entities with UUID-style names (raw device IDs)
                    if filter_type == "music_assistant":
                        # Check if entity_id looks like a UUID (8-4-4-4-12 or similar patterns)
                        if re.match(r'^media_player\.[a-f0-9]{8}[-_][a-f0-9]+', entity_id):
                            continue

                    media_players.append({
                        "entity_id": entity_id,
                        "friendly_name": friendly_name,
                        "state": state.get("state", "unknown"),
                        "device_class": attrs.get("device_class"),
                        "supported_features": attrs.get("supported_features", 0),
                        "source_list": attrs.get("source_list", []),
                        "volume_level": attrs.get("volume_level"),
                        "integration": integration,
                    })

            # Sort by friendly name
            media_players.sort(key=lambda x: x["friendly_name"])

            return media_players

    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Could not connect to Home Assistant")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Home Assistant request timed out")
    except Exception as e:
        logger.error("discover_media_players_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================================
# Audio Testing
# ============================================================================

@router.post("/{config_id}/test")
async def test_room_audio(
    config_id: int,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Test audio playback for a room configuration.

    Plays a short test sound to verify the configuration works.
    """
    config = db.query(RoomAudioConfig).filter(
        RoomAudioConfig.id == config_id
    ).first()

    if not config:
        raise HTTPException(status_code=404, detail="Room config not found")

    if not HA_TOKEN:
        raise HTTPException(status_code=500, detail="Home Assistant token not configured")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get entities to test based on speaker type
            entities_to_test = [config.primary_entity_id]

            if config.speaker_type == "stereo_pair" and config.secondary_entity_id:
                entities_to_test.append(config.secondary_entity_id)
            elif config.speaker_type == "group" and config.group_entity_ids:
                entities_to_test.extend(config.group_entity_ids)

            # Test each entity with a TTS announcement
            results = []
            for entity_id in entities_to_test:
                try:
                    # Call TTS service for testing
                    response = await client.post(
                        f"{HA_URL}/api/services/tts/speak",
                        headers={"Authorization": f"Bearer {HA_TOKEN}"},
                        json={
                            "entity_id": entity_id,
                            "message": f"Audio test for {config.display_name or config.room_name}"
                        }
                    )

                    results.append({
                        "entity_id": entity_id,
                        "success": response.status_code == 200,
                        "status_code": response.status_code
                    })

                except Exception as e:
                    results.append({
                        "entity_id": entity_id,
                        "success": False,
                        "error": str(e)
                    })

            # Update test status
            all_success = all(r["success"] for r in results)
            config.last_tested_at = datetime.utcnow()
            config.last_test_result = "success" if all_success else "failed"
            db.commit()
            db.refresh(config)

            logger.info(
                "room_audio_test",
                room=config.room_name,
                result="success" if all_success else "failed",
                entities_tested=len(entities_to_test)
            )

            return {
                "success": all_success,
                "room": config.room_name,
                "results": results,
                "tested_at": config.last_tested_at.isoformat()
            }

    except httpx.ConnectError:
        config.last_tested_at = datetime.utcnow()
        config.last_test_result = "failed"
        db.commit()
        raise HTTPException(status_code=502, detail="Could not connect to Home Assistant")
    except httpx.TimeoutException:
        config.last_tested_at = datetime.utcnow()
        config.last_test_result = "timeout"
        db.commit()
        raise HTTPException(status_code=504, detail="Test timed out")
    except Exception as e:
        config.last_tested_at = datetime.utcnow()
        config.last_test_result = "failed"
        db.commit()
        logger.error("room_audio_test_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
