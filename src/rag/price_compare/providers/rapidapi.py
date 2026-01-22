"""RapidAPI Price Comparison provider (free tier)."""

import httpx
from typing import List, Optional
import structlog

from .base import PriceProvider, PriceResult

logger = structlog.get_logger()


class RapidAPIPriceProvider(PriceProvider):
    """
    RapidAPI Price Comparison provider.

    Uses the free tier which includes Amazon, eBay, Walmart, Target.
    Free tier: ~100 requests/month
    """

    name = "rapidapi"
    requires_api_key = True

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://price-comparison1.p.rapidapi.com"
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={
                "X-RapidAPI-Key": api_key,
                "X-RapidAPI-Host": "price-comparison1.p.rapidapi.com"
            }
        )

    async def search(self, query: str, **kwargs) -> List[PriceResult]:
        """Search for product prices."""
        try:
            response = await self.client.get(
                f"{self.base_url}/search",
                params={"q": query, "country": "us"}
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("products", []):
                results.append(PriceResult(
                    product_name=item.get("title", query),
                    price=float(item.get("price", 0)),
                    currency="USD",
                    retailer=item.get("merchant", "Unknown"),
                    url=item.get("url", ""),
                    in_stock=item.get("in_stock", True),
                    shipping=item.get("shipping"),
                    image_url=item.get("image")
                ))

            return results

        except Exception as e:
            logger.warning("rapidapi_search_failed", error=str(e), query=query)
            return []

    async def close(self):
        await self.client.aclose()


class GoogleShoppingProvider(PriceProvider):
    """
    Google Shopping via SerpAPI (free tier: 100 searches/month).

    Provides comprehensive price comparison across many retailers.
    """

    name = "google-shopping"
    requires_api_key = True

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = "https://serpapi.com/search"
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def search(self, query: str, **kwargs) -> List[PriceResult]:
        """Search Google Shopping."""
        try:
            response = await self.client.get(
                self.base_url,
                params={
                    "engine": "google_shopping",
                    "q": query,
                    "api_key": self.api_key,
                    "gl": "us",
                    "hl": "en"
                }
            )
            response.raise_for_status()
            data = response.json()

            results = []
            for item in data.get("shopping_results", []):
                price_str = item.get("extracted_price", 0)
                try:
                    price = float(price_str) if price_str else 0
                except (ValueError, TypeError):
                    price = 0

                results.append(PriceResult(
                    product_name=item.get("title", query),
                    price=price,
                    currency="USD",
                    retailer=item.get("source", "Unknown"),
                    url=item.get("link", ""),
                    in_stock=True,
                    image_url=item.get("thumbnail")
                ))

            return results

        except Exception as e:
            logger.warning("google_shopping_search_failed", error=str(e), query=query)
            return []

    async def close(self):
        await self.client.aclose()
