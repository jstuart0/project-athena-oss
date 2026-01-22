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
from shared.cache import cached
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.service_registry import register_service, unregister_service
from shared.metrics import setup_metrics_endpoint

setup_logging(service_name="flights-rag")
logger = structlog.get_logger()

SERVICE_NAME = "flights"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8013"))  # Default port

FLIGHTAWARE_API_KEY = os.getenv("FLIGHTAWARE_API_KEY", "")
FLIGHTAWARE_BASE_URL = "https://aeroapi.flightaware.com/aeroapi"
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, admin_client, FLIGHTAWARE_API_KEY
    logger.info("flights_service.startup")

    # Register in service registry
    try:
        await register_service(SERVICE_NAME, SERVICE_PORT, "Flight tracking and airline information")
        logger.info(f"Service registered: {SERVICE_NAME} on port {SERVICE_PORT}")
    except Exception as e:
        logger.error(f"Failed to register service: {e}")

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

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        headers={"x-apikey": FLIGHTAWARE_API_KEY} if FLIGHTAWARE_API_KEY else {}
    )
    yield
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

app = FastAPI(title="Flights RAG Service", version="1.0.0", lifespan=lifespan)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@cached(ttl=300)
async def search_flights(query: str, max_results: int = 20) -> Dict[str, Any]:
    """Search for flights by destination airport code or flight number."""
    if not FLIGHTAWARE_API_KEY:
        raise ValueError("FlightAware API key not configured")

    # Check if query is an airport code (3 letters) or flight number
    if len(query) == 3 and query.isalpha():
        # Search for flights to/from this airport
        # Use the airports endpoint to get flights
        try:
            response = await http_client.get(
                f"{FLIGHTAWARE_BASE_URL}/airports/{query.upper()}/flights",
                params={"max_pages": 1, "type": "arrivals"}
            )
            response.raise_for_status()
        except Exception:
            # If that fails, try as a flight number
            response = await http_client.get(
                f"{FLIGHTAWARE_BASE_URL}/flights/{query}",
                params={"max_pages": 1}
            )
            response.raise_for_status()
    else:
        # Assume it's a flight number
        response = await http_client.get(
            f"{FLIGHTAWARE_BASE_URL}/flights/{query}",
            params={"max_pages": 1}
        )
        response.raise_for_status()

    data = response.json()
    flights = []

    # Handle different response structures
    if "flights" in data:
        flight_list = data.get("flights", [])
    elif "arrivals" in data:
        flight_list = data.get("arrivals", [])
    elif "scheduled" in data:
        flight_list = data.get("scheduled", [])
    else:
        flight_list = []

    for f in flight_list[:max_results]:
        flights.append({
            "ident": f.get("ident"),
            "fa_flight_id": f.get("fa_flight_id"),
            "origin": f.get("origin", {}).get("code") if isinstance(f.get("origin"), dict) else f.get("origin"),
            "destination": f.get("destination", {}).get("code") if isinstance(f.get("destination"), dict) else f.get("destination"),
            "filed_departure_time": f.get("filed_departure_time", {}).get("epoch") if isinstance(f.get("filed_departure_time"), dict) else f.get("filed_departure_time"),
            "estimated_arrival_time": f.get("estimated_arrival_time", {}).get("epoch") if isinstance(f.get("estimated_arrival_time"), dict) else f.get("estimated_arrival_time"),
            "status": f.get("status")
        })

    return {"flights": flights, "total": len(flights)}

@cached(ttl=60)
async def get_flight_status(flight_id: str) -> Dict[str, Any]:
    if not FLIGHTAWARE_API_KEY:
        raise ValueError("FlightAware API key not configured")
    response = await http_client.get(f"{FLIGHTAWARE_BASE_URL}/flights/{flight_id}")
    response.raise_for_status()
    f = response.json()
    return {
        "ident": f.get("ident"),
        "fa_flight_id": f.get("fa_flight_id"),
        "origin": f.get("origin"),
        "destination": f.get("destination"),
        "filed_departure_time": f.get("filed_departure_time"),
        "actual_departure_time": f.get("actual_departure_time"),
        "estimated_arrival_time": f.get("estimated_arrival_time"),
        "actual_arrival_time": f.get("actual_arrival_time"),
        "status": f.get("status"),
        "aircraft_type": f.get("aircraft_type")
    }

@cached(ttl=3600)
async def get_airport_info(airport_code: str) -> Dict[str, Any]:
    if not FLIGHTAWARE_API_KEY:
        raise ValueError("FlightAware API key not configured")
    response = await http_client.get(f"{FLIGHTAWARE_BASE_URL}/airports/{airport_code.upper()}")
    response.raise_for_status()
    a = response.json()
    return {
        "code": a.get("code"),
        "name": a.get("name"),
        "city": a.get("city"),
        "timezone": a.get("timezone"),
        "elevation": a.get("elevation"),
        "latitude": a.get("latitude"),
        "longitude": a.get("longitude")
    }

@app.get("/health")
async def health_check():
    return JSONResponse(status_code=200, content={"status": "healthy", "service": "flights-rag"})

@app.get("/flights/search")
async def search(query: str = Query(...), max_results: int = Query(20, ge=1, le=50)):
    try:
        return await search_flights(query, max_results)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"FlightAware API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/flights/{flight_id}")
async def get_status(flight_id: str):
    try:
        return await get_flight_status(flight_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"FlightAware API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/airports/{airport_code}")
async def get_airport(airport_code: str):
    try:
        return await get_airport_info(airport_code)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"FlightAware API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True, log_config=None)
