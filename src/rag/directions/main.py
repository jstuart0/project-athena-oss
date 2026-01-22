"""Directions RAG Service - Google Maps Directions API Integration

Provides navigation and routing functionality with waypoint support.

Endpoints:
- GET /health - Health check
- GET /directions/route - Get directions between two points
- POST /directions/route-with-stops - Get directions with intermediate stops
- GET /directions/search-along-route - Search for places along a route
- GET /settings - Get current settings
"""

import os
import sys
import json
import hashlib
from typing import Dict, Any, Optional, List
from urllib.parse import quote, urlencode
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
import httpx
from contextlib import asynccontextmanager
from pydantic import BaseModel

# Import shared utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient, cached
from shared.service_registry import startup_service, unregister_service
from shared.logging_config import configure_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
logger = configure_logging("directions-rag")

SERVICE_NAME = "directions-rag"

# Environment variables (fallback if database unavailable)
GOOGLE_DIRECTIONS_API_KEY = os.getenv("GOOGLE_DIRECTIONS_API_KEY", "")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "http://localhost:8080")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("DIRECTIONS_PORT", "8030"))

# Cache client, HTTP client, admin client
cache = None
http_client = None
admin_client = None

# Settings from admin (loaded at startup)
SETTINGS: Dict[str, Any] = {}

# Base Knowledge (home location)
BASE_KNOWLEDGE: Dict[str, str] = {}


class RouteStop(BaseModel):
    """Model for a stop along the route."""
    type: str  # "category" or "place"
    value: str  # Category name or place name/address
    position: Optional[str] = "halfway"  # beginning, quarter, halfway, three_quarters, end
    brand: Optional[str] = None  # Brand preference (e.g., "Starbucks")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache, http_client, admin_client, GOOGLE_DIRECTIONS_API_KEY, GOOGLE_PLACES_API_KEY, SETTINGS, BASE_KNOWLEDGE

    # Startup
    logger.info("Starting Directions RAG service")

    # Register service in registry
    await startup_service("directions", SERVICE_PORT, "Directions Service")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API
    try:
        api_config = await admin_client.get_external_api_key("google-directions")
        if api_config and api_config.get("api_key"):
            GOOGLE_DIRECTIONS_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="google-directions")
        else:
            logger.warning("api_key_missing", service="google-directions")

        # Also try to get Places API key (may be same or different)
        places_config = await admin_client.get_external_api_key("google-places")
        if places_config and places_config.get("api_key"):
            GOOGLE_PLACES_API_KEY = places_config["api_key"]
            logger.info("api_key_from_admin", service="google-places")
        else:
            # Fall back to directions key if places not set
            GOOGLE_PLACES_API_KEY = GOOGLE_DIRECTIONS_API_KEY
            logger.info("places_key_fallback_to_directions")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e))
        logger.info("api_key_from_env_fallback")

    # Fetch settings from admin
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ADMIN_API_URL}/api/directions-settings/public")
            if response.status_code == 200:
                SETTINGS = response.json()
                logger.info("settings_loaded", count=len(SETTINGS))
            else:
                logger.warning("settings_fetch_failed", status=response.status_code)
                load_default_settings()
    except Exception as e:
        logger.warning("settings_fetch_error", error=str(e))
        load_default_settings()

    # Fetch base knowledge for default origin
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{ADMIN_API_URL}/api/base-knowledge/public")
            if response.status_code == 200:
                data = response.json()
                BASE_KNOWLEDGE = {item["key"]: item["value"] for item in data.get("items", [])}
                logger.info("base_knowledge_loaded", keys=list(BASE_KNOWLEDGE.keys()))
    except Exception as e:
        logger.warning("base_knowledge_fetch_error", error=str(e))

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # Initialize HTTP client
    http_client = httpx.AsyncClient(timeout=30.0)
    logger.info("HTTP client initialized")

    yield

    # Shutdown
    logger.info("Shutting down Directions RAG service")
    await unregister_service("directions")

    if http_client:
        await http_client.aclose()
    if cache:
        await cache.disconnect()
    if admin_client:
        await admin_client.close()


def load_default_settings():
    """Load default settings if admin fetch fails."""
    global SETTINGS
    SETTINGS = {
        "default_travel_mode": "driving",
        "default_transit_mode": "train",
        "include_traffic": False,
        "cache_ttl_seconds": 300,
        "offer_sms": True,
        "include_step_details": False,
        "google_maps_link": True,
        "max_alternatives": 1,
        "waypoints_enabled": True,
        "max_waypoints": 3,
        "default_stop_position": "halfway",
        "places_search_radius_meters": 5000,
        "prefer_chain_restaurants": False,
        "min_rating_for_stops": 4.0,
    }
    logger.info("default_settings_loaded")


app = FastAPI(
    title="Directions RAG Service",
    description="Google Maps Directions API integration with waypoint support",
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
        "service": "directions-rag",
        "version": "1.0.0",
        "api_key_configured": bool(GOOGLE_DIRECTIONS_API_KEY),
        "settings_loaded": len(SETTINGS) > 0,
        "base_knowledge_loaded": len(BASE_KNOWLEDGE) > 0,
    }


@app.get("/settings")
async def get_settings():
    """Get current settings."""
    return {"settings": SETTINGS}


def get_default_origin() -> str:
    """Get default origin from base knowledge."""
    # Try various keys that might contain address
    for key in ["address", "home_address", "default_location", "location"]:
        if key in BASE_KNOWLEDGE:
            return BASE_KNOWLEDGE[key]

    # Construct from city/state if available
    if "city" in BASE_KNOWLEDGE and "state" in BASE_KNOWLEDGE:
        return f"{BASE_KNOWLEDGE['city']}, {BASE_KNOWLEDGE['state']}"

    # Fallback
    return "Baltimore, MD"


def generate_google_maps_url(
    origin: str,
    destination: str,
    mode: str = "driving",
    waypoints: Optional[List[str]] = None
) -> str:
    """Generate Google Maps URL for directions."""
    base_url = "https://www.google.com/maps/dir"

    # Build path: origin/waypoint1/waypoint2/.../destination
    parts = [quote(origin)]
    if waypoints:
        for wp in waypoints:
            parts.append(quote(wp))
    parts.append(quote(destination))

    url = f"{base_url}/{'/'.join(parts)}"

    # Add travel mode as data parameter
    mode_map = {"driving": "!4m2!4m1!3e0", "walking": "!4m2!4m1!3e2", "bicycling": "!4m2!4m1!3e1", "transit": "!4m2!4m1!3e3"}
    if mode in mode_map:
        url += f"/data={mode_map[mode]}"

    return url


def get_cache_key(endpoint: str, params: Dict[str, Any]) -> str:
    """Generate cache key for API request."""
    param_str = json.dumps(params, sort_keys=True)
    hash_str = hashlib.md5(f"{endpoint}:{param_str}".encode()).hexdigest()
    return f"directions:{endpoint}:{hash_str}"


@app.get("/directions/route")
async def get_directions(
    destination: str = Query(..., description="Destination address or place name"),
    origin: Optional[str] = Query(None, description="Starting location (defaults to home)"),
    mode: Optional[str] = Query(None, description="Travel mode: driving, walking, bicycling, transit"),
    transit_mode: Optional[str] = Query(None, description="Transit mode: bus, train, subway, tram"),
    departure_time: Optional[str] = Query(None, description="Departure time (ISO format or 'now')"),
    avoid: Optional[str] = Query(None, description="Features to avoid: tolls, highways, ferries"),
):
    """Get directions from origin to destination."""
    if not GOOGLE_DIRECTIONS_API_KEY:
        raise HTTPException(status_code=503, detail="Google Directions API key not configured")

    # Apply defaults
    actual_origin = origin or get_default_origin()
    actual_mode = mode or SETTINGS.get("default_travel_mode", "driving")

    # Build API request
    params = {
        "origin": actual_origin,
        "destination": destination,
        "mode": actual_mode,
        "key": GOOGLE_DIRECTIONS_API_KEY,
    }

    if actual_mode == "transit":
        transit = transit_mode or SETTINGS.get("default_transit_mode", "train")
        params["transit_mode"] = transit

    if departure_time:
        if departure_time.lower() == "now":
            import time
            params["departure_time"] = int(time.time())
        else:
            params["departure_time"] = departure_time

    if avoid:
        params["avoid"] = avoid

    if SETTINGS.get("include_traffic", False) and actual_mode == "driving":
        params["traffic_model"] = "best_guess"
        if "departure_time" not in params:
            import time
            params["departure_time"] = int(time.time())

    # Check cache
    cache_key = get_cache_key("route", {k: v for k, v in params.items() if k != "key"})
    cached_result = await cache.get(cache_key) if cache else None

    if cached_result:
        logger.info("cache_hit", cache_key=cache_key)
        # Cache already returns dict, no need to parse JSON
        return cached_result if isinstance(cached_result, dict) else json.loads(cached_result)

    # Call Google Directions API
    try:
        url = "https://maps.googleapis.com/maps/api/directions/json"
        response = await http_client.get(url, params=params)
        data = response.json()

        if data.get("status") != "OK":
            error_msg = data.get("error_message", data.get("status", "Unknown error"))
            logger.error("directions_api_error", status=data.get("status"), message=error_msg)
            raise HTTPException(status_code=400, detail=f"Directions API error: {error_msg}")

        # Extract route info
        route = data["routes"][0]
        leg = route["legs"][0]

        result = {
            "origin": leg["start_address"],
            "destination": leg["end_address"],
            "distance": leg["distance"]["text"],
            "distance_meters": leg["distance"]["value"],
            "duration": leg["duration"]["text"],
            "duration_seconds": leg["duration"]["value"],
            "mode": actual_mode,
            "steps_summary": extract_steps_summary(leg.get("steps", []), actual_mode),
        }

        # Add traffic info if available
        if "duration_in_traffic" in leg:
            result["duration_in_traffic"] = leg["duration_in_traffic"]["text"]
            result["duration_in_traffic_seconds"] = leg["duration_in_traffic"]["value"]

        # Add transit details if applicable
        if actual_mode == "transit":
            result["transit_details"] = extract_transit_details(leg.get("steps", []))

        # Add Google Maps URL
        if SETTINGS.get("google_maps_link", True):
            result["google_maps_url"] = generate_google_maps_url(
                actual_origin, destination, actual_mode
            )

        # Store polyline for waypoint searches
        result["_polyline"] = route.get("overview_polyline", {}).get("points", "")

        # Cache result
        ttl = SETTINGS.get("cache_ttl_seconds", 300)
        if cache:
            await cache.set(cache_key, result, ttl=ttl)
            logger.info("cache_set", cache_key=cache_key, ttl=ttl)

        return result

    except httpx.HTTPError as e:
        logger.error("directions_api_http_error", error=str(e))
        raise HTTPException(status_code=502, detail=f"Failed to reach Directions API: {str(e)}")


def extract_steps_summary(steps: List[Dict], mode: str) -> List[str]:
    """Extract human-readable summary of route steps."""
    summary = []

    for step in steps:
        if mode == "transit" and "transit_details" in step:
            transit = step["transit_details"]
            line = transit.get("line", {})
            vehicle = line.get("vehicle", {}).get("name", "Transit")
            short_name = line.get("short_name", line.get("name", ""))
            departure = transit.get("departure_stop", {}).get("name", "")
            arrival = transit.get("arrival_stop", {}).get("name", "")
            num_stops = transit.get("num_stops", 0)

            if short_name:
                summary.append(f"Take {vehicle} {short_name} from {departure} to {arrival} ({num_stops} stops)")
            else:
                summary.append(f"Take {vehicle} from {departure} to {arrival}")
        else:
            # Strip HTML from instructions
            import re
            instruction = re.sub(r'<[^>]+>', '', step.get("html_instructions", ""))
            if instruction:
                summary.append(instruction)

    return summary[:5]  # Limit to 5 steps for voice


def extract_transit_details(steps: List[Dict]) -> List[Dict]:
    """Extract transit details from route steps."""
    details = []

    for step in steps:
        if "transit_details" in step:
            transit = step["transit_details"]
            line = transit.get("line", {})

            details.append({
                "type": line.get("vehicle", {}).get("name", "Transit"),
                "line_name": line.get("short_name") or line.get("name", ""),
                "departure_stop": transit.get("departure_stop", {}).get("name", ""),
                "departure_time": transit.get("departure_time", {}).get("text", ""),
                "arrival_stop": transit.get("arrival_stop", {}).get("name", ""),
                "arrival_time": transit.get("arrival_time", {}).get("text", ""),
                "num_stops": transit.get("num_stops", 0),
            })

    return details


@app.post("/directions/route-with-stops")
async def get_route_with_stops(
    destination: str = Query(..., description="Final destination"),
    origin: Optional[str] = Query(None, description="Starting location"),
    mode: Optional[str] = Query(None, description="Travel mode"),
    stops: str = Query("[]", description="JSON array of stops"),
):
    """Get directions with intermediate stops."""
    if not SETTINGS.get("waypoints_enabled", True):
        # Fall back to regular directions
        return await get_directions(destination=destination, origin=origin, mode=mode)

    try:
        stop_list = json.loads(stops)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid stops JSON")

    if not stop_list:
        return await get_directions(destination=destination, origin=origin, mode=mode)

    # Limit waypoints
    max_waypoints = SETTINGS.get("max_waypoints", 3)
    if len(stop_list) > max_waypoints:
        logger.warning("waypoints_limited", requested=len(stop_list), max=max_waypoints)
        stop_list = stop_list[:max_waypoints]

    # First, get the base route to get the polyline
    base_route = await get_directions(destination=destination, origin=origin, mode=mode)
    polyline = base_route.get("_polyline", "")

    if not polyline:
        logger.warning("no_polyline_for_waypoints")
        return base_route

    # Resolve waypoints
    resolved_waypoints = []
    for stop in stop_list:
        if stop.get("type") == "place":
            # Use the place name directly
            resolved_waypoints.append(stop["value"])
        elif stop.get("type") == "category":
            # Search for place along route
            place = await search_place_along_route(
                polyline=polyline,
                category=stop["value"],
                position=stop.get("position", SETTINGS.get("default_stop_position", "halfway")),
                brand=stop.get("brand"),
            )
            if place:
                resolved_waypoints.append(place["address"])

    if not resolved_waypoints:
        return base_route

    # Get new route with waypoints
    actual_origin = origin or get_default_origin()
    actual_mode = mode or SETTINGS.get("default_travel_mode", "driving")

    params = {
        "origin": actual_origin,
        "destination": destination,
        "mode": actual_mode,
        "waypoints": "|".join(resolved_waypoints),
        "key": GOOGLE_DIRECTIONS_API_KEY,
    }

    try:
        url = "https://maps.googleapis.com/maps/api/directions/json"
        response = await http_client.get(url, params=params)
        data = response.json()

        if data.get("status") != "OK":
            logger.warning("waypoint_route_failed", status=data.get("status"))
            return base_route

        route = data["routes"][0]

        # Build result with multiple legs
        result = {
            "origin": route["legs"][0]["start_address"],
            "destination": route["legs"][-1]["end_address"],
            "waypoints": [
                {"address": wp, "resolved": True}
                for wp in resolved_waypoints
            ],
            "legs": [],
            "total_distance": "",
            "total_duration": "",
            "mode": actual_mode,
        }

        total_distance = 0
        total_duration = 0

        for i, leg in enumerate(route["legs"]):
            leg_info = {
                "start": leg["start_address"],
                "end": leg["end_address"],
                "distance": leg["distance"]["text"],
                "duration": leg["duration"]["text"],
            }
            result["legs"].append(leg_info)
            total_distance += leg["distance"]["value"]
            total_duration += leg["duration"]["value"]

        # Format totals
        result["total_distance"] = format_distance(total_distance)
        result["total_duration"] = format_duration(total_duration)
        result["total_distance_meters"] = total_distance
        result["total_duration_seconds"] = total_duration

        # Add Google Maps URL with waypoints
        if SETTINGS.get("google_maps_link", True):
            result["google_maps_url"] = generate_google_maps_url(
                actual_origin, destination, actual_mode, resolved_waypoints
            )

        return result

    except Exception as e:
        logger.error("waypoint_route_error", error=str(e))
        return base_route


async def search_place_along_route(
    polyline: str,
    category: str,
    position: str = "halfway",
    brand: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Search for a place along the route at the given position."""
    from .polyline_utils import decode_polyline, get_point_at_fraction
    from .categories import get_place_types, get_position_fraction

    if not GOOGLE_PLACES_API_KEY:
        logger.warning("places_api_key_not_configured")
        return None

    try:
        # Decode polyline to get route points
        points = decode_polyline(polyline)
        if not points:
            return None

        # Get the point at the desired position
        fraction = get_position_fraction(position)
        lat, lng = get_point_at_fraction(points, fraction)

        # Get place types for category
        place_types = get_place_types(category)

        # Search using Places API (New)
        radius = SETTINGS.get("places_search_radius_meters", 5000)
        min_rating = float(SETTINGS.get("min_rating_for_stops", 4.0))

        # Build search request
        search_data = {
            "includedTypes": place_types,
            "maxResultCount": 5,
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": lat, "longitude": lng},
                    "radius": radius
                }
            }
        }

        if brand:
            search_data["textQuery"] = brand

        headers = {
            "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.rating,places.location"
        }

        response = await http_client.post(
            "https://places.googleapis.com/v1/places:searchNearby",
            json=search_data,
            headers=headers,
        )

        data = response.json()
        places = data.get("places", [])

        # Filter by rating
        suitable_places = [
            p for p in places
            if p.get("rating", 0) >= min_rating
        ]

        if not suitable_places:
            suitable_places = places

        if suitable_places:
            place = suitable_places[0]
            return {
                "name": place.get("displayName", {}).get("text", "Unknown"),
                "address": place.get("formattedAddress", ""),
                "rating": place.get("rating"),
                "location": place.get("location", {}),
            }

        return None

    except Exception as e:
        logger.error("place_search_error", error=str(e), category=category)
        return None


@app.get("/directions/search-along-route")
async def search_along_route(
    polyline: str = Query(..., description="Encoded polyline from a previous route"),
    category: str = Query(..., description="Category to search for (food, gas, coffee, etc.)"),
    position: str = Query("halfway", description="Position along route"),
    brand: Optional[str] = Query(None, description="Brand preference"),
):
    """Search for places along an existing route."""
    place = await search_place_along_route(polyline, category, position, brand)

    if not place:
        raise HTTPException(status_code=404, detail=f"No {category} found along route")

    return {
        "place": place,
        "search_category": category,
        "search_position": position,
        "brand": brand,
    }


def format_distance(meters: int) -> str:
    """Format distance in human-readable form."""
    miles = meters / 1609.34
    if miles < 0.1:
        feet = meters * 3.28084
        return f"{int(feet)} ft"
    return f"{miles:.1f} mi"


def format_duration(seconds: int) -> str:
    """Format duration in human-readable form."""
    if seconds < 60:
        return f"{seconds} sec"
    elif seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} min"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes:
            return f"{hours} hr {minutes} min"
        return f"{hours} hr"


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
