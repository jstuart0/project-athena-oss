"""Unit tests for orchestrator.nodes.route_tv.route_tv_node.

Covers major routing branches:

 1.  tv_handler not initialised — returns not-configured answer.
 2.  node_timings set on early-exit path.
 3.  launch intent — single room (happy path).
 4.  launch intent — all_tvs (launch_everywhere).
 5.  power intent.
 6.  navigate intent.
 7.  playback intent.
 8.  YouTube deep-link intent.
 9.  Unknown intent — fallback "not sure" message.
10.  store_conversation_context called on success.
11.  store_conversation_context NOT called when result.success is False.
12.  store_conversation_context NOT called when session_id is None.
13.  Outer exception handler — error set, fallback answer returned.
14.  timing_tracker.track_sync called after node completes.
15.  node_timings["route_tv"] always set.

Patching strategy:
- tv_handler installed via orchestrator.nodes._runtime.set_tv_handler() —
  the _TVHandlerProxy delegates to _runtime at call time.
- store_conversation_context patched at source:
  orchestrator.helpers.store_conversation_context
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, MagicMock, patch

# Stub heavy deps before any orchestrator import.
for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator.nodes import route_tv_node  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.state import OrchestratorState  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_state(
    *,
    query: str = "open Netflix",
    session_id: str | None = "sess-tv-1",
    room: str | None = "living room",
    mode: str = "default",
    timing_tracker=None,
) -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.session_id = session_id
    state.room = room
    state.mode = mode
    state.timing_tracker = timing_tracker
    state.node_timings = {}
    state.retrieved_data = {}
    return state


def _make_intent(
    *,
    action: str = "launch",
    app_name: str = "Netflix",
    room: str | None = None,
    all_tvs: bool = False,
    power_action: str | None = None,
    command: str | None = None,
    youtube_video_id: str | None = None,
) -> MagicMock:
    intent = MagicMock()
    intent.action = action
    intent.app_name = app_name
    intent.room = room
    intent.all_tvs = all_tvs
    intent.power_action = power_action
    intent.command = command
    intent.youtube_video_id = youtube_video_id
    intent.__dict__ = {
        "action": action,
        "app_name": app_name,
        "room": room,
        "all_tvs": all_tvs,
        "power_action": power_action,
        "command": command,
        "youtube_video_id": youtube_video_id,
    }
    return intent


def _make_tv_handler(**overrides):
    th = MagicMock()
    th.parse_tv_intent = AsyncMock(return_value=_make_intent())
    th.handle_launch = AsyncMock(return_value={"success": True, "message": "Opened Netflix."})
    th.handle_launch_everywhere = AsyncMock(return_value={"success": True, "message": "Opened Netflix everywhere."})
    th.handle_power = AsyncMock(return_value={"success": True, "message": "TV turned on."})
    th.handle_navigate = AsyncMock(return_value={"success": True, "message": "Navigated up."})
    th.handle_playback = AsyncMock(return_value={"success": True, "message": "Paused."})
    th.handle_youtube_video = AsyncMock(return_value={"success": True, "message": "Playing YouTube video."})
    for k, v in overrides.items():
        setattr(th, k, v)
    return th


# Disable strict mode so unset singletons don't warn.
_runtime.reset_for_test()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTVHandlerNotInitialised:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_returns_not_configured_when_handler_absent(self):
        _runtime.set_tv_handler(None)
        state = _make_state()
        result = _run(route_tv_node(state))
        assert result.answer == "Apple TV control is not configured. Please set up the TV handler."
        assert result.error == "tv_handler_not_initialized"
        assert "route_tv" in result.node_timings

    def test_node_timings_set_on_early_exit(self):
        _runtime.set_tv_handler(None)
        state = _make_state()
        result = _run(route_tv_node(state))
        assert isinstance(result.node_timings.get("route_tv"), float)


class TestLaunchIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_single_room_launch_happy_path(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="launch", app_name="Netflix", room="living room", all_tvs=False
        ))
        _runtime.set_tv_handler(th)
        state = _make_state(query="open Netflix in the living room")
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_tv_node(state))
        th.handle_launch.assert_awaited_once_with(
            app_name="Netflix",
            room="living room",
            guest_mode=False,
        )
        th.handle_launch_everywhere.assert_not_awaited()
        assert result.answer == "Opened Netflix."
        assert "route_tv" in result.node_timings

    def test_all_tvs_routes_to_launch_everywhere(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="launch", app_name="Disney+", all_tvs=True
        ))
        _runtime.set_tv_handler(th)
        state = _make_state(query="open Disney+ everywhere")
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_tv_node(state))
        th.handle_launch_everywhere.assert_awaited_once_with(
            app_name="Disney+",
            guest_mode=False,
        )
        th.handle_launch.assert_not_awaited()
        assert result.answer == "Opened Netflix everywhere."

    def test_guest_mode_passed_correctly(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(action="launch"))
        _runtime.set_tv_handler(th)
        state = _make_state(mode="guest")
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            _run(route_tv_node(state))
        _, kwargs = th.handle_launch.call_args
        assert kwargs["guest_mode"] is True


class TestPowerIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_power_intent_routes_correctly(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="power", power_action="on", room="bedroom"
        ))
        _runtime.set_tv_handler(th)
        state = _make_state(query="turn on the bedroom TV")
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_tv_node(state))
        th.handle_power.assert_awaited_once_with(
            action="on",
            room="bedroom",
        )
        assert result.answer == "TV turned on."


class TestNavigateIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_navigate_intent_routes_correctly(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="navigate", command="up", room="living room"
        ))
        _runtime.set_tv_handler(th)
        state = _make_state(query="press up on the TV")
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_tv_node(state))
        th.handle_navigate.assert_awaited_once_with(
            command="up",
            room="living room",
        )
        assert result.answer == "Navigated up."


class TestPlaybackIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_playback_intent_routes_correctly(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="playback", command="pause", room=None
        ))
        _runtime.set_tv_handler(th)
        state = _make_state(query="pause the TV", room="living room")
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_tv_node(state))
        th.handle_playback.assert_awaited_once_with(
            command="pause",
            room="living room",  # falls back to state.room
        )
        assert result.answer == "Paused."


class TestYouTubeIntent:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_youtube_video_id_routes_to_youtube_handler(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="other", youtube_video_id="dQw4w9WgXcQ", room="office"
        ))
        _runtime.set_tv_handler(th)
        state = _make_state(query="play that YouTube video on the TV")
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_tv_node(state))
        th.handle_youtube_video.assert_awaited_once_with(
            video_id="dQw4w9WgXcQ",
            room="office",
        )
        assert result.answer == "Playing YouTube video."


class TestUnknownIntentFallback:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_unknown_intent_returns_not_sure_message(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="unknown", youtube_video_id=None
        ))
        _runtime.set_tv_handler(th)
        state = _make_state(query="do something with the TV")
        result = _run(route_tv_node(state))
        assert "not sure" in result.answer
        assert result.error == "unknown_error"


class TestContextStorage:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_store_context_called_on_success(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="launch", app_name="Netflix", room="living room"
        ))
        th.handle_launch = AsyncMock(return_value={"success": True, "message": "Opened Netflix."})
        _runtime.set_tv_handler(th)
        state = _make_state(session_id="ctx-session")
        mock_store = AsyncMock()
        with patch("orchestrator.nodes.route_tv.store_conversation_context", mock_store):
            _run(route_tv_node(state))
        mock_store.assert_awaited_once()
        kwargs = mock_store.call_args[1]
        assert kwargs["session_id"] == "ctx-session"
        assert kwargs["intent"] == "tv_control"
        assert kwargs["ttl"] == 300

    def test_store_context_not_called_when_result_not_success(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(return_value=_make_intent(
            action="launch", app_name="Netflix"
        ))
        th.handle_launch = AsyncMock(return_value={"success": False, "message": "Failed.", "error": "device_offline"})
        _runtime.set_tv_handler(th)
        state = _make_state(session_id="sess-fail")
        mock_store = AsyncMock()
        with patch("orchestrator.nodes.route_tv.store_conversation_context", mock_store):
            _run(route_tv_node(state))
        mock_store.assert_not_awaited()

    def test_store_context_not_called_when_session_id_none(self):
        th = _make_tv_handler()
        _runtime.set_tv_handler(th)
        state = _make_state(session_id=None)
        mock_store = AsyncMock()
        with patch("orchestrator.nodes.route_tv.store_conversation_context", mock_store):
            _run(route_tv_node(state))
        mock_store.assert_not_awaited()


class TestErrorHandlingAndMetrics:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_outer_exception_sets_error_and_fallback_answer(self):
        th = _make_tv_handler()
        th.parse_tv_intent = AsyncMock(side_effect=RuntimeError("ATV unreachable"))
        _runtime.set_tv_handler(th)
        state = _make_state()
        result = _run(route_tv_node(state))
        assert result.error == "ATV unreachable"
        assert "error" in result.answer.lower()
        assert "route_tv" in result.node_timings

    def test_timing_tracker_called(self):
        th = _make_tv_handler()
        _runtime.set_tv_handler(th)
        tracker = MagicMock()
        state = _make_state(timing_tracker=tracker)
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            _run(route_tv_node(state))
        tracker.track_sync.assert_called_once_with("graph", "route_tv", mock.ANY)

    def test_node_timings_always_set(self):
        th = _make_tv_handler()
        _runtime.set_tv_handler(th)
        state = _make_state()
        with patch("orchestrator.nodes.route_tv.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_tv_node(state))
        assert isinstance(result.node_timings.get("route_tv"), float)
