"""OneCall Weather RAG Service - OpenWeatherMap OneCall API 3.0 Integration

Provides comprehensive weather data with caching support.

Endpoints:
- GET /health - Health check
- GET /weather/onecall?location={location} - Full weather data (current + forecast + alerts)
- GET /weather/current?location={location} - Current weather only
- GET /weather/forecast?location={location}&days={days} - Daily forecast (up to 8 days)
- GET /weather/hourly?location={location}&hours={hours} - Hourly forecast (up to 48 hours)
- GET /weather/alerts?location={location} - Weather alerts only
"""

import os
import sys
import time
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx
from contextlib import asynccontextmanager
from datetime import datetime

# Import shared utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient, cached
from shared.service_registry import startup_service, unregister_service
from shared.logging_config import configure_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint, record_external_api_call

# Configure logging
logger = configure_logging("onecall-rag")

SERVICE_NAME = "onecall-rag"

# Environment variables (fallback if database unavailable)
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8021"))

# Cache client, HTTP client, and admin client
cache = None
http_client = None
admin_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache, http_client, admin_client, OPENWEATHER_API_KEY

    # Startup
    logger.info("Starting OneCall Weather RAG service")

    # Register service in registry (kills stale process on port if any)
    await startup_service("onecall", SERVICE_PORT, "OneCall Weather Service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    # Note: OneCall 3.0 requires separate subscription, may use different key
    try:
        api_config = await admin_client.get_external_api_key("openweather-onecall")
        if api_config and api_config.get("api_key"):
            OPENWEATHER_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="openweather-onecall")
        else:
            # Fallback to standard openweather key
            api_config = await admin_client.get_external_api_key("openweather")
            if api_config and api_config.get("api_key"):
                OPENWEATHER_API_KEY = api_config["api_key"]
                logger.info("api_key_from_admin_fallback", service="openweather")
            else:
                logger.info("api_key_from_env", service="openweather")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="openweather-onecall")
        logger.info("api_key_from_env_fallback", service="openweather")

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # Create reusable HTTP client
    http_client = httpx.AsyncClient(timeout=15.0)
    logger.info("HTTP client initialized")

    yield

    # Shutdown
    logger.info("Shutting down OneCall Weather RAG service")

    # Unregister from service registry
    await unregister_service("onecall")

    if http_client:
        await http_client.aclose()
    if cache:
        await cache.disconnect()
    if admin_client:
        await admin_client.close()


app = FastAPI(
    title="OneCall Weather RAG Service",
    description="OpenWeatherMap OneCall API 3.0 integration with caching",
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
        "service": "onecall-rag",
        "version": "1.0.0",
        "api": "OpenWeatherMap OneCall 3.0"
    }


@cached(ttl=600, key_prefix="geocode")  # Cache for 10 minutes
async def geocode_location(location: str) -> Dict[str, Any]:
    """
    Geocode location name to lat/lon coordinates.

    Reuses same geocoding logic as standard weather service.
    """
    logger.info(f"Geocoding location: {location}")

    # Handle full street addresses by extracting city/state
    import re
    parts = [p.strip() for p in location.split(",")]
    if len(parts) >= 3:
        city = parts[-2]
        state_part = parts[-1]
        state_match = re.match(r'^([A-Z]{2})\s*\d*$', state_part.strip())
        if state_match:
            state = state_match.group(1)
            location = f"{city}, {state}"
            logger.info(f"Extracted city/state from address: {location}")

    # Normalize location format
    normalized_location = location.replace(", ", ",").replace(" ,", ",")
    parts = normalized_location.split(",")
    if len(parts) == 2 and len(parts[1]) == 2 and not parts[1].isdigit():
        normalized_location = f"{normalized_location},US"

    url = "http://api.openweathermap.org/geo/1.0/direct"
    params = {
        "q": normalized_location,
        "limit": 1,
        "appid": OPENWEATHER_API_KEY
    }

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
        "country": result.get("country", ""),
        "state": result.get("state", "")
    }


@cached(ttl=300, key_prefix="onecall")  # Cache for 5 minutes
async def get_onecall_data(
    lat: float,
    lon: float,
    exclude: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get comprehensive weather data from OneCall API 3.0.

    Args:
        lat: Latitude
        lon: Longitude
        exclude: Comma-separated list to exclude (minutely,hourly,daily,alerts)

    Returns:
        Full OneCall response with current, minutely, hourly, daily, alerts
    """
    logger.info(f"Fetching OneCall data for lat={lat}, lon={lon}, exclude={exclude}")

    url = "https://api.openweathermap.org/data/3.0/onecall"
    params = {
        "lat": lat,
        "lon": lon,
        "appid": OPENWEATHER_API_KEY,
        "units": "imperial"  # Fahrenheit
    }
    if exclude:
        params["exclude"] = exclude

    start = time.time()
    status = "success"
    try:
        response = await http_client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        status = "error"
        if e.response.status_code == 401:
            logger.error("OneCall API key invalid or subscription not active")
            raise HTTPException(status_code=502, detail="OneCall API subscription required")
        raise
    except Exception as e:
        status = "error"
        raise
    finally:
        record_external_api_call(SERVICE_NAME, "openweather_onecall", status, time.time() - start)


def format_current_weather(data: Dict[str, Any], location_info: Dict[str, Any]) -> Dict[str, Any]:
    """Format current weather for voice response."""
    current = data.get("current", {})
    weather = current.get("weather", [{}])[0]

    return {
        "location": location_info,
        "current": {
            "temperature": current.get("temp"),
            "feels_like": current.get("feels_like"),
            "humidity": current.get("humidity"),
            "description": weather.get("description", ""),
            "wind_speed": current.get("wind_speed"),
            "uvi": current.get("uvi"),
            "visibility": current.get("visibility"),
            "pressure": current.get("pressure"),
            "dew_point": current.get("dew_point"),
            "clouds": current.get("clouds")
        },
        "timestamp": current.get("dt")
    }


def format_daily_forecast(data: Dict[str, Any], location_info: Dict[str, Any], days: int = 8) -> Dict[str, Any]:
    """Format daily forecast for voice response."""
    daily = data.get("daily", [])[:days]

    forecast = []
    for day in daily:
        weather = day.get("weather", [{}])[0]
        forecast.append({
            "timestamp": day.get("dt"),
            "date": datetime.fromtimestamp(day.get("dt", 0)).strftime("%A, %B %d"),
            "temp_high": day.get("temp", {}).get("max"),
            "temp_low": day.get("temp", {}).get("min"),
            "description": weather.get("description", ""),
            "humidity": day.get("humidity"),
            "wind_speed": day.get("wind_speed"),
            "pop": day.get("pop", 0) * 100,  # Probability of precipitation as %
            "rain": day.get("rain", 0),
            "snow": day.get("snow", 0),
            "uvi": day.get("uvi"),
            "summary": day.get("summary", "")  # AI-generated summary if available
        })

    return {
        "location": location_info,
        "forecast": forecast,
        "days": len(forecast)
    }


def format_hourly_forecast(data: Dict[str, Any], location_info: Dict[str, Any], hours: int = 24) -> Dict[str, Any]:
    """Format hourly forecast for voice response."""
    hourly = data.get("hourly", [])[:hours]

    forecast = []
    for hour in hourly:
        weather = hour.get("weather", [{}])[0]
        forecast.append({
            "timestamp": hour.get("dt"),
            "time": datetime.fromtimestamp(hour.get("dt", 0)).strftime("%I:%M %p"),
            "temperature": hour.get("temp"),
            "feels_like": hour.get("feels_like"),
            "description": weather.get("description", ""),
            "pop": hour.get("pop", 0) * 100,  # Probability of precipitation as %
            "humidity": hour.get("humidity"),
            "wind_speed": hour.get("wind_speed")
        })

    return {
        "location": location_info,
        "hourly": forecast,
        "hours": len(forecast)
    }


def format_alerts(data: Dict[str, Any], location_info: Dict[str, Any]) -> Dict[str, Any]:
    """Format weather alerts for voice response."""
    alerts = data.get("alerts", [])

    formatted_alerts = []
    for alert in alerts:
        formatted_alerts.append({
            "sender": alert.get("sender_name", ""),
            "event": alert.get("event", ""),
            "start": alert.get("start"),
            "end": alert.get("end"),
            "description": alert.get("description", ""),
            "tags": alert.get("tags", [])
        })

    return {
        "location": location_info,
        "alerts": formatted_alerts,
        "has_alerts": len(formatted_alerts) > 0
    }


@app.get("/weather/onecall")
async def onecall_weather(
    location: str = Query(..., description="City name (e.g., 'Baltimore, MD')"),
    exclude: Optional[str] = Query(None, description="Exclude: minutely,hourly,daily,alerts")
):
    """
    Get comprehensive weather data from OneCall API.

    Returns current conditions, minute forecast (1hr), hourly forecast (48hr),
    daily forecast (8 days), and government weather alerts.
    """
    try:
        coords = await geocode_location(location)
        data = await get_onecall_data(coords["lat"], coords["lon"], exclude)

        location_info = {
            "name": coords["name"],
            "country": coords["country"],
            "state": coords.get("state", ""),
            "lat": coords["lat"],
            "lon": coords["lon"]
        }

        return {
            "location": location_info,
            "current": format_current_weather(data, location_info)["current"],
            "minutely": data.get("minutely", [])[:60],  # Next 60 minutes
            "hourly": format_hourly_forecast(data, location_info, 48)["hourly"],
            "daily": format_daily_forecast(data, location_info, 8)["forecast"],
            "alerts": format_alerts(data, location_info)["alerts"],
            "timezone": data.get("timezone"),
            "data_source": "OpenWeatherMap OneCall 3.0"
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"OneCall API error: {e}")
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/weather/current")
async def current_weather(
    location: str = Query(..., description="City name (e.g., 'Baltimore, MD')")
):
    """Get current weather only (compatible with standard weather service)."""
    try:
        coords = await geocode_location(location)
        data = await get_onecall_data(
            coords["lat"],
            coords["lon"],
            exclude="minutely,hourly,daily,alerts"
        )

        location_info = {
            "name": coords["name"],
            "country": coords["country"],
            "state": coords.get("state", ""),
            "lat": coords["lat"],
            "lon": coords["lon"]
        }

        return format_current_weather(data, location_info)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"OneCall API error: {e}")
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/weather/forecast")
async def weather_forecast(
    location: str = Query(..., description="City name (e.g., 'Baltimore, MD')"),
    days: int = Query(5, ge=1, le=8, description="Number of days (1-8)")
):
    """Get daily forecast (compatible with standard weather service, but up to 8 days)."""
    try:
        coords = await geocode_location(location)
        data = await get_onecall_data(
            coords["lat"],
            coords["lon"],
            exclude="minutely,current,alerts"
        )

        location_info = {
            "name": coords["name"],
            "country": coords["country"],
            "state": coords.get("state", ""),
            "lat": coords["lat"],
            "lon": coords["lon"]
        }

        return format_daily_forecast(data, location_info, days)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"OneCall API error: {e}")
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/weather/hourly")
async def hourly_forecast(
    location: str = Query(..., description="City name (e.g., 'Baltimore, MD')"),
    hours: int = Query(24, ge=1, le=48, description="Number of hours (1-48)")
):
    """Get hourly forecast (up to 48 hours)."""
    try:
        coords = await geocode_location(location)
        data = await get_onecall_data(
            coords["lat"],
            coords["lon"],
            exclude="minutely,daily,alerts"
        )

        location_info = {
            "name": coords["name"],
            "country": coords["country"],
            "state": coords.get("state", ""),
            "lat": coords["lat"],
            "lon": coords["lon"]
        }

        return format_hourly_forecast(data, location_info, hours)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"OneCall API error: {e}")
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/weather/alerts")
async def weather_alerts(
    location: str = Query(..., description="City name (e.g., 'Baltimore, MD')")
):
    """Get current weather alerts for location."""
    try:
        coords = await geocode_location(location)
        data = await get_onecall_data(
            coords["lat"],
            coords["lon"],
            exclude="minutely,hourly,daily,current"
        )

        location_info = {
            "name": coords["name"],
            "country": coords["country"],
            "state": coords.get("state", ""),
            "lat": coords["lat"],
            "lon": coords["lon"]
        }

        return format_alerts(data, location_info)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"OneCall API error: {e}")
        raise HTTPException(status_code=502, detail="Weather service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting OneCall Weather RAG service on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
