"""SerpAPI Events RAG Service - Google Events Integration

Provides event search via SerpAPI's Google Events engine.
Can search local events, concerts, sports, and more.

API Endpoints:
- GET /health - Health check
- GET /events/search - Search events
- GET /events/local - Search local events by location
"""

import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from datetime import datetime, timedelta

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from shared.cache import cached
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
setup_logging(service_name="serpapi-events-rag")
logger = structlog.get_logger()

SERVICE_NAME = "serpapi-events"
SERVICE_PORT = int(os.getenv("SERPAPI_EVENTS_PORT", "8032"))

# SerpAPI Configuration
SERPAPI_API_KEY = os.getenv("SERPAPI_API_KEY", "")
SERPAPI_BASE_URL = "https://serpapi.com/search"

# Global clients
http_client: Optional[httpx.AsyncClient] = None
admin_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - initialize and cleanup resources."""
    global http_client, admin_client, SERPAPI_API_KEY

    logger.info("serpapi_events_service.startup", msg="Initializing SerpAPI Events service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API
    try:
        api_config = await admin_client.get_external_api_key("serpapi")
        if api_config and api_config.get("api_key"):
            SERPAPI_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="serpapi")
        else:
            logger.info("api_key_from_env", service="serpapi")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="serpapi")
        logger.info("api_key_from_env_fallback", service="serpapi")

    if not SERPAPI_API_KEY:
        logger.warning(
            "serpapi_events_service.config.missing_key",
            msg="SERPAPI_API_KEY not set - service will return errors"
        )

    # Initialize HTTP client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))

    logger.info("serpapi_events_service.startup.complete", msg="SerpAPI Events service ready")

    yield

    # Cleanup
    logger.info("serpapi_events_service.shutdown", msg="Shutting down SerpAPI Events service")
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()


app = FastAPI(
    title="SerpAPI Events RAG Service",
    description="Event search via SerpAPI Google Events",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


def format_date_for_serpapi(date_str: Optional[str]) -> Optional[str]:
    """Convert date string to SerpAPI format (YYYY-MM-DD)."""
    if not date_str:
        return None
    try:
        # Handle various formats
        for fmt in ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"]:
            try:
                dt = datetime.strptime(date_str, fmt)
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                continue
        return date_str
    except Exception:
        return date_str


@cached(ttl=1800)  # Cache for 30 minutes
async def search_google_events(
    query: str,
    location: Optional[str] = None,
    date_filter: Optional[str] = None,
    num_results: int = 20
) -> Dict[str, Any]:
    """
    Search for events via SerpAPI Google Events.

    Args:
        query: Search query (e.g., "concerts", "sports events", "comedy shows")
        location: Location (e.g., "Baltimore, MD")
        date_filter: Date filter ("today", "tomorrow", "this week", "next week", or date range)
        num_results: Number of results to return

    Returns:
        Dictionary containing events and metadata
    """
    if not SERPAPI_API_KEY:
        raise ValueError("SerpAPI key not configured")

    params = {
        "engine": "google_events",
        "q": query,
        "api_key": SERPAPI_API_KEY,
        "hl": "en",
        "gl": "us"
    }

    if location:
        params["location"] = location

    # Handle date filters
    if date_filter:
        date_lower = date_filter.lower()
        if date_lower == "today":
            params["htichips"] = "date:today"
        elif date_lower == "tomorrow":
            params["htichips"] = "date:tomorrow"
        elif date_lower in ["this week", "week"]:
            params["htichips"] = "date:week"
        elif date_lower in ["next week"]:
            params["htichips"] = "date:next_week"
        elif date_lower in ["this month", "month"]:
            params["htichips"] = "date:month"
        elif date_lower in ["next month"]:
            params["htichips"] = "date:next_month"
        else:
            # Try to parse as date range
            formatted = format_date_for_serpapi(date_filter)
            if formatted:
                params["htichips"] = f"date:{formatted}"

    logger.info(
        "serpapi_events.search",
        query=query,
        location=location,
        date_filter=date_filter
    )

    response = await http_client.get(SERPAPI_BASE_URL, params=params)
    response.raise_for_status()

    data = response.json()

    # Extract events from response
    events = []
    events_results = data.get("events_results", [])

    for event in events_results[:num_results]:
        event_info = {
            "title": event.get("title"),
            "date": event.get("date", {}).get("start_date") if isinstance(event.get("date"), dict) else event.get("date"),
            "time": event.get("date", {}).get("when") if isinstance(event.get("date"), dict) else None,
            "address": None,
            "venue": None,
            "description": event.get("description"),
            "link": event.get("link"),
            "thumbnail": event.get("thumbnail"),
            "source": "serpapi_google_events"
        }

        # Extract venue info
        venue = event.get("venue", {})
        if isinstance(venue, dict):
            event_info["venue"] = venue.get("name")
            event_info["address"] = ", ".join(filter(None, [
                venue.get("name"),
                venue.get("address")
            ]))
        elif isinstance(venue, str):
            event_info["venue"] = venue

        # Extract address from address field
        address_info = event.get("address", [])
        if isinstance(address_info, list) and address_info:
            event_info["address"] = ", ".join(address_info)

        # Extract ticket info if available
        ticket_info = event.get("ticket_info", [])
        if ticket_info:
            event_info["tickets"] = ticket_info

        events.append(event_info)

    return {
        "events": events,
        "total_events": len(events),
        "query": query,
        "location": location,
        "source": "serpapi_google_events"
    }


@cached(ttl=1800)
async def search_local_events(
    location: str,
    event_type: Optional[str] = None,
    date_filter: Optional[str] = None,
    num_results: int = 20
) -> Dict[str, Any]:
    """
    Search for local events in a specific location.

    Args:
        location: Location (e.g., "Baltimore, MD", "912 S Clinton St, Baltimore, MD")
        event_type: Type of event (concerts, sports, comedy, theater, etc.)
        date_filter: Date filter
        num_results: Number of results

    Returns:
        Dictionary containing local events
    """
    # Build query
    query_parts = ["events"]
    if event_type:
        query_parts.insert(0, event_type)

    query = " ".join(query_parts)

    return await search_google_events(
        query=query,
        location=location,
        date_filter=date_filter,
        num_results=num_results
    )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "serpapi-events-rag",
            "api_key_configured": bool(SERPAPI_API_KEY)
        }
    )


@app.get("/events/search")
async def search_events_endpoint(
    query: str = Query(..., description="Search query (e.g., 'concerts', 'sports events')"),
    location: Optional[str] = Query(None, description="Location (e.g., 'Baltimore, MD')"),
    date: Optional[str] = Query(None, description="Date filter (today, tomorrow, this week, next week, or YYYY-MM-DD)"),
    size: int = Query(20, description="Number of results", ge=1, le=50)
):
    """
    Search for events using SerpAPI Google Events.

    Parameters:
    - query: Search query (required)
    - location: Location filter (optional)
    - date: Date filter (optional)
    - size: Number of results (1-50, default: 20)

    Returns:
        JSON response with events
    """
    try:
        result = await search_google_events(
            query=query,
            location=location,
            date_filter=date,
            num_results=size
        )

        logger.info(
            "serpapi_events.search.success",
            events_count=len(result["events"]),
            query=query,
            location=location
        )

        return result

    except ValueError as e:
        logger.warning("serpapi_events.search.invalid_request", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "serpapi_events.search.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"SerpAPI error: {e}")

    except Exception as e:
        logger.error("serpapi_events.search.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/events/local")
async def local_events_endpoint(
    location: str = Query(..., description="Location (e.g., 'Baltimore, MD')"),
    type: Optional[str] = Query(None, description="Event type (concerts, sports, comedy, theater)"),
    date: Optional[str] = Query(None, description="Date filter (today, tomorrow, this week)"),
    size: int = Query(20, description="Number of results", ge=1, le=50)
):
    """
    Search for local events in a specific location.

    Parameters:
    - location: Location (required)
    - type: Event type filter (optional)
    - date: Date filter (optional)
    - size: Number of results (1-50, default: 20)

    Returns:
        JSON response with local events
    """
    try:
        result = await search_local_events(
            location=location,
            event_type=type,
            date_filter=date,
            num_results=size
        )

        logger.info(
            "serpapi_events.local.success",
            events_count=len(result["events"]),
            location=location,
            event_type=type
        )

        return result

    except ValueError as e:
        logger.warning("serpapi_events.local.invalid_request", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "serpapi_events.local.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"SerpAPI error: {e}")

    except Exception as e:
        logger.error("serpapi_events.local.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None
    )
