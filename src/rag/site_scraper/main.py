"""Site Scraper RAG Service - Website content extraction.

Fetches and extracts content from specific URLs to answer follow-up questions
like "Does this restaurant have happy hour?" or "What are their hours?"

API Endpoints:
- GET /health - Health check
- GET /scrape - Fetch and extract content from a URL
- GET /search-and-scrape - Search a site and extract content
"""

import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.cache import cached, CacheClient
from shared.service_registry import startup_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Import ContentFetcher from orchestrator
from orchestrator.search_providers.content_fetcher import ContentFetcher

# Configure logging
setup_logging(service_name="site-scraper-rag")
logger = structlog.get_logger()

SERVICE_NAME = "site-scraper"
SERVICE_PORT = int(os.getenv("SITE_SCRAPER_PORT", "8031"))
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
BRAVE_API_KEY = os.getenv("BRAVE_API_KEY", "")

# Global clients
cache: Optional[CacheClient] = None
http_client: Optional[httpx.AsyncClient] = None
admin_client = None
content_fetcher: Optional[ContentFetcher] = None

# Configuration (loaded from admin at startup)
config = {
    "owner_mode_any_url": True,      # Owner can scrape any URL
    "guest_mode_any_url": False,     # Guest restricted to search-result URLs
    "allowed_domains": [],            # Whitelist for guest mode (empty = all)
    "blocked_domains": [],            # Blacklist for all modes
    "max_content_length": 50000,      # Max extracted content length
    "cache_ttl": 1800                 # 30 minutes
}


async def load_config():
    """Load configuration from Admin API."""
    global config
    try:
        # Fetch service-specific configuration from public endpoint
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            admin_url = os.getenv("ADMIN_API_URL", "http://localhost:8080")
            response = await client.get(f"{admin_url}/api/site-scraper/config/public")
            if response.status_code == 200:
                svc_config = response.json()
                config.update(svc_config)
                logger.info("config_loaded_from_admin", config=config)
            else:
                logger.warning("config_load_failed_using_defaults", status=response.status_code)
    except Exception as e:
        logger.warning("config_load_failed_using_defaults", error=str(e))


def is_url_allowed(url: str, mode: str) -> Tuple[bool, str]:
    """
    Check if URL is allowed for the given mode.

    Args:
        url: URL to check
        mode: "owner" or "guest"

    Returns:
        Tuple of (allowed: bool, reason: str)
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        # Check blocked domains (applies to all modes)
        for blocked in config.get("blocked_domains", []):
            if blocked.lower() in domain:
                return False, f"Domain {domain} is blocked"

        # Owner mode - check if any URL allowed
        if mode == "owner":
            if config.get("owner_mode_any_url", True):
                return True, "Owner mode allows any URL"
            # Fall through to allowed domains check

        # Guest mode - check if any URL allowed
        if mode == "guest":
            if not config.get("guest_mode_any_url", False):
                # Check against allowed domains whitelist
                allowed = config.get("allowed_domains", [])
                if allowed:
                    for allowed_domain in allowed:
                        if allowed_domain.lower() in domain:
                            return True, f"Domain {domain} is whitelisted"
                    return False, f"Domain {domain} not in allowed list for guest mode"
                # Empty whitelist = all domains allowed

        return True, "URL allowed"

    except Exception as e:
        return False, f"Invalid URL: {str(e)}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan."""
    global cache, http_client, admin_client, content_fetcher, BRAVE_API_KEY

    logger.info("site_scraper_service.startup", msg="Initializing Site Scraper RAG service")

    # Register service
    await startup_service(SERVICE_NAME, SERVICE_PORT, "Site Scraper Service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Load configuration
    await load_config()

    # Try to fetch Brave API key for site search
    try:
        api_config = await admin_client.get_external_api_key("brave-search")
        if api_config and api_config.get("api_key"):
            BRAVE_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="brave-search")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e))

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # Initialize HTTP client (for site search)
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": BRAVE_API_KEY
        } if BRAVE_API_KEY else {"Accept": "application/json"}
    )

    # Initialize ContentFetcher
    content_fetcher = ContentFetcher(timeout=3.0, max_concurrent=2)

    logger.info("site_scraper_service.startup.complete")

    yield

    # Cleanup
    logger.info("site_scraper_service.shutdown")
    await unregister_service(SERVICE_NAME)

    if content_fetcher:
        await content_fetcher.close()
    if http_client:
        await http_client.aclose()
    if cache:
        await cache.disconnect()
    if admin_client:
        await admin_client.close()


app = FastAPI(
    title="Site Scraper RAG Service",
    description="Website content extraction for follow-up questions",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "site-scraper-rag",
            "brave_api_configured": bool(BRAVE_API_KEY)
        }
    )


@cached(ttl=1800, key_prefix="site_scrape")
async def scrape_url(url: str, extraction_hint: str = "auto") -> Dict[str, Any]:
    """
    Fetch and extract content from a URL.

    Args:
        url: URL to scrape
        extraction_hint: Extraction method hint ("jsonld", "table", "article", "auto")

    Returns:
        Extracted content dictionary
    """
    result = await content_fetcher.fetch_structured_content(url, extraction_hint)

    if not result:
        raise ValueError(f"Could not extract content from {url}")

    # Truncate if too long
    max_len = config.get("max_content_length", 50000)
    if result.get("type") == "article" and len(result.get("data", "")) > max_len:
        result["data"] = result["data"][:max_len] + "... [truncated]"

    return result


@app.get("/scrape")
async def scrape(
    url: str = Query(..., description="URL to scrape"),
    mode: str = Query("owner", description="User mode (owner/guest)"),
    extraction_hint: str = Query("auto", description="Extraction hint (jsonld/table/article/auto)")
):
    """
    Fetch and extract content from a specific URL.

    Use this endpoint when you have a specific URL and want to extract
    its content (e.g., checking a restaurant's website for happy hour info).

    Parameters:
    - url: The URL to scrape (required)
    - mode: User mode - "owner" or "guest" (affects URL restrictions)
    - extraction_hint: Preferred extraction method

    Returns:
        Extracted content with type, data, and metadata
    """
    # Check if URL is allowed
    allowed, reason = is_url_allowed(url, mode)
    if not allowed:
        logger.warning("scrape_url_blocked", url=url, mode=mode, reason=reason)
        raise HTTPException(status_code=403, detail=reason)

    try:
        result = await scrape_url(url, extraction_hint)

        logger.info(
            "site_scraper_service.scrape.success",
            url=url,
            mode=mode,
            content_type=result.get("type")
        )

        return {
            "success": True,
            "url": url,
            "content": result
        }

    except ValueError as e:
        logger.warning("site_scraper_service.scrape.no_content", url=url, error=str(e))
        raise HTTPException(status_code=404, detail=str(e))

    except Exception as e:
        logger.error("site_scraper_service.scrape.error", url=url, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to scrape URL")


@cached(ttl=3600, key_prefix="site_search")
async def search_site(query: str, site: str, count: int = 3) -> List[Dict[str, Any]]:
    """
    Search within a specific site using Brave Search.

    Args:
        query: Search query
        site: Domain to search within
        count: Number of results

    Returns:
        List of search results
    """
    if not BRAVE_API_KEY:
        raise ValueError("Brave Search API key not configured")

    # Add site: prefix to query
    site_query = f"site:{site} {query}"

    response = await http_client.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={
            "q": site_query,
            "count": min(count, 10),
            "safesearch": "moderate"
        }
    )
    response.raise_for_status()

    data = response.json()
    results = []

    for result in data.get("web", {}).get("results", []):
        results.append({
            "title": result.get("title"),
            "url": result.get("url"),
            "description": result.get("description")
        })

    return results


@app.get("/search-and-scrape")
async def search_and_scrape(
    query: str = Query(..., description="Search query"),
    site: str = Query(..., description="Domain to search within (e.g., restaurant.com)"),
    mode: str = Query("owner", description="User mode (owner/guest)"),
    extraction_hint: str = Query("auto", description="Extraction hint")
):
    """
    Search within a specific site and extract content from top result.

    Use this endpoint to search a specific website for information.
    Example: "happy hour" on "joes-restaurant.com"

    Parameters:
    - query: What to search for (e.g., "happy hour", "menu", "hours")
    - site: Domain to search (e.g., "restaurant.com")
    - mode: User mode - affects URL restrictions
    - extraction_hint: Preferred extraction method

    Returns:
        Search results with extracted content from top result
    """
    try:
        # Search the site
        search_results = await search_site(query, site)

        if not search_results:
            raise HTTPException(
                status_code=404,
                detail=f"No results found for '{query}' on {site}"
            )

        # Scrape the top result
        top_url = search_results[0]["url"]

        # Check if URL is allowed
        allowed, reason = is_url_allowed(top_url, mode)
        if not allowed:
            logger.warning("search_result_blocked", url=top_url, mode=mode, reason=reason)
            raise HTTPException(status_code=403, detail=reason)

        # Extract content
        content = await scrape_url(top_url, extraction_hint)

        logger.info(
            "site_scraper_service.search_and_scrape.success",
            query=query,
            site=site,
            url=top_url
        )

        return {
            "success": True,
            "query": query,
            "site": site,
            "search_results": search_results,
            "extracted_content": {
                "url": top_url,
                "content": content
            }
        }

    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("site_scraper_service.search_and_scrape.error", error=str(e))
        raise HTTPException(status_code=500, detail="Search and scrape failed")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None
    )
