"""Transportation RAG Service - Region-Configurable Transit Integration

Provides transit data for an operator-configured region via two JSON env
vars: TRANSIT_GTFS_FEEDS (GTFS feed URLs) and TRANSIT_STATIC_SERVICES
(non-GTFS services with fixed schedules, e.g. a ferry or water taxi). See
.env.example for the schema and CONTRIBUTING.md's SSRF guard section for the
per-feed allow_private flag. Neither variable set means the service reports
"not configured" rather than serving any hardcoded region's data.

Endpoints:
- GET /health - Health check
- GET /transit/nearby?lat={lat}&lon={lon}&radius={radius} - Nearby stops
- GET /transit/routes?agency={agency} - List routes by agency
- GET /transit/departures?stop_id={stop_id}&limit={limit} - Next departures from stop
- GET /transit/route/{route_id} - Route details and schedule
- GET /transit/search?query={query} - Search stops and routes
- POST /transit/refresh - Refresh GTFS data
"""

import os
import sys
import csv
import io
import json
import zipfile
import asyncio
from datetime import datetime, time, timedelta
from typing import Dict, Any, List, NamedTuple, Optional, Tuple
from dataclasses import dataclass, asdict
from math import radians, sin, cos, sqrt, atan2

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
import httpx
from contextlib import asynccontextmanager
from bs4 import BeautifulSoup

# Import shared utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient
from shared.config import get_config
from shared.logging_config import configure_logging
from shared.metrics import setup_metrics_endpoint
from shared.url_safety import safe_get, SsrfBlockedError

# Configure logging
logger = configure_logging("transportation-rag")

SERVICE_NAME = "transportation-rag"

# Environment variables
REDIS_URL = get_config().redis_url
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8025"))

_BOUNDS_KEYS = ("min_lat", "max_lat", "min_lon", "max_lon")


class TransitConfig(NamedTuple):
    """Parsed TRANSIT_GTFS_FEEDS / TRANSIT_STATIC_SERVICES config.

    Never raises: a malformed value produces configured=False plus an error
    string naming the key and, where applicable, the feed/service id -- so
    one operator typo can only make this service report not-configured, not
    crash-loop the pod (every RAG deployment shares one ConfigMap via
    envFrom).
    """
    configured: bool
    feeds: Dict[str, Dict[str, Any]]
    static_services: Dict[str, Dict[str, Any]]
    region_name: str
    error: Optional[str]


def _validate_bounds(feed_id: str, bounds: Any) -> Optional[str]:
    """Validate a feed's optional `bounds` box. Returns an error string, or
    None when valid. Comparisons at filter time are exclusive at the edges,
    so bounds here are validated as strict min < max (2.2, R11)."""
    if not isinstance(bounds, dict) or set(bounds.keys()) != set(_BOUNDS_KEYS):
        return f"TRANSIT_GTFS_FEEDS: feed '{feed_id}' bounds must have exactly {sorted(_BOUNDS_KEYS)}"
    try:
        min_lat, max_lat, min_lon, max_lon = (float(bounds[k]) for k in _BOUNDS_KEYS)
    except (TypeError, ValueError):
        return f"TRANSIT_GTFS_FEEDS: feed '{feed_id}' bounds values must be numeric"
    if not (min_lat < max_lat):
        return f"TRANSIT_GTFS_FEEDS: feed '{feed_id}' bounds min_lat must be less than max_lat"
    if not (min_lon < max_lon):
        return f"TRANSIT_GTFS_FEEDS: feed '{feed_id}' bounds min_lon must be less than max_lon"
    return None


def load_transit_config(cfg) -> TransitConfig:
    """Parse TRANSIT_* config. Never raises."""
    region_name = cfg.transit_region_name
    feeds: Dict[str, Dict[str, Any]] = {}
    static_services: Dict[str, Dict[str, Any]] = {}

    feeds_raw = cfg.transit_gtfs_feeds
    if feeds_raw:
        try:
            parsed = json.loads(feeds_raw)
        except json.JSONDecodeError as e:
            return TransitConfig(False, {}, {}, region_name, f"TRANSIT_GTFS_FEEDS: invalid JSON: {e}")
        if not isinstance(parsed, dict):
            return TransitConfig(False, {}, {}, region_name, "TRANSIT_GTFS_FEEDS: must be a JSON object")
        for feed_id, feed in parsed.items():
            if not isinstance(feed, dict) or not feed.get("name"):
                return TransitConfig(False, {}, {}, region_name, f"TRANSIT_GTFS_FEEDS: feed '{feed_id}' missing 'name'")
            if not feed.get("url"):
                return TransitConfig(False, {}, {}, region_name, f"TRANSIT_GTFS_FEEDS: feed '{feed_id}' missing 'url'")
            bounds = feed.get("bounds")
            if bounds is not None:
                err = _validate_bounds(feed_id, bounds)
                if err:
                    return TransitConfig(False, {}, {}, region_name, err)
            feeds[feed_id] = feed

    static_raw = cfg.transit_static_services
    if static_raw:
        try:
            parsed = json.loads(static_raw)
        except json.JSONDecodeError as e:
            return TransitConfig(False, {}, {}, region_name, f"TRANSIT_STATIC_SERVICES: invalid JSON: {e}")
        if not isinstance(parsed, dict):
            return TransitConfig(False, {}, {}, region_name, "TRANSIT_STATIC_SERVICES: must be a JSON object")
        for service_id, svc in parsed.items():
            if not isinstance(svc, dict) or not svc.get("name"):
                return TransitConfig(False, {}, {}, region_name, f"TRANSIT_STATIC_SERVICES: service '{service_id}' missing 'name'")
            static_services[service_id] = svc

    configured = bool(feeds) or bool(static_services)
    return TransitConfig(configured, feeds, static_services, region_name, None)


transit_config: TransitConfig = load_transit_config(get_config())

# Per-feed fetch failures (e.g. SSRF-blocked), surfaced in /health's message.
fetch_errors: Dict[str, str] = {}

# Cache client
cache: Optional[CacheClient] = None

# In-memory transit data
transit_data: Dict[str, Any] = {
    "stops": {},       # stop_id -> stop info
    "routes": {},      # route_id -> route info
    "trips": {},       # trip_id -> trip info
    "stop_times": {},  # stop_id -> list of stop times
    "agencies": {},    # agency_id -> agency info
    "last_updated": None
}


@dataclass
class Stop:
    stop_id: str
    stop_name: str
    stop_lat: float
    stop_lon: float
    feed_id: str
    stop_type: str = "bus_stop"
    wheelchair_boarding: int = 0


@dataclass
class Route:
    route_id: str
    route_short_name: str
    route_long_name: str
    route_type: int
    feed_id: str
    agency_id: str = ""
    route_color: str = ""
    route_text_color: str = ""


@dataclass
class StopTime:
    trip_id: str
    stop_id: str
    arrival_time: str
    departure_time: str
    stop_sequence: int
    feed_id: str


def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance between two points in meters."""
    R = 6371000  # Earth's radius in meters
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return R * c


def parse_gtfs_time(time_str: str) -> Tuple[int, int, int]:
    """Parse GTFS time (can be > 24:00:00 for overnight trips)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1]), int(parts[2])


def normalize_time(time_str: str) -> str:
    """Normalize GTFS time to standard 24-hour format."""
    h, m, s = parse_gtfs_time(time_str)
    h = h % 24
    return f"{h:02d}:{m:02d}:{s:02d}"


def _in_bounds(bounds: Optional[Dict[str, float]], lat: float, lon: float) -> bool:
    """Exclusive-comparison bounds check. No bounds configured means no
    filtering (2.2, R11)."""
    if not bounds:
        return True
    return bounds["min_lat"] < lat < bounds["max_lat"] and bounds["min_lon"] < lon < bounds["max_lon"]


async def download_and_parse_gtfs(feed_id: str, feed_config: Dict[str, Any]) -> Dict[str, Any]:
    """Download and parse a GTFS feed."""
    logger.info(f"Downloading GTFS feed: {feed_id} from {feed_config['url']}")

    result = {
        "stops": [],
        "routes": [],
        "stop_times": [],
        "agencies": []
    }

    url = feed_config["url"]
    bounds = feed_config.get("bounds")
    max_bytes = feed_config.get("max_bytes", 100 * 2**20)

    # D13: operator-configured fetch URL. By default no
    # allowed_private_hosts is passed, so every hop -- including the first
    # -- is validated against the private-address block. allow_private:true
    # on this feed exempts only this feed's own hostname, on any hop; a
    # redirect to any OTHER private host is still blocked. Stricter than the
    # CONTRIBUTING.md Class 3 exemption because this fetches third-party
    # content and a hijacked upstream redirect shouldn't reach
    # cluster-internal addresses.
    safe_get_kwargs: Dict[str, Any] = {}
    if feed_config.get("allow_private"):
        safe_get_kwargs["allowed_private_hosts"] = [httpx.URL(url).host]

    try:
        response = await safe_get(
            url,
            max_hops=5,
            max_bytes=max_bytes,
            timeout=60.0,
            **safe_get_kwargs,
        )
        response.raise_for_status()
    except SsrfBlockedError as e:
        logger.warning("transit_feed_ssrf_blocked", feed_id=feed_id, reason=str(e))
        fetch_errors[feed_id] = str(e)
        return result
    except Exception as e:
        logger.error(f"Error downloading/parsing {feed_id}: {e}")
        fetch_errors[feed_id] = str(e)
        return result

    try:
        # Parse ZIP file
        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            file_list = zf.namelist()
            logger.info(f"GTFS {feed_id} contains: {file_list}")

            # Parse stops.txt
            if "stops.txt" in file_list:
                with zf.open("stops.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
                    for row in reader:
                        try:
                            stop = Stop(
                                stop_id=f"{feed_id}_{row['stop_id']}",
                                stop_name=row.get('stop_name', ''),
                                stop_lat=float(row.get('stop_lat', 0)),
                                stop_lon=float(row.get('stop_lon', 0)),
                                feed_id=feed_id,
                                stop_type=feed_config.get("type", "bus_stop"),
                                wheelchair_boarding=int(row.get('wheelchair_boarding', 0))
                            )
                            if _in_bounds(bounds, stop.stop_lat, stop.stop_lon):
                                result["stops"].append(asdict(stop))
                        except (ValueError, KeyError):
                            continue

            # Parse routes.txt
            if "routes.txt" in file_list:
                with zf.open("routes.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
                    for row in reader:
                        try:
                            route = Route(
                                route_id=f"{feed_id}_{row['route_id']}",
                                route_short_name=row.get('route_short_name', ''),
                                route_long_name=row.get('route_long_name', ''),
                                route_type=int(row.get('route_type', 3)),
                                feed_id=feed_id,
                                agency_id=row.get('agency_id', ''),
                                route_color=row.get('route_color', ''),
                                route_text_color=row.get('route_text_color', '')
                            )
                            result["routes"].append(asdict(route))
                        except (ValueError, KeyError):
                            continue

            # Parse stop_times.txt (limited for memory)
            if "stop_times.txt" in file_list:
                with zf.open("stop_times.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
                    count = 0
                    for row in reader:
                        if count >= 100000:  # Limit entries
                            break
                        try:
                            stop_time = StopTime(
                                trip_id=f"{feed_id}_{row['trip_id']}",
                                stop_id=f"{feed_id}_{row['stop_id']}",
                                arrival_time=row.get('arrival_time', ''),
                                departure_time=row.get('departure_time', ''),
                                stop_sequence=int(row.get('stop_sequence', 0)),
                                feed_id=feed_id
                            )
                            result["stop_times"].append(asdict(stop_time))
                            count += 1
                        except (ValueError, KeyError):
                            continue

            # Parse agency.txt
            if "agency.txt" in file_list:
                with zf.open("agency.txt") as f:
                    reader = csv.DictReader(io.TextIOWrapper(f, encoding='utf-8-sig'))
                    for row in reader:
                        result["agencies"].append({
                            "agency_id": f"{feed_id}_{row.get('agency_id', feed_id)}",
                            "agency_name": row.get('agency_name', feed_config['name']),
                            "agency_url": row.get('agency_url', ''),
                            "feed_id": feed_id
                        })

        logger.info(f"Parsed {feed_id}: {len(result['stops'])} stops, {len(result['routes'])} routes, {len(result['stop_times'])} stop_times")
        fetch_errors.pop(feed_id, None)
        return result

    except Exception as e:
        logger.error(f"Error downloading/parsing {feed_id}: {e}")
        fetch_errors[feed_id] = str(e)
        return result


async def load_gtfs_data():
    """Load all configured GTFS feeds and static services into memory."""
    global transit_data

    if not transit_config.configured:
        logger.warning("transit_not_configured")
        return

    logger.info("Loading GTFS data from all configured feeds...")

    all_stops = {}
    all_routes = {}
    all_stop_times = {}
    all_agencies = {}

    # Download and parse each feed
    tasks = []
    for feed_id, feed_config in transit_config.feeds.items():
        tasks.append(download_and_parse_gtfs(feed_id, feed_config))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for feed_id, result in zip(transit_config.feeds.keys(), results):
        if isinstance(result, Exception):
            logger.error(f"Failed to load {feed_id}: {result}")
            continue

        # Merge data
        for stop in result.get("stops", []):
            all_stops[stop["stop_id"]] = stop

        for route in result.get("routes", []):
            all_routes[route["route_id"]] = route

        for st in result.get("stop_times", []):
            stop_id = st["stop_id"]
            if stop_id not in all_stop_times:
                all_stop_times[stop_id] = []
            all_stop_times[stop_id].append(st)

        for agency in result.get("agencies", []):
            all_agencies[agency["agency_id"]] = agency

    # Add static-service stops (e.g. a ferry not modeled as GTFS)
    for service_id, service in transit_config.static_services.items():
        for i, stop in enumerate(service.get("stops", [])):
            stop_id = f"{service_id}_{i}"
            all_stops[stop_id] = {
                "stop_id": stop_id,
                "stop_name": stop["name"],
                "stop_lat": stop["lat"],
                "stop_lon": stop["lon"],
                "feed_id": service_id,
                "stop_type": "ferry_terminal",
                "wheelchair_boarding": 1,
                "service_info": {
                    "name": service["name"],
                    "free": service.get("free", False),
                    "hours": service.get("hours"),
                    "frequency_minutes": service.get("frequency_minutes")
                }
            }

    # Update global data
    transit_data = {
        "stops": all_stops,
        "routes": all_routes,
        "stop_times": all_stop_times,
        "agencies": all_agencies,
        "last_updated": datetime.now().isoformat()
    }

    # Cache summary stats
    if cache:
        await cache.set(
            "transportation:stats",
            {
                "total_stops": len(all_stops),
                "total_routes": len(all_routes),
                "total_stop_times": sum(len(v) for v in all_stop_times.values()),
                "last_updated": transit_data["last_updated"]
            },
            ttl=86400
        )

    logger.info(f"Loaded {len(all_stops)} stops, {len(all_routes)} routes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache

    # Startup
    logger.info("Starting Transportation RAG service")

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    if not transit_config.configured:
        logger.warning("transit_not_configured")
    else:
        # Load GTFS data in background
        asyncio.create_task(load_gtfs_data())

    yield

    # Shutdown
    logger.info("Shutting down Transportation RAG service")
    if cache:
        await cache.disconnect()


app = FastAPI(
    title="Transportation RAG Service",
    description="Region-configurable transit data integration",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


async def require_transit_configured():
    """FastAPI dependency: 503 when no transit region is configured.

    Generalises the `if not X: raise HTTPException(503, ...)` idiom used at
    src/rag/tesla/main.py and directions/main.py into a shared dependency.
    """
    if not transit_config.configured:
        raise HTTPException(
            status_code=503,
            detail="Transit region not configured: set TRANSIT_GTFS_FEEDS and/or TRANSIT_STATIC_SERVICES",
        )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    message = None
    if not transit_config.configured:
        message = transit_config.error or "not configured: set TRANSIT_GTFS_FEEDS and/or TRANSIT_STATIC_SERVICES"
    elif fetch_errors:
        message = "; ".join(f"{fid}: {reason}" for fid, reason in fetch_errors.items())

    return {
        "status": "healthy",
        "service": "transportation-rag",
        "version": "1.0.0",
        "configured": transit_config.configured,
        "region": transit_config.region_name,
        "config_error": transit_config.error,
        "data_loaded": transit_data["last_updated"] is not None,
        "stats": {
            "stops": len(transit_data["stops"]),
            "routes": len(transit_data["routes"])
        },
        **({"message": message} if message else {}),
    }


@app.get("/transit/nearby", dependencies=[Depends(require_transit_configured)])
async def get_nearby_stops(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    radius: int = Query(500, ge=100, le=5000, description="Search radius in meters"),
    limit: int = Query(20, ge=1, le=100, description="Maximum results"),
    transit_type: Optional[str] = Query(None, description="Filter by type: bus, metro, rail, ferry")
):
    """Find nearby transit stops."""
    if not transit_data["stops"]:
        raise HTTPException(status_code=503, detail="Transit data not loaded yet")

    nearby = []
    for stop_id, stop in transit_data["stops"].items():
        distance = haversine_distance(lat, lon, stop["stop_lat"], stop["stop_lon"])
        if distance <= radius:
            if transit_type and stop.get("stop_type") != transit_type:
                continue
            nearby.append({
                **stop,
                "distance_meters": round(distance)
            })

    # Sort by distance
    nearby.sort(key=lambda x: x["distance_meters"])

    return {
        "location": {"lat": lat, "lon": lon},
        "radius_meters": radius,
        "count": len(nearby[:limit]),
        "stops": nearby[:limit]
    }


@app.get("/transit/routes", dependencies=[Depends(require_transit_configured)])
async def get_routes(
    agency: Optional[str] = Query(None, description="Filter by agency (feed id prefix)"),
    route_type: Optional[int] = Query(None, description="GTFS route type (0=tram, 1=metro, 2=rail, 3=bus)")
):
    """List available routes."""
    if not transit_data["routes"]:
        raise HTTPException(status_code=503, detail="Transit data not loaded yet")

    routes = []
    for route_id, route in transit_data["routes"].items():
        if agency and not route["feed_id"].startswith(agency):
            continue
        if route_type is not None and route["route_type"] != route_type:
            continue
        routes.append(route)

    return {
        "count": len(routes),
        "routes": routes
    }


@app.get("/transit/departures", dependencies=[Depends(require_transit_configured)])
async def get_departures(
    stop_id: str = Query(..., description="Stop ID"),
    limit: int = Query(10, ge=1, le=50, description="Maximum results")
):
    """Get next departures from a stop."""
    if stop_id not in transit_data["stops"]:
        raise HTTPException(status_code=404, detail=f"Stop not found: {stop_id}")

    stop = transit_data["stops"][stop_id]

    # Check if a non-GTFS static service
    if "service_info" in stop:
        service = stop["service_info"]
        now = datetime.now()
        is_weekend = now.weekday() >= 5

        hours = (service.get("hours") or {}).get("weekend" if is_weekend else "weekday")
        if not hours:
            return {
                "stop": stop,
                "departures": [],
                "message": f"No service {'on weekends' if not is_weekend else 'on weekdays'}"
            }

        start = datetime.strptime(hours["start"], "%H:%M").time()
        end = datetime.strptime(hours["end"], "%H:%M").time()
        current_time = now.time()

        if current_time < start or current_time > end:
            return {
                "stop": stop,
                "departures": [],
                "message": f"Service runs {hours['start']} - {hours['end']}"
            }

        # Generate next departures based on frequency
        departures = []
        freq = service["frequency_minutes"]
        next_dep = now.replace(second=0, microsecond=0)
        next_dep += timedelta(minutes=freq - (next_dep.minute % freq))

        for _ in range(limit):
            if next_dep.time() > end:
                break
            departures.append({
                "departure_time": next_dep.strftime("%H:%M"),
                "service": service["name"],
                "free": service["free"]
            })
            next_dep += timedelta(minutes=freq)

        return {
            "stop": stop,
            "departures": departures
        }

    # Regular GTFS stop
    stop_times = transit_data["stop_times"].get(stop_id, [])
    if not stop_times:
        return {
            "stop": stop,
            "departures": [],
            "message": "No schedule data available"
        }

    # Get current time
    now = datetime.now()
    current_time = now.strftime("%H:%M:%S")

    # Filter to upcoming departures
    upcoming = []
    for st in stop_times:
        dep_time = normalize_time(st["departure_time"])
        if dep_time >= current_time:
            upcoming.append({
                "departure_time": dep_time[:5],
                "trip_id": st["trip_id"],
                "feed_id": st["feed_id"]
            })

    # Sort by time
    upcoming.sort(key=lambda x: x["departure_time"])

    return {
        "stop": stop,
        "departures": upcoming[:limit]
    }


@app.get("/transit/route/{route_id}", dependencies=[Depends(require_transit_configured)])
async def get_route_details(route_id: str):
    """Get route details."""
    if route_id not in transit_data["routes"]:
        raise HTTPException(status_code=404, detail=f"Route not found: {route_id}")

    route = transit_data["routes"][route_id]

    # Find stops served by this route
    # This would require trips.txt parsing - simplified for now
    return {
        "route": route,
        "feed_config": transit_config.feeds.get(route["feed_id"], {})
    }


@app.get("/transit/search", dependencies=[Depends(require_transit_configured)])
async def search_transit(
    query: str = Query(..., min_length=2, description="Search query"),
    limit: int = Query(20, ge=1, le=50, description="Maximum results")
):
    """Search stops and routes by name."""
    query_lower = query.lower()

    matching_stops = []
    for stop_id, stop in transit_data["stops"].items():
        if query_lower in stop["stop_name"].lower():
            matching_stops.append(stop)
            if len(matching_stops) >= limit:
                break

    matching_routes = []
    for route_id, route in transit_data["routes"].items():
        name = f"{route['route_short_name']} {route['route_long_name']}".lower()
        if query_lower in name:
            matching_routes.append(route)
            if len(matching_routes) >= limit:
                break

    return {
        "query": query,
        "stops": matching_stops,
        "routes": matching_routes
    }


@app.get("/transit/water", dependencies=[Depends(require_transit_configured)])
async def get_water_transit():
    """Get non-GTFS static transit services (e.g. ferries) for the configured region."""
    services = []
    now = datetime.now()
    is_weekend = now.weekday() >= 5
    for service_id, service in transit_config.static_services.items():
        hours = (service.get("hours") or {}).get("weekend" if is_weekend else "weekday")

        services.append({
            "id": service_id,
            "name": service["name"],
            "type": service.get("type", ""),
            "free": service.get("free", False),
            "operating_today": hours is not None,
            "hours": hours,
            "frequency_minutes": service.get("frequency_minutes"),
            "stops": service.get("stops", [])
        })

    return {
        "services": services,
        "day_type": "weekend" if is_weekend else "weekday"
    }


@app.get("/transit/agencies", dependencies=[Depends(require_transit_configured)])
async def get_agencies():
    """List all transit agencies."""
    agencies = list(transit_data["agencies"].values())

    # Add configured static services as pseudo-agencies
    for service_id, service in transit_config.static_services.items():
        agencies.append({
            "agency_id": service_id,
            "agency_name": service.get("agency_name", service["name"]),
            "feed_id": service_id
        })

    return {"agencies": agencies}


@app.post("/transit/refresh", dependencies=[Depends(require_transit_configured)])
async def refresh_data(background_tasks: BackgroundTasks):
    """Trigger refresh of GTFS data."""
    background_tasks.add_task(load_gtfs_data)
    return {"status": "refresh_started", "message": "GTFS data refresh initiated"}


@app.get("/transit/free", dependencies=[Depends(require_transit_configured)])
async def get_free_transit():
    """Get all free transit options for the configured region."""
    free_options = []

    for feed_id, feed in transit_config.feeds.items():
        if not feed.get("free"):
            continue
        feed_routes = [r for r in transit_data["routes"].values() if r["feed_id"] == feed_id]
        if feed_routes:
            free_options.append({
                "name": feed["name"],
                "type": feed.get("type", ""),
                "routes": feed_routes,
                "description": feed.get("description", "")
            })

    for service_id, service in transit_config.static_services.items():
        if service.get("free"):
            free_options.append({
                "name": service["name"],
                "type": service.get("type", ""),
                "hours": service.get("hours"),
                "frequency_minutes": service.get("frequency_minutes"),
                "stops": service.get("stops", []),
                "description": service.get("description", "")
            })

    return {"free_transit_options": free_options}


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Transportation RAG service on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
