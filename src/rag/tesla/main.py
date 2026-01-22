"""Tesla Metrics RAG Service - TeslaMate Integration

Provides access to Tesla vehicle metrics from TeslaMate PostgreSQL database.
OWNER MODE ONLY - This service exposes sensitive vehicle data.

API Endpoints:
- GET /health - Health check
- GET /car - Vehicle information
- GET /status - Current vehicle status (battery, range, location)
- GET /drives - Drive history and statistics
- GET /charges - Charging history and statistics
- GET /efficiency - Efficiency metrics
- GET /battery - Battery health and degradation
- GET /states - Vehicle state history
- GET /updates - Software update history
- GET /stats - Aggregate statistics
- GET /query - Natural language query interface
"""

import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import asyncpg
import structlog
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

# Add parent directories to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from shared.service_registry import startup_service, unregister_service
from shared.logging_config import setup_logging
from shared.metrics import setup_metrics_endpoint

# Configure logging
setup_logging(service_name="tesla-rag")
logger = structlog.get_logger()

SERVICE_NAME = "tesla"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8028"))

# TeslaMate PostgreSQL connection
# Configure via environment variables for your installation
TESLAMATE_DB_HOST = os.getenv("TESLAMATE_DB_HOST", "localhost")
TESLAMATE_DB_PORT = int(os.getenv("TESLAMATE_DB_PORT", "5432"))
TESLAMATE_DB_NAME = os.getenv("TESLAMATE_DB_NAME", "teslamate")
TESLAMATE_DB_USER = os.getenv("TESLAMATE_DB_USER", "teslamate")
TESLAMATE_DB_PASS = os.getenv("TESLAMATE_DB_PASS", "")

# Global database pool
db_pool: Optional[asyncpg.Pool] = None


def km_to_miles(km) -> float:
    """Convert kilometers to miles."""
    if km is None:
        return 0
    return float(km) * 0.621371


def celsius_to_fahrenheit(c) -> float:
    """Convert Celsius to Fahrenheit."""
    if c is None:
        return None
    return (float(c) * 9/5) + 32


def bar_to_psi(bar) -> float:
    """Convert bar to PSI."""
    if bar is None:
        return None
    return float(bar) * 14.5038


def parse_timeframe_from_query(query: str) -> Dict[str, Any]:
    """
    Parse timeframe from natural language query.
    Returns dict with 'days' (for simple lookback) or 'start_date'/'end_date' for specific ranges.

    Examples:
    - "last week" / "past week" → {'days': 7, 'description': 'last week'}
    - "on December 25th" → {'start_date': date, 'end_date': date, 'description': 'on December 25'}
    - "from Dec 20 to Dec 25" → {'start_date': date, 'end_date': date, 'description': 'December 20-25'}
    - "on Christmas" → {'start_date': date, 'end_date': date, 'description': 'on Christmas'}
    """
    import re
    from datetime import date
    from dateutil import parser as date_parser
    from dateutil.relativedelta import relativedelta

    query_lower = query.lower()
    today = date.today()

    # Helper to format timeframe description
    def days_to_description(d: int) -> str:
        if d == 1:
            return "today"
        elif d == 2:
            return "yesterday"
        elif d == 7:
            return "in the last week"
        elif d == 30:
            return "in the last 30 days"
        elif d == 365:
            return "in the last year"
        else:
            return f"in the last {d} days"

    # Check for date ranges: "from X to Y", "between X and Y"
    # Use more specific patterns to capture full date strings
    month_pattern = r'(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{4})?'
    date_num_pattern = r'\d{1,2}/\d{1,2}(?:/\d{2,4})?'
    combined_date = f'(?:{month_pattern}|{date_num_pattern})'

    range_patterns = [
        rf'from\s+({combined_date})\s+to\s+({combined_date})',
        rf'between\s+({combined_date})\s+and\s+({combined_date})',
    ]
    for pattern in range_patterns:
        range_match = re.search(pattern, query_lower)
        if range_match:
            try:
                start_str, end_str = range_match.groups()
                start_date = date_parser.parse(start_str, fuzzy=True).date()
                end_date = date_parser.parse(end_str, fuzzy=True).date()
                # Handle year - assume current year if not specified
                if start_date.year == 1900:
                    start_date = start_date.replace(year=today.year)
                if end_date.year == 1900:
                    end_date = end_date.replace(year=today.year)
                return {
                    'start_date': start_date,
                    'end_date': end_date,
                    'description': f"from {start_date.strftime('%B %d')} to {end_date.strftime('%B %d')}"
                }
            except:
                pass

    # Check for specific date: "on December 25", "on 12/25", "on the 25th"
    specific_date_patterns = [
        r'on\s+((?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?(?:\s*,?\s*\d{4})?)',
        r'on\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)',  # MM/DD or MM/DD/YYYY
        r'on\s+(the\s+\d{1,2}(?:st|nd|rd|th)?)',  # "on the 25th"
        r'(?:drives?|trips?|charge)\s+(?:on|for)\s+((?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2}(?:st|nd|rd|th)?)',
    ]
    for pattern in specific_date_patterns:
        date_match = re.search(pattern, query_lower)
        if date_match:
            date_str = date_match.group(1).strip()
            # Skip common words that aren't dates
            if date_str in ['average', 'my', 'the', 'a', 'it']:
                continue
            try:
                parsed_date = date_parser.parse(date_str, fuzzy=True).date()
                # Handle year - assume current year if not specified
                if parsed_date > today:
                    parsed_date = parsed_date.replace(year=today.year - 1)
                return {
                    'start_date': parsed_date,
                    'end_date': parsed_date,
                    'description': f"on {parsed_date.strftime('%B %d')}"
                }
            except:
                pass

    # Check for named holidays (for current or recent year)
    holidays = {
        'christmas': (12, 25),
        'christmas eve': (12, 24),
        'new year': (1, 1),
        "new year's": (1, 1),
        "new year's eve": (12, 31),
        'thanksgiving': None,  # Varies by year
        'halloween': (10, 31),
        'independence day': (7, 4),
        'july 4th': (7, 4),
        'fourth of july': (7, 4),
        'labor day': None,  # First Monday of September
        'memorial day': None,  # Last Monday of May
        "valentine's day": (2, 14),
        'valentines day': (2, 14),
    }

    # Sort holidays by length (longest first) to match "christmas eve" before "christmas"
    for holiday, date_tuple in sorted(holidays.items(), key=lambda x: len(x[0]), reverse=True):
        if holiday in query_lower:
            if date_tuple:
                month, day = date_tuple
                holiday_date = date(today.year, month, day)
                if holiday_date > today:
                    holiday_date = holiday_date.replace(year=today.year - 1)
                return {
                    'start_date': holiday_date,
                    'end_date': holiday_date,
                    'description': f"on {holiday.title()}"
                }

    # Check for specific day patterns: "last X days", "past X days"
    day_match = re.search(r'(?:last|past)\s+(\d+)\s+days?', query_lower)
    if day_match:
        days = int(day_match.group(1))
        return {'days': days, 'description': days_to_description(days)}

    # Check for week patterns: "last X weeks", "past X weeks"
    week_match = re.search(r'(?:last|past)\s+(\d+)\s+weeks?', query_lower)
    if week_match:
        days = int(week_match.group(1)) * 7
        return {'days': days, 'description': days_to_description(days)}

    # Check for month patterns: "last X months", "past X months"
    month_match = re.search(r'(?:last|past)\s+(\d+)\s+months?', query_lower)
    if month_match:
        days = int(month_match.group(1)) * 30
        return {'days': days, 'description': days_to_description(days)}

    # Common timeframe keywords
    if any(kw in query_lower for kw in ["today", "this morning", "this afternoon", "this evening"]):
        return {'days': 1, 'description': 'today'}

    if "yesterday" in query_lower:
        return {'days': 2, 'description': 'yesterday'}

    if any(kw in query_lower for kw in ["last week", "past week", "this week"]):
        return {'days': 7, 'description': 'in the last week'}

    if any(kw in query_lower for kw in ["last month", "past month", "this month"]):
        return {'days': 30, 'description': 'in the last 30 days'}

    if any(kw in query_lower for kw in ["last year", "past year", "this year"]):
        return {'days': 365, 'description': 'in the last year'}

    # Default to 30 days if no timeframe specified
    return {'days': 30, 'description': 'in the last 30 days'}


def parse_location_from_query(query: str) -> Dict[str, Optional[str]]:
    """
    Parse location filters from natural language query.

    Examples:
    - "drives to Philadelphia" → {'destination': 'Philadelphia'}
    - "trips from home" → {'origin': 'home'}
    - "drives to the office" → {'destination': 'office'}
    - "drives from Costco to home" → {'origin': 'Costco', 'destination': 'home'}
    """
    import re
    query_lower = query.lower()

    result = {'origin': None, 'destination': None}

    # Pattern: "from X to Y"
    from_to_match = re.search(r'from\s+(?:the\s+)?([a-z\s]+?)\s+to\s+(?:the\s+)?([a-z\s]+?)(?:\s|$|\.|\?)', query_lower)
    if from_to_match:
        result['origin'] = from_to_match.group(1).strip()
        result['destination'] = from_to_match.group(2).strip()
        return result

    # Pattern: "to [location]"
    to_match = re.search(r'(?:drives?|trips?|drove)\s+to\s+(?:the\s+)?([a-z\s]+?)(?:\s|$|\.|\?)', query_lower)
    if to_match:
        result['destination'] = to_match.group(1).strip()

    # Pattern: "from [location]"
    from_match = re.search(r'(?:drives?|trips?|drove)\s+from\s+(?:the\s+)?([a-z\s]+?)(?:\s|$|\.|\?)', query_lower)
    if from_match:
        result['origin'] = from_match.group(1).strip()

    return result


def parse_superlative_from_query(query: str) -> Optional[Dict[str, Any]]:
    """
    Parse superlative queries (longest, shortest, fastest, etc.)

    Returns:
    - {'type': 'longest', 'metric': 'distance', 'order': 'DESC'}
    - {'type': 'shortest', 'metric': 'distance', 'order': 'ASC'}
    - None if no superlative found
    """
    query_lower = query.lower()

    superlatives = {
        'longest': {'metric': 'distance', 'order': 'DESC'},
        'farthest': {'metric': 'distance', 'order': 'DESC'},
        'shortest': {'metric': 'distance', 'order': 'ASC'},
        'fastest': {'metric': 'duration', 'order': 'ASC'},  # Fastest = shortest duration
        'slowest': {'metric': 'duration', 'order': 'DESC'},
        'quickest': {'metric': 'duration', 'order': 'ASC'},
        'most efficient': {'metric': 'efficiency', 'order': 'DESC'},
        'least efficient': {'metric': 'efficiency', 'order': 'ASC'},
    }

    for term, config in superlatives.items():
        if term in query_lower:
            return {'type': term, **config}

    return None


def parse_threshold_from_query(query: str) -> Optional[Dict[str, Any]]:
    """
    Parse threshold queries (long drives, short trips, etc.)

    Returns:
    - {'threshold': 'long', 'min_miles': 20}
    - {'threshold': 'short', 'max_miles': 5}
    """
    import re
    query_lower = query.lower()

    # Long/short drives
    if 'long drive' in query_lower or 'long trip' in query_lower:
        return {'threshold': 'long', 'min_miles': 20}
    if 'short drive' in query_lower or 'short trip' in query_lower:
        return {'threshold': 'short', 'max_miles': 5}

    # Specific distance thresholds: "drives over 50 miles", "trips under 10 miles"
    over_match = re.search(r'(?:over|more than|greater than)\s+(\d+)\s*(?:miles?|mi)', query_lower)
    if over_match:
        return {'threshold': 'over', 'min_miles': int(over_match.group(1))}

    under_match = re.search(r'(?:under|less than|shorter than)\s+(\d+)\s*(?:miles?|mi)', query_lower)
    if under_match:
        return {'threshold': 'under', 'max_miles': int(under_match.group(1))}

    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan."""
    global db_pool

    logger.info("tesla_service.startup", msg="Initializing Tesla Metrics RAG service")

    # Register service
    await startup_service(SERVICE_NAME, SERVICE_PORT, "Tesla Metrics Service (Owner Mode Only)")

    # Initialize database pool
    try:
        db_pool = await asyncpg.create_pool(
            host=TESLAMATE_DB_HOST,
            port=TESLAMATE_DB_PORT,
            database=TESLAMATE_DB_NAME,
            user=TESLAMATE_DB_USER,
            password=TESLAMATE_DB_PASS,
            min_size=1,
            max_size=5,
            command_timeout=30,
            ssl=False  # TeslaMate postgres doesn't use SSL
        )
        logger.info("tesla_service.db_connected", host=TESLAMATE_DB_HOST)
    except Exception as e:
        logger.error("tesla_service.db_connection_failed", error=str(e))
        raise

    logger.info("tesla_service.startup.complete")

    yield

    # Cleanup
    logger.info("tesla_service.shutdown")
    await unregister_service(SERVICE_NAME)
    if db_pool:
        await db_pool.close()


app = FastAPI(
    title="Tesla Metrics RAG Service",
    description="Access Tesla vehicle metrics from TeslaMate (Owner Mode Only)",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    db_healthy = False
    if db_pool:
        try:
            async with db_pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_healthy = True
        except Exception:
            pass

    return JSONResponse(
        status_code=200 if db_healthy else 503,
        content={
            "status": "healthy" if db_healthy else "unhealthy",
            "service": "tesla-rag",
            "database": "connected" if db_healthy else "disconnected",
            "owner_mode_only": True
        }
    )


@app.get("/car")
async def get_car_info():
    """Get vehicle information."""
    async with db_pool.acquire() as conn:
        car = await conn.fetchrow("""
            SELECT
                c.id, c.name, c.model, c.vin, c.efficiency,
                c.marketing_name, c.exterior_color, c.wheel_type,
                c.trim_badging
            FROM cars c
            LIMIT 1
        """)

        if not car:
            raise HTTPException(status_code=404, detail="No vehicle found")

        # Get total mileage
        latest_pos = await conn.fetchrow("""
            SELECT odometer FROM positions ORDER BY date DESC LIMIT 1
        """)

        return {
            "success": True,
            "car": {
                "name": car["name"],
                "model": f"Model {car['model']}",
                "marketing_name": car["marketing_name"],
                "vin": car["vin"],
                "color": car["exterior_color"],
                "wheels": car["wheel_type"],
                "trim": car["trim_badging"],
                "efficiency": car["efficiency"],
                "odometer_km": float(latest_pos["odometer"]) if latest_pos else None,
                "odometer_miles": km_to_miles(latest_pos["odometer"]) if latest_pos else None
            }
        }


@app.get("/status")
async def get_current_status():
    """Get current vehicle status including battery, range, and location."""
    async with db_pool.acquire() as conn:
        # Get latest position data
        pos = await conn.fetchrow("""
            SELECT
                p.date, p.battery_level, p.usable_battery_level,
                p.rated_battery_range_km, p.ideal_battery_range_km,
                p.est_battery_range_km, p.odometer,
                p.inside_temp, p.outside_temp,
                p.latitude, p.longitude,
                p.tpms_pressure_fl, p.tpms_pressure_fr,
                p.tpms_pressure_rl, p.tpms_pressure_rr,
                p.is_climate_on
            FROM positions p
            ORDER BY p.date DESC
            LIMIT 1
        """)

        # Get current state
        state = await conn.fetchrow("""
            SELECT state, start_date
            FROM states
            WHERE end_date IS NULL
            ORDER BY start_date DESC
            LIMIT 1
        """)

        if not pos:
            raise HTTPException(status_code=404, detail="No position data found")

        return {
            "success": True,
            "timestamp": pos["date"].isoformat() if pos["date"] else None,
            "state": state["state"] if state else "unknown",
            "state_since": state["start_date"].isoformat() if state else None,
            "battery": {
                "level": pos["battery_level"],
                "usable_level": pos["usable_battery_level"],
                "range_miles": km_to_miles(pos["rated_battery_range_km"]),
                "range_km": float(pos["rated_battery_range_km"]) if pos["rated_battery_range_km"] else None,
                "ideal_range_miles": km_to_miles(pos["ideal_battery_range_km"]),
                "est_range_miles": km_to_miles(pos["est_battery_range_km"])
            },
            "odometer": {
                "km": float(pos["odometer"]) if pos["odometer"] else None,
                "miles": km_to_miles(pos["odometer"])
            },
            "temperature": {
                "inside_f": celsius_to_fahrenheit(pos["inside_temp"]),
                "inside_c": float(pos["inside_temp"]) if pos["inside_temp"] else None,
                "outside_f": celsius_to_fahrenheit(pos["outside_temp"]),
                "outside_c": float(pos["outside_temp"]) if pos["outside_temp"] else None
            },
            "tire_pressure_psi": {
                "front_left": bar_to_psi(pos["tpms_pressure_fl"]),
                "front_right": bar_to_psi(pos["tpms_pressure_fr"]),
                "rear_left": bar_to_psi(pos["tpms_pressure_rl"]),
                "rear_right": bar_to_psi(pos["tpms_pressure_rr"])
            },
            "climate_on": pos["is_climate_on"]
        }


async def _get_drives_internal(
    days: int = 30,
    limit: int = 20,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    destination: Optional[str] = None,
    origin: Optional[str] = None,
    min_distance_miles: Optional[float] = None,
    sort_by: str = "date"
):
    """Internal drive query implementation with plain Python defaults."""
    from datetime import datetime as dt

    async with db_pool.acquire() as conn:
        # Build WHERE clause based on parameters
        where_clauses = []
        params = []
        param_idx = 1

        if start_date and end_date:
            # Use specific date range
            # Convert string dates to date objects for asyncpg
            from datetime import date as date_type
            if isinstance(start_date, str):
                start_date = dt.fromisoformat(start_date).date() if 'T' in start_date else date_type.fromisoformat(start_date)
            if isinstance(end_date, str):
                end_date = dt.fromisoformat(end_date).date() if 'T' in end_date else date_type.fromisoformat(end_date)
            where_clauses.append(f"d.start_date >= ${param_idx}::timestamp AND d.start_date < (${param_idx + 1}::timestamp + interval '1 day')")
            params.extend([start_date, end_date])
            param_idx += 2
        else:
            # Use days lookback
            where_clauses.append(f"d.start_date > NOW() - (${param_idx} * interval '1 day')")
            params.append(days)
            param_idx += 1

        if destination:
            where_clauses.append(f"LOWER(ea.display_name) LIKE ${param_idx}")
            params.append(f"%{destination.lower()}%")
            param_idx += 1

        if origin:
            where_clauses.append(f"LOWER(sa.display_name) LIKE ${param_idx}")
            params.append(f"%{origin.lower()}%")
            param_idx += 1

        if min_distance_miles:
            min_distance_km = min_distance_miles / 0.621371
            where_clauses.append(f"d.distance >= ${param_idx}")
            params.append(min_distance_km)
            param_idx += 1

        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

        # Determine sort order
        sort_map = {
            "date": "d.start_date DESC",
            "distance": "d.distance DESC",
            "duration": "d.duration_min DESC"
        }
        order_sql = sort_map.get(sort_by, "d.start_date DESC")

        # Get recent drives
        drives = await conn.fetch(f"""
            SELECT
                d.id, d.start_date, d.end_date,
                d.distance, d.duration_min,
                d.start_km, d.end_km,
                d.start_rated_range_km, d.end_rated_range_km,
                d.speed_max, d.outside_temp_avg, d.inside_temp_avg,
                sa.display_name as start_location,
                ea.display_name as end_location
            FROM drives d
            LEFT JOIN addresses sa ON d.start_address_id = sa.id
            LEFT JOIN addresses ea ON d.end_address_id = ea.id
            WHERE {where_sql}
            ORDER BY {order_sql}
            LIMIT ${param_idx}
        """, *params, limit)

        # Get aggregate stats with same filters including location
        stats = await conn.fetchrow(f"""
            SELECT
                COUNT(*) as total_drives,
                SUM(d.distance) as total_distance_km,
                AVG(d.distance) as avg_distance_km,
                SUM(d.duration_min) as total_duration_min,
                AVG(d.duration_min) as avg_duration_min,
                MAX(d.speed_max) as max_speed_ever,
                AVG(d.outside_temp_avg) as avg_outside_temp
            FROM drives d
            LEFT JOIN addresses sa ON d.start_address_id = sa.id
            LEFT JOIN addresses ea ON d.end_address_id = ea.id
            WHERE {where_sql}
        """, *params)  # Same params as main query (without limit)

        # Build period description
        if start_date and end_date:
            period_desc = f"{start_date} to {end_date}"
        else:
            period_desc = f"last {days} days"

        return {
            "success": True,
            "period_days": days if not (start_date and end_date) else None,
            "period_description": period_desc,
            "statistics": {
                "total_drives": stats["total_drives"],
                "total_distance_miles": km_to_miles(stats["total_distance_km"]),
                "total_distance_km": float(stats["total_distance_km"]) if stats["total_distance_km"] else 0,
                "avg_distance_miles": km_to_miles(stats["avg_distance_km"]),
                "total_duration_hours": float(stats["total_duration_min"] or 0) / 60,
                "avg_duration_min": float(stats["avg_duration_min"]) if stats["avg_duration_min"] else 0,
                "max_speed_mph": float(stats["max_speed_ever"]) if stats["max_speed_ever"] else 0
            },
            "recent_drives": [
                {
                    "id": d["id"],
                    "start_time": d["start_date"].isoformat() if d["start_date"] else None,
                    "end_time": d["end_date"].isoformat() if d["end_date"] else None,
                    "from": d["start_location"],
                    "to": d["end_location"],
                    "distance_miles": km_to_miles(d["distance"]),
                    "duration_min": d["duration_min"],
                    "range_used_miles": km_to_miles((d["start_rated_range_km"] or 0) - (d["end_rated_range_km"] or 0))
                }
                for d in drives
            ]
        }


@app.get("/drives")
async def get_drives(
    days: int = Query(30, description="Number of days to look back", ge=1, le=365),
    limit: int = Query(20, description="Maximum drives to return", ge=1, le=100),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD) for date range query"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD) for date range query"),
    destination: Optional[str] = Query(None, description="Filter by destination location (partial match)"),
    origin: Optional[str] = Query(None, description="Filter by origin location (partial match)"),
    min_distance_miles: Optional[float] = Query(None, description="Minimum distance in miles"),
    sort_by: Optional[str] = Query("date", description="Sort by: date, distance, duration")
):
    """Get drive history and statistics with flexible filtering."""
    return await _get_drives_internal(
        days=days,
        limit=limit,
        start_date=start_date,
        end_date=end_date,
        destination=destination,
        origin=origin,
        min_distance_miles=min_distance_miles,
        sort_by=sort_by or "date"
    )


@app.get("/charges")
async def get_charges(
    days: int = Query(30, description="Number of days to look back", ge=1, le=365),
    limit: int = Query(20, description="Maximum charges to return", ge=1, le=100)
):
    """Get charging history and statistics."""
    async with db_pool.acquire() as conn:
        # Get recent charging sessions
        charges = await conn.fetch("""
            SELECT
                cp.id, cp.start_date, cp.end_date,
                cp.charge_energy_added, cp.charge_energy_used,
                cp.start_battery_level, cp.end_battery_level,
                cp.start_rated_range_km, cp.end_rated_range_km,
                cp.duration_min, cp.outside_temp_avg, cp.cost,
                a.display_name as location,
                g.name as geofence_name
            FROM charging_processes cp
            LEFT JOIN addresses a ON cp.address_id = a.id
            LEFT JOIN geofences g ON cp.geofence_id = g.id
            WHERE cp.start_date > NOW() - ($1 * interval '1 day')
            ORDER BY cp.start_date DESC
            LIMIT $2
        """, days, limit)

        # Get aggregate stats
        stats = await conn.fetchrow("""
            SELECT
                COUNT(*) as total_charges,
                SUM(charge_energy_added) as total_energy_kwh,
                AVG(charge_energy_added) as avg_energy_kwh,
                SUM(duration_min) as total_duration_min,
                AVG(duration_min) as avg_duration_min,
                SUM(cost) as total_cost,
                AVG(end_battery_level - start_battery_level) as avg_battery_gain
            FROM charging_processes
            WHERE start_date > NOW() - ($1 * interval '1 day')
              AND charge_energy_added > 0
        """, days)

        return {
            "success": True,
            "period_days": days,
            "statistics": {
                "total_charges": stats["total_charges"],
                "total_energy_kwh": float(stats["total_energy_kwh"]) if stats["total_energy_kwh"] else 0,
                "avg_energy_kwh": float(stats["avg_energy_kwh"]) if stats["avg_energy_kwh"] else 0,
                "total_duration_hours": float(stats["total_duration_min"] or 0) / 60,
                "avg_duration_min": float(stats["avg_duration_min"]) if stats["avg_duration_min"] else 0,
                "total_cost": float(stats["total_cost"]) if stats["total_cost"] else None,
                "avg_battery_gain_percent": float(stats["avg_battery_gain"]) if stats["avg_battery_gain"] else 0
            },
            "recent_charges": [
                {
                    "id": c["id"],
                    "start_time": c["start_date"].isoformat() if c["start_date"] else None,
                    "end_time": c["end_date"].isoformat() if c["end_date"] else None,
                    "location": c["location"] or c["geofence_name"],
                    "energy_added_kwh": float(c["charge_energy_added"]) if c["charge_energy_added"] else 0,
                    "start_battery": c["start_battery_level"],
                    "end_battery": c["end_battery_level"],
                    "duration_min": c["duration_min"],
                    "cost": float(c["cost"]) if c["cost"] else None
                }
                for c in charges
            ]
        }


@app.get("/efficiency")
async def get_efficiency(
    days: int = Query(30, description="Number of days to analyze", ge=1, le=365)
):
    """Get efficiency metrics."""
    async with db_pool.acquire() as conn:
        # Calculate efficiency from drives
        efficiency = await conn.fetchrow("""
            SELECT
                SUM(distance) as total_distance_km,
                SUM(start_rated_range_km - end_rated_range_km) as range_used_km,
                COUNT(*) as drive_count
            FROM drives
            WHERE start_date > NOW() - ($1 * interval '1 day')
              AND distance > 0
              AND start_rated_range_km > end_rated_range_km
        """, days)

        # Get car efficiency rating
        car = await conn.fetchrow("SELECT efficiency FROM cars LIMIT 1")

        # Calculate actual vs rated
        if efficiency["total_distance_km"] and efficiency["range_used_km"] and efficiency["range_used_km"] > 0:
            actual_efficiency = float(efficiency["total_distance_km"]) / float(efficiency["range_used_km"])
        else:
            actual_efficiency = None

        return {
            "success": True,
            "period_days": days,
            "efficiency": {
                "rated_efficiency": car["efficiency"] if car else None,
                "actual_efficiency": actual_efficiency,
                "total_distance_miles": km_to_miles(efficiency["total_distance_km"]),
                "total_range_used_miles": km_to_miles(efficiency["range_used_km"]),
                "drives_analyzed": efficiency["drive_count"],
                "efficiency_rating": "good" if actual_efficiency and actual_efficiency >= 1.0 else "below_rated"
            }
        }


@app.get("/battery")
async def get_battery_health(
    days: int = Query(90, description="Days to analyze for degradation", ge=30, le=365)
):
    """Get battery health and degradation metrics."""
    async with db_pool.acquire() as conn:
        # Get battery level samples at 100% charge
        full_charges = await conn.fetch("""
            SELECT
                date, rated_battery_range_km
            FROM positions
            WHERE battery_level >= 99
              AND date > NOW() - ($1 * interval '1 day')
            ORDER BY date
            LIMIT 100
        """, days)

        # Get current and initial ranges
        current = await conn.fetchrow("""
            SELECT rated_battery_range_km, battery_level
            FROM positions
            WHERE rated_battery_range_km IS NOT NULL
            ORDER BY date DESC
            LIMIT 1
        """)

        # Get earliest range at high charge
        earliest = await conn.fetchrow("""
            SELECT rated_battery_range_km
            FROM positions
            WHERE battery_level >= 95
              AND rated_battery_range_km IS NOT NULL
            ORDER BY date ASC
            LIMIT 1
        """)

        # Calculate degradation estimate
        if earliest and current and earliest["rated_battery_range_km"]:
            # Normalize to 100% for comparison
            current_full_range = float(current["rated_battery_range_km"]) * (100 / current["battery_level"])
            earliest_full_range = float(earliest["rated_battery_range_km"]) * (100 / 95)  # Approximate
            degradation_percent = ((earliest_full_range - current_full_range) / earliest_full_range) * 100
        else:
            degradation_percent = None

        return {
            "success": True,
            "battery_health": {
                "current_level": current["battery_level"] if current else None,
                "current_range_miles": km_to_miles(current["rated_battery_range_km"]) if current else None,
                "estimated_degradation_percent": round(degradation_percent, 2) if degradation_percent else None,
                "health_status": "good" if degradation_percent and degradation_percent < 10 else
                               "moderate" if degradation_percent and degradation_percent < 20 else "check_needed",
                "full_charge_samples": len(full_charges)
            }
        }


@app.get("/states")
async def get_state_history(
    days: int = Query(7, description="Days to look back", ge=1, le=30),
    limit: int = Query(50, description="Maximum states to return", ge=1, le=200)
):
    """Get vehicle state history (online, offline, driving, charging, etc.)."""
    async with db_pool.acquire() as conn:
        states = await conn.fetch("""
            SELECT state, start_date, end_date,
                   EXTRACT(EPOCH FROM (COALESCE(end_date, NOW()) - start_date))/3600 as duration_hours
            FROM states
            WHERE start_date > NOW() - ($1 * interval '1 day')
            ORDER BY start_date DESC
            LIMIT $2
        """, days, limit)

        # Aggregate time in each state
        state_summary = await conn.fetch("""
            SELECT
                state,
                COUNT(*) as count,
                SUM(EXTRACT(EPOCH FROM (COALESCE(end_date, NOW()) - start_date))/3600) as total_hours
            FROM states
            WHERE start_date > NOW() - ($1 * interval '1 day')
            GROUP BY state
        """, days)

        return {
            "success": True,
            "period_days": days,
            "summary": {
                s["state"]: {
                    "count": s["count"],
                    "total_hours": round(float(s["total_hours"]), 2)
                }
                for s in state_summary
            },
            "history": [
                {
                    "state": s["state"],
                    "start": s["start_date"].isoformat(),
                    "end": s["end_date"].isoformat() if s["end_date"] else None,
                    "duration_hours": round(float(s["duration_hours"]), 2)
                }
                for s in states
            ]
        }


@app.get("/updates")
async def get_software_updates():
    """Get software update history."""
    async with db_pool.acquire() as conn:
        updates = await conn.fetch("""
            SELECT version, start_date, end_date
            FROM updates
            ORDER BY start_date DESC
            LIMIT 20
        """)

        return {
            "success": True,
            "updates": [
                {
                    "version": u["version"],
                    "installed_date": u["start_date"].isoformat() if u["start_date"] else None,
                    "superseded_date": u["end_date"].isoformat() if u["end_date"] else None,
                    "current": u["end_date"] is None
                }
                for u in updates
            ],
            "current_version": updates[0]["version"] if updates else None
        }


@app.get("/vampire-drain")
async def get_vampire_drain(
    days: int = Query(7, description="Days to analyze", ge=1, le=30)
):
    """Analyze phantom/vampire drain when vehicle is parked."""
    async with db_pool.acquire() as conn:
        # Find periods where car was parked (not driving, not charging) and lost range
        drain_periods = await conn.fetch("""
            WITH ranked_positions AS (
                SELECT
                    date,
                    battery_level,
                    rated_battery_range_km,
                    LAG(battery_level) OVER (ORDER BY date) as prev_battery,
                    LAG(rated_battery_range_km) OVER (ORDER BY date) as prev_range,
                    LAG(date) OVER (ORDER BY date) as prev_date,
                    drive_id
                FROM positions
                WHERE date > NOW() - ($1 * interval '1 day')
            )
            SELECT
                prev_date as start_date,
                date as end_date,
                prev_battery - battery_level as battery_lost,
                prev_range - rated_battery_range_km as range_lost_km,
                EXTRACT(EPOCH FROM (date - prev_date))/3600 as hours_parked
            FROM ranked_positions
            WHERE drive_id IS NULL
              AND prev_battery > battery_level
              AND prev_battery - battery_level > 0
              AND EXTRACT(EPOCH FROM (date - prev_date))/3600 > 1
            ORDER BY start_date DESC
            LIMIT 50
        """, days)

        # Calculate average drain rate
        total_drain = sum(d["battery_lost"] for d in drain_periods if d["battery_lost"])
        total_hours = sum(d["hours_parked"] for d in drain_periods if d["hours_parked"])

        avg_drain_per_day = (total_drain / total_hours * 24) if total_hours > 0 else 0

        return {
            "success": True,
            "period_days": days,
            "vampire_drain": {
                "avg_drain_percent_per_day": round(avg_drain_per_day, 2),
                "total_drain_percent": round(total_drain, 1) if total_drain else 0,
                "total_parked_hours": round(total_hours, 1) if total_hours else 0,
                "drain_events_analyzed": len(drain_periods),
                "status": "normal" if avg_drain_per_day < 2 else
                         "elevated" if avg_drain_per_day < 5 else "high"
            }
        }


@app.get("/stats")
async def get_aggregate_stats():
    """Get comprehensive aggregate statistics."""
    async with db_pool.acquire() as conn:
        # Lifetime stats
        lifetime = await conn.fetchrow("""
            SELECT
                (SELECT MAX(odometer) FROM positions) as total_km,
                (SELECT COUNT(*) FROM drives) as total_drives,
                (SELECT SUM(distance) FROM drives) as total_driven_km,
                (SELECT COUNT(*) FROM charging_processes) as total_charges,
                (SELECT SUM(charge_energy_added) FROM charging_processes) as total_energy_kwh,
                (SELECT MIN(date) FROM positions) as tracking_since
        """)

        # 30-day stats
        monthly = await conn.fetchrow("""
            SELECT
                (SELECT COUNT(*) FROM drives WHERE start_date > NOW() - INTERVAL '30 days') as drives_30d,
                (SELECT SUM(distance) FROM drives WHERE start_date > NOW() - INTERVAL '30 days') as distance_30d_km,
                (SELECT COUNT(*) FROM charging_processes WHERE start_date > NOW() - INTERVAL '30 days') as charges_30d,
                (SELECT SUM(charge_energy_added) FROM charging_processes WHERE start_date > NOW() - INTERVAL '30 days') as energy_30d_kwh
        """)

        return {
            "success": True,
            "lifetime": {
                "total_miles": km_to_miles(lifetime["total_km"]),
                "total_drives": lifetime["total_drives"],
                "total_driven_miles": km_to_miles(lifetime["total_driven_km"]),
                "total_charges": lifetime["total_charges"],
                "total_energy_kwh": float(lifetime["total_energy_kwh"]) if lifetime["total_energy_kwh"] else 0,
                "tracking_since": lifetime["tracking_since"].isoformat() if lifetime["tracking_since"] else None
            },
            "last_30_days": {
                "drives": monthly["drives_30d"],
                "distance_miles": km_to_miles(monthly["distance_30d_km"]),
                "charges": monthly["charges_30d"],
                "energy_kwh": float(monthly["energy_30d_kwh"]) if monthly["energy_30d_kwh"] else 0
            }
        }


@app.get("/query")
async def natural_query(
    q: str = Query(..., description="Natural language query about your Tesla")
):
    """
    Process natural language queries about Tesla metrics.

    Examples:
    - "What's my current battery level?"
    - "How many miles have I driven this month?"
    - "When was my last charge?"
    - "What's my vampire drain like?"
    """
    query_lower = q.lower()

    try:
        # Route to appropriate endpoint based on query keywords
        if any(kw in query_lower for kw in ["battery", "charge level", "range", "how much charge"]):
            status = await get_current_status()
            return {
                "success": True,
                "query": q,
                "answer": f"Your Tesla is currently at {status['battery']['level']}% battery with an estimated range of {status['battery']['range_miles']:.0f} miles. The vehicle is {status['state']}.",
                "data": status
            }

        elif any(kw in query_lower for kw in [
            "drive", "drove", "trip", "miles driven", "distance",
            "miles", "far", "traveled",
            "christmas", "thanksgiving", "new year", "halloween", "easter",
            "july 4", "memorial day", "labor day", "veterans day"
        ]):
            # Parse all query components
            timeframe = parse_timeframe_from_query(q)
            locations = parse_location_from_query(q)
            superlative = parse_superlative_from_query(q)
            threshold = parse_threshold_from_query(q)

            # Build parameters for get_drives
            drive_params = {'limit': 20}

            # Handle timeframe (days or date range)
            if 'start_date' in timeframe:
                drive_params['start_date'] = timeframe['start_date'].isoformat()
                drive_params['end_date'] = timeframe['end_date'].isoformat()
            else:
                drive_params['days'] = timeframe.get('days', 30)

            # Handle location filters
            if locations['destination']:
                drive_params['destination'] = locations['destination']
            if locations['origin']:
                drive_params['origin'] = locations['origin']

            # Handle superlative (sort order)
            if superlative:
                drive_params['sort_by'] = superlative['metric']
                drive_params['limit'] = 5  # Return top N for superlatives

            # Handle threshold (min distance)
            if threshold and 'min_miles' in threshold:
                drive_params['min_distance_miles'] = threshold['min_miles']

            drives = await _get_drives_internal(**drive_params)
            stats = drives["statistics"]
            timeframe_desc = timeframe.get('description', 'in the last 30 days')

            # Build answer based on query type
            if superlative:
                if drives["recent_drives"]:
                    top_drive = drives["recent_drives"][0]
                    answer = f"Your {superlative['type']} drive was {top_drive['distance_miles']:.1f} miles from {top_drive['from'] or 'unknown'} to {top_drive['to'] or 'unknown'} on {top_drive['start_time'][:10] if top_drive['start_time'] else 'unknown date'}."
                else:
                    answer = f"No drives found {timeframe_desc}."
            elif locations['destination'] or locations['origin']:
                loc_desc = ""
                if locations['destination']:
                    loc_desc = f"to {locations['destination']}"
                if locations['origin']:
                    loc_desc = f"from {locations['origin']}" + (f" {loc_desc}" if loc_desc else "")
                answer = f"{timeframe_desc.capitalize()}, you've taken {stats['total_drives']} drives {loc_desc} covering {stats['total_distance_miles']:.1f} miles total."
            else:
                answer = f"{timeframe_desc.capitalize()}, you've taken {stats['total_drives']} drives covering {stats['total_distance_miles']:.1f} miles total. Average drive is {stats['avg_distance_miles']:.1f} miles."

            return {
                "success": True,
                "query": q,
                "answer": answer,
                "data": drives
            }

        elif any(kw in query_lower for kw in ["charg", "energy", "kwh"]):
            timeframe = parse_timeframe_from_query(q)
            days = timeframe.get('days', 30)
            charges = await get_charges(days=days, limit=20)
            stats = charges["statistics"]
            timeframe_desc = timeframe.get('description', 'in the last 30 days')
            return {
                "success": True,
                "query": q,
                "answer": f"{timeframe_desc.capitalize()}, you've charged {stats['total_charges']} times, adding {stats['total_energy_kwh']:.1f} kWh of energy. Average charge adds {stats['avg_energy_kwh']:.1f} kWh.",
                "data": charges
            }

        elif any(kw in query_lower for kw in ["vampire", "phantom", "drain", "parked"]):
            drain = await get_vampire_drain(days=7)
            return {
                "success": True,
                "query": q,
                "answer": f"Your vampire drain is {drain['vampire_drain']['status']} at {drain['vampire_drain']['avg_drain_percent_per_day']:.1f}% per day when parked.",
                "data": drain
            }

        elif any(kw in query_lower for kw in ["efficiency", "kwh/mile", "consumption"]):
            timeframe = parse_timeframe_from_query(q)
            days = timeframe.get('days', 30)
            eff = await get_efficiency(days=days)
            timeframe_desc = timeframe.get('description', 'the last 30 days')
            return {
                "success": True,
                "query": q,
                "answer": f"Your driving efficiency over {timeframe_desc} is {eff['efficiency']['efficiency_rating']}. You've driven {eff['efficiency']['total_distance_miles']:.1f} miles using {eff['efficiency']['total_range_used_miles']:.1f} miles of rated range.",
                "data": eff
            }

        elif any(kw in query_lower for kw in ["health", "degradation", "battery health"]):
            health = await get_battery_health(days=90)
            bh = health["battery_health"]
            return {
                "success": True,
                "query": q,
                "answer": f"Battery health status: {bh['health_status']}. Estimated degradation: {bh['estimated_degradation_percent']}%." if bh['estimated_degradation_percent'] else "Battery health data still being collected.",
                "data": health
            }

        elif any(kw in query_lower for kw in ["update", "software", "version"]):
            updates = await get_software_updates()
            return {
                "success": True,
                "query": q,
                "answer": f"Your Tesla is running software version {updates['current_version']}.",
                "data": updates
            }

        elif any(kw in query_lower for kw in ["car", "vehicle", "info", "model", "what car"]):
            car = await get_car_info()
            c = car["car"]
            return {
                "success": True,
                "query": q,
                "answer": f"Your Tesla is a {c['marketing_name']} ({c['model']}) named '{c['name']}' in {c['color']} with {c['odometer_miles']:.0f} miles on it.",
                "data": car
            }

        elif any(kw in query_lower for kw in ["status", "state", "where", "temperature", "tire"]):
            status = await get_current_status()
            return {
                "success": True,
                "query": q,
                "answer": f"Your Tesla is currently {status['state']}. Battery at {status['battery']['level']}% ({status['battery']['range_miles']:.0f} miles range). Inside temp: {status['temperature']['inside_f']:.0f}°F, Outside: {status['temperature']['outside_f']:.0f}°F.",
                "data": status
            }

        elif any(kw in query_lower for kw in ["stat", "summary", "overview", "total"]):
            stats = await get_aggregate_stats()
            return {
                "success": True,
                "query": q,
                "answer": f"Lifetime: {stats['lifetime']['total_miles']:.0f} miles, {stats['lifetime']['total_drives']} drives, {stats['lifetime']['total_charges']} charges. Last 30 days: {stats['last_30_days']['distance_miles']:.0f} miles driven.",
                "data": stats
            }

        else:
            # Default to status
            status = await get_current_status()
            return {
                "success": True,
                "query": q,
                "answer": f"Your Tesla is {status['state']} with {status['battery']['level']}% battery and {status['battery']['range_miles']:.0f} miles of range.",
                "data": status
            }

    except Exception as e:
        logger.error("tesla_query_error", error=str(e), query=q)
        raise HTTPException(status_code=500, detail=f"Query failed: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None
    )
