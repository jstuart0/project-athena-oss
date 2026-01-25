"""
Real-Time Event System for Project Athena

Provides event emission and subscription for real-time pipeline monitoring.
Supports Redis Pub/Sub for multi-instance scaling.

Usage:
    # Create emitter with Redis bridge
    emitter = await EventEmitterFactory.create(use_redis=True)

    # Subscribe to events (for WebSocket handlers)
    async def handler(event: PipelineEvent):
        await websocket.send_json(event.to_dict())
    emitter.subscribe(handler)

    # Emit events
    await emitter.emit(EventType.TOOL_SELECTED, session_id, {
        'tool_name': 'get_weather',
        'args': {'location': 'Baltimore'},
    })
"""

import asyncio
import json
import os
import time
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

import structlog

logger = structlog.get_logger()


class EventType(Enum):
    """Pipeline event types"""
    # Session lifecycle
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    # STT events
    STT_START = "stt_start"
    STT_PROGRESS = "stt_progress"  # For streaming STT
    STT_COMPLETE = "stt_complete"

    # Intent classification
    INTENT_CLASSIFYING = "intent_classifying"
    INTENT_CLASSIFIED = "intent_classified"

    # Tool events
    TOOL_SELECTING = "tool_selecting"
    TOOL_SELECTED = "tool_selected"
    TOOL_EXECUTING = "tool_executing"
    TOOL_COMPLETE = "tool_complete"
    TOOL_ERROR = "tool_error"

    # LLM events
    LLM_GENERATING = "llm_generating"
    LLM_STREAMING = "llm_streaming"  # For streaming responses
    LLM_COMPLETE = "llm_complete"

    # TTS events
    TTS_START = "tts_start"
    TTS_COMPLETE = "tts_complete"

    # Response
    RESPONSE_READY = "response_ready"


@dataclass
class PipelineEvent:
    """A single pipeline event"""
    event_type: EventType
    session_id: str
    timestamp: float
    data: Dict[str, Any]
    interface: Optional[str] = None
    duration_ms: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'event_type': self.event_type.value,
            'session_id': self.session_id,
            'timestamp': self.timestamp,
            'timestamp_iso': datetime.fromtimestamp(self.timestamp).isoformat(),
            'data': self.data,
            'interface': self.interface,
            'duration_ms': self.duration_ms,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PipelineEvent':
        """Create event from dictionary."""
        return cls(
            event_type=EventType(data['event_type']),
            session_id=data['session_id'],
            timestamp=data['timestamp'],
            data=data['data'],
            interface=data.get('interface'),
            duration_ms=data.get('duration_ms'),
        )


class EventEmitter:
    """
    Manages real-time event emission to connected clients.

    Usage:
        emitter = await EventEmitterFactory.create()

        # Subscribe to events
        async def handler(event: PipelineEvent):
            await websocket.send_json(event.to_dict())
        emitter.subscribe(handler)

        # Emit an event
        await emitter.emit(EventType.TOOL_SELECTED, session_id, {
            'tool_name': 'get_weather',
            'args': {'location': 'Baltimore'},
        })
    """

    def __init__(self):
        self._subscribers: Set[Callable] = set()
        self._session_start_times: Dict[str, float] = {}
        self._last_event_times: Dict[str, float] = {}
        self._enabled = True
        self._redis_bridge: Optional['RedisEventBridge'] = None

    async def check_enabled(self):
        """Check if real-time events are enabled via feature flag."""
        try:
            from shared.admin_config import get_admin_client
            client = get_admin_client()
            flags = await client.get_feature_flags()
            for f in flags:
                if f['flag_name'] == 'real_time_events':
                    self._enabled = f.get('enabled', True)
                    return
        except Exception:
            pass
        self._enabled = True

    def subscribe(self, handler: Callable):
        """Subscribe to events."""
        self._subscribers.add(handler)
        logger.debug(f"Event subscriber added, total: {len(self._subscribers)}")

    def unsubscribe(self, handler: Callable):
        """Unsubscribe from events."""
        self._subscribers.discard(handler)
        logger.debug(f"Event subscriber removed, total: {len(self._subscribers)}")

    def get_subscriber_count(self) -> int:
        """Get number of active subscribers."""
        return len(self._subscribers)

    async def emit(
        self,
        event_type: EventType,
        session_id: str,
        data: Dict[str, Any],
        interface: Optional[str] = None
    ):
        """Emit an event to all subscribers."""
        if not self._enabled:
            return

        now = time.time()

        # Track session start time
        if event_type == EventType.SESSION_START:
            self._session_start_times[session_id] = now
            self._last_event_times[session_id] = now

        # Calculate duration since last event
        last_time = self._last_event_times.get(session_id, now)
        duration_ms = int((now - last_time) * 1000)
        self._last_event_times[session_id] = now

        event = PipelineEvent(
            event_type=event_type,
            session_id=session_id,
            timestamp=now,
            data=data,
            interface=interface,
            duration_ms=duration_ms,
        )

        # Emit to local subscribers
        await self._emit_local(event)

        # Broadcast via Redis if available
        if self._redis_bridge:
            await self._redis_bridge.broadcast(event)

        # Clean up ended sessions
        if event_type == EventType.SESSION_END:
            self._session_start_times.pop(session_id, None)
            self._last_event_times.pop(session_id, None)

    async def _emit_local(self, event: PipelineEvent):
        """Emit event to local subscribers only."""
        if not self._subscribers:
            return

        # Notify all subscribers concurrently
        tasks = []
        for handler in self._subscribers:
            try:
                if asyncio.iscoroutinefunction(handler):
                    tasks.append(asyncio.create_task(handler(event)))
                else:
                    handler(event)
            except Exception as e:
                logger.warning(f"Event handler error: {e}")

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def get_session_duration(self, session_id: str) -> Optional[int]:
        """Get total duration of a session in milliseconds."""
        start_time = self._session_start_times.get(session_id)
        if start_time:
            return int((time.time() - start_time) * 1000)
        return None

    def get_stats(self) -> Dict[str, Any]:
        """Get emitter statistics."""
        return {
            'enabled': self._enabled,
            'subscriber_count': len(self._subscribers),
            'active_sessions': len(self._session_start_times),
            'has_redis_bridge': self._redis_bridge is not None,
        }


class RedisEventBridge:
    """
    Redis Pub/Sub bridge for multi-instance event distribution.

    Why Redis?
    - WebSocket subscribers are local to each server instance
    - Events emitted on Instance A won't reach subscribers on Instance B
    - Redis Pub/Sub broadcasts events to all instances

    Architecture:
        ┌─────────────┐    ┌─────────────┐    ┌─────────────┐
        │ Instance A  │    │   Redis     │    │ Instance B  │
        │             │    │             │    │             │
        │ emit() ─────┼───>│  Pub/Sub ───┼───>│ subscribers │
        │ subscribers │<───┼─── Channel  │<───┼── emit()    │
        └─────────────┘    └─────────────┘    └─────────────┘
    """

    def __init__(self, redis_url: str = None):
        self.redis_url = redis_url or os.getenv('REDIS_URL', 'redis://localhost:6379')
        self._redis: Optional[Any] = None
        self._pubsub: Optional[Any] = None
        self._listener_task: Optional[asyncio.Task] = None
        self._local_emitter: Optional[EventEmitter] = None
        self._channel = 'athena:pipeline_events'
        self._origin_id = id(self)
        self._connected = False

    async def connect(self, local_emitter: EventEmitter):
        """Connect to Redis and start listening for events."""
        self._local_emitter = local_emitter

        try:
            import redis.asyncio as redis_async
            self._redis = redis_async.from_url(self.redis_url)
            self._pubsub = self._redis.pubsub()
            await self._pubsub.subscribe(self._channel)

            # Start background listener
            self._listener_task = asyncio.create_task(self._listen())
            self._connected = True
            logger.info("Redis event bridge connected", channel=self._channel)
        except ImportError:
            logger.warning("redis-py not installed, Redis bridge disabled")
        except Exception as e:
            logger.warning(f"Redis connection failed, events are local-only: {e}")

    async def _listen(self):
        """Listen for events from other instances."""
        try:
            async for message in self._pubsub.listen():
                if message['type'] == 'message':
                    try:
                        event_data = json.loads(message['data'])
                        # Don't re-broadcast events we originated
                        if event_data.get('_origin') != self._origin_id:
                            await self._local_emitter._emit_local(
                                PipelineEvent.from_dict(event_data)
                            )
                    except Exception as e:
                        logger.warning(f"Error processing Redis event: {e}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Redis listener error: {e}")

    async def broadcast(self, event: PipelineEvent):
        """Broadcast event to all instances via Redis."""
        if self._redis and self._connected:
            try:
                event_dict = event.to_dict()
                event_dict['_origin'] = self._origin_id  # Mark origin to prevent loops
                await self._redis.publish(self._channel, json.dumps(event_dict))
            except Exception as e:
                logger.warning(f"Redis broadcast failed: {e}")

    async def disconnect(self):
        """Disconnect from Redis."""
        self._connected = False
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            await self._pubsub.unsubscribe(self._channel)
        if self._redis:
            await self._redis.close()
        logger.info("Redis event bridge disconnected")

    @property
    def is_connected(self) -> bool:
        """Check if Redis bridge is connected."""
        return self._connected


class EventEmitterFactory:
    """
    Factory for event emitters with optional Redis bridge.

    Usage:
        # In FastAPI lifespan
        async def lifespan(app):
            emitter = await EventEmitterFactory.create(use_redis=True)
            app.state.event_emitter = emitter
            yield
            await EventEmitterFactory.shutdown()
    """

    _instance: Optional[EventEmitter] = None
    _redis_bridge: Optional[RedisEventBridge] = None

    @classmethod
    async def create(cls, use_redis: bool = True) -> EventEmitter:
        """Create event emitter with optional Redis bridge."""
        if cls._instance is None:
            cls._instance = EventEmitter()

            if use_redis:
                cls._redis_bridge = RedisEventBridge()
                await cls._redis_bridge.connect(cls._instance)
                cls._instance._redis_bridge = cls._redis_bridge

        return cls._instance

    @classmethod
    def get(cls) -> Optional[EventEmitter]:
        """Get existing emitter."""
        return cls._instance

    @classmethod
    async def shutdown(cls):
        """Shutdown emitter and Redis bridge."""
        if cls._redis_bridge:
            await cls._redis_bridge.disconnect()
        cls._instance = None
        cls._redis_bridge = None

    @classmethod
    def clear(cls):
        """Clear factory state (for testing)."""
        cls._instance = None
        cls._redis_bridge = None


# Deprecated: For backwards compatibility
def get_event_emitter() -> EventEmitter:
    """
    DEPRECATED: Use EventEmitterFactory.create() or dependency injection.
    """
    warnings.warn(
        "get_event_emitter() is deprecated. Use EventEmitterFactory.",
        DeprecationWarning,
        stacklevel=2
    )
    return EventEmitterFactory.get() or EventEmitter()


# =============================================================================
# Convenience Functions for Common Events
# =============================================================================

async def emit_session_start(
    session_id: str,
    interface: str,
    metadata: Optional[Dict[str, Any]] = None
):
    """Emit session start event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.SESSION_START, session_id, {
            'interface': interface,
            **(metadata or {}),
        }, interface)


async def emit_session_end(
    session_id: str,
    interface: Optional[str] = None,
    success: bool = True,
    error: Optional[str] = None
):
    """Emit session end event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.SESSION_END, session_id, {
            'success': success,
            'error': error,
        }, interface)


async def emit_stt_complete(
    session_id: str,
    text: str,
    engine: str,
    duration_ms: int,
    interface: Optional[str] = None
):
    """Emit STT completion event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.STT_COMPLETE, session_id, {
            'text': text,
            'engine': engine,
            'duration_ms': duration_ms,
        }, interface)


async def emit_intent_classified(
    session_id: str,
    intent: str,
    confidence: float,
    entities: Dict[str, Any],
    requires_llm: bool,
    interface: Optional[str] = None
):
    """Emit intent classification event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.INTENT_CLASSIFIED, session_id, {
            'intent': intent,
            'confidence': confidence,
            'entities': entities,
            'requires_llm': requires_llm,
        }, interface)


async def emit_tool_selected(
    session_id: str,
    tool_name: str,
    tool_source: str,
    args: Dict[str, Any],
    interface: Optional[str] = None
):
    """Emit tool selection event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.TOOL_SELECTED, session_id, {
            'tool_name': tool_name,
            'tool_source': tool_source,
            'args': args,
        }, interface)


async def emit_tool_executing(
    session_id: str,
    tool_name: str,
    interface: Optional[str] = None
):
    """Emit tool execution start event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.TOOL_EXECUTING, session_id, {
            'tool_name': tool_name,
        }, interface)


async def emit_tool_complete(
    session_id: str,
    tool_name: str,
    success: bool,
    result_summary: str,
    execution_time_ms: int,
    interface: Optional[str] = None
):
    """Emit tool completion event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.TOOL_COMPLETE, session_id, {
            'tool_name': tool_name,
            'success': success,
            'result_summary': result_summary[:200],  # Truncate for display
            'execution_time_ms': execution_time_ms,
        }, interface)


async def emit_tool_error(
    session_id: str,
    tool_name: str,
    error: str,
    interface: Optional[str] = None
):
    """Emit tool error event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.TOOL_ERROR, session_id, {
            'tool_name': tool_name,
            'error': error,
        }, interface)


async def emit_llm_generating(
    session_id: str,
    model: str,
    interface: Optional[str] = None
):
    """Emit LLM generation start event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.LLM_GENERATING, session_id, {
            'model': model,
        }, interface)


async def emit_llm_complete(
    session_id: str,
    model: str,
    tokens: int,
    duration_ms: int,
    interface: Optional[str] = None
):
    """Emit LLM completion event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.LLM_COMPLETE, session_id, {
            'model': model,
            'tokens': tokens,
            'duration_ms': duration_ms,
        }, interface)


async def emit_tts_complete(
    session_id: str,
    engine: str,
    text_length: int,
    audio_duration_ms: int,
    interface: Optional[str] = None
):
    """Emit TTS completion event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.TTS_COMPLETE, session_id, {
            'engine': engine,
            'text_length': text_length,
            'audio_duration_ms': audio_duration_ms,
        }, interface)


async def emit_response_ready(
    session_id: str,
    response_text: str,
    total_duration_ms: int,
    interface: Optional[str] = None
):
    """Emit response ready event."""
    emitter = EventEmitterFactory.get()
    if emitter:
        await emitter.emit(EventType.RESPONSE_READY, session_id, {
            'response_preview': response_text[:100],
            'total_duration_ms': total_duration_ms,
        }, interface)
