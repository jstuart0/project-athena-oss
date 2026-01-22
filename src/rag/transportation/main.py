"""Transportation RAG Service - Baltimore Transit Integration

Provides transit data for Baltimore area including:
- Maryland MTA (Bus, Metro, Light Rail, MARC, Commuter Bus)
- Charm City Circulator (free bus)
- Harbor Connector (free water taxi)
- Baltimore Water Taxi (paid)
- Amtrak

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
import zipfile
import asyncio
from datetime import datetime, time, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, asdict
from math import radians, sin, cos, sqrt, atan2

from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse
import httpx
from contextlib import asynccontextmanager
from bs4 import BeautifulSoup

# Import shared utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient
from shared.logging_config import configure_logging
from shared.metrics import setup_metrics_endpoint

# Configure logging
logger = configure_logging("transportation-rag")

SERVICE_NAME = "transportation-rag"

# Environment variables
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8025"))

# GTFS Feed Configuration
GTFS_FEEDS = {
    "mta_bus": {
        "name": "MTA Local Bus",
        "agency": "mta",
        "url": "https://feeds.mta.maryland.gov/gtfs/local-bus",
        "type": "bus",
        "free": False
    },
    "mta_metro": {
        "name": "MTA Metro",
        "agency": "mta",
        "url": "https://feeds.mta.maryland.gov/gtfs/metro",
        "type": "metro",
        "free": False
    },
    "mta_light_rail": {
        "name": "MTA Light Rail",
        "agency": "mta",
        "url": "https://feeds.mta.maryland.gov/gtfs/light-rail",
        "type": "light_rail",
        "free": False
    },
    "mta_marc": {
        "name": "MARC Train",
        "agency": "mta",
        "url": "https://feeds.mta.maryland.gov/gtfs/marc",
        "type": "commuter_rail",
        "free": False
    },
    "mta_commuter_bus": {
        "name": "MTA Commuter Bus",
        "agency": "mta",
        "url": "https://feeds.mta.maryland.gov/gtfs/commuter-bus",
        "type": "commuter_bus",
        "free": False
    },
    "circulator": {
        "name": "Charm City Circulator",
        "agency": "baltimore_dot",
        "url": "https://transportation.baltimorecity.gov/files/cccgtfs824zip",
        "type": "bus",
        "free": True
    },
    "amtrak": {
        "name": "Amtrak",
        "agency": "amtrak",
        "url": "https://content.amtrak.com/content/gtfs/GTFS.zip",
        "type": "rail",
        "free": False
    }
}

# Baltimore Water Taxi / Harbor Connector - No GTFS, manual data
WATER_TRANSIT = {
    "harbor_connector": {
        "name": "Harbor Connector",
        "type": "ferry",
        "free": True,
        "hours": {"weekday": {"start": "06:00", "end": "20:00"}, "weekend": None},
        "frequency_minutes": 15,
        "stops": [
            {"name": "Maritime Park", "lat": 39.2659, "lon": -76.5812},
            {"name": "Locust Point", "lat": 39.2697, "lon": -76.5916},
            {"name": "Federal Hill", "lat": 39.2789, "lon": -76.6098},
            {"name": "Pier 5", "lat": 39.2854, "lon": -76.6062},
            {"name": "Harbor East", "lat": 39.2850, "lon": -76.5968},
            {"name": "Fells Point", "lat": 39.2826, "lon": -76.5919}
        ]
    },
    "water_taxi_downtown": {
        "name": "Baltimore Water Taxi - Downtown",
        "type": "ferry",
        "free": False,
        "hours": {"weekday": None, "weekend": {"start": "11:00", "end": "20:00"}},
        "frequency_minutes": 15,
        "stops": [
            {"name": "Harborplace", "lat": 39.2864, "lon": -76.6120},
            {"name": "Federal Hill", "lat": 39.2789, "lon": -76.6098},
            {"name": "Fells Point", "lat": 39.2826, "lon": -76.5919},
            {"name": "Harbor East", "lat": 39.2850, "lon": -76.5968}
        ]
    }
}

# Cache client and HTTP client
cache: Optional[CacheClient] = None
http_client: Optional[httpx.AsyncClient] = None

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


async def download_and_parse_gtfs(feed_id: str, feed_config: Dict[str, Any]) -> Dict[str, Any]:
    """Download and parse a GTFS feed."""
    logger.info(f"Downloading GTFS feed: {feed_id} from {feed_config['url']}")

    result = {
        "stops": [],
        "routes": [],
        "stop_times": [],
        "agencies": []
    }

    try:
        response = await http_client.get(
            feed_config["url"],
            follow_redirects=True,
            timeout=60.0
        )
        response.raise_for_status()

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
                            # Filter to Baltimore area (roughly)
                            if 39.1 < stop.stop_lat < 39.5 and -77.0 < stop.stop_lon < -76.3:
                                result["stops"].append(asdict(stop))
                        except (ValueError, KeyError) as e:
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
                        except (ValueError, KeyError) as e:
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
        return result

    except Exception as e:
        logger.error(f"Error downloading/parsing {feed_id}: {e}")
        return result


async def load_gtfs_data():
    """Load all GTFS feeds into memory."""
    global transit_data

    logger.info("Loading GTFS data from all feeds...")

    all_stops = {}
    all_routes = {}
    all_stop_times = {}
    all_agencies = {}

    # Download and parse each feed
    tasks = []
    for feed_id, feed_config in GTFS_FEEDS.items():
        tasks.append(download_and_parse_gtfs(feed_id, feed_config))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for feed_id, result in zip(GTFS_FEEDS.keys(), results):
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

    # Add water transit stops
    for service_id, service in WATER_TRANSIT.items():
        for i, stop in enumerate(service["stops"]):
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
                    "free": service["free"],
                    "hours": service["hours"],
                    "frequency_minutes": service["frequency_minutes"]
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
    global cache, http_client

    # Startup
    logger.info("Starting Transportation RAG service")

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # Initialize HTTP client
    http_client = httpx.AsyncClient(timeout=60.0)

    # Load GTFS data in background
    asyncio.create_task(load_gtfs_data())

    yield

    # Shutdown
    logger.info("Shutting down Transportation RAG service")
    if http_client:
        await http_client.aclose()
    if cache:
        await cache.disconnect()


app = FastAPI(
    title="Transportation RAG Service",
    description="Baltimore transit data integration",
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
        "service": "transportation-rag",
        "version": "1.0.0",
        "data_loaded": transit_data["last_updated"] is not None,
        "stats": {
            "stops": len(transit_data["stops"]),
            "routes": len(transit_data["routes"])
        }
    }


@app.get("/transit/nearby")
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


@app.get("/transit/routes")
async def get_routes(
    agency: Optional[str] = Query(None, description="Filter by agency: mta, baltimore_dot, amtrak"),
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


@app.get("/transit/departures")
async def get_departures(
    stop_id: str = Query(..., description="Stop ID"),
    limit: int = Query(10, ge=1, le=50, description="Maximum results")
):
    """Get next departures from a stop."""
    if stop_id not in transit_data["stops"]:
        raise HTTPException(status_code=404, detail=f"Stop not found: {stop_id}")

    stop = transit_data["stops"][stop_id]

    # Check if water transit
    if "service_info" in stop:
        service = stop["service_info"]
        now = datetime.now()
        is_weekend = now.weekday() >= 5

        hours = service["hours"]["weekend" if is_weekend else "weekday"]
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


@app.get("/transit/route/{route_id}")
async def get_route_details(route_id: str):
    """Get route details."""
    if route_id not in transit_data["routes"]:
        raise HTTPException(status_code=404, detail=f"Route not found: {route_id}")

    route = transit_data["routes"][route_id]

    # Find stops served by this route
    # This would require trips.txt parsing - simplified for now
    return {
        "route": route,
        "feed_config": GTFS_FEEDS.get(route["feed_id"], {})
    }


@app.get("/transit/search")
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


@app.get("/transit/water")
async def get_water_transit():
    """Get water transit services (Harbor Connector, Water Taxi)."""
    services = []
    for service_id, service in WATER_TRANSIT.items():
        now = datetime.now()
        is_weekend = now.weekday() >= 5
        hours = service["hours"]["weekend" if is_weekend else "weekday"]

        services.append({
            "id": service_id,
            "name": service["name"],
            "type": service["type"],
            "free": service["free"],
            "operating_today": hours is not None,
            "hours": hours,
            "frequency_minutes": service["frequency_minutes"],
            "stops": service["stops"]
        })

    return {
        "services": services,
        "day_type": "weekend" if now.weekday() >= 5 else "weekday"
    }


@app.get("/transit/agencies")
async def get_agencies():
    """List all transit agencies."""
    agencies = list(transit_data["agencies"].values())

    # Add water transit as pseudo-agencies
    agencies.extend([
        {
            "agency_id": "harbor_connector",
            "agency_name": "Harbor Connector (Baltimore DOT)",
            "feed_id": "harbor_connector"
        },
        {
            "agency_id": "water_taxi",
            "agency_name": "Baltimore Water Taxi",
            "feed_id": "water_taxi"
        }
    ])

    return {"agencies": agencies}


@app.post("/transit/refresh")
async def refresh_data(background_tasks: BackgroundTasks):
    """Trigger refresh of GTFS data."""
    background_tasks.add_task(load_gtfs_data)
    return {"status": "refresh_started", "message": "GTFS data refresh initiated"}


@app.get("/transit/free")
async def get_free_transit():
    """Get all free transit options in Baltimore."""
    free_options = []

    # Charm City Circulator
    circulator_routes = [r for r in transit_data["routes"].values() if r["feed_id"] == "circulator"]
    if circulator_routes:
        free_options.append({
            "name": "Charm City Circulator",
            "type": "bus",
            "routes": circulator_routes,
            "description": "Free bus service connecting downtown neighborhoods"
        })

    # Water transit
    for service_id, service in WATER_TRANSIT.items():
        if service["free"]:
            free_options.append({
                "name": service["name"],
                "type": service["type"],
                "hours": service["hours"],
                "frequency_minutes": service["frequency_minutes"],
                "stops": service["stops"],
                "description": "Free water transit connecting harbor destinations"
            })

    return {"free_transit_options": free_options}


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Transportation RAG service on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
