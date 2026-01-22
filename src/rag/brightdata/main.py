"""Bright Data RAG Service - Web Scraping via Web Unlocker API

Provides reliable web scraping using Bright Data's Web Unlocker REST API.
Bypasses anti-bot systems, CAPTCHAs, and returns content as markdown.

API Endpoints:
- GET /health - Health check with budget status
- POST /scrape - Scrape webpage to markdown
- GET /search - Web search via Google SERP scraping
- POST /scrape_batch - Scrape multiple URLs in parallel
"""

import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

setup_logging(service_name="brightdata-rag")
logger = structlog.get_logger()

SERVICE_NAME = "brightdata"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8040"))

# Bright Data Web Unlocker REST API
BRIGHT_DATA_API_URL = "https://api.brightdata.com/request"
MONTHLY_BUDGET = 5000  # Free tier limit

# Global state
http_client: Optional[httpx.AsyncClient] = None
admin_client = None
api_token: str = ""
zone_name: str = ""  # Zone name from admin config
request_count: int = 0  # In-memory counter (Phase 3 will add persistent tracking)


class ScrapeRequest(BaseModel):
    url: str
    wait_for: Optional[str] = None  # CSS selector to wait for


class BatchScrapeRequest(BaseModel):
    urls: List[str]
    wait_for: Optional[str] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, admin_client, api_token, zone_name, request_count

    logger.info("brightdata_service.startup")

    admin_client = get_admin_client()

    # Fetch API token and zone from Admin
    # The api_key field contains the API token
    # The api_key2 field (if present) contains the zone name
    try:
        api_config = await admin_client.get_external_api_key("bright-data")
        if api_config and api_config.get("api_key"):
            api_token = api_config["api_key"]
            # Zone name might be in api_key2 or endpoint_url params
            zone_name = api_config.get("api_key2", "") or os.getenv("BRIGHT_DATA_ZONE", "web_unlocker1")
            logger.info("api_token_loaded", source="admin", zone=zone_name)
        else:
            api_token = os.getenv("BRIGHT_DATA_API_TOKEN", "")
            zone_name = os.getenv("BRIGHT_DATA_ZONE", "web_unlocker1")
            logger.info("api_token_loaded", source="env", zone=zone_name)
    except Exception as e:
        logger.warning("admin_unavailable", error=str(e))
        api_token = os.getenv("BRIGHT_DATA_API_TOKEN", "")
        zone_name = os.getenv("BRIGHT_DATA_ZONE", "web_unlocker1")

    if not api_token:
        logger.error("no_api_token", msg="Bright Data API token not configured")

    # Note: Usage tracking will be added in Phase 3
    # For now, request_count starts at 0 each restart
    request_count = 0

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0),  # Longer timeout for anti-bot bypass
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }
    )

    # Register with service registry
    await register_service(SERVICE_NAME, SERVICE_PORT, "Bright Data Web Scraping")

    logger.info("brightdata_service.ready", zone=zone_name)
    yield

    await unregister_service(SERVICE_NAME)
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()


app = FastAPI(
    title="Bright Data RAG Service",
    description="Web scraping and search via Bright Data MCP",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


async def check_budget() -> bool:
    """Check if we have budget remaining using persistent tracking."""
    global request_count

    # Try to get usage from admin API (persistent tracking)
    if admin_client:
        try:
            usage = await admin_client.get_service_usage("bright-data")
            monthly_count = usage.get("monthly_count", 0)
            monthly_limit = usage.get("monthly_limit", MONTHLY_BUDGET)

            # Update local counter to stay in sync
            request_count = monthly_count

            if monthly_limit and monthly_count >= monthly_limit:
                logger.warning("budget_exceeded", count=monthly_count, limit=monthly_limit)
                return False
            return True
        except Exception as e:
            logger.warning("budget_check_failed", error=str(e))

    # Fallback to local counter
    if request_count >= MONTHLY_BUDGET:
        logger.warning("budget_exceeded_local", count=request_count, limit=MONTHLY_BUDGET)
        return False
    return True


async def increment_usage():
    """Increment usage counter with persistent tracking via admin API."""
    global request_count

    # Record to admin API for persistent tracking
    if admin_client:
        try:
            result = await admin_client.record_service_usage("bright-data", 1)
            request_count = result.get("monthly_count", request_count + 1)
            logger.debug("usage_recorded", monthly_count=request_count)
            return
        except Exception as e:
            logger.warning("usage_record_failed", error=str(e))

    # Fallback to local counter
    request_count += 1


@app.get("/health")
async def health_check():
    """Health check with budget status."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "brightdata-rag",
            "api_token_configured": bool(api_token),
            "budget": {
                "used": request_count,
                "limit": MONTHLY_BUDGET,
                "remaining": max(0, MONTHLY_BUDGET - request_count)
            }
        }
    )


@cached(ttl=86400)  # Cache for 24 hours (pages don't change often)
async def scrape_webpage(url: str, wait_for: Optional[str] = None) -> Dict[str, Any]:
    """
    Scrape webpage to markdown via Bright Data Web Unlocker API.

    Uses the REST API with data_format=markdown for clean LLM-ready content.
    Automatically handles CAPTCHAs and anti-bot systems.
    """
    if not api_token:
        raise ValueError("Bright Data API token not configured")

    if not await check_budget():
        raise ValueError("Monthly request budget exceeded")

    logger.info("scraping", url=url, zone=zone_name)

    # Call Bright Data Web Unlocker REST API
    payload = {
        "zone": zone_name,
        "url": url,
        "format": "json",
        "data_format": "markdown",  # Returns content as markdown
        "country": "us"
    }

    response = await http_client.post(
        BRIGHT_DATA_API_URL,
        json=payload
    )
    response.raise_for_status()

    await increment_usage()

    data = response.json()

    # Extract title from markdown if present (first # heading)
    content = data.get("body", "") or data.get("content", "")
    title = ""
    if content:
        title_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        if title_match:
            title = title_match.group(1)

    return {
        "url": url,
        "content": content,
        "title": title or data.get("title", ""),
        "source": "bright_data"
    }


@cached(ttl=3600)  # Cache for 1 hour
async def web_search(query: str, count: int = 5) -> Dict[str, Any]:
    """
    Web search via Bright Data by scraping Google SERP.

    Uses the Web Unlocker to fetch Google search results and parse them.
    This is a fallback when Brave Search is unavailable.
    """
    if not api_token:
        raise ValueError("Bright Data API token not configured")

    if not await check_budget():
        raise ValueError("Monthly request budget exceeded")

    logger.info("searching", query=query, count=count)

    # Build Google search URL
    import urllib.parse
    encoded_query = urllib.parse.quote_plus(query)
    google_url = f"https://www.google.com/search?q={encoded_query}&num={min(count, 10)}"

    # Use Web Unlocker to fetch Google SERP
    payload = {
        "zone": zone_name,
        "url": google_url,
        "format": "json",
        "data_format": "markdown",
        "country": "us"
    }

    response = await http_client.post(
        BRIGHT_DATA_API_URL,
        json=payload
    )
    response.raise_for_status()

    await increment_usage()

    data = response.json()
    content = data.get("body", "") or data.get("content", "")

    # Parse search results from markdown
    # Google SERP markdown typically has links in format [Title](URL)
    results = []
    link_pattern = re.compile(r'\[([^\]]+)\]\((https?://[^\)]+)\)')

    for match in link_pattern.finditer(content):
        title = match.group(1)
        url = match.group(2)
        # Skip Google internal links
        if 'google.com' in url or 'gstatic.com' in url:
            continue
        results.append({
            "title": title,
            "url": url,
            "description": "",  # Would need more parsing for snippets
            "source": "bright_data"
        })
        if len(results) >= count:
            break

    return {
        "query": query,
        "results": results,
        "total_results": len(results)
    }


@app.post("/scrape")
async def scrape(request: ScrapeRequest):
    """
    Scrape a webpage and return as markdown.

    This is the primary use case for Bright Data - reliable scraping
    that bypasses anti-bot systems.
    """
    try:
        result = await scrape_webpage(request.url, request.wait_for)
        logger.info("scrape_success", url=request.url)
        return result
    except ValueError as e:
        logger.warning("scrape_rejected", error=str(e))
        raise HTTPException(status_code=429, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("scrape_api_error", status=e.response.status_code)
        raise HTTPException(status_code=502, detail=f"Bright Data API error: {e}")
    except Exception as e:
        logger.error("scrape_error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/search")
async def search(
    query: str = Query(..., description="Search query"),
    count: int = Query(5, description="Number of results", ge=1, le=10)
):
    """
    Web search via Bright Data.

    Use this as fallback when Brave Search fails or is blocked.
    """
    try:
        result = await web_search(query, count)
        logger.info("search_success", query=query, results=len(result["results"]))
        return result
    except ValueError as e:
        logger.warning("search_rejected", error=str(e))
        raise HTTPException(status_code=429, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("search_api_error", status=e.response.status_code)
        raise HTTPException(status_code=502, detail=f"Bright Data API error: {e}")
    except Exception as e:
        logger.error("search_error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/scrape_batch")
async def scrape_batch(request: BatchScrapeRequest):
    """
    Scrape multiple webpages in PARALLEL.

    All URLs are scraped concurrently using asyncio.gather().
    This is 2-3x faster than sequential scraping for comparison queries.

    Example: "Compare prices on Amazon and Best Buy" triggers parallel scrape.
    """
    if len(request.urls) > 5:
        raise HTTPException(status_code=400, detail="Maximum 5 URLs per batch")

    # Check budget for all URLs
    urls_needed = len(request.urls)
    if request_count + urls_needed > MONTHLY_BUDGET:
        raise HTTPException(
            status_code=429,
            detail=f"Batch would exceed budget. Need {urls_needed}, have {MONTHLY_BUDGET - request_count} remaining"
        )

    logger.info("batch_scrape_start", url_count=len(request.urls))

    # PARALLEL EXECUTION: Scrape all URLs concurrently
    async def scrape_single(url: str):
        try:
            return await scrape_webpage(url, request.wait_for)
        except Exception as e:
            return {"url": url, "error": str(e)}

    results = await asyncio.gather(*[scrape_single(url) for url in request.urls])

    # Aggregate results
    successful = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]

    logger.info("batch_scrape_complete", successful=len(successful), failed=len(failed))

    return {
        "results": results,
        "successful": len(successful),
        "failed": len(failed),
        "budget_remaining": MONTHLY_BUDGET - request_count
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True)
