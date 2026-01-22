"""
Brave Search API provider.

General-purpose web search using Brave Search API.
Free tier: 2,000 queries/month
"""

from typing import List, Optional, Dict, Any
import logging
import httpx

from .base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)


class BraveSearchProvider(SearchProvider):
    """
    Brave Search API provider for general web search.

    Features:
    - Independent search index (not Google/Bing)
    - Privacy-focused
    - 2,000 free queries/month
    - Fast response times
    - Rich result metadata

    API Documentation: https://brave.com/search/api/
    """

    BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, timeout: float = 10.0):
        """
        Initialize Brave Search provider.

        Args:
            api_key: Brave Search API key
            timeout: Request timeout in seconds
        """
        super().__init__()
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=timeout)

    @property
    def name(self) -> str:
        """Provider name."""
        return "brave"

    async def search(
        self,
        query: str,
        location: Optional[str] = None,
        limit: int = 5,
        **kwargs
    ) -> List[SearchResult]:
        """
        Execute search via Brave Search API.

        Args:
            query: Search query string
            location: Geographic location (optional, not heavily used by Brave)
            limit: Maximum number of results to return
            **kwargs: Additional search parameters

        Returns:
            List of SearchResult objects
        """
        try:
            logger.info(f"Brave Search: Querying for '{query}'")

            # Build request headers
            headers = {
                "X-Subscription-Token": self.api_key,
                "Accept": "application/json",
                "Accept-Encoding": "gzip"
            }

            # Build request parameters
            params: Dict[str, Any] = {
                "q": query,
                "count": min(limit, 20),  # Brave max is 20
                "search_lang": "en",
                "country": "US",
                "safesearch": "moderate",
                "freshness": kwargs.get("freshness"),  # "pd" (past day), "pw" (past week), "pm" (past month)
                "text_decorations": False,  # Disable text highlighting
                "spellcheck": True
            }

            # Remove None values
            params = {k: v for k, v in params.items() if v is not None}

            # Execute search
            response = await self.client.get(
                self.BASE_URL,
                headers=headers,
                params=params
            )
            response.raise_for_status()
            data = response.json()

            # Parse results
            results = []

            # Web results
            web_results = data.get("web", {}).get("results", [])
            logger.info(f"Brave Search: Found {len(web_results)} web results")

            for item in web_results[:limit]:
                result = SearchResult(
                    source="brave",
                    title=item.get("title", ""),
                    snippet=item.get("description", ""),
                    url=item.get("url"),
                    confidence=0.85,  # Brave is generally reliable
                    metadata={
                        "age": item.get("age"),
                        "language": item.get("language"),
                        "family_friendly": item.get("family_friendly", True),
                        "page_age": item.get("page_age"),
                        "page_fetched": item.get("page_fetched")
                    }
                )
                results.append(result)

            # FAQ results (high confidence for factual queries)
            faq_results = data.get("faq", {}).get("results", [])
            if faq_results:
                logger.info(f"Brave Search: Found {len(faq_results)} FAQ results")

                for faq in faq_results[:2]:  # Limit to 2 FAQs
                    result = SearchResult(
                        source="brave",
                        title=faq.get("question", ""),
                        snippet=faq.get("answer", ""),
                        url=faq.get("url"),
                        confidence=0.95,  # FAQs are high confidence
                        metadata={
                            "type": "faq",
                            "title": faq.get("title")
                        }
                    )
                    results.append(result)

            # News results (for time-sensitive queries)
            news_results = data.get("news", {}).get("results", [])
            if news_results:
                logger.info(f"Brave Search: Found {len(news_results)} news results")

                for news in news_results[:3]:  # Limit to 3 news items
                    result = SearchResult(
                        source="brave",
                        title=news.get("title", ""),
                        snippet=news.get("description", ""),
                        url=news.get("url"),
                        confidence=0.9,  # News is highly relevant
                        metadata={
                            "type": "news",
                            "age": news.get("age"),
                            "breaking": news.get("breaking", False),
                            "source": news.get("meta_url", {}).get("hostname")
                        }
                    )
                    results.append(result)

            logger.info(f"Brave Search: Returning {len(results)} total results for '{query}'")
            return results

        except httpx.HTTPStatusError as e:
            logger.error(f"Brave Search HTTP error: {e.response.status_code} - {e.response.text}")
            return []

        except httpx.RequestError as e:
            logger.error(f"Brave Search request error: {str(e)}")
            return []

        except Exception as e:
            logger.error(f"Brave Search unexpected error: {str(e)}", exc_info=True)
            return []

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()
