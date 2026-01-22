"""Weather RAG Service - OpenWeatherMap Integration

Provides weather data retrieval with caching and geocoding support.

Endpoints:
- GET /health - Health check
- GET /weather/current?location={location} - Current weather
- GET /weather/forecast?location={location}&days={days} - Weather forecast
"""

import os
import sys
from typing import Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx
from contextlib import asynccontextmanager

# Import shared utilities (adjust path as needed when deployed)
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient, cached
from shared.service_registry import startup_service, unregister_service
from shared.logging_config import configure_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint, record_external_api_call

# Configure logging
logger = configure_logging("weather-rag")

SERVICE_NAME = "weather-rag"

# Environment variables (fallback if database unavailable)
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8010"))

# Cache client, HTTP client, and admin client
cache = None
http_client = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache, http_client, admin_client, OPENWEATHER_API_KEY

    # Startup
    logger.info("Starting Weather RAG service")

    # Register service in registry (kills stale process on port if any)
    await startup_service("weather", SERVICE_PORT, "Weather Service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("openweather")
        if api_config and api_config.get("api_key"):
            OPENWEATHER_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="openweather")
        else:
            logger.info("api_key_from_env", service="openweather")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="openweather")
        logger.info("api_key_from_env_fallback", service="openweather")

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # OPTIMIZATION: Create reusable HTTP client
    http_client = httpx.AsyncClient(timeout=10.0)
    logger.info("HTTP client initialized")

    yield

    # Shutdown
    logger.info("Shutting down Weather RAG service")

    # Unregister from service registry
    await unregister_service("weather")

    if http_client:
        await http_client.aclose()
    if cache:
        await cache.disconnect()
    if admin_client:
        await admin_client.close()

app = FastAPI(
    title="Weather RAG Service",
    description="OpenWeatherMap integration with caching",
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
        "service": "weather-rag",
        "version": "1.0.0"
    }

@cached(ttl=600, key_prefix="geocode")  # Cache for 10 minutes
async def geocode_location(location: str) -> Dict[str, Any]:
    """
    Geocode location name to lat/lon coordinates.

    Args:
        location: City name (e.g., "Los Angeles", "New York, NY")

    Returns:
        Dict with lat, lon, name, country
    """
    logger.info(f"Geocoding location: {location}")

    # Handle full street addresses by extracting city/state
    # Pattern: "123 Street Name, City, STATE ZIP" -> "City, STATE"
    import re
    parts = [p.strip() for p in location.split(",")]
    if len(parts) >= 3:
        # Looks like a full address - extract city and state
        # Last part might have "STATE ZIP" like "MD 21224"
        city = parts[-2]  # Second to last is usually city
        state_part = parts[-1]  # Last part is state (possibly with ZIP)
        # Extract just the state code if ZIP is included
        state_match = re.match(r'^([A-Z]{2})\s*\d*$', state_part.strip())
        if state_match:
            state = state_match.group(1)
            location = f"{city}, {state}"
            logger.info(f"Extracted city/state from address: {location}")

    # Normalize location format for OpenWeatherMap API
    # Remove spaces around commas: "Baltimore, MD" -> "Baltimore,MD"
    normalized_location = location.replace(", ", ",").replace(" ,", ",")

    # Add ,US if location doesn't have a country code and appears to be US format
    # (has state code like "MD", "CA", etc.)
    parts = normalized_location.split(",")
    if len(parts) == 2 and len(parts[1]) == 2 and not parts[1].isdigit():
        # Looks like "City,ST" format - add US country code
        normalized_location = f"{normalized_location},US"

    logger.debug(f"Normalized location: {normalized_location}")

    url = "http://api.openweathermap.org/geo/1.0/direct"
    params = {
        "q": normalized_location,
        "limit": 1,
        "appid": OPENWEATHER_API_KEY
    }

    # OPTIMIZATION: Use global HTTP client with timing
    import time
    start = time.time()
    status = "success"
    try:
        response = await http_client.get(url, params=params)
        response.raise_for_status()
        data = response.json()
    except Exception as e:
        status = "error"
        raise
    finally:
        record_external_api_call(SERVICE_NAME, "openweather_geocode", status, time.time() - start)

    if not data:
        raise ValueError(f"Location not found: {location}")

    result = data[0]
    return {
        "lat": result["lat"],
        "lon": result["lon"],
        "name": result["name"],
        "country": result.get("country", "")
    }

@cached(ttl=300, key_prefix="weather")  # Cache for 5 minutes
async def get_current_weather(lat: float, lon: float) -> Dict[str, Any]:
    """
    Get current weather for coordinates.

    Args:
        lat: Latitude
        lon: Longitude

    Returns:
        Current weather data
    """
    logger.info(f"Fetching current weather for lat={lat}, lon={lon}")

    url = "https://api.openweathermap.org/data/2.5/weather"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "imperial"  # Fahrenheit
    }

    # OPTIMIZATION: Use global HTTP client with timing
    import time
    start = time.time()
    status = "success"
    try:
        response = await http_client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        status = "error"
        raise
    finally:
        record_external_api_call(SERVICE_NAME, "openweather_current", status, time.time() - start)

@cached(ttl=600, key_prefix="forecast")  # Cache for 10 minutes
async def get_weather_forecast(lat: float, lon: float, days: int = 5) -> Dict[str, Any]:
    """
    Get weather forecast for coordinates.

    Args:
        lat: Latitude
        lon: Longitude
        days: Number of days (max 5 for free tier)

    Returns:
        Forecast data
    """
    logger.info(f"Fetching {days}-day forecast for lat={lat}, lon={lon}")

    url = "https://api.openweathermap.org/data/2.5/forecast"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "imperial",  # Fahrenheit
        "cnt": days * 8  # 8 data points per day (every 3 hours)
    }

    # OPTIMIZATION: Use global HTTP client with timing
    import time
    start = time.time()
    status = "success"
    try:
        response = await http_client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        status = "error"
        raise
    finally:
        record_external_api_call(SERVICE_NAME, "openweather_forecast", status, time.time() - start)

@app.get("/weather/current")
async def current_weather(
    location: str = Query(..., description="City name (e.g., 'Los Angeles, CA')")
):
    """Get current weather for a location."""
    try:
        # Geocode location
        coords = await geocode_location(location)

        # Get weather
        weather = await get_current_weather(coords["lat"], coords["lon"])

        # Format response
        return {
            "location": {
                "name": coords["name"],
                "country": coords["country"],
                "lat": coords["lat"],
                "lon": coords["lon"]
            },
            "current": {
                "temperature": weather["main"]["temp"],
                "feels_like": weather["main"]["feels_like"],
                "humidity": weather["main"]["humidity"],
                "description": weather["weather"][0]["description"],
                "wind_speed": weather["wind"]["speed"]
            },
            "timestamp": weather["dt"]
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenWeatherMap API error: {e}")
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/weather/forecast")
async def weather_forecast(
    location: str = Query(..., description="City name (e.g., 'Los Angeles, CA')"),
    days: int = Query(5, ge=1, le=5, description="Number of days (1-5)")
):
    """Get weather forecast for a location."""
    try:
        # Geocode location
        coords = await geocode_location(location)

        # Get forecast
        forecast = await get_weather_forecast(coords["lat"], coords["lon"], days)

        # Format response
        return {
            "location": {
                "name": coords["name"],
                "country": coords["country"],
                "lat": coords["lat"],
                "lon": coords["lon"]
            },
            "forecast": [
                {
                    "timestamp": item["dt"],
                    "temperature": item["main"]["temp"],
                    "description": item["weather"][0]["description"],
                    "humidity": item["main"]["humidity"],
                    "wind_speed": item["wind"]["speed"]
                }
                for item in forecast["list"]
            ]
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"OpenWeatherMap API error: {e}")
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Weather RAG service on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
