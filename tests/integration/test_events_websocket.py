"""
Integration tests for Events + WebSocket system.

Tests the end-to-end flow:
1. EventEmitter emits events
2. Events are broadcast to WebSocket clients
3. WebSocket clients receive real-time updates
"""

import asyncio
import json
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../admin/backend'))


class TestEventEmitterWebSocketIntegration:
    """Test EventEmitter to WebSocket integration."""

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket connection."""
        ws = AsyncMock()
        ws.send_json = AsyncMock()
        ws.receive_json = AsyncMock(return_value={"type": "ping"})
        return ws

    @pytest.fixture
    def event_emitter(self):
        """Create an EventEmitter instance."""
        from shared.events import EventEmitter
        return EventEmitter()

    @pytest.mark.asyncio
    async def test_event_broadcast_to_websocket(self, event_emitter, mock_websocket):
        """Test that events emitted are broadcast to WebSocket clients."""
        received_events = []

        # Create a handler that captures events
        async def capture_handler(event):
            received_events.append(event)
            # Simulate WebSocket broadcast
            await mock_websocket.send_json(event.to_dict())

        # Subscribe handler
        event_emitter.subscribe(capture_handler)

        # Emit an event
        await event_emitter.emit(
            event_type="session_start",
            session_id="test-session-123",
            data={"query": "What's the weather?", "interface": "voice"}
        )

        # Allow async processing
        await asyncio.sleep(0.1)

        # Verify event was received
        assert len(received_events) == 1
        assert received_events[0].session_id == "test-session-123"
        assert received_events[0].event_type.value == "session_start"

        # Verify WebSocket was called
        mock_websocket.send_json.assert_called_once()
        sent_data = mock_websocket.send_json.call_args[0][0]
        assert sent_data["session_id"] == "test-session-123"

    @pytest.mark.asyncio
    async def test_multiple_websocket_clients(self, event_emitter):
        """Test broadcasting to multiple WebSocket clients."""
        from shared.events import EventType

        # Create multiple mock WebSocket clients
        clients = [AsyncMock() for _ in range(3)]
        received_counts = [0, 0, 0]

        # Create handlers for each client
        async def create_handler(index):
            async def handler(event):
                received_counts[index] += 1
                await clients[index].send_json(event.to_dict())
            return handler

        # Subscribe all handlers
        for i in range(3):
            handler = await create_handler(i)
            event_emitter.subscribe(handler)

        # Emit event
        await event_emitter.emit(
            event_type=EventType.TOOL_SELECTED,
            session_id="session-multi",
            data={"tool_name": "weather"}
        )

        await asyncio.sleep(0.1)

        # All clients should receive the event
        assert received_counts == [1, 1, 1]
        for client in clients:
            client.send_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_event_types_flow(self, event_emitter, mock_websocket):
        """Test a complete session flow with multiple event types."""
        from shared.events import EventType

        events_received = []

        async def capture_handler(event):
            events_received.append(event)
            await mock_websocket.send_json(event.to_dict())

        event_emitter.subscribe(capture_handler)

        session_id = "flow-session-456"

        # Simulate complete request flow
        await event_emitter.emit(EventType.SESSION_START, session_id, {"query": "Turn on kitchen lights"})
        await event_emitter.emit(EventType.INTENT_DETECTED, session_id, {"intent": "control", "confidence": 0.95})
        await event_emitter.emit(EventType.TOOL_SELECTED, session_id, {"tool_name": "home_assistant"})
        await event_emitter.emit(EventType.TOOL_RESULT, session_id, {"success": True, "entities_affected": 1})
        await event_emitter.emit(EventType.RESPONSE_GENERATED, session_id, {"response": "I've turned on the kitchen lights."})
        await event_emitter.emit(EventType.SESSION_END, session_id, {"duration_ms": 1234})

        await asyncio.sleep(0.1)

        # Verify all events in order
        assert len(events_received) == 6
        assert events_received[0].event_type == EventType.SESSION_START
        assert events_received[1].event_type == EventType.INTENT_DETECTED
        assert events_received[2].event_type == EventType.TOOL_SELECTED
        assert events_received[3].event_type == EventType.TOOL_RESULT
        assert events_received[4].event_type == EventType.RESPONSE_GENERATED
        assert events_received[5].event_type == EventType.SESSION_END

        # Verify all have same session_id
        for event in events_received:
            assert event.session_id == session_id

    @pytest.mark.asyncio
    async def test_handler_exception_isolation(self, event_emitter):
        """Test that one handler exception doesn't affect others."""
        from shared.events import EventType

        results = {"good": 0, "also_good": 0}

        async def good_handler(event):
            results["good"] += 1

        async def bad_handler(event):
            raise Exception("Handler error!")

        async def also_good_handler(event):
            results["also_good"] += 1

        event_emitter.subscribe(good_handler)
        event_emitter.subscribe(bad_handler)
        event_emitter.subscribe(also_good_handler)

        # Emit event - should not raise despite bad_handler
        await event_emitter.emit(EventType.SESSION_START, "error-test", {})
        await asyncio.sleep(0.1)

        # Both good handlers should have been called
        assert results["good"] == 1
        assert results["also_good"] == 1

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_events(self, event_emitter):
        """Test that unsubscribed handlers stop receiving events."""
        from shared.events import EventType

        count = 0

        async def handler(event):
            nonlocal count
            count += 1

        event_emitter.subscribe(handler)

        # First event should be received
        await event_emitter.emit(EventType.SESSION_START, "unsub-test", {})
        await asyncio.sleep(0.1)
        assert count == 1

        # Unsubscribe
        event_emitter.unsubscribe(handler)

        # Second event should NOT be received
        await event_emitter.emit(EventType.SESSION_END, "unsub-test", {})
        await asyncio.sleep(0.1)
        assert count == 1  # Still 1, not 2


class TestPipelineEventSerialization:
    """Test PipelineEvent serialization for WebSocket transmission."""

    def test_event_to_dict(self):
        """Test PipelineEvent serialization."""
        from shared.events import PipelineEvent, EventType

        event = PipelineEvent(
            event_type=EventType.TOOL_SELECTED,
            session_id="serial-test",
            data={"tool_name": "weather", "confidence": 0.9},
            interface="voice"
        )

        d = event.to_dict()

        assert d["event_type"] == "tool_selected"
        assert d["session_id"] == "serial-test"
        assert d["interface"] == "voice"
        assert d["data"]["tool_name"] == "weather"
        assert "timestamp" in d

    def test_event_json_serializable(self):
        """Test that event dict is JSON serializable."""
        from shared.events import PipelineEvent, EventType

        event = PipelineEvent(
            event_type=EventType.RESPONSE_GENERATED,
            session_id="json-test",
            data={
                "response": "Here's the weather",
                "latency_ms": 1234,
                "tokens": 50
            }
        )

        # Should not raise
        json_str = json.dumps(event.to_dict())
        parsed = json.loads(json_str)

        assert parsed["event_type"] == "response_generated"
        assert parsed["data"]["latency_ms"] == 1234


class TestConvenienceFunctions:
    """Test convenience emit functions."""

    @pytest.fixture
    def mock_factory_emitter(self):
        """Setup mock EventEmitterFactory."""
        from shared.events import EventEmitterFactory, EventEmitter

        emitter = EventEmitter()
        EventEmitterFactory._instance = emitter
        yield emitter
        EventEmitterFactory._instance = None

    @pytest.mark.asyncio
    async def test_emit_session_start(self, mock_factory_emitter):
        """Test emit_session_start convenience function."""
        from shared import events

        received = []
        async def handler(event):
            received.append(event)

        mock_factory_emitter.subscribe(handler)

        await events.emit_session_start(
            session_id="conv-test",
            query="Hello",
            interface="chat"
        )

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].event_type == events.EventType.SESSION_START
        assert received[0].data["query"] == "Hello"

    @pytest.mark.asyncio
    async def test_emit_tool_selected(self, mock_factory_emitter):
        """Test emit_tool_selected convenience function."""
        from shared import events

        received = []
        async def handler(event):
            received.append(event)

        mock_factory_emitter.subscribe(handler)

        await events.emit_tool_selected(
            session_id="tool-test",
            tool_name="weather",
            reason="Intent matches weather keywords"
        )

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].data["tool_name"] == "weather"
        assert "reason" in received[0].data

    @pytest.mark.asyncio
    async def test_emit_error(self, mock_factory_emitter):
        """Test emit_error convenience function."""
        from shared import events

        received = []
        async def handler(event):
            received.append(event)

        mock_factory_emitter.subscribe(handler)

        await events.emit_error(
            session_id="error-conv-test",
            error="Something went wrong",
            error_type="ValidationError"
        )

        await asyncio.sleep(0.1)

        assert len(received) == 1
        assert received[0].event_type == events.EventType.ERROR
        assert received[0].data["error"] == "Something went wrong"


class TestWebSocketManagerIntegration:
    """Test WebSocketManager integration with events."""

    @pytest.mark.asyncio
    async def test_websocket_manager_broadcast(self):
        """Test WebSocketManager broadcasting to connected clients."""
        # Import WebSocket manager
        from app.routes.websocket import WebSocketManager

        manager = WebSocketManager()

        # Create mock clients
        client1 = AsyncMock()
        client2 = AsyncMock()

        # Manually add clients (simulating accepted connections)
        manager._clients.add(client1)
        manager._clients.add(client2)

        # Broadcast message
        await manager.broadcast({"event_type": "test", "data": {"message": "Hello"}})

        # Both clients should receive
        client1.send_json.assert_called_once()
        client2.send_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_websocket_manager_removes_failed_clients(self):
        """Test that failed clients are removed from broadcast list."""
        from app.routes.websocket import WebSocketManager

        manager = WebSocketManager()

        # Good client
        good_client = AsyncMock()

        # Bad client that raises on send
        bad_client = AsyncMock()
        bad_client.send_json = AsyncMock(side_effect=Exception("Connection lost"))

        manager._clients.add(good_client)
        manager._clients.add(bad_client)

        assert manager.client_count == 2

        # Broadcast - bad client should be removed
        await manager.broadcast({"event_type": "test"})

        # Good client still works
        good_client.send_json.assert_called_once()

        # Bad client should be removed
        assert manager.client_count == 1
        assert good_client in manager._clients
        assert bad_client not in manager._clients


class TestEventHandlerRegistration:
    """Test event handler registration with WebSocket."""

    @pytest.mark.asyncio
    async def test_register_event_handler(self):
        """Test registering WebSocket broadcast handler."""
        from shared.events import EventEmitterFactory, EventEmitter
        from app.routes.websocket import (
            register_event_handler,
            unregister_event_handler,
            ws_manager,
            _event_handler_registered
        )

        # Create emitter and register in factory
        emitter = EventEmitter()
        EventEmitterFactory._instance = emitter

        # Register handler
        await register_event_handler()

        # Handler should be subscribed
        assert len(emitter._handlers) == 1

        # Clean up
        await unregister_event_handler()
        EventEmitterFactory._instance = None

    @pytest.mark.asyncio
    async def test_event_handler_broadcasts_to_websocket(self):
        """Test that event handler forwards events to WebSocket clients."""
        from shared.events import EventEmitterFactory, EventEmitter, PipelineEvent, EventType
        from app.routes.websocket import (
            ws_manager,
            _websocket_event_handler
        )

        # Create mock client
        mock_client = AsyncMock()
        ws_manager._clients.add(mock_client)

        # Create event
        event = PipelineEvent(
            event_type=EventType.SESSION_START,
            session_id="handler-test",
            data={"query": "Test query"}
        )

        # Call handler directly
        await _websocket_event_handler(event)

        # Client should receive broadcast
        mock_client.send_json.assert_called_once()
        sent_data = mock_client.send_json.call_args[0][0]
        assert sent_data["session_id"] == "handler-test"

        # Clean up
        ws_manager._clients.clear()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
