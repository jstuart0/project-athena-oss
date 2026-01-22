"""Events RAG Service - Ticketmaster API Integration

Provides event search, discovery, and venue information.

API Endpoints:
- GET /health - Health check
- GET /events/search - Search events
- GET /events/{event_id} - Get event details
- GET /venues/search - Search venues
- GET /venues/{venue_id} - Get venue details
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
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse

from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
setup_logging(service_name="events-rag")
logger = structlog.get_logger()

SERVICE_NAME = "events"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8014"))
# Ticketmaster API Configuration
TICKETMASTER_API_KEY = os.getenv("TICKETMASTER_API_KEY", "")
TICKETMASTER_BASE_URL = "https://app.ticketmaster.com/discovery/v2"

# Global clients (will be initialized in lifespan)
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application lifespan - initialize and cleanup resources.

    Setup:
    - Admin client for fetching API keys
    - HTTP client for Ticketmaster API calls
    - Logging configuration

    Cleanup:
    - Close HTTP client and admin client connections
    """
    global http_client, admin_client, TICKETMASTER_API_KEY

    logger.info("events_service.startup", msg="Initializing Events RAG service")

    # Initialize admin client for configuration management
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("ticketmaster")
        if api_config and api_config.get("api_key"):
            TICKETMASTER_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="ticketmaster")
        else:
            logger.info("api_key_from_env", service="ticketmaster")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="ticketmaster")
        logger.info("api_key_from_env_fallback", service="ticketmaster")

    # Validate API key
    if not TICKETMASTER_API_KEY:
        logger.warning(
            "events_service.config.missing_key",
            msg="TICKETMASTER_API_KEY not set - service will return errors"
        )

    # Initialize HTTP client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        params={"apikey": TICKETMASTER_API_KEY} if TICKETMASTER_API_KEY else {}
    )

    logger.info("events_service.startup.complete", msg="Events RAG service ready")

    yield  # Application runs here

    # Cleanup
    logger.info("events_service.shutdown", msg="Shutting down Events RAG service")
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

# Create FastAPI app
app = FastAPI(
    title="Events RAG Service",
    description="Event and venue information via Ticketmaster API",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@cached(ttl=3600)  # Cache for 1 hour
async def search_events(
    keyword: Optional[str] = None,
    city: Optional[str] = None,
    state_code: Optional[str] = None,
    country_code: str = "US",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    classification_name: Optional[str] = None,
    size: int = 20
) -> Dict[str, Any]:
    """
    Search for events via Ticketmaster API.

    Args:
        keyword: Keyword to search (artist, event name, venue, etc.)
        city: City name
        state_code: State code (e.g., "CA", "NY")
        country_code: Country code (default: "US")
        start_date: Start date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)
        end_date: End date (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SSZ)
        classification_name: Event classification (Music, Sports, Arts, etc.)
        size: Number of results (max 200)

    Returns:
        Dictionary containing events and metadata

    Raises:
        ValueError: If parameters are invalid
        httpx.HTTPStatusError: If Ticketmaster API request fails
    """
    if not TICKETMASTER_API_KEY:
        raise ValueError("Ticketmaster API key not configured")

    # Build request parameters
    params = {
        "countryCode": country_code,
        "size": min(size, 200)
    }

    if keyword:
        params["keyword"] = keyword
    if city:
        params["city"] = city
    if state_code:
        params["stateCode"] = state_code
    if start_date:
        params["startDateTime"] = start_date
    if end_date:
        params["endDateTime"] = end_date
    if classification_name:
        params["classificationName"] = classification_name

    logger.info(
        "events_service.search",
        keyword=keyword,
        city=city,
        state_code=state_code,
        classification=classification_name
    )

    # Make API request
    response = await http_client.get(
        f"{TICKETMASTER_BASE_URL}/events.json",
        params=params
    )
    response.raise_for_status()

    data = response.json()

    # Extract events from response
    events = []
    embedded = data.get("_embedded", {})

    for event in embedded.get("events", []):
        # Extract basic info
        event_info = {
            "id": event.get("id"),
            "name": event.get("name"),
            "url": event.get("url"),
            "type": event.get("type")
        }

        # Extract dates
        dates = event.get("dates", {})
        start = dates.get("start", {})
        event_info["date"] = start.get("localDate")
        event_info["time"] = start.get("localTime")
        event_info["timezone"] = start.get("timezone")

        # Extract venue
        venues = event.get("_embedded", {}).get("venues", [])
        if venues:
            venue = venues[0]
            event_info["venue"] = {
                "name": venue.get("name"),
                "city": venue.get("city", {}).get("name"),
                "state": venue.get("state", {}).get("name"),
                "country": venue.get("country", {}).get("name")
            }

        # Extract classifications
        classifications = event.get("classifications", [])
        if classifications:
            classification = classifications[0]
            event_info["classification"] = {
                "segment": classification.get("segment", {}).get("name"),
                "genre": classification.get("genre", {}).get("name"),
                "subGenre": classification.get("subGenre", {}).get("name")
            }

        # Extract price range
        price_ranges = event.get("priceRanges", [])
        if price_ranges:
            price_range = price_ranges[0]
            event_info["price_range"] = {
                "min": price_range.get("min"),
                "max": price_range.get("max"),
                "currency": price_range.get("currency")
            }

        events.append(event_info)

    # Extract pagination info
    page = data.get("page", {})

    return {
        "events": events,
        "total_events": page.get("totalElements", 0),
        "page": page.get("number", 0),
        "total_pages": page.get("totalPages", 0),
        "size": page.get("size", 0)
    }

@cached(ttl=86400)  # Cache for 24 hours
async def get_event_details(event_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific event.

    Args:
        event_id: Ticketmaster event ID

    Returns:
        Dictionary containing event details

    Raises:
        ValueError: If event ID is invalid
        httpx.HTTPStatusError: If Ticketmaster API request fails
    """
    if not TICKETMASTER_API_KEY:
        raise ValueError("Ticketmaster API key not configured")

    if not event_id:
        raise ValueError("Event ID is required")

    logger.info("events_service.get_event", event_id=event_id)

    # Make API request
    response = await http_client.get(
        f"{TICKETMASTER_BASE_URL}/events/{event_id}.json"
    )
    response.raise_for_status()

    event = response.json()

    # Extract comprehensive event info
    event_info = {
        "id": event.get("id"),
        "name": event.get("name"),
        "description": event.get("info"),
        "url": event.get("url"),
        "type": event.get("type")
    }

    # Extract dates
    dates = event.get("dates", {})
    start = dates.get("start", {})
    event_info["dates"] = {
        "date": start.get("localDate"),
        "time": start.get("localTime"),
        "timezone": start.get("timezone"),
        "status": dates.get("status", {}).get("code")
    }

    # Extract venues
    venues = event.get("_embedded", {}).get("venues", [])
    event_info["venues"] = []
    for venue in venues:
        event_info["venues"].append({
            "id": venue.get("id"),
            "name": venue.get("name"),
            "url": venue.get("url"),
            "address": venue.get("address", {}).get("line1"),
            "city": venue.get("city", {}).get("name"),
            "state": venue.get("state", {}).get("name"),
            "state_code": venue.get("state", {}).get("stateCode"),
            "postal_code": venue.get("postalCode"),
            "country": venue.get("country", {}).get("name"),
            "country_code": venue.get("country", {}).get("countryCode")
        })

    # Extract classifications
    classifications = event.get("classifications", [])
    event_info["classifications"] = []
    for classification in classifications:
        event_info["classifications"].append({
            "segment": classification.get("segment", {}).get("name"),
            "genre": classification.get("genre", {}).get("name"),
            "subGenre": classification.get("subGenre", {}).get("name"),
            "type": classification.get("type", {}).get("name"),
            "subType": classification.get("subType", {}).get("name")
        })

    # Extract price ranges
    price_ranges = event.get("priceRanges", [])
    event_info["price_ranges"] = []
    for price_range in price_ranges:
        event_info["price_ranges"].append({
            "type": price_range.get("type"),
            "currency": price_range.get("currency"),
            "min": price_range.get("min"),
            "max": price_range.get("max")
        })

    return event_info

@cached(ttl=3600)  # Cache for 1 hour
async def search_venues(
    keyword: Optional[str] = None,
    city: Optional[str] = None,
    state_code: Optional[str] = None,
    country_code: str = "US",
    size: int = 20
) -> Dict[str, Any]:
    """
    Search for venues via Ticketmaster API.

    Args:
        keyword: Keyword to search (venue name, etc.)
        city: City name
        state_code: State code (e.g., "CA", "NY")
        country_code: Country code (default: "US")
        size: Number of results (max 200)

    Returns:
        Dictionary containing venues and metadata

    Raises:
        ValueError: If parameters are invalid
        httpx.HTTPStatusError: If Ticketmaster API request fails
    """
    if not TICKETMASTER_API_KEY:
        raise ValueError("Ticketmaster API key not configured")

    # Build request parameters
    params = {
        "countryCode": country_code,
        "size": min(size, 200)
    }

    if keyword:
        params["keyword"] = keyword
    if city:
        params["city"] = city
    if state_code:
        params["stateCode"] = state_code

    logger.info(
        "events_service.search_venues",
        keyword=keyword,
        city=city,
        state_code=state_code
    )

    # Make API request
    response = await http_client.get(
        f"{TICKETMASTER_BASE_URL}/venues.json",
        params=params
    )
    response.raise_for_status()

    data = response.json()

    # Extract venues from response
    venues = []
    embedded = data.get("_embedded", {})

    for venue in embedded.get("venues", []):
        venues.append({
            "id": venue.get("id"),
            "name": venue.get("name"),
            "url": venue.get("url"),
            "address": venue.get("address", {}).get("line1"),
            "city": venue.get("city", {}).get("name"),
            "state": venue.get("state", {}).get("name"),
            "state_code": venue.get("state", {}).get("stateCode"),
            "postal_code": venue.get("postalCode"),
            "country": venue.get("country", {}).get("name"),
            "country_code": venue.get("country", {}).get("countryCode"),
            "timezone": venue.get("timezone")
        })

    # Extract pagination info
    page = data.get("page", {})

    return {
        "venues": venues,
        "total_venues": page.get("totalElements", 0),
        "page": page.get("number", 0),
        "total_pages": page.get("totalPages", 0),
        "size": page.get("size", 0)
    }

@cached(ttl=86400)  # Cache for 24 hours
async def get_venue_details(venue_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific venue.

    Args:
        venue_id: Ticketmaster venue ID

    Returns:
        Dictionary containing venue details

    Raises:
        ValueError: If venue ID is invalid
        httpx.HTTPStatusError: If Ticketmaster API request fails
    """
    if not TICKETMASTER_API_KEY:
        raise ValueError("Ticketmaster API key not configured")

    if not venue_id:
        raise ValueError("Venue ID is required")

    logger.info("events_service.get_venue", venue_id=venue_id)

    # Make API request
    response = await http_client.get(
        f"{TICKETMASTER_BASE_URL}/venues/{venue_id}.json"
    )
    response.raise_for_status()

    venue = response.json()

    # Extract comprehensive venue info
    return {
        "id": venue.get("id"),
        "name": venue.get("name"),
        "description": venue.get("description"),
        "url": venue.get("url"),
        "address": {
            "line1": venue.get("address", {}).get("line1"),
            "line2": venue.get("address", {}).get("line2")
        },
        "city": venue.get("city", {}).get("name"),
        "state": venue.get("state", {}).get("name"),
        "state_code": venue.get("state", {}).get("stateCode"),
        "postal_code": venue.get("postalCode"),
        "country": venue.get("country", {}).get("name"),
        "country_code": venue.get("country", {}).get("countryCode"),
        "timezone": venue.get("timezone"),
        "location": {
            "longitude": venue.get("location", {}).get("longitude"),
            "latitude": venue.get("location", {}).get("latitude")
        },
        "markets": [market.get("name") for market in venue.get("markets", [])],
        "dmas": [dma.get("name") for dma in venue.get("dmas", [])]
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
            "service": "events-rag",
            "api_key_configured": TICKETMASTER_API_KEY is not None
        }
    )

@app.get("/events/search")
async def search_events_endpoint(
    keyword: Optional[str] = Query(None, description="Search keyword (artist, event name, venue)"),
    city: Optional[str] = Query(None, description="City name"),
    state_code: Optional[str] = Query(None, description="State code (e.g., CA, NY)"),
    country_code: str = Query("US", description="Country code"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    classification_name: Optional[str] = Query(None, description="Classification (Music, Sports, Arts)"),
    size: int = Query(20, description="Number of results", ge=1, le=200)
):
    """
    Search for events.

    Parameters:
    - keyword: Search keyword (optional)
    - city: City name (optional)
    - state_code: State code (optional)
    - country_code: Country code (default: US)
    - start_date: Start date filter (optional)
    - end_date: End date filter (optional)
    - classification_name: Event classification (optional)
    - size: Number of results (1-200, default: 20)

    Returns:
        JSON response with events

    Raises:
        404: If parameters are invalid
        502: If Ticketmaster API is unavailable
        500: For unexpected errors
    """
    try:
        result = await search_events(
            keyword=keyword,
            city=city,
            state_code=state_code,
            country_code=country_code,
            start_date=start_date,
            end_date=end_date,
            classification_name=classification_name,
            size=size
        )

        logger.info(
            "events_service.search.success",
            events_count=len(result["events"]),
            keyword=keyword
        )

        return result

    except ValueError as e:
        logger.warning(
            "events_service.search.invalid_request",
            error=str(e)
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "events_service.search.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Ticketmaster API error: {e}")

    except Exception as e:
        logger.error(
            "events_service.search.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/events/{event_id}")
async def get_event(
    event_id: str = Path(..., description="Ticketmaster event ID")
):
    """
    Get detailed information about a specific event.

    Parameters:
    - event_id: Ticketmaster event ID (required)

    Returns:
        JSON response with event details

    Raises:
        404: If event not found or parameters invalid
        502: If Ticketmaster API is unavailable
        500: For unexpected errors
    """
    try:
        result = await get_event_details(event_id)

        logger.info(
            "events_service.get_event.success",
            event_id=event_id
        )

        return result

    except ValueError as e:
        logger.warning(
            "events_service.get_event.invalid_request",
            error=str(e),
            event_id=event_id
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "events_service.get_event.api_error",
            status_code=e.response.status_code,
            error=str(e),
            event_id=event_id
        )
        raise HTTPException(status_code=502, detail=f"Ticketmaster API error: {e}")

    except Exception as e:
        logger.error(
            "events_service.get_event.error",
            error=str(e),
            event_id=event_id,
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/venues/search")
async def search_venues_endpoint(
    keyword: Optional[str] = Query(None, description="Search keyword (venue name)"),
    city: Optional[str] = Query(None, description="City name"),
    state_code: Optional[str] = Query(None, description="State code (e.g., CA, NY)"),
    country_code: str = Query("US", description="Country code"),
    size: int = Query(20, description="Number of results", ge=1, le=200)
):
    """
    Search for venues.

    Parameters:
    - keyword: Search keyword (optional)
    - city: City name (optional)
    - state_code: State code (optional)
    - country_code: Country code (default: US)
    - size: Number of results (1-200, default: 20)

    Returns:
        JSON response with venues

    Raises:
        404: If parameters are invalid
        502: If Ticketmaster API is unavailable
        500: For unexpected errors
    """
    try:
        result = await search_venues(
            keyword=keyword,
            city=city,
            state_code=state_code,
            country_code=country_code,
            size=size
        )

        logger.info(
            "events_service.search_venues.success",
            venues_count=len(result["venues"]),
            keyword=keyword
        )

        return result

    except ValueError as e:
        logger.warning(
            "events_service.search_venues.invalid_request",
            error=str(e)
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "events_service.search_venues.api_error",
            status_code=e.response.status_code,
            error=str(e)
        )
        raise HTTPException(status_code=502, detail=f"Ticketmaster API error: {e}")

    except Exception as e:
        logger.error(
            "events_service.search_venues.error",
            error=str(e),
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/venues/{venue_id}")
async def get_venue(
    venue_id: str = Path(..., description="Ticketmaster venue ID")
):
    """
    Get detailed information about a specific venue.

    Parameters:
    - venue_id: Ticketmaster venue ID (required)

    Returns:
        JSON response with venue details

    Raises:
        404: If venue not found or parameters invalid
        502: If Ticketmaster API is unavailable
        500: For unexpected errors
    """
    try:
        result = await get_venue_details(venue_id)

        logger.info(
            "events_service.get_venue.success",
            venue_id=venue_id
        )

        return result

    except ValueError as e:
        logger.warning(
            "events_service.get_venue.invalid_request",
            error=str(e),
            venue_id=venue_id
        )
        raise HTTPException(status_code=404, detail=str(e))

    except httpx.HTTPStatusError as e:
        logger.error(
            "events_service.get_venue.api_error",
            status_code=e.response.status_code,
            error=str(e),
            venue_id=venue_id
        )
        raise HTTPException(status_code=502, detail=f"Ticketmaster API error: {e}")

    except Exception as e:
        logger.error(
            "events_service.get_venue.error",
            error=str(e),
            venue_id=venue_id,
            exc_info=True
        )
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8013"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None  # Use structlog configuration
    )
