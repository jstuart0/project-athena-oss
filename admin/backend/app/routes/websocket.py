"""
WebSocket endpoint for Admin Jarvis real-time events.

Handles:
- JWT authentication on connection
- Event subscription
- Heartbeat/ping-pong
- Rate limiting
"""

import asyncio
import time
import os
from typing import Set, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
import jwt
import structlog

logger = structlog.get_logger()

router = APIRouter(tags=["websocket"])

# JWT secret for token validation
JWT_SECRET = os.getenv("JWT_SECRET", "dev-secret-change-in-production")
JWT_ALGORITHM = "HS256"

# Connected clients
admin_jarvis_clients: Set[WebSocket] = set()


class WebSocketManager:
    """Manages Admin Jarvis WebSocket connections."""

    def __init__(self):
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, user_id: str):
        """Accept and register a WebSocket connection."""
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        logger.info("websocket_connected", user_id=user_id, total_clients=len(self._clients))

    async def disconnect(self, websocket: WebSocket):
        """Remove a WebSocket connection."""
        async with self._lock:
            self._clients.discard(websocket)
        logger.info("websocket_disconnected", total_clients=len(self._clients))

    async def broadcast(self, message: dict):
        """Broadcast a message to all connected clients."""
        if not self._clients:
            return

        disconnected = set()
        for client in self._clients:
            try:
                await client.send_json(message)
            except Exception:
                disconnected.add(client)

        # Clean up disconnected clients
        if disconnected:
            async with self._lock:
                self._clients.difference_update(disconnected)

    @property
    def client_count(self) -> int:
        """Get number of connected clients."""
        return len(self._clients)


# Global WebSocket manager instance
ws_manager = WebSocketManager()


def validate_jwt_token(token: str) -> Optional[dict]:
    """Validate JWT token and return payload."""
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        return payload
    except jwt.ExpiredSignatureError:
        logger.warning("jwt_expired")
        return None
    except jwt.InvalidTokenError as e:
        logger.warning("jwt_invalid", error=str(e))
        return None


@router.websocket("/ws/admin-jarvis")
async def admin_jarvis_websocket(
    websocket: WebSocket,
    token: str = Query(None)
):
    """
    WebSocket endpoint for Admin Jarvis real-time events.

    Requires JWT token as query parameter for authentication.

    Message types:
    - ping: Client heartbeat (responds with pong)
    - subscribe: Subscribe to specific session events
    - unsubscribe: Unsubscribe from session events

    Server sends:
    - pong: Response to ping
    - heartbeat: Server-initiated keepalive
    - event: Pipeline event
    - error: Error message
    """
    logger.info("websocket_connection_attempt",
                token_provided=bool(token),
                token_prefix=token[:20] if token else None,
                headers=dict(websocket.headers) if hasattr(websocket, 'headers') else None)

    # Handle missing token
    if not token:
        # In dev mode, allow connection without token
        if os.getenv("DEV_MODE", "false").lower() == "true":
            user_id = "dev-user"
            logger.info("websocket_dev_mode", message="Allowing unauthenticated connection in dev mode")
        else:
            logger.warning("websocket_no_token", message="No token provided, closing connection")
            await websocket.close(code=4001, reason="Token required")
            return
    else:
        # Validate JWT token
        payload = validate_jwt_token(token)
        if not payload:
            logger.warning("websocket_invalid_token", message="Token validation failed")
            await websocket.close(code=4001, reason="Invalid token")
            return

        user_id = payload.get('sub') or payload.get('user_id') or 'unknown'
        logger.info("websocket_token_validated", user_id=user_id)

    # Accept connection
    await ws_manager.connect(websocket, str(user_id))

    # Rate limiting state
    message_count = 0
    rate_limit_window_start = time.time()
    RATE_LIMIT = 100  # messages per minute

    try:
        while True:
            try:
                # Receive message with timeout
                data = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=60.0  # 1 minute timeout
                )

                # Rate limiting
                now = time.time()
                if now - rate_limit_window_start > 60:
                    message_count = 0
                    rate_limit_window_start = now

                message_count += 1
                if message_count > RATE_LIMIT:
                    await websocket.send_json({
                        "event_type": "error",
                        "data": {"message": "Rate limit exceeded"}
                    })
                    continue

                # Handle message types
                msg_type = data.get('type')

                if msg_type == 'ping':
                    await websocket.send_json({"event_type": "pong", "timestamp": time.time()})

                elif msg_type == 'subscribe':
                    session_id = data.get('session_id')
                    logger.info("websocket_subscribe", session_id=session_id, user_id=user_id)
                    await websocket.send_json({
                        "event_type": "subscribed",
                        "data": {"session_id": session_id}
                    })

                elif msg_type == 'unsubscribe':
                    session_id = data.get('session_id')
                    logger.info("websocket_unsubscribe", session_id=session_id, user_id=user_id)
                    await websocket.send_json({
                        "event_type": "unsubscribed",
                        "data": {"session_id": session_id}
                    })

                else:
                    logger.debug("websocket_unknown_message", type=msg_type)

            except asyncio.TimeoutError:
                # Send heartbeat ping from server
                await websocket.send_json({
                    "event_type": "heartbeat",
                    "timestamp": time.time()
                })

    except WebSocketDisconnect:
        logger.info("websocket_client_disconnected", user_id=user_id)
    except Exception as e:
        logger.error("websocket_error", error=str(e), user_id=user_id)
    finally:
        await ws_manager.disconnect(websocket)


async def broadcast_to_admin_jarvis(event: dict):
    """
    Broadcast an event to all connected Admin Jarvis clients.

    This is called by the event emitter to push events to the UI.
    """
    await ws_manager.broadcast(event)


async def broadcast_model_download_event(
    event_type: str,
    download_id: int,
    **kwargs
):
    """
    Broadcast model download events to all connected clients.

    Event types:
    - model_download_started: Download has begun
    - model_download_progress: Progress update (progress_percent, downloaded_bytes, total_bytes)
    - model_download_completed: Download finished successfully (download_path)
    - model_download_failed: Download failed (error_message)
    - model_download_cancelled: Download was cancelled
    """
    event = {
        "event_type": event_type,
        "data": {
            "download_id": download_id,
            **kwargs
        }
    }
    await ws_manager.broadcast(event)


def get_websocket_stats() -> dict:
    """Get WebSocket connection statistics."""
    return {
        "connected_clients": ws_manager.client_count,
    }


# =============================================================================
# Event Emitter Integration
# =============================================================================

_event_handler_registered = False


async def _websocket_event_handler(event):
    """
    Handler that receives events from EventEmitter and broadcasts to WebSocket clients.

    This bridges the event system to the WebSocket layer.
    """
    try:
        # Convert PipelineEvent to dict if needed
        if hasattr(event, 'to_dict'):
            event_dict = event.to_dict()
        else:
            event_dict = event

        await broadcast_to_admin_jarvis(event_dict)
    except Exception as e:
        logger.warning("websocket_broadcast_error", error=str(e))


async def register_event_handler():
    """
    Register WebSocket broadcast handler with the EventEmitter.

    Call this during application startup to connect events to WebSocket.
    """
    global _event_handler_registered

    if _event_handler_registered:
        return

    try:
        # Try to import from shared module (when running with orchestrator)
        from shared.events import EventEmitterFactory

        emitter = EventEmitterFactory.get()
        if emitter:
            emitter.subscribe(_websocket_event_handler)
            _event_handler_registered = True
            logger.info("websocket_event_handler_registered")
        else:
            logger.warning("event_emitter_not_initialized",
                          message="EventEmitter not yet created, handler not registered")
    except ImportError:
        logger.debug("shared_events_not_available",
                    message="Event system not available in admin backend standalone mode")
    except Exception as e:
        logger.warning("event_handler_registration_failed", error=str(e))


async def unregister_event_handler():
    """Unregister WebSocket broadcast handler from EventEmitter."""
    global _event_handler_registered

    if not _event_handler_registered:
        return

    try:
        from shared.events import EventEmitterFactory

        emitter = EventEmitterFactory.get()
        if emitter:
            emitter.unsubscribe(_websocket_event_handler)
            _event_handler_registered = False
            logger.info("websocket_event_handler_unregistered")
    except Exception as e:
        logger.warning("event_handler_unregistration_failed", error=str(e))
