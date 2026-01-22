"""
SearXNG metasearch provider.

Aggregates results from multiple search engines via self-hosted SearXNG instance.
"""

import httpx
from typing import List, Optional
from urllib.parse import quote_plus

from .base import SearchProvider, SearchResult


class SearXNGProvider(SearchProvider):
    """
    SearXNG metasearch engine provider.

    Advantages:
    - Aggregates multiple search engines (DuckDuckGo, Startpage, Bing, etc.)
    - Privacy-focused (no tracking)
    - Self-hosted (no API limits)
    - Comprehensive coverage

    Configuration:
    - No API key required (self-hosted instance)
    - Internal URL: http://searxng.athena-admin.svc.cluster.local:8080
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        """
        Initialize SearXNG provider.

        Args:
            base_url: SearXNG instance URL (defaults to internal cluster service)
            api_key: Not used (SearXNG doesn't require API keys)
        """
        super().__init__(api_key=None)
        self.base_url = base_url or "http://searxng.athena-admin.svc.cluster.local:8080"
        self.client = httpx.AsyncClient(
            timeout=10.0,
            headers={
                "User-Agent": "Mozilla/5.0 (Athena/1.0)"
            }
        )

    @property
    def name(self) -> str:
        return "searxng"

    async def search(
        self,
        query: str,
        location: Optional[str] = None,
        limit: int = 5,
        **kwargs
    ) -> List[SearchResult]:
        """
        Search using SearXNG metasearch engine.

        Args:
            query: Search query
            location: Not used by SearXNG (ignored)
            limit: Maximum number of results (default 5)
            **kwargs: Additional parameters (ignored)

        Returns:
            List of SearchResult objects
        """
        try:
            self.logger.info(f"SearXNG search started: {query}")

            # SearXNG JSON API
            url = f"{self.base_url}/search?q={quote_plus(query)}&format=json&pageno=1"

            response = await self.client.get(url)
            response.raise_for_status()
            data = response.json()

            results = []

            # Parse SearXNG results
            for item in data.get("results", [])[:limit]:
                # Calculate confidence based on SearXNG score and engine
                base_score = item.get("score", 0.7)

                # Boost confidence for results from multiple engines
                engines = item.get("engines", [])
                multi_engine_boost = min(0.1 * (len(engines) - 1), 0.2) if len(engines) > 1 else 0.0

                confidence = min(1.0, base_score + multi_engine_boost)

                result = self.normalize_result(
                    title=item.get("title", ""),
                    snippet=item.get("content", ""),
                    url=item.get("url", ""),
                    confidence=confidence,
                    metadata={
                        "engines": engines,
                        "category": item.get("category", "general"),
                        "published_date": item.get("publishedDate")
                    }
                )
                results.append(result)

            self.logger.info(f"SearXNG search completed: {len(results)} results")

            return results

        except httpx.HTTPStatusError as e:
            self.logger.error(f"SearXNG HTTP error: {e}")
            raise
        except httpx.RequestError as e:
            self.logger.error(f"SearXNG request error: {e}")
            raise
        except Exception as e:
            self.logger.error(f"SearXNG search failed: {e}")
            raise

    async def close(self):
        """Close the HTTP client."""
        await self.client.aclose()
