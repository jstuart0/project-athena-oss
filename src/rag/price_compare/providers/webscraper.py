"""Web scraping price provider (free, no API key required)."""

import re
from typing import List, Optional
import httpx
import structlog

from .base import PriceProvider, PriceResult

logger = structlog.get_logger()

# Conditional imports
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False
    logger.warning("beautifulsoup4 not installed - web scraping limited")


class DuckDuckGoShoppingProvider(PriceProvider):
    """
    DuckDuckGo search for shopping results.

    Free, no API key required. Scrapes shopping results from DDG.
    """

    name = "duckduckgo"
    requires_api_key = False

    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            follow_redirects=True
        )

    async def search(self, query: str, **kwargs) -> List[PriceResult]:
        """Search DuckDuckGo for product prices."""
        try:
            # Use DuckDuckGo's instant answer API
            response = await self.client.get(
                "https://api.duckduckgo.com/",
                params={
                    "q": f"{query} price buy",
                    "format": "json",
                    "no_redirect": "1"
                }
            )
            response.raise_for_status()
            data = response.json()

            results = []

            # Extract results from related topics
            for topic in data.get("RelatedTopics", []):
                if isinstance(topic, dict) and "FirstURL" in topic:
                    text = topic.get("Text", "")
                    url = topic.get("FirstURL", "")

                    # Try to extract price from text
                    price_match = re.search(r'\$(\d+(?:\.\d{2})?)', text)
                    if price_match:
                        results.append(PriceResult(
                            product_name=query,
                            price=float(price_match.group(1)),
                            currency="USD",
                            retailer="DuckDuckGo Result",
                            url=url
                        ))

            return results

        except Exception as e:
            logger.warning("duckduckgo_search_failed", error=str(e), query=query)
            return []

    async def close(self):
        await self.client.aclose()


class BraveShoppingProvider(PriceProvider):
    """
    Brave Search for shopping results.

    Uses existing Brave API key from websearch service.
    Free tier: 2000 queries/month.
    """

    name = "brave-shopping"
    requires_api_key = True

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key
            }
        )

    async def search(self, query: str, **kwargs) -> List[PriceResult]:
        """Search Brave for product prices."""
        try:
            response = await self.client.get(
                "https://api.search.brave.com/res/v1/web/search",
                params={
                    "q": f"{query} price buy",
                    "count": 10
                }
            )
            response.raise_for_status()
            data = response.json()

            results = []

            for item in data.get("web", {}).get("results", []):
                # Try to extract price from description
                desc = item.get("description", "")
                price_match = re.search(r'\$(\d+(?:,\d{3})*(?:\.\d{2})?)', desc)

                if price_match:
                    price_str = price_match.group(1).replace(",", "")
                    results.append(PriceResult(
                        product_name=item.get("title", query),
                        price=float(price_str),
                        currency="USD",
                        retailer=item.get("meta_url", {}).get("hostname", "Unknown"),
                        url=item.get("url", "")
                    ))

            return results

        except Exception as e:
            logger.warning("brave_shopping_search_failed", error=str(e), query=query)
            return []

    async def close(self):
        await self.client.aclose()
