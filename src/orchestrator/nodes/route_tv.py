"""route_tv_node — extracted from orchestrator.main during Phase 3.4 (ATHENA-10).

Handles Apple TV control commands: app launches, power control, remote navigation,
playback control, YouTube deep links, and multi-TV control.

Byte-identical move. See thoughts/shared/plans/2026-05-06-deliver-orchestrator-refactor.md.
"""
from __future__ import annotations

import time

import structlog

from orchestrator.nodes._runtime import get_tv_handler
from orchestrator.state import OrchestratorState
from orchestrator.helpers import store_conversation_context

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Proxy shim — forward bare-global attribute access to _runtime singleton
# ---------------------------------------------------------------------------

class _TVHandlerProxy:
    """Forward attribute access to the runtime TV handler at call time.

    route_tv_node references ``tv_handler`` as a bare module global.
    The actual handler is registered in _runtime by main.py's lifespan, so
    we cannot bind it at import time.  This proxy resolves the current value on
    every attribute lookup, keeping the function body byte-identical.
    """

    def __bool__(self) -> bool:
        return get_tv_handler() is not None

    def __getattr__(self, name: str):  # type: ignore[override]
        return getattr(get_tv_handler(), name)


tv_handler = _TVHandlerProxy()


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

async def route_tv_node(state: OrchestratorState) -> OrchestratorState:
    """
    Handle Apple TV control commands.

    Supports:
    - Launching apps (Netflix, YouTube, Disney+, etc.)
    - Power control (on/off)
    - Remote navigation (up, down, left, right, select, menu, home)
    - Playback control (play, pause)
    - YouTube deep links
    - Multi-TV control ("open Netflix everywhere")
    """
    start = time.time()

    try:
        if not tv_handler:
            state.answer = "Apple TV control is not configured. Please set up the TV handler."
            state.error = "tv_handler_not_initialized"
            state.node_timings["route_tv"] = time.time() - start
            return state

        # Parse the TV intent from the query
        guest_mode = state.mode == "guest"
        intent = await tv_handler.parse_tv_intent(state.query, room=state.room, mode=state.mode)

        logger.info(
            "tv_intent_parsed",
            action=intent.action,
            app=intent.app_name,
            room=intent.room,
            all_tvs=intent.all_tvs
        )

        result = None

        if intent.action == "launch":
            if intent.all_tvs:
                result = await tv_handler.handle_launch_everywhere(
                    app_name=intent.app_name,
                    guest_mode=guest_mode
                )
            else:
                result = await tv_handler.handle_launch(
                    app_name=intent.app_name,
                    room=intent.room or state.room,
                    guest_mode=guest_mode
                )

        elif intent.action == "power":
            result = await tv_handler.handle_power(
                action=intent.power_action,
                room=intent.room or state.room
            )

        elif intent.action == "navigate":
            result = await tv_handler.handle_navigate(
                command=intent.command,
                room=intent.room or state.room
            )

        elif intent.action == "playback":
            result = await tv_handler.handle_playback(
                command=intent.command,
                room=intent.room or state.room
            )

        elif intent.youtube_video_id:
            result = await tv_handler.handle_youtube_video(
                video_id=intent.youtube_video_id,
                room=intent.room or state.room
            )

        else:
            result = {
                "success": False,
                "message": "I'm not sure what you want me to do with the TV. Try 'open Netflix' or 'turn on the TV'."
            }

        state.answer = result.get("message", "Done.")
        state.retrieved_data = {"tv_intent": intent.__dict__, "result": result}

        if not result.get("success"):
            state.error = result.get("error", "unknown_error")

        logger.info(
            "tv_command_handled",
            action=intent.action,
            success=result.get("success", False)
        )

        # Store context for potential follow-up commands
        if state.session_id and result.get("success"):
            await store_conversation_context(
                session_id=state.session_id,
                intent="tv_control",
                query=state.query,
                entities={"room": intent.room or state.room, "app": intent.app_name},
                parameters=intent.__dict__,
                response=state.answer,
                ttl=300  # 5 minutes
            )

    except Exception as e:
        logger.error(f"TV control error: {e}", exc_info=True)
        state.answer = "I encountered an error controlling the TV. Please try again."
        state.error = str(e)

    route_tv_duration = time.time() - start
    state.node_timings["route_tv"] = route_tv_duration
    # Track node time to Prometheus
    if state.timing_tracker:
        state.timing_tracker.track_sync("graph", "route_tv", route_tv_duration)
    return state
