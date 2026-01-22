"""Airports RAG Service - FlightAware Integration

Provides airport and flight data retrieval with caching.

Endpoints:
- GET /health - Health check
- GET /airports/search?query={query} - Search airports
- GET /airports/{code} - Get airport details
- GET /flights/{flight_id} - Get flight information
"""

import os
import sys
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
import httpx
from contextlib import asynccontextmanager

# Import shared utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient, cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import configure_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
logger = configure_logging("airports-rag")

# Environment variables (fallback if database unavailable)
FLIGHTAWARE_API_KEY = os.getenv("FLIGHTAWARE_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8011"))
SERVICE_NAME = "airports-rag"

# FlightAware API base URL
FLIGHTAWARE_BASE_URL = "https://aeroapi.flightaware.com/aeroapi"

# Cache client, HTTP client, and admin client
cache = None
http_client = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache, http_client, admin_client, FLIGHTAWARE_API_KEY

    # Startup
    logger.info("Starting Airports RAG service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("flightaware")
        if api_config and api_config.get("api_key"):
            FLIGHTAWARE_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="flightaware")
        else:
            logger.info("api_key_from_env", service="flightaware")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="flightaware")
        logger.info("api_key_from_env_fallback", service="flightaware")

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # OPTIMIZATION: Create reusable HTTP client
    http_client = httpx.AsyncClient(timeout=10.0)
    logger.info("HTTP client initialized")

    yield

    # Shutdown
    logger.info("Shutting down Airports RAG service")
    if http_client:
        await http_client.aclose()
    if cache:
        await cache.disconnect()
    if admin_client:
        await admin_client.close()

app = FastAPI(
    title="Airports RAG Service",
    description="FlightAware integration with caching",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "airports-rag",
        "version": "1.0.0"
    }

@cached(ttl=3600, key_prefix="airport_search")  # Cache for 1 hour
async def search_airports_api(query: str) -> List[Dict[str, Any]]:
    """
    Search for airports by name or code.

    Args:
        query: Search query (airport name or code)

    Returns:
        List of matching airports
    """
    logger.info(f"Searching airports: {query}")

    url = f"{FLIGHTAWARE_BASE_URL}/airports/{query}"
    headers = {
        "x-apikey": FLIGHTAWARE_API_KEY
    }

    # OPTIMIZATION: Use global HTTP client
    response = await http_client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

@cached(ttl=3600, key_prefix="airport_info")  # Cache for 1 hour
async def get_airport_info_api(code: str) -> Dict[str, Any]:
    """
    Get detailed airport information.

    Args:
        code: Airport ICAO or IATA code

    Returns:
        Airport details
    """
    logger.info(f"Fetching airport info: {code}")

    url = f"{FLIGHTAWARE_BASE_URL}/airports/{code}"
    headers = {
        "x-apikey": FLIGHTAWARE_API_KEY
    }

    # OPTIMIZATION: Use global HTTP client
    response = await http_client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

@cached(ttl=300, key_prefix="flight_info")  # Cache for 5 minutes
async def get_flight_info_api(flight_id: str) -> Dict[str, Any]:
    """
    Get flight information.

    Args:
        flight_id: Flight identifier

    Returns:
        Flight details
    """
    logger.info(f"Fetching flight info: {flight_id}")

    url = f"{FLIGHTAWARE_BASE_URL}/flights/{flight_id}"
    headers = {
        "x-apikey": FLIGHTAWARE_API_KEY
    }

    # OPTIMIZATION: Use global HTTP client
    response = await http_client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()

@app.get("/airports/search")
async def search_airports(
    query: str = Query(..., description="Airport name or code")
):
    """Search for airports."""
    try:
        results = await search_airports_api(query)
        return {"query": query, "results": results}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail="No airports found")
        logger.error(f"FlightAware API error: {e}")
        raise HTTPException(status_code=502, detail="Airport service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/airports/{code}")
async def get_airport(
    code: str = Path(..., description="Airport ICAO or IATA code")
):
    """Get airport details by code."""
    try:
        airport = await get_airport_info_api(code.upper())
        return airport
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Airport not found: {code}")
        logger.error(f"FlightAware API error: {e}")
        raise HTTPException(status_code=502, detail="Airport service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/flights/{flight_id}")
async def get_flight(
    flight_id: str = Path(..., description="Flight identifier")
):
    """Get flight information."""
    try:
        flight = await get_flight_info_api(flight_id.upper())
        return flight
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise HTTPException(status_code=404, detail=f"Flight not found: {flight_id}")
        logger.error(f"FlightAware API error: {e}")
        raise HTTPException(status_code=502, detail="Flight service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Airports RAG service on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
