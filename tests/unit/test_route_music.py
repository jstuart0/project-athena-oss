"""Unit tests for orchestrator.nodes.route_music.route_music_node.

Covers major routing branches:

 1.  music_handler not initialised — returns not-configured answer.
 2.  MUSIC_PLAY intent — single room playback (happy path).
 3.  MUSIC_PLAY intent — room group synced playback.
 4.  MUSIC_PLAY intent — browser playback fallback to room speaker.
 5.  MUSIC_PLAY intent — browser playback with jarvis_web room falls back to "office".
 6.  MUSIC_CONTROL intent — pause/next/volume command.
 7.  store_conversation_context called on success (no "sorry" in answer).
 8.  store_conversation_context NOT called when answer contains "sorry".
 9.  store_conversation_context NOT called when session_id is None.
10.  Outer exception handler — error set, fallback answer returned.
11.  timing_tracker.track_sync called after node completes.
12.  node_timings["route_music"] set on every return path (early-exit path).

Patching strategy:
- music_handler installed via orchestrator.nodes._runtime.set_music_handler() —
  the _MusicHandlerProxy delegates to _runtime at call time.
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

from orchestrator.nodes import route_music_node  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.state import OrchestratorState, IntentCategory  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


def _make_state(
    *,
    query: str = "play some jazz",
    session_id: str | None = "sess-1",
    room: str | None = "living room",
    intent: IntentCategory | None = IntentCategory.MUSIC_PLAY,
    interface_type: str | None = None,
    timing_tracker=None,
) -> OrchestratorState:
    state = OrchestratorState(query=query)
    state.session_id = session_id
    state.room = room
    state.intent = intent
    state.interface_type = interface_type
    state.timing_tracker = timing_tracker
    state.node_timings = {}
    state.retrieved_data = {}
    return state


def _make_music_handler(**overrides):
    mh = MagicMock()
    mh.parse_music_play_intent = AsyncMock(return_value={
        "media_type": "artist",
        "media_id": "Miles Davis",
        "room": "living room",
        "radio_mode": True,
        "play_in_browser": False,
        "is_room_group": False,
    })
    mh.handle_play = AsyncMock(return_value="Now playing Miles Davis in living room.")
    mh.handle_room_group_play = AsyncMock(return_value="Playing throughout the house.")
    mh.parse_music_control_intent = AsyncMock(return_value={
        "action": "pause",
        "room": "living room",
        "volume_level": None,
    })
    mh.handle_control = AsyncMock(return_value="Music paused.")
    for k, v in overrides.items():
        setattr(mh, k, v)
    return mh


# Disable strict mode so unset singletons don't warn.
_runtime.reset_for_test()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestMusicHandlerNotInitialised:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_returns_not_configured_when_handler_absent(self):
        _runtime.set_music_handler(None)
        state = _make_state()
        result = _run(route_music_node(state))
        assert result.answer == (
            "Music playback is not configured. Please set up Music Assistant in Home Assistant."
        )
        assert result.error == "music_handler_not_initialized"
        assert "route_music" in result.node_timings

    def test_node_timings_set_on_early_exit(self):
        _runtime.set_music_handler(None)
        state = _make_state()
        result = _run(route_music_node(state))
        assert isinstance(result.node_timings.get("route_music"), float)


class TestMusicPlaySingleRoom:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_single_room_play_happy_path(self):
        mh = _make_music_handler()
        _runtime.set_music_handler(mh)
        state = _make_state(query="play Miles Davis in the living room")
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_music_node(state))
        mh.parse_music_play_intent.assert_awaited_once_with(
            "play Miles Davis in the living room",
            room="living room",
            interface_type=None,
        )
        mh.handle_play.assert_awaited_once()
        assert result.answer == "Now playing Miles Davis in living room."
        assert result.retrieved_data == {"music_intent": mh.parse_music_play_intent.return_value}
        assert "route_music" in result.node_timings

    def test_interface_type_forwarded_to_parse(self):
        mh = _make_music_handler()
        _runtime.set_music_handler(mh)
        state = _make_state(interface_type="jarvis_web")
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            _run(route_music_node(state))
        _, kwargs = mh.parse_music_play_intent.call_args
        assert kwargs["interface_type"] == "jarvis_web"


class TestMusicPlayRoomGroup:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_room_group_play_routes_to_group_handler(self):
        mh = _make_music_handler()
        mh.parse_music_play_intent = AsyncMock(return_value={
            "media_type": "playlist",
            "media_id": "Morning Mix",
            "room": "everywhere",
            "radio_mode": False,
            "play_in_browser": False,
            "is_room_group": True,
        })
        _runtime.set_music_handler(mh)
        state = _make_state(query="play Morning Mix everywhere")
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_music_node(state))
        mh.handle_room_group_play.assert_awaited_once_with(
            group_name="everywhere",
            media_type="playlist",
            media_id="Morning Mix",
            radio_mode=False,
        )
        mh.handle_play.assert_not_awaited()
        assert result.answer == "Playing throughout the house."


class TestBrowserPlaybackFallback:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_browser_playback_falls_back_to_room_speaker(self):
        mh = _make_music_handler()
        mh.parse_music_play_intent = AsyncMock(return_value={
            "media_type": "artist",
            "media_id": "The Beatles",
            "room": "office",
            "radio_mode": True,
            "play_in_browser": True,
            "is_room_group": False,
        })
        mh.handle_play = AsyncMock(return_value="Playing The Beatles in office.")
        _runtime.set_music_handler(mh)
        state = _make_state(query="play The Beatles here", room="office")
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_music_node(state))
        mh.handle_play.assert_awaited_once_with(
            media_type="artist",
            media_id="The Beatles",
            room="office",
            radio_mode=True,
        )
        assert "The Beatles" in result.answer
        assert "office" in result.answer
        assert result.retrieved_data.get("playback_room") == "office"

    def test_browser_playback_jarvis_web_room_falls_back_to_office(self):
        mh = _make_music_handler()
        mh.parse_music_play_intent = AsyncMock(return_value={
            "media_type": "artist",
            "media_id": "Radiohead",
            "room": "jarvis_web",
            "radio_mode": True,
            "play_in_browser": True,
            "is_room_group": False,
        })
        mh.handle_play = AsyncMock(return_value="Playing in office.")
        _runtime.set_music_handler(mh)
        state = _make_state(query="play Radiohead here", room="jarvis_web")
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_music_node(state))
        _, kwargs = mh.handle_play.call_args
        assert kwargs["room"] == "office"
        assert result.retrieved_data.get("playback_room") == "office"


class TestMusicControl:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_music_control_pause(self):
        mh = _make_music_handler()
        _runtime.set_music_handler(mh)
        state = _make_state(
            query="pause the music",
            intent=IntentCategory.MUSIC_CONTROL,
        )
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_music_node(state))
        mh.parse_music_control_intent.assert_awaited_once_with(
            "pause the music",
            room="living room",
        )
        mh.handle_control.assert_awaited_once_with(
            action="pause",
            room="living room",
            volume_level=None,
        )
        assert result.answer == "Music paused."
        assert result.retrieved_data == {"music_intent": mh.parse_music_control_intent.return_value}
        assert "route_music" in result.node_timings


class TestContextStorage:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_store_context_called_on_success(self):
        mh = _make_music_handler()
        _runtime.set_music_handler(mh)
        state = _make_state(session_id="ctx-session")
        mock_store = AsyncMock()
        with patch("orchestrator.nodes.route_music.store_conversation_context", mock_store):
            _run(route_music_node(state))
        mock_store.assert_awaited_once()
        kwargs = mock_store.call_args[1]
        assert kwargs["session_id"] == "ctx-session"
        assert kwargs["intent"] == "music"
        assert kwargs["ttl"] == 300

    def test_store_context_not_called_when_answer_contains_sorry(self):
        mh = _make_music_handler()
        mh.handle_play = AsyncMock(return_value="I'm sorry, that artist wasn't found.")
        _runtime.set_music_handler(mh)
        state = _make_state()
        mock_store = AsyncMock()
        with patch("orchestrator.nodes.route_music.store_conversation_context", mock_store):
            _run(route_music_node(state))
        mock_store.assert_not_awaited()

    def test_store_context_not_called_when_session_id_none(self):
        mh = _make_music_handler()
        _runtime.set_music_handler(mh)
        state = _make_state(session_id=None)
        mock_store = AsyncMock()
        with patch("orchestrator.nodes.route_music.store_conversation_context", mock_store):
            _run(route_music_node(state))
        mock_store.assert_not_awaited()


class TestErrorHandlingAndMetrics:
    def setup_method(self):
        _runtime.reset_for_test()

    def test_outer_exception_sets_error_and_fallback_answer(self):
        mh = _make_music_handler()
        mh.parse_music_play_intent = AsyncMock(side_effect=RuntimeError("MA down"))
        _runtime.set_music_handler(mh)
        state = _make_state()
        result = _run(route_music_node(state))
        assert result.error == "MA down"
        assert "error" in result.answer.lower()
        assert "route_music" in result.node_timings

    def test_timing_tracker_called(self):
        mh = _make_music_handler()
        _runtime.set_music_handler(mh)
        tracker = MagicMock()
        state = _make_state(timing_tracker=tracker)
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            _run(route_music_node(state))
        tracker.track_sync.assert_called_once_with("graph", "route_music", mock.ANY)

    def test_node_timings_always_set(self):
        mh = _make_music_handler()
        _runtime.set_music_handler(mh)
        state = _make_state()
        with patch("orchestrator.nodes.route_music.store_conversation_context",
                   new_callable=AsyncMock):
            result = _run(route_music_node(state))
        assert isinstance(result.node_timings.get("route_music"), float)
