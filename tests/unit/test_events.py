"""
Unit tests for Event System.

Tests the EventEmitter, RedisEventBridge, and EventEmitterFactory.
"""
import pytest
import time
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, 'src')

from shared.events import (
    EventType,
    PipelineEvent,
    EventEmitter,
    RedisEventBridge,
    EventEmitterFactory,
    emit_tool_selected,
    emit_tool_complete,
    emit_intent_classified,
    emit_session_start,
    emit_session_end,
)


# =============================================================================
# Test EventType Enum
# =============================================================================

class TestEventType:
    """Tests for EventType enum."""

    def test_session_events(self):
        """Session lifecycle events exist."""
        assert EventType.SESSION_START.value == "session_start"
        assert EventType.SESSION_END.value == "session_end"

    def test_stt_events(self):
        """STT events exist."""
        assert EventType.STT_START.value == "stt_start"
        assert EventType.STT_PROGRESS.value == "stt_progress"
        assert EventType.STT_COMPLETE.value == "stt_complete"

    def test_tool_events(self):
        """Tool events exist."""
        assert EventType.TOOL_SELECTING.value == "tool_selecting"
        assert EventType.TOOL_SELECTED.value == "tool_selected"
        assert EventType.TOOL_EXECUTING.value == "tool_executing"
        assert EventType.TOOL_COMPLETE.value == "tool_complete"
        assert EventType.TOOL_ERROR.value == "tool_error"

    def test_llm_events(self):
        """LLM events exist."""
        assert EventType.LLM_GENERATING.value == "llm_generating"
        assert EventType.LLM_STREAMING.value == "llm_streaming"
        assert EventType.LLM_COMPLETE.value == "llm_complete"


# =============================================================================
# Test PipelineEvent Dataclass
# =============================================================================

class TestPipelineEvent:
    """Tests for PipelineEvent dataclass."""

    def test_event_creation(self):
        """PipelineEvent can be created."""
        event = PipelineEvent(
            event_type=EventType.TOOL_SELECTED,
            session_id="test-123",
            timestamp=1234567890.0,
            data={'tool_name': 'weather'},
            interface='web_jarvis',
            duration_ms=100
        )
        assert event.event_type == EventType.TOOL_SELECTED
        assert event.session_id == "test-123"
        assert event.data['tool_name'] == 'weather'

    def test_to_dict(self):
        """to_dict returns correct structure."""
        event = PipelineEvent(
            event_type=EventType.TOOL_COMPLETE,
            session_id="sess-1",
            timestamp=1234567890.0,
            data={'success': True},
        )
        d = event.to_dict()
        assert d['event_type'] == 'tool_complete'
        assert d['session_id'] == 'sess-1'
        assert d['data'] == {'success': True}
        assert 'timestamp_iso' in d

    def test_to_json(self):
        """to_json returns valid JSON string."""
        event = PipelineEvent(
            event_type=EventType.SESSION_START,
            session_id="sess-1",
            timestamp=1234567890.0,
            data={},
        )
        json_str = event.to_json()
        assert '"event_type": "session_start"' in json_str
        assert '"session_id": "sess-1"' in json_str

    def test_from_dict(self):
        """from_dict creates event from dictionary."""
        d = {
            'event_type': 'tool_selected',
            'session_id': 'sess-1',
            'timestamp': 1234567890.0,
            'data': {'tool': 'weather'},
            'interface': 'admin',
            'duration_ms': 50,
        }
        event = PipelineEvent.from_dict(d)
        assert event.event_type == EventType.TOOL_SELECTED
        assert event.data['tool'] == 'weather'
        assert event.interface == 'admin'


# =============================================================================
# Test EventEmitter
# =============================================================================

class TestEventEmitter:
    """Tests for EventEmitter class."""

    def setup_method(self):
        """Clear factory before each test."""
        EventEmitterFactory.clear()

    def test_emitter_initial_state(self):
        """Emitter starts with no subscribers."""
        emitter = EventEmitter()
        assert emitter.get_subscriber_count() == 0
        assert emitter._enabled is True

    def test_subscribe(self):
        """subscribe adds handler."""
        emitter = EventEmitter()

        async def handler(event):
            pass

        emitter.subscribe(handler)
        assert emitter.get_subscriber_count() == 1

    def test_unsubscribe(self):
        """unsubscribe removes handler."""
        emitter = EventEmitter()

        async def handler(event):
            pass

        emitter.subscribe(handler)
        assert emitter.get_subscriber_count() == 1

        emitter.unsubscribe(handler)
        assert emitter.get_subscriber_count() == 0

    @pytest.mark.asyncio
    async def test_emit_to_subscribers(self):
        """Events are delivered to all subscribers."""
        emitter = EventEmitter()
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(handler)
        await emitter.emit(
            EventType.TOOL_SELECTED,
            session_id='test-123',
            data={'tool_name': 'weather'},
        )

        assert len(received) == 1
        assert received[0].data['tool_name'] == 'weather'
        assert received[0].event_type == EventType.TOOL_SELECTED

    @pytest.mark.asyncio
    async def test_emit_multiple_subscribers(self):
        """Events go to all subscribers."""
        emitter = EventEmitter()
        received1 = []
        received2 = []

        async def handler1(event):
            received1.append(event)

        async def handler2(event):
            received2.append(event)

        emitter.subscribe(handler1)
        emitter.subscribe(handler2)

        await emitter.emit(EventType.SESSION_START, 'sess-1', {})

        assert len(received1) == 1
        assert len(received2) == 1

    @pytest.mark.asyncio
    async def test_emit_disabled(self):
        """Disabled emitter doesn't emit."""
        emitter = EventEmitter()
        emitter._enabled = False
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(handler)
        await emitter.emit(EventType.SESSION_START, 'sess-1', {})

        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_duration_tracking(self):
        """Duration is calculated between events."""
        emitter = EventEmitter()
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(handler)

        await emitter.emit(EventType.SESSION_START, 'sess-1', {})
        await asyncio.sleep(0.1)  # 100ms
        await emitter.emit(EventType.STT_COMPLETE, 'sess-1', {})

        # First event should have 0 duration
        assert received[0].duration_ms == 0
        # Second event should have ~100ms duration
        assert received[1].duration_ms >= 90  # Allow some variance

    def test_get_session_duration(self):
        """get_session_duration returns elapsed time."""
        emitter = EventEmitter()
        emitter._session_start_times['sess-1'] = time.time() - 1.0  # 1 second ago

        duration = emitter.get_session_duration('sess-1')
        assert duration >= 1000  # At least 1000ms

    def test_get_session_duration_unknown(self):
        """get_session_duration returns None for unknown session."""
        emitter = EventEmitter()
        assert emitter.get_session_duration('unknown') is None

    def test_get_stats(self):
        """get_stats returns correct structure."""
        emitter = EventEmitter()
        emitter._session_start_times['sess-1'] = time.time()

        async def handler(event):
            pass
        emitter.subscribe(handler)

        stats = emitter.get_stats()
        assert stats['enabled'] is True
        assert stats['subscriber_count'] == 1
        assert stats['active_sessions'] == 1
        assert stats['has_redis_bridge'] is False


# =============================================================================
# Test RedisEventBridge
# =============================================================================

class TestRedisEventBridge:
    """Tests for RedisEventBridge class."""

    def test_bridge_initialization(self):
        """Bridge initializes with defaults."""
        bridge = RedisEventBridge()
        assert 'redis://' in bridge.redis_url
        assert bridge._channel == 'athena:pipeline_events'
        assert bridge.is_connected is False

    def test_custom_redis_url(self):
        """Bridge accepts custom Redis URL."""
        bridge = RedisEventBridge(redis_url='redis://custom:6379')
        assert bridge.redis_url == 'redis://custom:6379'

    @pytest.mark.asyncio
    async def test_connect_without_redis(self):
        """Connect handles missing redis gracefully."""
        bridge = RedisEventBridge()
        emitter = EventEmitter()

        # Mock redis import failure
        with patch.dict('sys.modules', {'redis.asyncio': None}):
            await bridge.connect(emitter)

        # Should not be connected but also not crash
        assert bridge.is_connected is False

    @pytest.mark.asyncio
    async def test_broadcast_when_not_connected(self):
        """Broadcast does nothing when not connected."""
        bridge = RedisEventBridge()
        event = PipelineEvent(
            event_type=EventType.TOOL_SELECTED,
            session_id='sess-1',
            timestamp=time.time(),
            data={},
        )
        # Should not raise
        await bridge.broadcast(event)


# =============================================================================
# Test EventEmitterFactory
# =============================================================================

class TestEventEmitterFactory:
    """Tests for EventEmitterFactory class."""

    def setup_method(self):
        """Clear factory before each test."""
        EventEmitterFactory.clear()

    @pytest.mark.asyncio
    async def test_create_without_redis(self):
        """create returns emitter without Redis."""
        emitter = await EventEmitterFactory.create(use_redis=False)
        assert isinstance(emitter, EventEmitter)
        assert emitter._redis_bridge is None

    @pytest.mark.asyncio
    async def test_create_returns_same_instance(self):
        """create returns same instance on subsequent calls."""
        emitter1 = await EventEmitterFactory.create(use_redis=False)
        emitter2 = await EventEmitterFactory.create(use_redis=False)
        assert emitter1 is emitter2

    def test_get_before_create(self):
        """get returns None before create."""
        assert EventEmitterFactory.get() is None

    @pytest.mark.asyncio
    async def test_get_after_create(self):
        """get returns emitter after create."""
        await EventEmitterFactory.create(use_redis=False)
        emitter = EventEmitterFactory.get()
        assert isinstance(emitter, EventEmitter)

    @pytest.mark.asyncio
    async def test_shutdown(self):
        """shutdown clears instance."""
        await EventEmitterFactory.create(use_redis=False)
        await EventEmitterFactory.shutdown()
        assert EventEmitterFactory.get() is None


# =============================================================================
# Test Convenience Functions
# =============================================================================

class TestConvenienceFunctions:
    """Tests for convenience event functions."""

    def setup_method(self):
        """Clear factory before each test."""
        EventEmitterFactory.clear()

    @pytest.mark.asyncio
    async def test_emit_tool_selected(self):
        """emit_tool_selected emits correct event."""
        emitter = await EventEmitterFactory.create(use_redis=False)
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(handler)

        await emit_tool_selected(
            session_id='sess-1',
            tool_name='weather',
            tool_source='admin',
            args={'location': 'Baltimore'},
        )

        assert len(received) == 1
        assert received[0].event_type == EventType.TOOL_SELECTED
        assert received[0].data['tool_name'] == 'weather'
        assert received[0].data['tool_source'] == 'admin'

    @pytest.mark.asyncio
    async def test_emit_tool_complete(self):
        """emit_tool_complete emits correct event."""
        emitter = await EventEmitterFactory.create(use_redis=False)
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(handler)

        await emit_tool_complete(
            session_id='sess-1',
            tool_name='weather',
            success=True,
            result_summary='Temperature is 72F',
            execution_time_ms=150,
        )

        assert len(received) == 1
        assert received[0].data['success'] is True
        assert received[0].data['execution_time_ms'] == 150

    @pytest.mark.asyncio
    async def test_emit_intent_classified(self):
        """emit_intent_classified emits correct event."""
        emitter = await EventEmitterFactory.create(use_redis=False)
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(handler)

        await emit_intent_classified(
            session_id='sess-1',
            intent='weather_query',
            confidence=0.95,
            entities={'location': 'Baltimore'},
            requires_llm=False,
        )

        assert len(received) == 1
        assert received[0].event_type == EventType.INTENT_CLASSIFIED
        assert received[0].data['intent'] == 'weather_query'
        assert received[0].data['confidence'] == 0.95

    @pytest.mark.asyncio
    async def test_emit_session_lifecycle(self):
        """Session start and end events work."""
        emitter = await EventEmitterFactory.create(use_redis=False)
        received = []

        async def handler(event):
            received.append(event)

        emitter.subscribe(handler)

        await emit_session_start('sess-1', 'web_jarvis')
        await emit_session_end('sess-1', success=True)

        assert len(received) == 2
        assert received[0].event_type == EventType.SESSION_START
        assert received[1].event_type == EventType.SESSION_END

    @pytest.mark.asyncio
    async def test_convenience_no_emitter(self):
        """Convenience functions don't crash without emitter."""
        # Don't create emitter
        await emit_tool_selected('sess-1', 'tool', 'source', {})
        # Should not raise
