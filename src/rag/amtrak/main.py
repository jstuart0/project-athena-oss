"""Amtrak RAG Service - Train Schedule Integration

Provides Amtrak train schedule data using GTFS (General Transit Feed Specification).

Endpoints:
- GET /health - Health check
- GET /amtrak/schedule - Get train schedules between stations
- GET /amtrak/stations - Search stations
- GET /amtrak/query - Natural language query (for orchestrator)
"""

import os
import sys
import csv
import zipfile
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, Any, Optional, List
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

# Import shared utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient, cached
from shared.service_registry import startup_service, unregister_service
from shared.logging_config import configure_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

# Configure logging
logger = configure_logging("amtrak-rag")

SERVICE_NAME = "amtrak-rag"

# Environment variables
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8027"))

# GTFS data directory
DATA_DIR = Path(__file__).parent / "data"
GTFS_DIR = DATA_DIR / "gtfs"
GTFS_URL = "https://content.amtrak.com/content/gtfs/GTFS.zip"

# Timezone
EASTERN = ZoneInfo("America/New_York")

# Default origin (Baltimore Penn Station)
DEFAULT_ORIGIN = "BAL"

# Common station aliases for natural language processing
STATION_ALIASES = {
    "baltimore": "BAL",
    "baltimore penn": "BAL",
    "bwi": "BWI",
    "new york": "NYP",
    "nyc": "NYP",
    "penn station": "NYP",
    "new york penn": "NYP",
    "moynihan": "NYP",
    "washington": "WAS",
    "dc": "WAS",
    "union station": "WAS",
    "philadelphia": "PHL",
    "philly": "PHL",
    "30th street": "PHL",
    "boston": "BOS",
    "south station": "BOS",
    "chicago": "CHI",
    "chicago union": "CHI",
    "los angeles": "LAX",
    "la union": "LAX",
    "seattle": "SEA",
    "san diego": "SAN",
    "san francisco": "SFC",
    "portland": "PDX",
    "denver": "DEN",
    "miami": "MIA",
    "atlanta": "ATL",
    "new orleans": "NOL",
    "dallas": "DAL",
    "houston": "HOS",
    "trenton": "TRE",
    "newark": "NWK",
    "wilmington": "WIL",
    "providence": "PVD",
    "new haven": "NHV",
    "stamford": "STM",
}

# Cache for GTFS data
_gtfs_cache = {
    'routes': {},
    'trips': {},
    'stop_times': {},
    'stops': {},
    'calendar': {},
    'calendar_dates': {},
    'loaded': False,
    'last_updated': None
}

# Global clients
cache = None
admin_client = None


def download_gtfs():
    """Download and extract GTFS data."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DATA_DIR / "gtfs.zip"

    logger.info("Downloading GTFS data from Amtrak...")
    urllib.request.urlretrieve(GTFS_URL, zip_path)

    logger.info("Extracting GTFS data...")
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(GTFS_DIR)

    # Clean up zip
    zip_path.unlink()
    logger.info("GTFS data ready")
    return True


def load_gtfs():
    """Load GTFS data into memory."""
    global _gtfs_cache

    if _gtfs_cache['loaded']:
        return

    if not GTFS_DIR.exists():
        download_gtfs()

    logger.info("Loading GTFS data into memory...")

    # Load routes
    with open(GTFS_DIR / 'routes.txt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            _gtfs_cache['routes'][row['route_id']] = {
                'name': row.get('route_long_name') or row.get('route_short_name', ''),
                'type': row.get('route_type', '')
            }

    # Load stops
    with open(GTFS_DIR / 'stops.txt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            _gtfs_cache['stops'][row['stop_id']] = {
                'name': row['stop_name'],
                'timezone': row.get('stop_timezone', 'America/New_York'),
                'lat': float(row['stop_lat']) if row.get('stop_lat') else None,
                'lon': float(row['stop_lon']) if row.get('stop_lon') else None
            }

    # Load trips
    with open(GTFS_DIR / 'trips.txt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            _gtfs_cache['trips'][row['trip_id']] = {
                'route_id': row['route_id'],
                'service_id': row['service_id'],
                'train_number': row.get('trip_short_name', ''),
                'headsign': row.get('trip_headsign', '')
            }

    # Load calendar
    with open(GTFS_DIR / 'calendar.txt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            _gtfs_cache['calendar'][row['service_id']] = {
                'monday': row['monday'] == '1',
                'tuesday': row['tuesday'] == '1',
                'wednesday': row['wednesday'] == '1',
                'thursday': row['thursday'] == '1',
                'friday': row['friday'] == '1',
                'saturday': row['saturday'] == '1',
                'sunday': row['sunday'] == '1',
                'start_date': datetime.strptime(row['start_date'], '%Y%m%d').date(),
                'end_date': datetime.strptime(row['end_date'], '%Y%m%d').date()
            }

    # Load calendar_dates (exceptions)
    calendar_dates_path = GTFS_DIR / 'calendar_dates.txt'
    if calendar_dates_path.exists():
        with open(calendar_dates_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                service_id = row['service_id']
                exception_date = datetime.strptime(row['date'], '%Y%m%d').date()
                exception_type = row['exception_type']  # 1=added, 2=removed

                if service_id not in _gtfs_cache['calendar_dates']:
                    _gtfs_cache['calendar_dates'][service_id] = {}
                _gtfs_cache['calendar_dates'][service_id][exception_date] = exception_type

    # Load stop_times (grouped by trip)
    with open(GTFS_DIR / 'stop_times.txt') as f:
        reader = csv.DictReader(f)
        for row in reader:
            trip_id = row['trip_id']
            if trip_id not in _gtfs_cache['stop_times']:
                _gtfs_cache['stop_times'][trip_id] = []
            _gtfs_cache['stop_times'][trip_id].append({
                'stop_id': row['stop_id'],
                'arrival': row['arrival_time'],
                'departure': row['departure_time'],
                'sequence': int(row['stop_sequence'])
            })

    # Sort stop_times by sequence
    for trip_id in _gtfs_cache['stop_times']:
        _gtfs_cache['stop_times'][trip_id].sort(key=lambda x: x['sequence'])

    _gtfs_cache['loaded'] = True
    _gtfs_cache['last_updated'] = datetime.now(EASTERN)

    logger.info(f"Loaded {len(_gtfs_cache['trips'])} trips, {len(_gtfs_cache['stops'])} stops")


def get_services_for_date(target_date: date) -> set:
    """Get service IDs that run on a specific date."""
    day_name = target_date.strftime('%A').lower()
    services = set()

    for service_id, cal in _gtfs_cache['calendar'].items():
        if cal['start_date'] <= target_date <= cal['end_date']:
            # Check regular schedule
            if cal.get(day_name, False):
                services.add(service_id)

        # Check exceptions
        if service_id in _gtfs_cache['calendar_dates']:
            exception = _gtfs_cache['calendar_dates'][service_id].get(target_date)
            if exception == '1':  # Service added
                services.add(service_id)
            elif exception == '2':  # Service removed
                services.discard(service_id)

    return services


def resolve_station_code(query: str) -> str:
    """Resolve a station name/alias to a code."""
    if not query:
        return DEFAULT_ORIGIN

    query_lower = query.lower().strip()

    # Check aliases first
    if query_lower in STATION_ALIASES:
        return STATION_ALIASES[query_lower]

    # If it's already a 3-letter code, use it
    if len(query) == 3 and query.isalpha():
        return query.upper()

    # Try to find partial match in aliases
    for alias, code in STATION_ALIASES.items():
        if query_lower in alias or alias in query_lower:
            return code

    return query.upper()


def parse_gtfs_time(time_str: str, base_date: date) -> datetime:
    """Parse GTFS time (can be >24:00 for next day) to datetime."""
    parts = time_str.split(':')
    hours = int(parts[0])
    minutes = int(parts[1])
    seconds = int(parts[2]) if len(parts) > 2 else 0

    # Handle times >= 24:00 (next day)
    day_offset = hours // 24
    hours = hours % 24

    dt = datetime(base_date.year, base_date.month, base_date.day,
                  hours, minutes, seconds, tzinfo=EASTERN)
    if day_offset:
        dt += timedelta(days=day_offset)

    return dt


def generate_booking_url(origin: str, destination: str, departure_date: date,
                         return_date: Optional[date] = None) -> str:
    """Generate Amtrak booking URL with pre-filled parameters."""
    # Amtrak booking URL format (reverse engineered from their website)
    base_url = "https://www.amtrak.com/tickets/departure.html"

    # Format date as MM/DD/YYYY
    date_str = departure_date.strftime("%m/%d/%Y")

    params = [
        f"wdf.origin={origin}",
        f"wdf.destination={destination}",
        f"wdf.travelDate={date_str}",
        "wdf.adult=1",
    ]

    if return_date:
        return_str = return_date.strftime("%m/%d/%Y")
        params.append(f"wdf.returnDate={return_str}")
        params.append("wdf.tripType=roundTrip")
    else:
        params.append("wdf.tripType=oneWay")

    return f"{base_url}?{'&'.join(params)}"


def get_schedule_internal(origin: str, destination: str, travel_date: Optional[str] = None,
                          return_date: Optional[str] = None, limit: int = 10) -> Dict[str, Any]:
    """Internal function to get train schedules between two stations."""
    load_gtfs()

    origin_code = resolve_station_code(origin)
    dest_code = resolve_station_code(destination)

    # Parse dates
    if travel_date:
        target_date = datetime.strptime(travel_date, '%Y-%m-%d').date()
    else:
        target_date = datetime.now(EASTERN).date()

    return_dt = None
    if return_date:
        return_dt = datetime.strptime(return_date, '%Y-%m-%d').date()

    # Get services running on target date
    services_today = get_services_for_date(target_date)

    outbound_results = []
    now = datetime.now(EASTERN)

    for trip_id, stops in _gtfs_cache['stop_times'].items():
        if trip_id not in _gtfs_cache['trips']:
            continue

        trip = _gtfs_cache['trips'][trip_id]
        if trip['service_id'] not in services_today:
            continue

        stop_ids = [s['stop_id'] for s in stops]

        if origin_code in stop_ids and dest_code in stop_ids:
            origin_idx = stop_ids.index(origin_code)
            dest_idx = stop_ids.index(dest_code)

            if origin_idx < dest_idx:  # Correct direction
                origin_stop = stops[origin_idx]
                dest_stop = stops[dest_idx]

                dep_time = parse_gtfs_time(origin_stop['departure'], target_date)
                arr_time = parse_gtfs_time(dest_stop['arrival'], target_date)

                # Skip past trains (if querying today)
                if target_date == datetime.now(EASTERN).date() and dep_time < now:
                    continue

                route = _gtfs_cache['routes'].get(trip['route_id'], {})
                duration = int((arr_time - dep_time).total_seconds() / 60)

                outbound_results.append({
                    'train_number': trip['train_number'],
                    'route': route.get('name', 'Unknown'),
                    'origin_code': origin_code,
                    'origin_name': _gtfs_cache['stops'].get(origin_code, {}).get('name'),
                    'destination_code': dest_code,
                    'destination_name': _gtfs_cache['stops'].get(dest_code, {}).get('name'),
                    'departure': dep_time.strftime('%I:%M %p').lstrip('0'),
                    'departure_24h': dep_time.strftime('%H:%M'),
                    'departure_iso': dep_time.isoformat(),
                    'arrival': arr_time.strftime('%I:%M %p').lstrip('0'),
                    'arrival_24h': arr_time.strftime('%H:%M'),
                    'arrival_iso': arr_time.isoformat(),
                    'duration_minutes': duration,
                    'duration_str': f"{duration // 60}h {duration % 60}m",
                    'booking_url': generate_booking_url(origin_code, dest_code, target_date, return_dt)
                })

    # Sort by departure time and limit
    outbound_results.sort(key=lambda x: x['departure_24h'])
    outbound_results = outbound_results[:limit]

    result = {
        'origin': {'code': origin_code, 'name': _gtfs_cache['stops'].get(origin_code, {}).get('name')},
        'destination': {'code': dest_code, 'name': _gtfs_cache['stops'].get(dest_code, {}).get('name')},
        'date': target_date.isoformat(),
        'outbound': outbound_results,
        'count': len(outbound_results)
    }

    # Handle return trip if requested
    if return_dt:
        services_return = get_services_for_date(return_dt)
        return_results = []

        for trip_id, stops in _gtfs_cache['stop_times'].items():
            if trip_id not in _gtfs_cache['trips']:
                continue

            trip = _gtfs_cache['trips'][trip_id]
            if trip['service_id'] not in services_return:
                continue

            stop_ids = [s['stop_id'] for s in stops]

            # Reverse direction for return
            if dest_code in stop_ids and origin_code in stop_ids:
                origin_idx = stop_ids.index(dest_code)
                dest_idx = stop_ids.index(origin_code)

                if origin_idx < dest_idx:  # Correct direction (reversed)
                    origin_stop = stops[origin_idx]
                    dest_stop = stops[dest_idx]

                    dep_time = parse_gtfs_time(origin_stop['departure'], return_dt)
                    arr_time = parse_gtfs_time(dest_stop['arrival'], return_dt)

                    route = _gtfs_cache['routes'].get(trip['route_id'], {})
                    duration = int((arr_time - dep_time).total_seconds() / 60)

                    return_results.append({
                        'train_number': trip['train_number'],
                        'route': route.get('name', 'Unknown'),
                        'origin_code': dest_code,
                        'origin_name': _gtfs_cache['stops'].get(dest_code, {}).get('name'),
                        'destination_code': origin_code,
                        'destination_name': _gtfs_cache['stops'].get(origin_code, {}).get('name'),
                        'departure': dep_time.strftime('%I:%M %p').lstrip('0'),
                        'departure_24h': dep_time.strftime('%H:%M'),
                        'departure_iso': dep_time.isoformat(),
                        'arrival': arr_time.strftime('%I:%M %p').lstrip('0'),
                        'arrival_24h': arr_time.strftime('%H:%M'),
                        'arrival_iso': arr_time.isoformat(),
                        'duration_minutes': duration,
                        'duration_str': f"{duration // 60}h {duration % 60}m",
                        'booking_url': generate_booking_url(dest_code, origin_code, return_dt)
                    })

        return_results.sort(key=lambda x: x['departure_24h'])
        return_results = return_results[:limit]

        result['return_date'] = return_dt.isoformat()
        result['return'] = return_results
        result['return_count'] = len(return_results)

    return result


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache, admin_client

    # Startup
    logger.info("Starting Amtrak RAG service")

    # Register service in registry (kills stale process on port if any)
    await startup_service("amtrak", SERVICE_PORT, "Amtrak Train Schedules")

    # Initialize admin client
    admin_client = get_admin_client()

    # Initialize cache
    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # Pre-load GTFS data
    load_gtfs()

    yield

    # Shutdown
    logger.info("Shutting down Amtrak RAG service")

    # Unregister from service registry
    await unregister_service("amtrak")

    if cache:
        await cache.disconnect()
    if admin_client:
        await admin_client.close()


app = FastAPI(
    title="Amtrak RAG Service",
    description="Amtrak train schedule integration using GTFS data",
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
        "service": "amtrak-rag",
        "version": "1.0.0",
        "gtfs_loaded": _gtfs_cache['loaded'],
        "stations": len(_gtfs_cache['stops']),
        "trips": len(_gtfs_cache['trips'])
    }


@app.get("/amtrak/stations")
async def search_stations(
    search: Optional[str] = Query(None, description="Search term for station name or code")
):
    """Search for Amtrak stations."""
    load_gtfs()

    stations = [
        {"code": code, **data}
        for code, data in _gtfs_cache['stops'].items()
    ]

    if search:
        search_lower = search.lower()
        stations = [
            s for s in stations
            if search_lower in s['name'].lower() or search_lower in s['code'].lower()
        ]

    # Sort by name
    stations.sort(key=lambda x: x['name'])

    return {"stations": stations[:50], "count": len(stations)}


@app.get("/amtrak/schedule")
async def get_schedule(
    origin: Optional[str] = Query(None, description="Origin station (default: Baltimore Penn)"),
    destination: str = Query(..., description="Destination station code or name"),
    date: Optional[str] = Query(None, description="Travel date YYYY-MM-DD (default: today)"),
    return_date: Optional[str] = Query(None, description="Return date YYYY-MM-DD for round trip"),
    limit: int = Query(10, ge=1, le=50, description="Max results per direction")
):
    """
    Get train schedules between two stations.

    Supports round trips when return_date is provided.
    Default origin is Baltimore Penn Station if not specified.
    """
    try:
        # Default to Baltimore if no origin
        if not origin:
            origin = DEFAULT_ORIGIN
            logger.info("Using default origin: Baltimore Penn (BAL)")

        result = get_schedule_internal(origin, destination, date, return_date, limit)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Schedule error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to retrieve schedule")


@app.get("/amtrak/query")
async def natural_language_query(
    query: str = Query(..., description="Natural language query about train schedules")
):
    """
    Natural language query endpoint for orchestrator integration.

    Examples:
    - "next train to new york"
    - "train from baltimore to dc"
    - "round trip to boston returning friday"
    """
    load_gtfs()
    query_lower = query.lower()

    # Extract origin and destination
    origin = None
    destination = None

    # Check for "from X" pattern
    for alias, code in STATION_ALIASES.items():
        if f"from {alias}" in query_lower:
            origin = code
            break

    # Check for "to X" pattern
    for alias, code in STATION_ALIASES.items():
        if f"to {alias}" in query_lower:
            destination = code
            break

    # If no origin specified, default to Baltimore
    if not origin:
        origin = DEFAULT_ORIGIN
        logger.info("No origin specified, defaulting to Baltimore Penn")

    if not destination:
        return {
            "error": "Could not determine destination",
            "hint": "Try: 'train to new york' or 'train from dc to boston'"
        }

    # Check for return/round trip
    return_date = None
    today = datetime.now(EASTERN).date()

    if "round trip" in query_lower or "returning" in query_lower or "return" in query_lower:
        # Default return to 3 days from now if not specified
        return_date = (today + timedelta(days=3)).isoformat()

        # Try to parse specific return date mentions
        import re
        days_of_week = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        for i, day in enumerate(days_of_week):
            if day in query_lower:
                # Find the next occurrence of this day
                days_ahead = i - today.weekday()
                if days_ahead <= 0:
                    days_ahead += 7
                return_date = (today + timedelta(days=days_ahead)).isoformat()
                break

    # Get schedule
    result = get_schedule_internal(origin, destination, return_date=return_date)
    outbound = result.get('outbound', [])

    if not outbound:
        return {
            "answer": f"No trains found from {result['origin']['name']} to {result['destination']['name']} today.",
            "schedules": [],
            "booking_url": generate_booking_url(origin, destination, today)
        }

    # Format answer
    next_train = outbound[0]
    answer = (
        f"The next train from {result['origin']['name']} to {result['destination']['name']} is "
        f"Train {next_train['train_number']} ({next_train['route']}). "
        f"It departs at {next_train['departure']} and arrives at {next_train['arrival']} ({next_train['duration_str']}). "
    )

    if len(outbound) > 1:
        answer += f"There are {len(outbound) - 1} more trains today. "

    if return_date and result.get('return'):
        return_train = result['return'][0]
        answer += (
            f"For the return on {result['return_date']}, Train {return_train['train_number']} "
            f"departs at {return_train['departure']}."
        )

    response = {
        "answer": answer,
        "outbound": outbound,
        "count": len(outbound),
        "origin": result['origin'],
        "destination": result['destination'],
        "booking_url": next_train['booking_url']
    }

    if return_date and result.get('return'):
        response['return'] = result['return']
        response['return_date'] = result['return_date']

    return response


@app.post("/amtrak/refresh")
async def refresh_gtfs():
    """Re-download GTFS data from Amtrak."""
    global _gtfs_cache

    logger.info("Refreshing GTFS data...")
    _gtfs_cache = {
        'routes': {}, 'trips': {}, 'stop_times': {},
        'stops': {}, 'calendar': {}, 'calendar_dates': {},
        'loaded': False, 'last_updated': None
    }

    try:
        download_gtfs()
        load_gtfs()
        return {
            "success": True,
            "message": "GTFS data refreshed",
            "stations": len(_gtfs_cache['stops']),
            "trips": len(_gtfs_cache['trips'])
        }
    except Exception as e:
        logger.error(f"Failed to refresh GTFS: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to refresh: {str(e)}")


if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Amtrak RAG service on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
