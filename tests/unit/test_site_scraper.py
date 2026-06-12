"""Unit tests for Site Scraper RAG service."""

import importlib.util
import os
import sys

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Load the site_scraper main module via importlib so that the bare name 'main'
# in sys.modules (which admin/backend/tests also populate with a different
# main.py) never interferes.  All tests reference _scraper_main directly.
# ---------------------------------------------------------------------------
_SCRAPER_MAIN_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), '../../src/rag/site_scraper/main.py')
)
# Also ensure src/ is on the path so the scraper's own imports (shared.*) resolve.
_SRC_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), '../../src'))
if _SRC_PATH not in sys.path:
    sys.path.insert(0, _SRC_PATH)

def _load_scraper_main():
    spec = importlib.util.spec_from_file_location('_scraper_site_scraper_main', _SCRAPER_MAIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

_scraper_main = _load_scraper_main()


class TestIsUrlAllowed:
    """Test URL validation logic."""

    def test_owner_mode_allows_any_url(self):
        """Owner mode should allow any URL by default."""
        is_url_allowed = _scraper_main.is_url_allowed

        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': [],
            'blocked_domains': []
        }):
            allowed, reason = is_url_allowed("https://example.com/page", "owner")
            assert allowed is True

    def test_guest_mode_restricts_by_default(self):
        """Guest mode should restrict URLs by default."""
        is_url_allowed = _scraper_main.is_url_allowed

        with patch.object(_scraper_main, 'config', {
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
        is_url_allowed = _scraper_main.is_url_allowed

        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': ['whitelisted.com'],
            'blocked_domains': []
        }):
            allowed, reason = is_url_allowed("https://whitelisted.com/page", "guest")
            assert allowed is True

    def test_blocked_domains_apply_to_all_modes(self):
        """Blocked domains should block all users."""
        is_url_allowed = _scraper_main.is_url_allowed

        with patch.object(_scraper_main, 'config', {
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
        is_url_allowed = _scraper_main.is_url_allowed

        with patch.object(_scraper_main, 'config', {
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

        app = _scraper_main.app

        # Use TestClient for sync testing of FastAPI
        with patch.object(_scraper_main, 'BRAVE_API_KEY', 'test-key'):
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


# ---------------------------------------------------------------------------
# ATHENA-59 Phase 0: exact-host/suffix domain matching (xander H-1 fix)
# ---------------------------------------------------------------------------

class TestDomainMatches:
    """Tests for _domain_matches helper (xander H-1 SSRF fix in sitescraper)."""

    def setup_method(self):
        self._fn = _scraper_main._domain_matches

    def test_exact_match(self):
        assert self._fn("evil.com", "evil.com") is True

    def test_subdomain_match(self):
        assert self._fn("sub.evil.com", "evil.com") is True

    def test_deep_subdomain_match(self):
        assert self._fn("a.b.evil.com", "evil.com") is True

    def test_suffix_not_domain_not_matched(self):
        # "notevil.com" should NOT match pattern "evil.com" (old substring bug)
        assert self._fn("notevil.com", "evil.com") is False

    def test_attacker_suffix_bypass_blocked(self):
        # Classic bypass: attacker hosts "evil.com.attacker.net"
        assert self._fn("evil.com.attacker.net", "evil.com") is False

    def test_case_insensitive(self):
        assert self._fn("EVIL.COM", "evil.com") is True
        assert self._fn("evil.com", "EVIL.COM") is True

    def test_empty_pattern_matches_nothing(self):
        # empty pattern after strip — should not crash, and should not match
        # any real domain unless domain is also empty
        assert self._fn("evil.com", "") is False

    def test_unrelated_domain_not_matched(self):
        assert self._fn("example.com", "evil.com") is False


class TestIsUrlAllowedWithDomainMatching:
    """Integration tests for is_url_allowed with exact-host/suffix matching."""

    def test_blocked_exact_domain(self):
        is_url_allowed = _scraper_main.is_url_allowed
        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': True,
            'allowed_domains': [],
            'blocked_domains': ['blocked.com'],
        }):
            with patch.object(_scraper_main, 'get_config') as mock_cfg:
                mock_cfg.return_value.sitescraper_allowed_private_hosts = ''
                with patch('shared.url_safety.validate_url_not_private') as mock_val:
                    mock_val.return_value = MagicMock(allowed=True)
                    allowed, reason = is_url_allowed("https://blocked.com/page", "owner")
                    assert allowed is False

    def test_blocked_subdomain(self):
        is_url_allowed = _scraper_main.is_url_allowed
        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': True,
            'allowed_domains': [],
            'blocked_domains': ['blocked.com'],
        }):
            with patch.object(_scraper_main, 'get_config') as mock_cfg:
                mock_cfg.return_value.sitescraper_allowed_private_hosts = ''
                with patch('shared.url_safety.validate_url_not_private') as mock_val:
                    mock_val.return_value = MagicMock(allowed=True)
                    allowed, reason = is_url_allowed("https://sub.blocked.com/page", "owner")
                    assert allowed is False

    def test_suffix_bypass_not_blocked(self):
        """notblocked.com should NOT be blocked by pattern 'blocked.com'."""
        is_url_allowed = _scraper_main.is_url_allowed
        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': True,
            'allowed_domains': [],
            'blocked_domains': ['blocked.com'],
        }):
            with patch.object(_scraper_main, 'get_config') as mock_cfg:
                mock_cfg.return_value.sitescraper_allowed_private_hosts = ''
                with patch('shared.url_safety.validate_url_not_private') as mock_val:
                    mock_val.return_value = MagicMock(allowed=True)
                    allowed, reason = is_url_allowed("https://notblocked.com/page", "owner")
                    assert allowed is True

    def test_guest_allowed_domain_exact(self):
        is_url_allowed = _scraper_main.is_url_allowed
        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': ['whitelisted.com'],
            'blocked_domains': [],
        }):
            with patch.object(_scraper_main, 'get_config') as mock_cfg:
                mock_cfg.return_value.sitescraper_allowed_private_hosts = ''
                with patch('shared.url_safety.validate_url_not_private') as mock_val:
                    mock_val.return_value = MagicMock(allowed=True)
                    allowed, reason = is_url_allowed("https://whitelisted.com/page", "guest")
                    assert allowed is True

    def test_guest_allowed_domain_subdomain(self):
        is_url_allowed = _scraper_main.is_url_allowed
        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': ['whitelisted.com'],
            'blocked_domains': [],
        }):
            with patch.object(_scraper_main, 'get_config') as mock_cfg:
                mock_cfg.return_value.sitescraper_allowed_private_hosts = ''
                with patch('shared.url_safety.validate_url_not_private') as mock_val:
                    mock_val.return_value = MagicMock(allowed=True)
                    allowed, reason = is_url_allowed("https://sub.whitelisted.com/page", "guest")
                    assert allowed is True

    def test_guest_suffix_bypass_not_allowed(self):
        """notwhitelisted.com must not pass when allowlist only has 'whitelisted.com'."""
        is_url_allowed = _scraper_main.is_url_allowed
        with patch.object(_scraper_main, 'config', {
            'owner_mode_any_url': True,
            'guest_mode_any_url': False,
            'allowed_domains': ['whitelisted.com'],
            'blocked_domains': [],
        }):
            with patch.object(_scraper_main, 'get_config') as mock_cfg:
                mock_cfg.return_value.sitescraper_allowed_private_hosts = ''
                with patch('shared.url_safety.validate_url_not_private') as mock_val:
                    mock_val.return_value = MagicMock(allowed=True)
                    allowed, reason = is_url_allowed("https://notwhitelisted.com/page", "guest")
                    assert allowed is False
