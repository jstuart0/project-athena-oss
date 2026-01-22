"""
LiveKit API Routes for Gateway.

Provides endpoints for:
- Creating/joining LiveKit rooms
- Generating participant tokens
- Session management
- Configuration
"""

import os
from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field
import structlog

from gateway.livekit_service import get_livekit_service, LiveKitService

logger = structlog.get_logger()

router = APIRouter(prefix="/livekit", tags=["livekit"])


# =============================================================================
# Request/Response Models
# =============================================================================

class CreateRoomRequest(BaseModel):
    """Request to create a new LiveKit room."""
    room_name: Optional[str] = Field(None, description="Optional room name (auto-generated if not provided)")
    participant_name: str = Field("User", description="Display name for participant")
    room_config: Optional[str] = Field(None, description="Room configuration: 'office', 'mobile', etc.")


class CreateRoomResponse(BaseModel):
    """Response with room credentials."""
    room_name: str
    livekit_url: str
    token: str
    expires_in: int = 86400  # 24 hours


class JoinRoomRequest(BaseModel):
    """Request to join existing room."""
    room_name: str
    participant_name: str = "User"
    participant_identity: Optional[str] = None


class LiveKitConfigResponse(BaseModel):
    """LiveKit configuration for client."""
    enabled: bool
    livekit_url: Optional[str] = None
    wake_words: list = ["jarvis", "athena"]
    sample_rate: int = 16000
    vad_threshold: float = 0.5
    silence_timeout_ms: int = 2000
    max_query_duration_ms: int = 30000


class SessionInfo(BaseModel):
    """Active session information."""
    session_id: str
    room_name: str
    state: str
    created_at: float
    last_activity: float


# =============================================================================
# Dependency
# =============================================================================

def get_service() -> LiveKitService:
    """Get LiveKit service, raise if not available."""
    service = get_livekit_service()
    if not service.is_available:
        raise HTTPException(
            status_code=503,
            detail="LiveKit not configured. Set LIVEKIT_API_KEY and LIVEKIT_API_SECRET."
        )
    return service


# =============================================================================
# Routes
# =============================================================================

@router.get("/config", response_model=LiveKitConfigResponse)
async def get_livekit_config():
    """
    Get LiveKit configuration for client.

    Returns whether LiveKit is enabled and configuration settings.
    Client uses this to decide whether to offer WebRTC mode.
    """
    service = get_livekit_service()

    return LiveKitConfigResponse(
        enabled=service.is_available,
        livekit_url=service.livekit_url if service.is_available else None,
        wake_words=["jarvis", "athena"],
        sample_rate=16000,
        vad_threshold=0.5,
        silence_timeout_ms=service.silence_timeout_ms,
        max_query_duration_ms=service.max_query_duration_ms
    )


@router.post("/rooms", response_model=CreateRoomResponse)
async def create_room(
    request: CreateRoomRequest,
    service: LiveKitService = Depends(get_service)
):
    """
    Create a new LiveKit room for audio session.

    Returns room credentials for client connection.
    """
    try:
        room_info = await service.create_room(
            room_name=request.room_name
        )

        # Generate user token
        token = service.generate_room_token(
            room_name=room_info["room_name"],
            participant_name=request.participant_name
        )

        logger.info("livekit_room_created_for_user",
                   room=room_info["room_name"],
                   participant=request.participant_name)

        return CreateRoomResponse(
            room_name=room_info["room_name"],
            livekit_url=service.livekit_url,
            token=token
        )

    except Exception as e:
        logger.error("livekit_room_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rooms/{room_name}/join", response_model=CreateRoomResponse)
async def join_room(
    room_name: str,
    request: JoinRoomRequest,
    service: LiveKitService = Depends(get_service)
):
    """
    Get token to join existing room.
    """
    try:
        token = service.generate_room_token(
            room_name=room_name,
            participant_name=request.participant_name,
            participant_identity=request.participant_identity
        )

        return CreateRoomResponse(
            room_name=room_name,
            livekit_url=service.livekit_url,
            token=token
        )

    except Exception as e:
        logger.error("livekit_join_failed", room=room_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rooms/{room_name}/athena-join")
async def athena_join_room(
    room_name: str,
    service: LiveKitService = Depends(get_service)
):
    """
    Have Athena join the room to process audio.

    This is called after creating a room to enable server-side
    audio processing (wake word detection, STT, etc.).
    """
    try:
        # Generate Athena's token
        athena_token = service.generate_room_token(
            room_name=room_name,
            participant_name="Athena",
            participant_identity=f"athena_{room_name}"
        )

        # Join as Athena
        success = await service.join_room_as_athena(room_name, athena_token)

        if success:
            logger.info("athena_joined_livekit_room", room=room_name)
            return {"status": "joined", "room_name": room_name}
        else:
            raise HTTPException(status_code=500, detail="Failed to join room")

    except HTTPException:
        raise
    except Exception as e:
        logger.error("athena_join_failed", room=room_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions(
    service: LiveKitService = Depends(get_service)
):
    """
    List active LiveKit sessions.
    """
    sessions = await service.get_active_sessions()
    return [SessionInfo(**s) for s in sessions]


@router.delete("/rooms/{room_name}")
async def close_room(
    room_name: str,
    service: LiveKitService = Depends(get_service)
):
    """
    Close a LiveKit room and cleanup session.
    """
    try:
        await service._cleanup_session(room_name)
        return {"status": "closed", "room_name": room_name}
    except Exception as e:
        logger.error("room_close_failed", room=room_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/health")
async def livekit_health():
    """
    Check LiveKit service health.
    """
    service = get_livekit_service()

    return {
        "status": "healthy" if service.is_available else "unavailable",
        "livekit_available": service.is_available,
        "livekit_url": service.livekit_url if service.is_available else None,
        "active_sessions": len(service._sessions),
        "active_rooms": len(service._rooms)
    }
