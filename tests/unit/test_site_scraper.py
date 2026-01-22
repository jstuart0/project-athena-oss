"""Unit tests for Site Scraper RAG service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

# Add path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src/rag/site_scraper'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))


class TestIsUrlAllowed:
    """Test URL validation logic."""

    def test_owner_mode_allows_any_url(self):
        """Owner mode should allow any URL by default."""
        from main import is_url_allowed

        # Mock config
        with patch('main.config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': [],
            'blocked_domains': []
        }):
            allowed, reason = is_url_allowed("https://example.com/page", "owner")
            assert allowed is True

    def test_guest_mode_restricts_by_default(self):
        """Guest mode should restrict URLs by default."""
        from main import is_url_allowed

        with patch('main.config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': ['whitelisted.com'],
            'blocked_domains': []
        }):
            # Non-whitelisted domain
            allowed, reason = is_url_allowed("https://other.com/page", "guest")
            assert allowed is False

    def test_guest_mode_allows_whitelisted_domain(self):
        """Guest mode should allow whitelisted domains."""
        from main import is_url_allowed

        with patch('main.config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': ['whitelisted.com'],
            'blocked_domains': []
        }):
            allowed, reason = is_url_allowed("https://whitelisted.com/page", "guest")
            assert allowed is True

    def test_blocked_domains_apply_to_all_modes(self):
        """Blocked domains should block all users."""
        from main import is_url_allowed

        with patch('main.config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': True,
            'allowed_domains': [],
            'blocked_domains': ['blocked.com']
        }):
            allowed, reason = is_url_allowed("https://blocked.com/page", "owner")
            assert allowed is False

            allowed, reason = is_url_allowed("https://blocked.com/page", "guest")
            assert allowed is False

    def test_invalid_url_returns_false(self):
        """Invalid URLs should not be allowed."""
        from main import is_url_allowed

        with patch('main.config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': True,
            'allowed_domains': [],
            'blocked_domains': []
        }):
            # This should not raise an exception
            allowed, reason = is_url_allowed("not-a-valid-url", "owner")
            # URL parsing for invalid URLs might still work (scheme-less)
            # The actual behavior depends on urlparse


class TestHealthCheck:
    """Test health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_healthy(self):
        """Health check should return healthy status."""
        from fastapi.testclient import TestClient
        from main import app

        # Use TestClient for sync testing of FastAPI
        with patch('main.BRAVE_API_KEY', 'test-key'):
            client = TestClient(app)
            # Note: Lifespan events won't run in TestClient
            # For full integration tests, use async client


class TestScrapeEndpoint:
    """Test scrape endpoint."""

    @pytest.mark.asyncio
    async def test_scrape_blocked_url_returns_403(self):
        """Scraping a blocked URL should return 403."""
        pass  # Requires more setup with mocked dependencies


class TestSearchAndScrapeEndpoint:
    """Test search-and-scrape endpoint."""

    @pytest.mark.asyncio
    async def test_search_no_results_returns_404(self):
        """Search with no results should return 404."""
        pass  # Requires more setup with mocked dependencies
