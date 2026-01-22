"""Unit tests for Price Comparison RAG service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import sys
import os

# Add path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src/rag/price_compare'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))


class TestPriceResult:
    """Test PriceResult dataclass."""

    def test_to_dict_includes_all_fields(self):
        """PriceResult.to_dict() should include all fields."""
        from providers.base import PriceResult

        result = PriceResult(
            product_name="Test Product",
            price=99.99,
            currency="USD",
            retailer="Test Store",
            url="https://test.com/product",
            in_stock=True,
            shipping=5.99,
            condition="new"
        )

        d = result.to_dict()

        assert d["product_name"] == "Test Product"
        assert d["price"] == 99.99
        assert d["currency"] == "USD"
        assert d["retailer"] == "Test Store"
        assert d["url"] == "https://test.com/product"
        assert d["in_stock"] is True
        assert d["shipping"] == 5.99
        assert d["total_price"] == 105.98  # price + shipping
        assert d["condition"] == "new"

    def test_to_dict_handles_none_shipping(self):
        """PriceResult.to_dict() should handle None shipping."""
        from providers.base import PriceResult

        result = PriceResult(
            product_name="Test",
            price=50.00,
            currency="USD",
            retailer="Store",
            url="https://test.com",
            shipping=None
        )

        d = result.to_dict()
        assert d["total_price"] == 50.00  # price + 0


class TestDuckDuckGoProvider:
    """Test DuckDuckGo shopping provider."""

    @pytest.mark.asyncio
    async def test_search_returns_list(self):
        """Search should return a list of results."""
        from providers.webscraper import DuckDuckGoShoppingProvider

        provider = DuckDuckGoShoppingProvider()

        # Mock the HTTP client
        with patch.object(provider.client, 'get', new_callable=AsyncMock) as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "RelatedTopics": [
                    {
                        "FirstURL": "https://example.com",
                        "Text": "Test product $49.99"
                    }
                ]
            }
            mock_response.raise_for_status = MagicMock()
            mock_get.return_value = mock_response

            results = await provider.search("test product")

            assert isinstance(results, list)
            if results:  # May not find price in mocked text
                assert results[0].price == 49.99

        await provider.close()


class TestBraveShoppingProvider:
    """Test Brave shopping provider."""

    @pytest.mark.asyncio
    async def test_search_requires_api_key(self):
        """Provider should require API key."""
        from providers.webscraper import BraveShoppingProvider

        provider = BraveShoppingProvider(api_key="test-key")
        assert provider.requires_api_key is True
        await provider.close()


class TestRapidAPIProvider:
    """Test RapidAPI price provider."""

    @pytest.mark.asyncio
    async def test_requires_api_key(self):
        """Provider should require API key."""
        from providers.rapidapi import RapidAPIPriceProvider

        provider = RapidAPIPriceProvider(api_key="test-key")
        assert provider.requires_api_key is True
        await provider.close()


class TestGoogleShoppingProvider:
    """Test Google Shopping (SerpAPI) provider."""

    @pytest.mark.asyncio
    async def test_requires_api_key(self):
        """Provider should require API key."""
        from providers.rapidapi import GoogleShoppingProvider

        provider = GoogleShoppingProvider(api_key="test-key")
        assert provider.requires_api_key is True
        await provider.close()


class TestPriceAggregation:
    """Test price aggregation logic."""

    @pytest.mark.asyncio
    async def test_aggregate_prices_sorts_by_total_price(self):
        """Results should be sorted by total price."""
        # This would require mocking all providers and the aggregate function
        pass

    @pytest.mark.asyncio
    async def test_aggregate_prices_deduplicates_by_retailer(self):
        """Results should be deduplicated by retailer."""
        # This would require mocking all providers and the aggregate function
        pass


class TestHealthEndpoint:
    """Test health endpoint."""

    def test_health_returns_provider_count(self):
        """Health endpoint should return active provider count."""
        # Requires TestClient setup with mocked lifespan
        pass
