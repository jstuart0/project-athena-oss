"""Integration tests for orchestrator and gateway."""

import pytest
import httpx
import asyncio
import time

BASE_GATEWAY_URL = "http://localhost:8000"
BASE_ORCHESTRATOR_URL = "http://localhost:8001"

@pytest.mark.asyncio
async def test_health_endpoints():
    """Test both services are healthy."""
    async with httpx.AsyncClient() as client:
        # Gateway health
        gateway_health = await client.get(f"{BASE_GATEWAY_URL}/health")
        assert gateway_health.status_code == 200
        assert gateway_health.json()["service"] == "gateway"

        # Orchestrator health
        orch_health = await client.get(f"{BASE_ORCHESTRATOR_URL}/health")
        assert orch_health.status_code == 200
        assert orch_health.json()["service"] == "orchestrator"

@pytest.mark.asyncio
async def test_control_query_flow():
    """Test home control query through gateway to orchestrator."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_GATEWAY_URL}/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "user", "content": "Turn on the office lights"}
                ]
            },
            headers={"Authorization": "Bearer dummy-key"}
        )

        assert response.status_code == 200
        result = response.json()
        assert "choices" in result
        assert len(result["choices"]) > 0
        assert "turn" in result["choices"][0]["message"]["content"].lower() or \
               "control" in result["choices"][0]["message"]["content"].lower()

@pytest.mark.asyncio
async def test_weather_query_flow():
    """Test weather query through full stack."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_GATEWAY_URL}/v1/chat/completions",
            json={
                "model": "gpt-4",
                "messages": [
                    {"role": "user", "content": "What's the weather in Baltimore?"}
                ]
            },
            headers={"Authorization": "Bearer dummy-key"}
        )

        assert response.status_code == 200
        result = response.json()
        content = result["choices"][0]["message"]["content"].lower()

        # Should mention Baltimore or weather terms
        assert "baltimore" in content or "weather" in content or "temperature" in content

@pytest.mark.asyncio
async def test_direct_orchestrator_query():
    """Test direct orchestrator query."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_ORCHESTRATOR_URL}/query",
            json={
                "query": "What time is it?",
                "mode": "owner",
                "room": "office"
            }
        )

        assert response.status_code == 200
        result = response.json()
        assert "answer" in result
        assert "intent" in result
        assert "request_id" in result

@pytest.mark.asyncio
async def test_streaming_response():
    """Test streaming response from gateway."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{BASE_GATEWAY_URL}/v1/chat/completions",
            json={
                "model": "gpt-3.5-turbo",
                "messages": [
                    {"role": "user", "content": "Tell me a joke"}
                ],
                "stream": True
            },
            headers={"Authorization": "Bearer dummy-key"}
        )

        assert response.status_code == 200
        # Check it's an event stream
        content_type = response.headers.get("content-type", "")
        assert "text/event-stream" in content_type or "stream" in content_type

@pytest.mark.asyncio
async def test_latency_requirements():
    """Test that latency meets Phase 1 requirements."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Control query (target: ≤3.5s)
        start = time.time()
        response = await client.post(
            f"{BASE_ORCHESTRATOR_URL}/query",
            json={"query": "turn off bedroom lights"}
        )
        control_time = time.time() - start

        assert response.status_code == 200
        assert control_time <= 3.5, f"Control query took {control_time:.2f}s (target: ≤3.5s)"

        # Knowledge query (target: ≤5.5s)
        start = time.time()
        response = await client.post(
            f"{BASE_ORCHESTRATOR_URL}/query",
            json={"query": "what's the weather forecast for tomorrow?"}
        )
        knowledge_time = time.time() - start

        assert response.status_code == 200
        assert knowledge_time <= 5.5, f"Knowledge query took {knowledge_time:.2f}s (target: ≤5.5s)"

@pytest.mark.asyncio
async def test_gateway_model_list():
    """Test OpenAI-compatible models endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_GATEWAY_URL}/v1/models")
        assert response.status_code == 200
        result = response.json()
        assert "data" in result
        assert len(result["data"]) > 0
        assert any(m["id"] == "gpt-3.5-turbo" for m in result["data"])

@pytest.mark.asyncio
async def test_orchestrator_metrics():
    """Test Prometheus metrics endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_ORCHESTRATOR_URL}/metrics")
        assert response.status_code == 200
        assert "orchestrator_requests_total" in response.text

@pytest.mark.asyncio
async def test_gateway_metrics():
    """Test Prometheus metrics endpoint."""
    async with httpx.AsyncClient() as client:
        response = await client.get(f"{BASE_GATEWAY_URL}/metrics")
        assert response.status_code == 200
        assert "gateway_requests_total" in response.text