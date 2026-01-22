"""WebSearch RAG Service - Brave Search API Integration

Provides web search capabilities as a fallback for questions the LLM cannot answer
with its training data or when recent information is needed.

API Endpoints:
- GET /health - Health check
- GET /search - Web search
- GET /search/news - News search
"""

import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
setup_logging(service_name="websearch-rag")
logger = structlog.get_logger()

SERVICE_NAME = "websearch"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8018"))
# Brave Search API Configuration
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")
BRAVE_BASE_URL = "https://api.search.brave.com/res/v1"

# Global clients
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifespan - initialize and cleanup resources.

    Setup:
    - Admin client for fetching API keys
    - HTTP client for Brave Search API calls
    - Logging configuration

    Cleanup:
    - Close HTTP client and admin client connections
    """
    global http_client, admin_client, BRAVE_API_KEY

    logger.info("websearch_service.startup", msg="Initializing WebSearch RAG service")

    # Initialize admin client for configuration management
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("brave-search")
        if api_config and api_config.get("api_key"):
            BRAVE_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="brave-search")
        else:
            logger.info("api_key_from_env", service="brave-search")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="brave-search")
        logger.info("api_key_from_env_fallback", service="brave-search")

    # Validate API key
    if not BRAVE_API_KEY:
        logger.warning(
            "websearch_service.config.missing_key",
            msg="BRAVE_API_KEY not set - service will return errors"
        )

    # Initialize HTTP client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": BRAVE_API_KEY
        } if BRAVE_API_KEY else {"Accept": "application/json"}
    )

    logger.info("websearch_service.startup.complete", msg="WebSearch RAG service ready")

    yield  # Application runs here

    # Cleanup
    logger.info("websearch_service.shutdown", msg="Shutting down WebSearch RAG service")
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

# Create FastAPI app
app = FastAPI(
    title="WebSearch RAG Service",
    description="Web search capabilities via Brave Search API - fallback for unknown questions",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@cached(ttl=3600)  # Cache for 1 hour
async def web_search(
    query: str,
    count: int = 10,
    safesearch: str = "moderate"
) -> Dict[str, Any]:
    """
    Perform web search via Brave Search API.

    Args:
        query: Search query
        count: Number of results (max 20)
        safesearch: Safe search level (off, moderate, strict)

    Returns:
        Dictionary containing search results

    Raises:
        ValueError: If parameters are invalid
        httpx.HTTPStatusError: If Brave API request fails
    """
    if not BRAVE_API_KEY:
        raise ValueError("Brave Search API key not configured")

    if not query or len(query.strip()) == 0:
        raise ValueError("Search query cannot be empty")

    # Validate safesearch
    valid_safesearch = ["off", "moderate", "strict"]
    if safesearch not in valid_safesearch:
        safesearch = "moderate"

    logger.info(
        "websearch_service.search",
        query=query,
        count=count,
        safesearch=safesearch
    )

    # Build request parameters
    params = {
        "q": query,
        "count": min(count, 20),
        "safesearch": safesearch,
        "text_decorations": False,
        "search_lang": "en"
    }

    # Make API request
    response = await http_client.get(
        f"{BRAVE_BASE_URL}/web/search",
        params=params
    )
    response.raise_for_status()

    data = response.json()

    # Extract web results
    results = []
    for result in data.get("web", {}).get("results", []):
        results.append({
            "title": result.get("title"),
            "url": result.get("url"),
            "description": result.get("description"),
            "age": result.get("age"),  # How recent the content is
            "language": result.get("language")
        })

    return {
        "query": query,
        "results": results,
        "total_results": len(results)
    }

@cached(ttl=1800)  # Cache for 30 minutes (news changes faster)
async def news_search(
    query: str,
    count: int = 10
) -> Dict[str, Any]:
    """
    Search for news articles via Brave Search API.

    Args:
        query: Search query
        count: Number of results (max 20)

    Returns:
        Dictionary containing news results

    Raises:
        ValueError: If parameters are invalid
        httpx.HTTPStatusError: If Brave API request fails
    """
    if not BRAVE_API_KEY:
        raise ValueError("Brave Search API key not configured")

    if not query or len(query.strip()) == 0:
        raise ValueError("Search query cannot be empty")

    logger.info(
        "websearch_service.news",
        query=query,
        count=count
    )

    # Build request parameters
    params = {
        "q": query,
        "count": min(count, 20),
        "search_lang": "en"
    }

    # Make API request
    response = await http_client.get(
        f"{BRAVE_BASE_URL}/news/search",
        params=params
    )
    response.raise_for_status()

    data = response.json()

    # Extract news results
    results = []
    for result in data.get("results", []):
        results.append({
            "title": result.get("title"),
            "url": result.get("url"),
            "description": result.get("description"),
            "age": result.get("age"),
            "source": result.get("meta_url", {}).get("hostname")
        })

    return {
        "query": query,
        "news": results,
        "total_results": len(results)
    }

@app.get("/health")
async def health_check():
    """
    Health check endpoint.

    Returns:
        200 OK if service is healthy
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "websearch-rag",
            "api_key_configured": BRAVE_API_KEY is not None
        }
    )

@app.get("/search")
async def search(
    query: str = Query(..., description="Search query"),
    count: int = Query(10, description="Number of results", ge=1, le=20),
    safesearch: str = Query("moderate", description="Safe search level (off, moderate, strict)")
):
    """
    Perform web search.

    This is the fallback service for questions that:
    - Don't match specific intents (weather, sports, etc.)
    - Require recent information beyond LLM training data
    - Are general knowledge questions

    Parameters:
    - query: Search query (required)
    - count: Number of results (1-20, default: 10)
    - safesearch: Safe search level (default: moderate)

    Returns:
        JSON response with search results

    Raises:
        404: If parameters are invalid
        502: If Brave Search API is unavailable
        500: For unexpected errors
    """
    try:
        result = await web_search(query, count, safesearch)

        logger.info(
            "websearch_service.search.success",
            query=query,
            results_count=len(result["results"])
        )

        return result

    except ValueError as e:
        logger.warning(
            "websearch_service.search.invalid_request",
            error=str(e),
            query=query
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "websearch_service.search.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Brave Search API error: {e}")

    except Exception as e:
        logger.error(
            "websearch_service.search.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/search/news")
async def search_news(
    query: str = Query(..., description="Search query"),
    count: int = Query(10, description="Number of results", ge=1, le=20)
):
    """
    Search for news articles.

    Useful for current events and breaking news.

    Parameters:
    - query: Search query (required)
    - count: Number of results (1-20, default: 10)

    Returns:
        JSON response with news articles

    Raises:
        404: If parameters are invalid
        502: If Brave Search API is unavailable
        500: For unexpected errors
    """
    try:
        result = await news_search(query, count)

        logger.info(
            "websearch_service.news.success",
            query=query,
            results_count=len(result["news"])
        )

        return result

    except ValueError as e:
        logger.warning(
            "websearch_service.news.invalid_request",
            error=str(e),
            query=query
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "websearch_service.news.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Brave Search API error: {e}")

    except Exception as e:
        logger.error(
            "websearch_service.news.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8018"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None  # Use structlog configuration
    )
