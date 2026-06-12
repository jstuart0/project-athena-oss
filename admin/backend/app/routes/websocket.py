"""
WebSocket endpoint for Admin Jarvis real-time events.

Handles:
- JWT authentication on connection (ticket or legacy session JWT)
- Event subscription
- Heartbeat/ping-pong
- Rate limiting
"""

import asyncio
import time
from typing import Set, Optional
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
import structlog
from jose.exceptions import JWTClaimsError
from shared.config import get_config

logger = structlog.get_logger()

router = APIRouter(tags=["websocket"])

# JWT secret and algorithm come exclusively from oidc.py (xander M-3, ATHENA-55 Phase 3).
# Do NOT re-read JWT_SECRET / JWT_ALGORITHM from os.getenv here.
# Import the module-level primitives so there is exactly one signing-secret source.
from app.auth.oidc import decode_access_token, decode_ws_ticket

# Module-level redis client — wired by main.py during startup (same pattern as
# start_health_polling).  None in DEV_MODE; in that case the in-memory fallback is used.
_redis_client = None

# In-memory single-use set for DEV_MODE (single-process — no shared state needed).
# Each entry is (jti, expiry_timestamp); entries are cleaned lazily on insert.
_used_jti_memory: dict = {}  # jti -> expiry_ts

# Connected clients
admin_jarvis_clients: Set[WebSocket] = set()


def configure_redis(redis_client) -> None:
    """
    Wire the module-level redis client for single-use jti tracking.

    Called by main.py during startup (same pattern as start_health_polling).
    In DEV_MODE this is never called; _redis_client stays None and the
    in-memory fallback (_used_jti_memory) is used instead.
    """
    global _redis_client
    _redis_client = redis_client


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


async def _claim_jti(jti: str, ttl: int) -> bool:
    """
    Atomically claim a jti for single-use enforcement (ATHENA-55 Phase 3).

    Production (redis_client set): Redis SET NX EX — returns True on first claim,
    False if already consumed (replay).  Key: athena:ws_ticket:<jti>.

    DEV_MODE (redis_client None): in-memory dict with lazy TTL eviction.

    Args:
        jti: The JWT ID claim from the ticket.
        ttl: TTL in seconds (must be >= ticket exp, i.e. >= 45).

    Returns:
        True if this is the first use, False if replayed.
    """
    if _redis_client is not None:
        key = f"athena:ws_ticket:{jti}"
        result = await _redis_client.set(key, "", nx=True, ex=ttl)
        return result is not None  # truthy on first claim, None on replay

    # DEV_MODE in-memory fallback — evict expired entries lazily
    now = time.time()
    # Lazy eviction: remove expired entries
    expired = [k for k, exp in list(_used_jti_memory.items()) if exp < now]
    for k in expired:
        _used_jti_memory.pop(k, None)

    if jti in _used_jti_memory:
        return False  # replay

    _used_jti_memory[jti] = now + ttl
    return True


@router.websocket("/ws/admin-jarvis")
async def admin_jarvis_websocket(
    websocket: WebSocket,
    token: str = Query(None)
):
    """
    WebSocket endpoint for Admin Jarvis real-time events.

    Authentication (ATHENA-55 Phase 3 dual-mode):
    1. Ticket path: token is a ws-ticket (aud="ws", ws_ticket=True) —
       validated via decode_ws_ticket, single-use jti check, identity check.
    2. Legacy path (deprecation window): token is a plain session JWT —
       validated via decode_access_token, accepted with a deprecation warning.
    3. DEV_MODE: unauthenticated connection allowed.

    Origin check: production rejects non-CORS_ORIGINS origins with close 4003.

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
    cfg = get_config()
    origin = websocket.headers.get("origin")

    # ATHENA-55 Phase 3: Origin check before accept (xander H-1).
    # CORSMiddleware does NOT cover WebSocket scope — we enforce it here.
    # Read CORS_ORIGINS from the same env var as main.py (CORS_ALLOWED_ORIGINS).
    # DEV_MODE skips the check (no origin enforcement in local dev).
    if not cfg.dev_mode:
        import os
        _cors_env = os.getenv("CORS_ALLOWED_ORIGINS", "")
        CORS_ORIGINS = [o.strip() for o in _cors_env.split(",") if o.strip()] or ["http://localhost:8080"]
        if origin not in CORS_ORIGINS:
            logger.warning(
                "websocket_origin_rejected",
                origin=origin,
                allowed=CORS_ORIGINS,
            )
            await websocket.close(code=4003, reason="Origin not allowed")
            return

    logger.info(
        "websocket_connection_attempt",
        token_provided=bool(token),
        origin=origin,
    )

    # Handle missing token
    if not token:
        if cfg.dev_mode:
            user_id = "dev-user"
            logger.info("websocket_dev_mode", message="Allowing unauthenticated connection in dev mode")
        else:
            logger.warning("websocket_no_token", message="No token provided, closing connection")
            await websocket.close(code=4001, reason="Token required")
            return
    else:
        user_id = None

        # --- Ticket path (ATHENA-55 Phase 3) ---
        try:
            payload = decode_ws_ticket(token)
        except JWTClaimsError:
            # Wrong/missing audience — fall through to legacy decode
            payload = None
            aud_mismatch = True
        else:
            aud_mismatch = False

        if payload is not None and payload.get("ws_ticket") is True:
            # Ticket path: aud="ws" validated positively + ws_ticket identity check.
            jti = payload.get("jti")
            if not jti:
                logger.warning("websocket_ticket_missing_jti")
                await websocket.close(code=4001, reason="Invalid token")
                return

            # Single-use claim — ttl must be >= ticket exp (45s); use 90s for safety
            claimed = await _claim_jti(jti, ttl=90)
            if not claimed:
                logger.warning("websocket_ticket_replayed", jti=jti)
                await websocket.close(code=4001, reason="Invalid token")
                return

            user_id = payload.get("sub") or payload.get("user_id") or "unknown"
            logger.info("websocket_ticket_validated", user_id=user_id)

        else:
            # --- Legacy path: plain session JWT (no aud / no ws_ticket flag) ---
            # decode_access_token accepts any valid signed JWT without aud="ws".
            # A legacy session JWT carries neither, so it passes the REST guard.
            try:
                from fastapi import HTTPException
                legacy_payload = decode_access_token(token)
            except Exception:
                logger.warning("websocket_invalid_token", message="Token validation failed")
                await websocket.close(code=4001, reason="Invalid token")
                return

            logger.warning(
                "websocket_legacy_token_auth_deprecated",
                message=(
                    "WebSocket authenticated via legacy session JWT in ?token=. "
                    "This path will be removed in the next release. "
                    "Clients should use POST /api/auth/ws-ticket instead."
                ),
            )
            user_id = legacy_payload.get("sub") or legacy_payload.get("user_id") or "unknown"
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
