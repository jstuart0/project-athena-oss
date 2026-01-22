import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import configure_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

logger = configure_logging(service_name="dining-rag")

SERVICE_NAME = "dining"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8019"))  # Default port

GOOGLE_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
GOOGLE_PLACES_BASE_URL = "https://maps.googleapis.com/maps/api/place"
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, admin_client, GOOGLE_API_KEY
    logger.info("dining_service.startup")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("google-places")
        if api_config and api_config.get("api_key"):
            GOOGLE_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="google-places")
        else:
            logger.info("api_key_from_env", service="google-places")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="google-places")
        logger.info("api_key_from_env_fallback", service="google-places")

    http_client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
    yield
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

app = FastAPI(title="Dining RAG Service", version="2.0.0", lifespan=lifespan)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@cached(ttl=3600)
async def search_places(location: str, term: Optional[str] = None, radius: int = 5000, limit: int = 20) -> Dict[str, Any]:
    """Search for restaurants using Google Places Text Search API."""
    if not GOOGLE_API_KEY:
        raise ValueError("Google Places API key not configured")

    # Build query
    query = term if term else "restaurants"
    query = f"{query} in {location}"

    params = {
        "query": query,
        "key": GOOGLE_API_KEY,
    }

    response = await http_client.get(
        f"{GOOGLE_PLACES_BASE_URL}/textsearch/json",
        params=params
    )
    response.raise_for_status()
    data = response.json()

    if data.get("status") != "OK" and data.get("status") != "ZERO_RESULTS":
        logger.error("google_places_error", status=data.get("status"), error=data.get("error_message"))
        raise HTTPException(status_code=502, detail=f"Google Places API error: {data.get('status')}")

    # Transform results to match expected format
    places = []
    for place in data.get("results", [])[:limit]:
        places.append({
            "id": place.get("place_id"),
            "name": place.get("name"),
            "rating": place.get("rating"),
            "price_level": "$" * place.get("price_level", 0) if place.get("price_level") else None,
            "address": place.get("formatted_address"),
            "location": place.get("geometry", {}).get("location"),
            "types": place.get("types", []),
            "user_ratings_total": place.get("user_ratings_total"),
            "open_now": place.get("opening_hours", {}).get("open_now") if place.get("opening_hours") else None
        })

    return {
        "places": places,
        "total": len(places),
        "status": data.get("status")
    }

@cached(ttl=86400)
async def get_place_details(place_id: str) -> Dict[str, Any]:
    """Get detailed information about a specific place."""
    if not GOOGLE_API_KEY:
        raise ValueError("Google Places API key not configured")

    params = {
        "place_id": place_id,
        "key": GOOGLE_API_KEY,
        "fields": "name,rating,formatted_phone_number,formatted_address,opening_hours,website,price_level,photos,reviews,user_ratings_total,types"
    }

    response = await http_client.get(
        f"{GOOGLE_PLACES_BASE_URL}/details/json",
        params=params
    )
    response.raise_for_status()
    data = response.json()

    if data.get("status") != "OK":
        logger.error("google_places_details_error", status=data.get("status"))
        raise HTTPException(status_code=502, detail=f"Google Places API error: {data.get('status')}")

    result = data.get("result", {})

    return {
        "id": place_id,
        "name": result.get("name"),
        "rating": result.get("rating"),
        "price_level": "$" * result.get("price_level", 0) if result.get("price_level") else None,
        "phone": result.get("formatted_phone_number"),
        "address": result.get("formatted_address"),
        "website": result.get("website"),
        "opening_hours": result.get("opening_hours", {}).get("weekday_text"),
        "types": result.get("types", []),
        "user_ratings_total": result.get("user_ratings_total"),
        "reviews": result.get("reviews", [])[:3]  # Top 3 reviews
    }

class SearchRequest(BaseModel):
    """Request model for restaurant search."""
    location: str
    cuisine: Optional[str] = None
    term: Optional[str] = None
    price_range: Optional[str] = None
    radius: int = 5000
    limit: int = 10  # OPTIMIZATION: Reduced from 20 to 10 for faster responses

@app.get("/health")
async def health_check():
    return JSONResponse(status_code=200, content={"status": "healthy", "service": "dining-rag", "version": "2.0.0", "provider": "google-places"})

@app.post("/dining/search")
async def search_post(request: SearchRequest):
    """
    Search for restaurants using Google Places (POST endpoint for tool calling).

    Returns highly-rated local dining options.
    """
    # Combine cuisine and term if both provided
    search_term = request.cuisine or request.term

    try:
        results = await search_places(request.location, search_term, request.radius, request.limit)

        # Sort by rating
        if results.get("places"):
            results["places"] = sorted(
                results["places"],
                key=lambda x: (x.get("rating") or 0, x.get("user_ratings_total") or 0),
                reverse=True
            )

        return results
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("http_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Google Places API error: {e}")
    except Exception as e:
        logger.error("search_error", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/dining/search")
async def search(
    location: str = Query(..., description="Location to search (city, address, etc.)"),
    term: Optional[str] = Query(None, description="Search term (e.g., 'italian', 'sushi')"),
    radius: int = Query(5000, ge=100, le=50000, description="Search radius in meters"),
    limit: int = Query(10, ge=1, le=50, description="Maximum number of results"),  # OPTIMIZATION: Reduced default
    sort_by: Optional[str] = Query("rating", description="Sort by: rating, distance (not yet implemented)")
):
    """
    Search for restaurants using Google Places (GET endpoint for direct access).

    Returns highly-rated local dining options.
    """
    try:
        results = await search_places(location, term, radius, limit)

        # Sort by rating if requested
        if sort_by == "rating" and results.get("places"):
            results["places"] = sorted(
                results["places"],
                key=lambda x: (x.get("rating") or 0, x.get("user_ratings_total") or 0),
                reverse=True
            )

        return results
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("http_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Google Places API error: {e}")
    except Exception as e:
        logger.error("search_error", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/dining/{place_id}")
async def get_place(place_id: str):
    """
    Get detailed information about a specific restaurant.

    Includes reviews, hours, contact info, etc.
    """
    try:
        return await get_place_details(place_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error("http_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Google Places API error: {e}")
    except Exception as e:
        logger.error("details_error", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True, log_config=None)
