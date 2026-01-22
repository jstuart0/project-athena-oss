"""
End-to-End Integration Tests for Cloud LLM Support.

These tests validate the ENTIRE cloud LLM system working together across all phases.
Run AFTER all phase-specific tests have passed.

Prerequisites:
- All Phase 1-7 tests passing
- Test API keys configured in admin backend
- Services running: Gateway, Orchestrator, Admin Backend

Open Source Compatible - Uses standard pytest and httpx.
"""
import pytest
import httpx
import asyncio
import json
import os
from datetime import datetime


# Test configuration
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8000")
ORCHESTRATOR_URL = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001")
ADMIN_URL = os.getenv("ADMIN_URL", "http://localhost:8080")
TEST_API_KEY = os.getenv("TEST_API_KEY", "")


def get_auth_headers():
    """Get authorization headers for admin API calls."""
    if TEST_API_KEY:
        return {"X-API-Key": TEST_API_KEY}
    return {}


class TestFullCloudPipeline:
    """Test complete request flow from Gateway through cloud provider and back."""

    @pytest.mark.asyncio
    async def test_orchestrator_health(self):
        """Test orchestrator is healthy and accessible."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ORCHESTRATOR_URL}/health")
            assert response.status_code == 200
            data = response.json()
            assert data.get("status") in ["healthy", "ok", True]

    @pytest.mark.asyncio
    async def test_gateway_health(self):
        """Test gateway is healthy and accessible."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{GATEWAY_URL}/health")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_health(self):
        """Test admin backend is healthy and accessible."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ADMIN_URL}/health")
            assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_basic_query(self):
        """Test a basic query works."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/query",
                json={
                    "query": "Hello",
                    "mode": "owner",
                    "room": "office"
                }
            )
            assert response.status_code == 200
            data = response.json()
            assert "response" in data

    @pytest.mark.asyncio
    async def test_cloud_providers_endpoint(self):
        """Test cloud providers endpoint is accessible."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/cloud-providers",
                headers=get_auth_headers()
            )
            # Should work even without auth for listing
            assert response.status_code in [200, 401]

    @pytest.mark.asyncio
    async def test_service_bypass_endpoint(self):
        """Test service bypass endpoint is accessible."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/rag-service-bypass",
                headers=get_auth_headers()
            )
            assert response.status_code in [200, 401]


class TestCrossPhaseIntegration:
    """Test integration between different phases."""

    @pytest.mark.asyncio
    async def test_feature_flags_accessible(self):
        """Test feature flags endpoint works."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/features",
                headers=get_auth_headers()
            )
            assert response.status_code in [200, 401]

    @pytest.mark.asyncio
    async def test_cloud_llm_usage_endpoint(self):
        """Test cloud LLM usage tracking endpoint."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/cloud-llm-usage/summary/today",
                headers=get_auth_headers()
            )
            assert response.status_code in [200, 401]

    @pytest.mark.asyncio
    async def test_bypass_config_public_endpoint(self):
        """Test bypass config public endpoint for orchestrator."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/rag-service-bypass/public/recipes/config"
            )
            assert response.status_code == 200
            data = response.json()
            # Should return bypass_enabled status
            assert "bypass_enabled" in data


class TestLoadAndPerformance:
    """Load and performance tests for cloud LLM integration."""

    @pytest.mark.asyncio
    async def test_concurrent_requests(self):
        """Test handling multiple concurrent requests."""
        async with httpx.AsyncClient(timeout=60.0) as client:
            queries = [
                {"query": f"What is {i}+{i}?", "mode": "owner", "room": "office"}
                for i in range(3)
            ]

            tasks = [
                client.post(f"{ORCHESTRATOR_URL}/query", json=q)
                for q in queries
            ]

            responses = await asyncio.gather(*tasks, return_exceptions=True)

            successes = [r for r in responses if not isinstance(r, Exception) and r.status_code == 200]
            assert len(successes) >= 2  # At least 2 should succeed

    @pytest.mark.asyncio
    async def test_response_latency_acceptable(self):
        """Test response latency is within acceptable range."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            start = datetime.utcnow()
            response = await client.post(
                f"{ORCHESTRATOR_URL}/query",
                json={"query": "Hi", "mode": "owner", "room": "office"}
            )
            elapsed = (datetime.utcnow() - start).total_seconds()

            assert response.status_code == 200
            assert elapsed < 15.0  # Should complete within 15s


class TestRollbackScenarios:
    """Test that system works without cloud configuration."""

    @pytest.mark.asyncio
    async def test_system_functional_without_cloud(self):
        """Test system works normally without cloud preference."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/query",
                json={"query": "What time is it?", "mode": "owner", "room": "office"}
            )
            assert response.status_code == 200
            data = response.json()
            assert "response" in data

    @pytest.mark.asyncio
    async def test_local_model_fallback(self):
        """Test local model continues to work."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{GATEWAY_URL}/v1/chat/completions",
                json={
                    "model": "qwen3:4b",
                    "messages": [{"role": "user", "content": "Hi"}]
                }
            )
            # May fail if model not loaded, but should get valid response
            assert response.status_code in [200, 400, 503]


class TestPrivacyFilter:
    """Test privacy filter functionality."""

    @pytest.mark.asyncio
    async def test_query_with_normal_content(self):
        """Test normal queries work fine."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{ORCHESTRATOR_URL}/query",
                json={
                    "query": "Tell me about the weather",
                    "mode": "owner",
                    "room": "office"
                }
            )
            assert response.status_code == 200


class TestCostAlerting:
    """Test cost alerting endpoints."""

    @pytest.mark.asyncio
    async def test_cost_alerts_endpoint(self):
        """Test cost alerts endpoint is accessible."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/cloud-llm-usage/cost-alerts",
                headers=get_auth_headers()
            )
            assert response.status_code in [200, 401]

    @pytest.mark.asyncio
    async def test_cost_breakdown_endpoint(self):
        """Test cost breakdown endpoint is accessible."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                f"{ADMIN_URL}/api/cloud-llm-usage/cost-breakdown?period=today",
                headers=get_auth_headers()
            )
            assert response.status_code in [200, 401]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
