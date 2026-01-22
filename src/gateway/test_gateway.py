"""Test suite for gateway service."""

import pytest
import httpx
import asyncio
from unittest.mock import AsyncMock, patch

from main import app, is_athena_query, ChatMessage

@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    return TestClient(app)

def test_health_endpoint(client):
    """Test health check endpoint."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "gateway"

def test_is_athena_query():
    """Test Athena query detection."""
    # Should route to Athena
    athena_messages = [
        ChatMessage(role="user", content="Turn on the office lights"),
        ChatMessage(role="user", content="What's the weather in Baltimore?"),
        ChatMessage(role="user", content="Any delays at BWI airport?"),
        ChatMessage(role="user", content="When is the next Ravens game?"),
    ]

    for msg in athena_messages:
        assert is_athena_query([msg]) == True

    # Should not route to Athena
    general_messages = [
        ChatMessage(role="user", content="What is quantum physics?"),
        ChatMessage(role="user", content="Write a poem about nature"),
        ChatMessage(role="user", content="Explain machine learning"),
    ]

    for msg in general_messages:
        assert is_athena_query([msg]) == False

@pytest.mark.asyncio
async def test_chat_completion_routing():
    """Test request routing logic."""
    # This would need mocking of orchestrator_client and ollama_client
    pass  # Implementation details omitted for brevity