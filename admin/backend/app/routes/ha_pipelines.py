"""
Home Assistant Voice Pipeline Management Routes.

Provides endpoints for managing HA voice pipelines:
- List available voice pipelines
- Get/set preferred pipeline
- Switch between streaming (Ollama) and non-streaming (Gateway) modes

Connects to HA via WebSocket API for pipeline operations.
"""
import os
import json
import asyncio
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
import structlog
import websockets

from app.auth.oidc import get_current_user

logger = structlog.get_logger()
router = APIRouter(prefix="/api/ha-pipelines", tags=["ha-pipelines"])

# Home Assistant configuration - defaults empty, should be configured via env or admin backend
HA_URL = os.getenv("HA_URL", "")
HA_WS_URL = os.getenv("HA_WS_URL", "")
HA_TOKEN = os.getenv("HA_TOKEN", "")

# Pipeline modes - Three options for different use cases
PIPELINE_MODES = {
    "streaming_rag": {
        "name": "Athena (Streaming + RAG)",
        "description": "True streaming WITH RAG - fastest AND smartest (Recommended)",
        "streaming": True,
        "has_rag": True,
        # Match by pipeline name since entity_id varies (unnamed_device_X)
        "conversation_engine": "conversation.unnamed_device",
        "pipeline_name_pattern": "Conversation Plus",
    },
    "full": {
        "name": "Athena (Full Pipeline)",
        "description": "Full RAG capabilities via Gateway - no streaming but proven stable",
        "streaming": False,
        "has_rag": True,
        "conversation_engine": "conversation.extended_openai_conversation",
    },
    "simple": {
        "name": "Athena (Simple)",
        "description": "Direct Ollama streaming - fastest but no RAG",
        "streaming": True,
        "has_rag": False,
        "conversation_engine": "conversation.ollama_conversation",
    },
}


class SetPipelineRequest(BaseModel):
    """Request to set the active pipeline."""
    pipeline_id: str


class SetModeRequest(BaseModel):
    """Request to set streaming mode."""
    mode: str  # "full" or "streaming"


async def ha_websocket_command(command: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send a command to Home Assistant via WebSocket API.

    Args:
        command: The command dict to send (without id field)

    Returns:
        The response dict from HA
    """
    if not HA_TOKEN:
        raise HTTPException(
            status_code=500,
            detail="HA_TOKEN not configured"
        )

    ws_url = HA_WS_URL.replace("https://", "wss://").replace("http://", "ws://")
    if not ws_url.endswith("/api/websocket"):
        ws_url = ws_url.rstrip("/") + "/api/websocket"

    import ssl
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with websockets.connect(
            ws_url,
            ssl=ssl_context,
            close_timeout=10
        ) as ws:
            # Wait for auth_required message
            auth_required = await asyncio.wait_for(ws.recv(), timeout=5)
            auth_msg = json.loads(auth_required)

            if auth_msg.get("type") != "auth_required":
                raise HTTPException(
                    status_code=500,
                    detail=f"Unexpected HA message: {auth_msg}"
                )

            # Send authentication
            await ws.send(json.dumps({
                "type": "auth",
                "access_token": HA_TOKEN
            }))

            # Wait for auth result
            auth_result = await asyncio.wait_for(ws.recv(), timeout=5)
            auth_result_msg = json.loads(auth_result)

            if auth_result_msg.get("type") != "auth_ok":
                raise HTTPException(
                    status_code=401,
                    detail=f"HA authentication failed: {auth_result_msg}"
                )

            # Send the command with ID
            command_with_id = {**command, "id": 1}
            await ws.send(json.dumps(command_with_id))

            # Wait for response
            response = await asyncio.wait_for(ws.recv(), timeout=10)
            response_msg = json.loads(response)

            if not response_msg.get("success", True):
                error = response_msg.get("error", {})
                raise HTTPException(
                    status_code=400,
                    detail=f"HA command failed: {error.get('message', 'Unknown error')}"
                )

            return response_msg

    except websockets.exceptions.WebSocketException as e:
        logger.error("ha_websocket_error", error=str(e))
        raise HTTPException(
            status_code=503,
            detail=f"Failed to connect to Home Assistant: {str(e)}"
        )
    except asyncio.TimeoutError:
        logger.error("ha_websocket_timeout")
        raise HTTPException(
            status_code=504,
            detail="Home Assistant WebSocket timeout"
        )


# =============================================================================
# Pipeline List and Info
# =============================================================================

@router.get("/pipelines")
async def list_pipelines() -> Dict[str, Any]:
    """
    List all available voice pipelines from Home Assistant.

    Returns pipeline names, IDs, and conversation engines.
    No authentication required - public config info.
    """
    try:
        response = await ha_websocket_command({
            "type": "assist_pipeline/pipeline/list"
        })

        pipelines = response.get("result", {}).get("pipelines", [])
        preferred_id = response.get("result", {}).get("preferred_pipeline")

        result = []
        for pipeline in pipelines:
            result.append({
                "id": pipeline.get("id"),
                "name": pipeline.get("name"),
                "conversation_engine": pipeline.get("conversation_engine"),
                "stt_engine": pipeline.get("stt_engine"),
                "tts_engine": pipeline.get("tts_engine"),
                "language": pipeline.get("language"),
                "is_preferred": pipeline.get("id") == preferred_id,
            })

        return {
            "pipelines": result,
            "preferred_pipeline": preferred_id
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("list_pipelines_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/pipelines/preferred")
async def get_preferred_pipeline() -> Dict[str, Any]:
    """
    Get the currently preferred (active) voice pipeline.

    No authentication required - public config info.
    """
    try:
        response = await ha_websocket_command({
            "type": "assist_pipeline/pipeline/list"
        })

        pipelines = response.get("result", {}).get("pipelines", [])
        preferred_id = response.get("result", {}).get("preferred_pipeline")

        # Find the preferred pipeline
        preferred = None
        for pipeline in pipelines:
            if pipeline.get("id") == preferred_id:
                preferred = pipeline
                break

        if not preferred:
            return {"preferred_pipeline": None, "streaming_enabled": False, "has_rag": False}

        # Determine current mode based on conversation engine
        conv_engine = preferred.get("conversation_engine", "")

        # Detect mode based on conversation engine
        # Check for streaming_rag: OpenAI Conversation Plus shows as "unnamed_device_X"
        if "openai_conversation_plus" in conv_engine.lower() or "unnamed_device" in conv_engine.lower():
            current_mode = "streaming_rag"
            is_streaming = True
            has_rag = True
        elif "extended_openai_conversation" in conv_engine.lower():
            current_mode = "full"
            is_streaming = False
            has_rag = True
        elif "ollama" in conv_engine.lower():
            current_mode = "simple"
            is_streaming = True
            has_rag = False
        else:
            # Unknown engine - default to full
            current_mode = "full"
            is_streaming = False
            has_rag = True

        return {
            "preferred_pipeline": {
                "id": preferred.get("id"),
                "name": preferred.get("name"),
                "conversation_engine": conv_engine,
            },
            "streaming_enabled": is_streaming,
            "has_rag": has_rag,
            "current_mode": current_mode
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("get_preferred_pipeline_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Pipeline Switching
# =============================================================================

@router.post("/pipelines/set-preferred")
async def set_preferred_pipeline(
    request: SetPipelineRequest,
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """
    Set the preferred voice pipeline in Home Assistant.

    Requires authentication.
    """
    try:
        # First verify the pipeline exists
        list_response = await ha_websocket_command({
            "type": "assist_pipeline/pipeline/list"
        })

        pipelines = list_response.get("result", {}).get("pipelines", [])
        pipeline_ids = [p.get("id") for p in pipelines]

        if request.pipeline_id not in pipeline_ids:
            raise HTTPException(
                status_code=404,
                detail=f"Pipeline '{request.pipeline_id}' not found"
            )

        # Set the preferred pipeline
        await ha_websocket_command({
            "type": "assist_pipeline/pipeline/set_preferred",
            "pipeline_id": request.pipeline_id
        })

        # Get the pipeline info for response
        pipeline_info = next(
            (p for p in pipelines if p.get("id") == request.pipeline_id),
            None
        )

        logger.info(
            "pipeline_preference_changed",
            pipeline_id=request.pipeline_id,
            pipeline_name=pipeline_info.get("name") if pipeline_info else "unknown",
            user=current_user.get("username")
        )

        return {
            "status": "success",
            "preferred_pipeline": request.pipeline_id,
            "pipeline_name": pipeline_info.get("name") if pipeline_info else "unknown",
            "message": f"Voice pipeline set to '{pipeline_info.get('name')}'"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("set_preferred_pipeline_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mode/set")
async def set_voice_mode(
    request: SetModeRequest
) -> Dict[str, Any]:
    """
    Set voice mode (streaming with RAG, full pipeline, or simple).

    Convenience endpoint that finds the correct pipeline and sets it as preferred.

    Modes:
    - "streaming_rag": Uses OpenAI Conversation Plus (Gateway → Orchestrator with true streaming)
    - "full": Uses Extended OpenAI Conversation (Gateway → Orchestrator, no streaming)
    - "simple": Uses Ollama Conversation (direct to Ollama, no RAG)

    No authentication required for voice mode switching.
    """
    if request.mode not in PIPELINE_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode '{request.mode}'. Must be 'streaming_rag', 'full', or 'simple'"
        )

    mode_config = PIPELINE_MODES[request.mode]
    target_engine = mode_config["conversation_engine"]

    try:
        # Get pipelines
        list_response = await ha_websocket_command({
            "type": "assist_pipeline/pipeline/list"
        })

        pipelines = list_response.get("result", {}).get("pipelines", [])

        # Find pipeline matching the target conversation engine or name pattern
        target_pipeline = None
        name_pattern = mode_config.get("pipeline_name_pattern")

        for pipeline in pipelines:
            conv_engine = pipeline.get("conversation_engine", "")
            pipeline_name = pipeline.get("name", "")

            # First try matching by pipeline name pattern (most specific)
            if name_pattern and name_pattern.lower() in pipeline_name.lower():
                target_pipeline = pipeline
                break
            # Then try exact match on conversation engine
            if conv_engine == target_engine:
                target_pipeline = pipeline
                break
            # Finally try partial match on conversation engine
            if not name_pattern and target_engine.split(".")[-1] in conv_engine:
                target_pipeline = pipeline
                break

        if not target_pipeline:
            # List available conversation engines for better error message
            available_engines = [f"{p.get('name')} ({p.get('conversation_engine')})" for p in pipelines]
            raise HTTPException(
                status_code=404,
                detail=f"No pipeline found for mode '{request.mode}'. "
                       f"Available pipelines: {available_engines}"
            )

        # Set as preferred
        await ha_websocket_command({
            "type": "assist_pipeline/pipeline/set_preferred",
            "pipeline_id": target_pipeline.get("id")
        })

        logger.info(
            "voice_mode_changed",
            mode=request.mode,
            pipeline_id=target_pipeline.get("id"),
            pipeline_name=target_pipeline.get("name"),
            streaming=mode_config["streaming"]
        )

        return {
            "status": "success",
            "mode": request.mode,
            "streaming_enabled": mode_config["streaming"],
            "has_rag": mode_config.get("has_rag", False),
            "pipeline": {
                "id": target_pipeline.get("id"),
                "name": target_pipeline.get("name"),
                "conversation_engine": target_pipeline.get("conversation_engine"),
            },
            "message": f"Voice mode set to '{mode_config['name']}'"
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("set_voice_mode_failed", error=str(e), mode=request.mode)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/modes")
async def get_available_modes() -> Dict[str, Any]:
    """
    Get available voice modes and their configurations.

    No authentication required - public config info.
    """
    return {
        "modes": [
            {
                "id": mode_id,
                "name": config["name"],
                "description": config["description"],
                "streaming": config["streaming"],
                "has_rag": config.get("has_rag", False),
            }
            for mode_id, config in PIPELINE_MODES.items()
        ]
    }


# =============================================================================
# Health Check
# =============================================================================

@router.get("/health")
async def check_ha_connection() -> Dict[str, Any]:
    """
    Check connectivity to Home Assistant WebSocket API.

    Tests authentication and basic command execution.
    """
    try:
        response = await ha_websocket_command({
            "type": "assist_pipeline/pipeline/list"
        })

        pipeline_count = len(response.get("result", {}).get("pipelines", []))

        return {
            "status": "healthy",
            "connected": True,
            "pipeline_count": pipeline_count,
            "ha_url": HA_URL.replace(HA_TOKEN, "***") if HA_TOKEN else HA_URL
        }

    except HTTPException as e:
        return {
            "status": "unhealthy",
            "connected": False,
            "error": e.detail
        }
    except Exception as e:
        return {
            "status": "error",
            "connected": False,
            "error": str(e)
        }
