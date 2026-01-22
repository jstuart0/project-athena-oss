"""
Airport Code Lookup Utility

Resolves city names to airport codes for the flights service.
Uses a combination of:
1. Static mapping for common cities (instant)
2. Airports RAG service lookup (with caching)

This fixes FlightAware API 400 errors when users say "flights to Miami"
instead of "flights to MIA".
"""

import re
from typing import Optional, Dict, Tuple
import httpx
import structlog

logger = structlog.get_logger()

# Static mapping of common cities to primary airport codes
# These are the most likely airports when a city is mentioned
CITY_TO_AIRPORT: Dict[str, str] = {
    # Major US cities
    "new york": "JFK",
    "nyc": "JFK",
    "manhattan": "JFK",
    "los angeles": "LAX",
    "la": "LAX",
    "chicago": "ORD",
    "miami": "MIA",
    "san francisco": "SFO",
    "sf": "SFO",
    "boston": "BOS",
    "seattle": "SEA",
    "denver": "DEN",
    "atlanta": "ATL",
    "dallas": "DFW",
    "houston": "IAH",
    "phoenix": "PHX",
    "las vegas": "LAS",
    "vegas": "LAS",
    "orlando": "MCO",
    "minneapolis": "MSP",
    "detroit": "DTW",
    "philadelphia": "PHL",
    "philly": "PHL",
    "washington": "DCA",
    "dc": "DCA",
    "washington dc": "DCA",
    "baltimore": "BWI",
    "san diego": "SAN",
    "tampa": "TPA",
    "portland": "PDX",
    "austin": "AUS",
    "nashville": "BNA",
    "new orleans": "MSY",
    "charlotte": "CLT",
    "salt lake city": "SLC",
    "san antonio": "SAT",
    "pittsburgh": "PIT",
    "cleveland": "CLE",
    "st louis": "STL",
    "kansas city": "MCI",
    "indianapolis": "IND",
    "cincinnati": "CVG",
    "raleigh": "RDU",
    "memphis": "MEM",
    "milwaukee": "MKE",
    "jacksonville": "JAX",
    "honolulu": "HNL",
    "hawaii": "HNL",
    "anchorage": "ANC",
    "alaska": "ANC",

    # International cities
    "london": "LHR",
    "paris": "CDG",
    "tokyo": "NRT",
    "beijing": "PEK",
    "shanghai": "PVG",
    "hong kong": "HKG",
    "singapore": "SIN",
    "dubai": "DXB",
    "sydney": "SYD",
    "melbourne": "MEL",
    "toronto": "YYZ",
    "vancouver": "YVR",
    "montreal": "YUL",
    "mexico city": "MEX",
    "cancun": "CUN",
    "amsterdam": "AMS",
    "frankfurt": "FRA",
    "munich": "MUC",
    "madrid": "MAD",
    "barcelona": "BCN",
    "rome": "FCO",
    "milan": "MXP",
    "dublin": "DUB",
    "zurich": "ZRH",
    "seoul": "ICN",
    "bangkok": "BKK",
    "mumbai": "BOM",
    "delhi": "DEL",
    "johannesburg": "JNB",
    "cairo": "CAI",
    "istanbul": "IST",
    "moscow": "SVO",
    "rio": "GIG",
    "rio de janeiro": "GIG",
    "sao paulo": "GRU",
    "buenos aires": "EZE",
    "lima": "LIM",
    "bogota": "BOG",
}

# Cache for airport lookups from the service
_airport_cache: Dict[str, str] = {}


def is_airport_code(value: str) -> bool:
    """Check if a string is already an airport code (3 uppercase letters)."""
    if not value:
        return False
    clean = value.strip().upper()
    return len(clean) == 3 and clean.isalpha()


def normalize_city_name(city: str) -> str:
    """Normalize city name for lookup."""
    if not city:
        return ""
    # Remove common words and clean up
    city = city.lower().strip()
    # Remove "to", "from", "airport", etc.
    city = re.sub(r'\b(to|from|the|airport|international|domestic)\b', '', city)
    # Remove extra whitespace
    city = ' '.join(city.split())
    return city


def lookup_airport_static(city: str) -> Optional[str]:
    """Look up airport code from static mapping."""
    normalized = normalize_city_name(city)
    return CITY_TO_AIRPORT.get(normalized)


async def lookup_airport_dynamic(city: str, airports_service_url: str = "http://localhost:8011") -> Optional[str]:
    """
    Look up airport code from airports RAG service.

    Args:
        city: City name to look up
        airports_service_url: URL of the airports service

    Returns:
        Airport code if found, None otherwise
    """
    normalized = normalize_city_name(city)

    # Check cache first
    if normalized in _airport_cache:
        logger.debug("airport_cache_hit", city=normalized, code=_airport_cache[normalized])
        return _airport_cache[normalized]

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{airports_service_url}/airports/search",
                params={"query": city}
            )

            if response.status_code == 200:
                data = response.json()
                results = data.get("results", {})

                # Try to extract airport code from response
                # FlightAware returns airport info with code/code_iata/code_icao
                if isinstance(results, dict):
                    code = results.get("code_iata") or results.get("code") or results.get("icao")
                    if code and len(code) <= 4:
                        _airport_cache[normalized] = code.upper()
                        logger.info("airport_lookup_success", city=city, code=code)
                        return code.upper()

            logger.debug("airport_lookup_no_result", city=city, status=response.status_code)
            return None

    except Exception as e:
        logger.warning("airport_lookup_error", city=city, error=str(e))
        return None


async def resolve_to_airport_code(
    value: str,
    airports_service_url: str = "http://localhost:8011",
    use_dynamic_lookup: bool = True
) -> Tuple[str, bool]:
    """
    Resolve a value to an airport code.

    Args:
        value: City name or airport code
        airports_service_url: URL of the airports service
        use_dynamic_lookup: Whether to use the airports service for unknown cities

    Returns:
        Tuple of (airport_code, was_resolved) where was_resolved indicates
        if the value was transformed (True) or already a code (False)
    """
    if not value:
        return (value, False)

    # Already an airport code
    if is_airport_code(value):
        logger.debug("airport_already_code", value=value)
        return (value.upper(), False)

    # Try static lookup first (instant)
    code = lookup_airport_static(value)
    if code:
        logger.info("airport_static_lookup", city=value, code=code)
        return (code, True)

    # Try dynamic lookup if enabled
    if use_dynamic_lookup:
        code = await lookup_airport_dynamic(value, airports_service_url)
        if code:
            return (code, True)

    # Return original value if no resolution found
    # The flights service will handle the error
    logger.warning("airport_lookup_failed", value=value)
    return (value, False)


async def resolve_flight_parameters(
    arguments: Dict,
    airports_service_url: str = "http://localhost:8011",
    feature_enabled: bool = True
) -> Dict:
    """
    Resolve city names to airport codes in flight search arguments.

    Args:
        arguments: The tool call arguments (origin, destination, etc.)
        airports_service_url: URL of the airports service
        feature_enabled: Whether airport code lookup feature is enabled

    Returns:
        Updated arguments with resolved airport codes
    """
    if not feature_enabled:
        return arguments

    resolved = arguments.copy()

    # Resolve origin if present
    if "origin" in resolved:
        origin, was_resolved = await resolve_to_airport_code(
            resolved["origin"],
            airports_service_url
        )
        if was_resolved:
            logger.info("flight_origin_resolved", original=resolved["origin"], resolved=origin)
        resolved["origin"] = origin

    # Resolve destination if present
    if "destination" in resolved:
        destination, was_resolved = await resolve_to_airport_code(
            resolved["destination"],
            airports_service_url
        )
        if was_resolved:
            logger.info("flight_destination_resolved", original=resolved["destination"], resolved=destination)
        resolved["destination"] = destination

    # Also resolve 'query' parameter if it looks like a city name
    if "query" in resolved and not is_airport_code(resolved["query"]):
        query, was_resolved = await resolve_to_airport_code(
            resolved["query"],
            airports_service_url
        )
        if was_resolved:
            logger.info("flight_query_resolved", original=resolved["query"], resolved=query)
        resolved["query"] = query

    return resolved
