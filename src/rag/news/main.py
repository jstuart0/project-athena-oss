"""News RAG Service - Dual API Integration with Parallel Processing

Provides current news articles, headlines, and news search capabilities.
Uses two news APIs in parallel for better coverage and reliability:
- NewsAPI.ai (Event Registry)
- Webz.io NewsAPI Lite

API Endpoints:
- GET /health - Health check
- GET /news/headlines - Get top headlines
- GET /news/search - Search news articles (queries both APIs in parallel)
- GET /news/sources - Get news sources
"""

import asyncio
import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from datetime import datetime

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import configure_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
configure_logging(service_name="news-rag")
logger = structlog.get_logger()

SERVICE_NAME = "news"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8016"))
# API Configuration (will be loaded from database)
NEWSAPIAI_KEY = None
NEWSAPIAI_URL = "https://eventregistry.org/api/v1/article/getArticles"

WEBZ_KEY = None
WEBZ_URL = "https://api.webz.io/newsApiLite"

# Global clients (will be initialized in lifespan)
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifespan - initialize and cleanup resources.

    Setup:
    - Admin client for fetching API keys
    - HTTP client for API calls
    - Load both news API keys from database
    - Logging configuration

    Cleanup:
    - Close HTTP client and admin client connections
    """
    global http_client, admin_client, NEWSAPIAI_KEY, WEBZ_KEY

    logger.info("news_service.startup", msg="Initializing News RAG service")

    # Initialize admin client for configuration management
    admin_client = get_admin_client()

    # Initialize HTTP client
    # OPTIMIZATION: Shorter per-API timeout (5s) so slow APIs don't delay response
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(5.0))

    # Fetch API keys from database
    try:
        # Fetch NewsAPI.ai key
        newsapiai_config = await admin_client.get_external_api_key("api-newsapiai")
        if newsapiai_config and newsapiai_config.get("api_key"):
            NEWSAPIAI_KEY = newsapiai_config["api_key"]
            logger.info("api_key_loaded", service="newsapiai")
        else:
            logger.warning("api_key_missing", service="newsapiai")

        # Fetch Webz.io key
        webz_config = await admin_client.get_external_api_key("api-webz")
        if webz_config and webz_config.get("api_key"):
            WEBZ_KEY = webz_config["api_key"]
            logger.info("api_key_loaded", service="webz")
        else:
            logger.warning("api_key_missing", service="webz")

    except Exception as e:
        logger.error("api_key_load_failed", error=str(e))

    # Validate at least one API key is available
    if not NEWSAPIAI_KEY and not WEBZ_KEY:
        logger.error(
            "news_service.config.no_keys",
            msg="No news API keys configured - service will return errors"
        )
    else:
        apis_available = []
        if NEWSAPIAI_KEY:
            apis_available.append("newsapiai")
        if WEBZ_KEY:
            apis_available.append("webz")
        logger.info("news_service.apis_available", apis=apis_available)

    logger.info("news_service.startup.complete", msg="News RAG service ready")

    yield  # Application runs here

    # Cleanup
    logger.info("news_service.shutdown", msg="Shutting down News RAG service")
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

# Create FastAPI app
app = FastAPI(
    title="News RAG Service",
    description="Dual-API news service with parallel processing",
    version="2.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

async def search_newsapiai(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Search news via NewsAPI.ai (Event Registry).

    Args:
        query: Search query
        max_results: Maximum number of articles to return

    Returns:
        List of articles from NewsAPI.ai

    Raises:
        httpx.HTTPError: If API request fails
    """
    if not NEWSAPIAI_KEY:
        logger.warning("newsapiai_key_missing")
        return []

    try:
        # EventRegistry API uses POST with JSON payload
        payload = {
            "action": "getArticles",
            "keyword": query,
            "articlesPage": 1,
            "articlesCount": max_results,
            "articlesSortBy": "date",
            "articlesSortByAsc": False,
            "apiKey": NEWSAPIAI_KEY,
            "resultType": "articles",
            "includeArticleImage": True,
            "includeArticleCategories": True
        }

        logger.info("newsapiai.search", query=query, max_results=max_results)

        response = await http_client.post(NEWSAPIAI_URL, json=payload)
        response.raise_for_status()

        data = response.json()

        # Transform EventRegistry response to standard format
        articles = []
        article_results = data.get("articles", {}).get("results", [])

        for article in article_results[:max_results]:
            articles.append({
                "title": article.get("title"),
                "description": article.get("body", "")[:200],  # Truncate to 200 chars
                "url": article.get("url"),
                "source": article.get("source", {}).get("title", "Unknown"),
                "published_at": article.get("dateTime"),
                "author": None,  # EventRegistry doesn't provide author
                "content": article.get("body"),
                "api_source": "newsapiai"
            })

        logger.info("newsapiai.success", articles_count=len(articles))
        return articles

    except Exception as e:
        logger.error("newsapiai.error", error=str(e), exc_info=True)
        return []

async def search_webz(query: str, max_results: int = 10) -> List[Dict[str, Any]]:
    """
    Search news via Webz.io NewsAPI Lite.

    Args:
        query: Search query
        max_results: Maximum number of articles to return

    Returns:
        List of articles from Webz.io

    Raises:
        httpx.HTTPError: If API request fails
    """
    if not WEBZ_KEY:
        logger.warning("webz_key_missing")
        return []

    try:
        # Webz.io uses GET with query parameters
        params = {
            "token": WEBZ_KEY,
            "q": query,
            "size": max_results,
            "sort": "crawled:desc"  # Most recent first
        }

        logger.info("webz.search", query=query, max_results=max_results)

        response = await http_client.get(WEBZ_URL, params=params)
        response.raise_for_status()

        data = response.json()

        # Transform Webz.io response to standard format
        articles = []
        posts = data.get("posts", [])

        for post in posts[:max_results]:
            articles.append({
                "title": post.get("title"),
                "description": post.get("text", "")[:200],  # Truncate to 200 chars
                "url": post.get("url"),
                "source": post.get("thread", {}).get("site", "Unknown"),
                "published_at": post.get("published"),
                "author": post.get("author"),
                "content": post.get("text"),
                "api_source": "webz"
            })

        logger.info("webz.success", articles_count=len(articles))
        return articles

    except Exception as e:
        logger.error("webz.error", error=str(e), exc_info=True)
        return []

def deduplicate_articles(articles: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate articles based on title similarity and URL.

    Args:
        articles: List of articles from multiple sources

    Returns:
        Deduplicated list of articles
    """
    seen_urls = set()
    seen_titles = set()
    unique_articles = []

    for article in articles:
        url = article.get("url", "")
        title = article.get("title", "").lower().strip()

        # Skip if we've seen this URL or very similar title
        if url in seen_urls:
            continue
        if title in seen_titles:
            continue

        seen_urls.add(url)
        seen_titles.add(title)
        unique_articles.append(article)

    return unique_articles

@cached(ttl=900)  # OPTIMIZATION: Cache for 15 minutes (news doesn't need second-level freshness)
async def search_news(
    query: str,
    language: str = "en",
    sort_by: str = "publishedAt",
    max_results: int = 10
) -> Dict[str, Any]:
    """
    Search news articles via both APIs in parallel.

    Queries NewsAPI.ai and Webz.io simultaneously, merges results,
    and deduplicates to provide comprehensive news coverage.

    Args:
        query: Search query
        language: Language code (e.g., "en", "es", "fr")
        sort_by: Sort order (relevancy, popularity, publishedAt)
        max_results: Maximum number of articles to return

    Returns:
        Dictionary containing merged articles from both APIs

    Raises:
        ValueError: If parameters are invalid
        HTTPException: If both APIs fail
    """
    if not query or len(query.strip()) == 0:
        raise ValueError("Query cannot be empty")

    if not NEWSAPIAI_KEY and not WEBZ_KEY:
        raise ValueError("No news API keys configured")

    logger.info(
        "news_service.search",
        query=query,
        language=language,
        sort_by=sort_by,
        max_results=max_results
    )

    # Query both APIs in parallel
    tasks = []
    if NEWSAPIAI_KEY:
        tasks.append(search_newsapiai(query, max_results))
    if WEBZ_KEY:
        tasks.append(search_webz(query, max_results))

    # Wait for all API calls to complete
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge results
    all_articles = []
    api_errors = []

    for result in results:
        if isinstance(result, Exception):
            api_errors.append(str(result))
            logger.error("api_call_failed", error=str(result))
        elif isinstance(result, list):
            all_articles.extend(result)

    # If both APIs failed, raise error
    if not all_articles and api_errors:
        raise HTTPException(
            status_code=502,
            detail=f"All news APIs failed: {'; '.join(api_errors)}"
        )

    # Deduplicate articles
    unique_articles = deduplicate_articles(all_articles)

    # Sort by published date (most recent first)
    if sort_by == "publishedAt":
        unique_articles.sort(
            key=lambda x: x.get("published_at", ""),
            reverse=True
        )

    # Limit to max_results
    final_articles = unique_articles[:max_results]

    # Count articles by source
    source_breakdown = {}
    for article in final_articles:
        api_source = article.get("api_source", "unknown")
        source_breakdown[api_source] = source_breakdown.get(api_source, 0) + 1

    logger.info(
        "news_service.search.success",
        query=query,
        total_articles=len(final_articles),
        source_breakdown=source_breakdown
    )

    return {
        "articles": final_articles,
        "total_results": len(final_articles),
        "query": query,
        "language": language,
        "sort_by": sort_by,
        "source_breakdown": source_breakdown,
        "apis_used": [article.get("api_source") for article in final_articles if article.get("api_source")]
    }

@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns:
        200 OK if service is healthy, with API availability status
    """
    apis_configured = []
    if NEWSAPIAI_KEY:
        apis_configured.append("newsapiai")
    if WEBZ_KEY:
        apis_configured.append("webz")

    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "news-rag",
            "version": "2.0.0",
            "apis_configured": apis_configured,
            "api_count": len(apis_configured)
        }
    )

@app.get("/news/search")
async def search_articles(
    query: str = Query(..., description="Search query"),
    language: str = Query("en", description="Language code (e.g., en, es, fr)"),
    sort_by: str = Query("publishedAt", description="Sort order (relevancy, popularity, publishedAt)"),
    max_results: int = Query(10, description="Maximum number of articles", ge=1, le=100)
):
    """
    Search news articles across multiple APIs in parallel.

    Queries both NewsAPI.ai and Webz.io simultaneously for comprehensive coverage.

    Parameters:
    - query: Search query (required)
    - language: Language code (default: en)
    - sort_by: Sort order (default: publishedAt)
    - max_results: Maximum number of articles (1-100, default: 10)

    Returns:
        JSON response with merged articles from both APIs

    Raises:
        404: If parameters are invalid
        502: If all news APIs are unavailable
        500: For unexpected errors
    """
    try:
        result = await search_news(
            query=query,
            language=language,
            sort_by=sort_by,
            max_results=max_results
        )

        logger.info(
            "news_service.search.endpoint.success",
            query=query,
            articles_count=len(result["articles"]),
            source_breakdown=result.get("source_breakdown", {})
        )

        return result

    except ValueError as e:
        logger.warning(
            "news_service.search.invalid_request",
            error=str(e),
            query=query
        )
        raise HTTPException(status_code=404, detail=str(e))

    except HTTPException:
        raise  # Re-raise HTTP exceptions as-is

    except Exception as e:
        logger.error(
            "news_service.search.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/news/headlines")
async def get_headlines(
    country: str = Query("us", description="Country code (e.g., us, gb, ca)"),
    category: Optional[str] = Query(None, description="Category (business, technology, etc.)"),
    max_results: int = Query(10, description="Maximum number of articles", ge=1, le=100)
):
    """
    Get top news headlines.

    Note: This endpoint uses the search functionality with general terms.
    For better results, use /news/search with specific queries.

    Parameters:
    - country: Country code (default: us)
    - category: News category (optional)
    - max_results: Maximum number of articles (1-100, default: 10)

    Returns:
        JSON response with headlines
    """
    # Use search with broad query for headlines
    search_query = f"top news {country}"
    if category:
        search_query += f" {category}"

    return await search_articles(
        query=search_query,
        max_results=max_results
    )

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8015"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True
    )
