"""
DuckDuckGo search provider.

Uses DuckDuckGo Instant Answer API (no API key required).
"""

import httpx
from typing import List, Optional
from urllib.parse import quote_plus

from .base import SearchProvider, SearchResult


class DuckDuckGoProvider(SearchProvider):
    """
    DuckDuckGo Instant Answer API provider.

    Advantages:
    - Free (no API key required)
    - Fast responses
    - Good for general knowledge queries

    Limitations:
    - Limited results for events/local information
    - No advanced filtering
    """

    def __init__(self, api_key: Optional[str] = None):
        """Initialize DuckDuckGo provider (no API key needed)."""
        super().__init__(api_key=None)  # DuckDuckGo doesn't require API key
        self.client = httpx.AsyncClient(
            timeout=10.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            }
        )

    @property
    def name(self) -> str:
        return "duckduckgo"

    async def search(
        self,
        query: str,
        location: Optional[str] = None,
        limit: int = 5,
        **kwargs
    ) -> List[SearchResult]:
        """
        Search using DuckDuckGo Instant Answer API.

        Args:
            query: Search query
            location: Not used by DuckDuckGo API (ignored)
            limit: Maximum number of results (default 5)
            **kwargs: Additional parameters (ignored)

        Returns:
            List of SearchResult objects
        """
        try:
            self.logger.info(f"DuckDuckGo search started: {query}")

            # DuckDuckGo instant answer API
            url = f"https://api.duckduckgo.com/?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"

            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()

            results = []

            # Abstract (instant answer) - highest quality
            if data.get("Abstract"):
                result = self.normalize_result(
                    title=data.get("Heading", "Instant Answer"),
                    snippet=data.get("Abstract"),
                    url=data.get("AbstractURL", ""),
                    confidence=0.9,  # High confidence for instant answers
                    metadata={
                        "abstract_source": data.get("AbstractSource", ""),
                        "image": data.get("Image", "")
                    }
                )
                results.append(result)

            # Related topics
            for topic in data.get("RelatedTopics", [])[:limit]:
                if isinstance(topic, dict) and "Text" in topic:
                    text = topic.get("Text", "")
                    result = self.normalize_result(
                        title=text[:100],  # First 100 chars as title
                        snippet=text,
                        url=topic.get("FirstURL", ""),
                        confidence=0.7,  # Medium confidence for related topics
                        metadata={"icon": topic.get("Icon", {})}
                    )
                    results.append(result)

            self.logger.info(f"DuckDuckGo search completed: {len(results)} results")

            return results[:limit]

        except httpx.HTTPStatusError as e:
            self.logger.error(f"DuckDuckGo HTTP error: {e}")
            raise
        except httpx.RequestError as e:
            self.logger.error(f"DuckDuckGo request error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"DuckDuckGo search failed: {e}")
            raise

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
