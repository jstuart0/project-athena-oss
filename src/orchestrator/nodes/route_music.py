"""route_music_node — extracted from orchestrator.main during Phase 3.3 (ATHENA-10).

Routes music playback and control commands through Music Assistant: play intent
parsing, browser-playback fallback, room-group synced playback, single-room
playback, and music-control (pause, next, volume).

Byte-identical move. See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md.
"""
from __future__ import annotations

import time

import structlog

from orchestrator.nodes._runtime import get_music_handler
from orchestrator.state import IntentCategory, OrchestratorState
from orchestrator.helpers import store_conversation_context

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Proxy shim — forward bare-global attribute access to _runtime singleton
# ---------------------------------------------------------------------------

class _MusicHandlerProxy:
    """Forward attribute access to the runtime music handler at call time.

    route_music_node references ``music_handler`` as a bare module global.
    The actual handler is registered in _runtime by main.py's lifespan, so
    we cannot bind it at import time.  This proxy resolves the current value on
    every attribute lookup, keeping the function body byte-identical.
    """

    def __bool__(self) -> bool:
        return get_music_handler() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_music_handler(), name)


music_handler = _MusicHandlerProxy()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def route_music_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle music playback and control commands via Music Assistant.

    Supports:
    - Playing music (artists, albums, playlists, genres)
    - Music controls (pause, next, volume)
    - Multi-room playback
    - Queue management
    - Music transfer between rooms
    - Browser playback (Jarvis Web)
    """
    start = time.time()

    try:
        if not music_handler:
            state.answer = "Music playback is not configured. Please set up Music Assistant in Home Assistant."
            state.error = "music_handler_not_initialized"
            state.node_timings["route_music"] = time.time() - start
            return state

        if state.intent == IntentCategory.MUSIC_PLAY:
            # Parse the play intent, passing interface_type for browser detection
            intent_data = await music_handler.parse_music_play_intent(
                state.query,
                room=state.room,
                interface_type=getattr(state, 'interface_type', None)
            )

            # Check for browser playback request
            play_in_browser = intent_data.get("play_in_browser", False)

            if play_in_browser:
                # Browser playback requested via "here", "on this device", etc.
                # Since direct browser streaming doesn't work for Spotify (DRM),
                # we play on the room speaker instead and show a mini player UI
                logger.info(
                    "browser_playback_requested_fallback_to_room",
                    media_type=intent_data.get("media_type"),
                    media_id=intent_data.get("media_id"),
                    room=state.room
                )

                media_id = intent_data.get("media_id", "")
                media_type = intent_data.get("media_type", "artist")

                # Play on the room speaker (fallback since browser streaming not supported)
                target_room = state.room if state.room and state.room != "jarvis_web" else "office"
                result = await music_handler.handle_play(
                    media_type=media_type,
                    media_id=media_id,
                    room=target_room,
                    radio_mode=intent_data.get("radio_mode", True)
                )

                state.answer = f"Playing {media_id} on {target_room}."
                state.retrieved_data = {
                    "music_intent": intent_data,
                    "playback_room": target_room
                }

            else:
                # Check if this is a room group request
                # parse_music_play_intent now sets is_room_group if it matched a group/alias
                is_room_group = intent_data.get("is_room_group", False)

                if is_room_group:
                    # Play to room group (synced playback)
                    result = await music_handler.handle_room_group_play(
                        group_name=intent_data.get("room"),
                        media_type=intent_data.get("media_type"),
                        media_id=intent_data.get("media_id"),
                        radio_mode=intent_data.get("radio_mode", True)
                    )
                else:
                    # Single room playback
                    result = await music_handler.handle_play(
                        media_type=intent_data.get("media_type"),
                        media_id=intent_data.get("media_id"),
                        room=intent_data.get("room"),
                        radio_mode=intent_data.get("radio_mode", True)
                    )

                state.answer = result
                state.retrieved_data = {"music_intent": intent_data}

            logger.info(
                "music_play_handled",
                media_id=intent_data.get("media_id"),
                room=intent_data.get("room"),
                browser=play_in_browser
            )

        elif state.intent == IntentCategory.MUSIC_CONTROL:
            # Parse the control intent
            intent_data = await music_handler.parse_music_control_intent(
                state.query,
                room=state.room
            )

            result = await music_handler.handle_control(
                action=intent_data.get("action"),
                room=intent_data.get("room"),
                volume_level=intent_data.get("volume_level")
            )

            state.answer = result
            state.retrieved_data = {"music_intent": intent_data}

            logger.info(
                "music_control_handled",
                action=intent_data.get("action"),
                room=intent_data.get("room")
            )

        # Store context for potential follow-up commands
        if state.session_id and state.answer and "sorry" not in state.answer.lower():
            await store_conversation_context(
                session_id=state.session_id,
                intent="music",
                query=state.query,
                entities={"room": state.room},
                parameters=state.retrieved_data.get("music_intent", {}),
                response=state.answer,
                ttl=300  # 5 minutes
            )

    except Exception as e:
        logger.error(f"Music execution error: {e}", exc_info=True)
        state.answer = "I encountered an error with music playback. Please try again."
        state.error = str(e)

    route_music_duration = time.time() - start
    state.node_timings["route_music"] = route_music_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "route_music", route_music_duration)
    return state
