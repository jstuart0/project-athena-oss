"""SeatGeek Events RAG Service - SeatGeek API Integration

Provides event search via SeatGeek's API.
Searches concerts, sports, theater, and more.

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
setup_logging(service_name="seatgeek-events-rag")
logger = structlog.get_logger()

SERVICE_NAME = "seatgeek-events"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8024"))

# SeatGeek API Configuration
SEATGEEK_CLIENT_ID = os.getenv("SEATGEEK_CLIENT_ID", "")
SEATGEEK_CLIENT_SECRET = os.getenv("SEATGEEK_CLIENT_SECRET", "")
SEATGEEK_BASE_URL = "https://api.seatgeek.com/2"

# Global clients
http_client: Optional[httpx.AsyncClient] = None
admin_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan - initialize and cleanup resources."""
    global http_client, admin_client, SEATGEEK_CLIENT_ID, SEATGEEK_CLIENT_SECRET

    logger.info("seatgeek_events_service.startup", msg="Initializing SeatGeek Events service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (stored as client_id:client_secret)
    try:
        api_config = await admin_client.get_external_api_key("seatgeek")
        if api_config and api_config.get("api_key"):
            api_key = api_config["api_key"]
            if ":" in api_key:
                SEATGEEK_CLIENT_ID, SEATGEEK_CLIENT_SECRET = api_key.split(":", 1)
            else:
                SEATGEEK_CLIENT_ID = api_key
            logger.info("api_key_from_admin", service="seatgeek")
        else:
            logger.info("api_key_from_env", service="seatgeek")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="seatgeek")
        logger.info("api_key_from_env_fallback", service="seatgeek")

    if not SEATGEEK_CLIENT_ID:
        logger.warning(
            "seatgeek_events_service.config.missing_key",
            msg="SEATGEEK_CLIENT_ID not set - service will return errors"
        )

    # Initialize HTTP client
    http_client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))

    logger.info("seatgeek_events_service.startup.complete", msg="SeatGeek Events service ready")

    yield

    # Cleanup
    logger.info("seatgeek_events_service.shutdown", msg="Shutting down SeatGeek Events service")
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()


app = FastAPI(
    title="SeatGeek Events RAG Service",
    description="Event search via SeatGeek API",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


def get_date_range(date_filter: Optional[str]) -> tuple:
    """Convert date filter to start/end datetime strings for SeatGeek."""
    if not date_filter:
        return None, None

    now = datetime.now()
    date_lower = date_filter.lower()

    if date_lower == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif date_lower == "tomorrow":
        start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif date_lower in ["this week", "week"]:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif date_lower == "next week":
        start = now + timedelta(days=7)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    elif date_lower in ["this month", "month"]:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=30)
    elif date_lower == "next month":
        start = now + timedelta(days=30)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=30)
    else:
        # Try to parse as date YYYY-MM-DD
        try:
            start = datetime.strptime(date_filter, "%Y-%m-%d")
            end = start + timedelta(days=1)
        except ValueError:
            return None, None

    # SeatGeek uses ISO format
    return start.strftime("%Y-%m-%dT%H:%M:%S"), end.strftime("%Y-%m-%dT%H:%M:%S")


@cached(ttl=1800)  # Cache for 30 minutes
async def search_seatgeek_events(
    query: str,
    location: Optional[str] = None,
    date_filter: Optional[str] = None,
    num_results: int = 20
) -> Dict[str, Any]:
    """
    Search for events via SeatGeek API.

    Args:
        query: Search query (e.g., "concerts", "sports", "comedy")
        location: Location (e.g., "Baltimore, MD")
        date_filter: Date filter ("today", "tomorrow", "this week", "next week", or YYYY-MM-DD)
        num_results: Number of results to return

    Returns:
        Dictionary containing events and metadata
    """
    if not SEATGEEK_CLIENT_ID:
        raise ValueError("SeatGeek client ID not configured")

    params = {
        "client_id": SEATGEEK_CLIENT_ID,
        "q": query,
        "per_page": min(num_results, 100)
    }

    if SEATGEEK_CLIENT_SECRET:
        params["client_secret"] = SEATGEEK_CLIENT_SECRET

    # Add location - SeatGeek can search by city/state
    if location:
        # Try to extract city and state
        parts = [p.strip() for p in location.split(",")]
        if len(parts) >= 2:
            params["venue.city"] = parts[0]
            params["venue.state"] = parts[1].replace(" ", "")
        else:
            # Use as general location query
            params["venue.city"] = location

    # Handle date filters
    start_date, end_date = get_date_range(date_filter)
    if start_date:
        params["datetime_utc.gte"] = start_date
    if end_date:
        params["datetime_utc.lte"] = end_date

    logger.info(
        "seatgeek_events.search",
        query=query,
        location=location,
        date_filter=date_filter
    )

    response = await http_client.get(
        f"{SEATGEEK_BASE_URL}/events",
        params=params
    )
    response.raise_for_status()

    data = response.json()

    # Extract events from response
    events = []
    events_results = data.get("events", [])

    for event in events_results[:num_results]:
        # Parse datetime
        datetime_local = event.get("datetime_local", "")
        event_date = None
        event_time = None
        if datetime_local:
            try:
                dt = datetime.fromisoformat(datetime_local.replace("Z", "+00:00"))
                event_date = dt.strftime("%b %d")
                event_time = dt.strftime("%a, %b %d, %-I:%M %p")
            except:
                event_date = datetime_local[:10] if len(datetime_local) >= 10 else datetime_local

        # Get venue info
        venue = event.get("venue", {})
        venue_name = venue.get("name")
        address_parts = []
        if venue.get("address"):
            address_parts.append(venue.get("address"))
        if venue.get("city"):
            address_parts.append(venue.get("city"))
        if venue.get("state"):
            address_parts.append(venue.get("state"))
        address = ", ".join(address_parts) if address_parts else None

        # Get price info
        stats = event.get("stats", {})
        lowest_price = stats.get("lowest_price")
        highest_price = stats.get("highest_price")

        tickets_info = None
        if lowest_price:
            if highest_price and highest_price != lowest_price:
                tickets_info = f"${lowest_price} - ${highest_price}"
            else:
                tickets_info = f"From ${lowest_price}"

        event_info = {
            "title": event.get("title") or event.get("short_title"),
            "date": event_date,
            "time": event_time,
            "address": address,
            "venue": venue_name,
            "description": event.get("description"),
            "link": event.get("url"),
            "thumbnail": event.get("performers", [{}])[0].get("image") if event.get("performers") else None,
            "source": "seatgeek",
            "event_type": event.get("type"),
            "tickets": tickets_info
        }

        events.append(event_info)

    return {
        "events": events,
        "total_events": len(events),
        "query": query,
        "location": location,
        "source": "seatgeek"
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
        location: Location (e.g., "Baltimore, MD")
        event_type: Type of event (concerts, sports, comedy, theater, etc.)
        date_filter: Date filter
        num_results: Number of results

    Returns:
        Dictionary containing local events
    """
    # Build query
    query = event_type if event_type else "events"

    return await search_seatgeek_events(
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
            "service": "seatgeek-events-rag",
            "api_key_configured": bool(SEATGEEK_CLIENT_ID)
        }
    )


@app.get("/events/search")
async def search_events_endpoint(
    query: str = Query(..., description="Search query (e.g., 'concerts', 'sports')"),
    location: Optional[str] = Query(None, description="Location (e.g., 'Baltimore, MD')"),
    date: Optional[str] = Query(None, description="Date filter (today, tomorrow, this week, next week, or YYYY-MM-DD)"),
    start_date: Optional[str] = Query(None, description="Alias for date filter (YYYY-MM-DD format)"),
    size: int = Query(20, description="Number of results", ge=1, le=100)
):
    """
    Search for events using SeatGeek API.

    Parameters:
    - query: Search query (required)
    - location: Location filter (optional)
    - date: Date filter (optional)
    - size: Number of results (1-100, default: 20)

    Returns:
        JSON response with events
    """
    try:
        # Use start_date as fallback if date is not provided
        effective_date = date or start_date

        logger.info(
            "seatgeek_events.search.request",
            query=query,
            location=location,
            date=date,
            start_date=start_date,
            effective_date=effective_date
        )

        result = await search_seatgeek_events(
            query=query,
            location=location,
            date_filter=effective_date,
            num_results=size
        )

        logger.info(
            "seatgeek_events.search.success",
            events_count=len(result["events"]),
            query=query,
            location=location
        )

        return result

    except ValueError as e:
        logger.warning("seatgeek_events.search.invalid_request", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "seatgeek_events.search.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"SeatGeek API error: {e}")

    except Exception as e:
        logger.error("seatgeek_events.search.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/events/local")
async def local_events_endpoint(
    location: str = Query(..., description="Location (e.g., 'Baltimore, MD')"),
    type: Optional[str] = Query(None, description="Event type (concerts, sports, comedy, theater)"),
    date: Optional[str] = Query(None, description="Date filter (today, tomorrow, this week)"),
    size: int = Query(20, description="Number of results", ge=1, le=100)
):
    """
    Search for local events in a specific location.

    Parameters:
    - location: Location (required)
    - type: Event type filter (optional)
    - date: Date filter (optional)
    - size: Number of results (1-100, default: 20)

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
            "seatgeek_events.local.success",
            events_count=len(result["events"]),
            location=location,
            event_type=type
        )

        return result

    except ValueError as e:
        logger.warning("seatgeek_events.local.invalid_request", error=str(e))
        raise HTTPException(status_code=400, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "seatgeek_events.local.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"SeatGeek API error: {e}")

    except Exception as e:
        logger.error("seatgeek_events.local.error", error=str(e), exc_info=True)
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
