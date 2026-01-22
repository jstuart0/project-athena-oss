"""Sports RAG Service - TheSportsDB Integration

Provides sports team and game data retrieval with caching.

Endpoints:
- GET /health - Health check
- GET /sports/teams/search?query={query} - Search teams
- GET /sports/teams/{team_id} - Get team details
- GET /sports/events/{team_id}/next - Get next events for team
- GET /sports/events/{team_id}/last - Get last events for team
"""

import os
import sys
import asyncio
import json
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
import httpx
from contextlib import asynccontextmanager
import feedparser

# Import shared utilities
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

from shared.cache import CacheClient, cached
from shared.service_registry import startup_service, unregister_service
from shared.logging_config import configure_logging
from shared.metrics import setup_metrics_endpoint

# Configure logging
logger = configure_logging("sports-rag")

SERVICE_NAME = "sports-rag"

# Environment variables / defaults
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "http://localhost:8080")
NEWS_GNEWS_API_KEY = os.getenv("GNEWS_API_KEY")  # Optional key if provided via admin
THESPORTSDB_API_KEY = os.getenv("THESPORTSDB_API_KEY", "3")  # Free tier key
THESPORTSDB_BASE_URL = os.getenv(
    "THESPORTSDB_BASE_URL",
    f"https://www.thesportsdb.com/api/v1/json/{THESPORTSDB_API_KEY}"
)
API_FOOTBALL_KEY_DEFAULT = os.getenv("API_FOOTBALL_KEY", "")
API_FOOTBALL_BASE_URL_DEFAULT = os.getenv("API_FOOTBALL_BASE_URL", "https://v3.football.api-sports.io")
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8017"))

# Fixed endpoints
ESPN_BASE_URL = "https://site.api.espn.com/apis/site/v2/sports"
OLYMPICS_BASE_URL = "https://olympics.com"

# Cache client and HTTP client
cache = None
http_client = None

# API configs loaded from admin database (with env fallbacks)
api_configs = {
    "thesportsdb": {"endpoint_url": THESPORTSDB_BASE_URL, "api_key": None},
    "espn": {"endpoint_url": ESPN_BASE_URL, "api_key": None},
    "api-football": {"endpoint_url": API_FOOTBALL_BASE_URL_DEFAULT, "api_key": API_FOOTBALL_KEY_DEFAULT},
    "olympics": {"endpoint_url": OLYMPICS_BASE_URL, "api_key": None},
}

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown."""
    global cache, http_client

    # Startup
    logger.info("Starting Sports RAG service")

    # Register service in registry (kills stale process on port if any)
    await startup_service("sports", SERVICE_PORT, "Sports Service")

    cache = CacheClient(url=REDIS_URL)
    await cache.connect()

    # OPTIMIZATION: Create reusable HTTP client
    http_client = httpx.AsyncClient(timeout=10.0)
    logger.info("HTTP client initialized")

    # Load API configs from admin API (with fallbacks)
    await initialize_api_configs()

    yield

    # Shutdown
    logger.info("Shutting down Sports RAG service")

    # Unregister from service registry
    await unregister_service("sports")

    if http_client:
        await http_client.aclose()
    if cache:
        await cache.disconnect()

app = FastAPI(
    title="Sports RAG Service",
    description="TheSportsDB integration with caching",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

async def get_api_key_config(service_name: str) -> Optional[Dict[str, Any]]:
    """Fetch API key/config from admin backend."""
    try:
        response = await http_client.get(
            f"{ADMIN_API_URL}/api/external-api-keys/public/{service_name}/key",
            timeout=5.0
        )
        response.raise_for_status()
        data = response.json()
        return {
            "endpoint_url": data.get("endpoint_url"),
            "api_key": data.get("api_key"),
            "rate_limit_per_minute": data.get("rate_limit_per_minute"),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch API key for {service_name}: {e}")
        return None

async def initialize_api_configs():
    """Load API configuration from admin backend with env fallbacks."""
    global api_configs

    api_football = await get_api_key_config("api-football")
    if api_football and api_football.get("api_key"):
        api_configs["api-football"].update(api_football)
        logger.info("Loaded API-Football config from admin API")
    else:
        logger.info("Using fallback API-Football config (env)", has_key=bool(API_FOOTBALL_KEY_DEFAULT))

    thesportsdb = await get_api_key_config("thesportsdb")
    if thesportsdb and thesportsdb.get("endpoint_url"):
        api_configs["thesportsdb"].update(thesportsdb)
        logger.info("Loaded TheSportsDB config from admin API")

    # ESPN has no key, but allow admin override of endpoint
    espn_cfg = await get_api_key_config("espn")
    if espn_cfg and espn_cfg.get("endpoint_url"):
        api_configs["espn"].update({"endpoint_url": espn_cfg["endpoint_url"]})

    # Olympics (currently public endpoints; no key expected)
    olympics_cfg = await get_api_key_config("olympics")
    if olympics_cfg and olympics_cfg.get("endpoint_url"):
        api_configs["olympics"].update({"endpoint_url": olympics_cfg["endpoint_url"]})

    logger.info(
        "api_configs_initialized",
        api_football=bool(api_configs.get("api-football", {}).get("api_key")),
        thesportsdb_endpoint=api_configs["thesportsdb"]["endpoint_url"],
        espn_endpoint=api_configs["espn"]["endpoint_url"],
        olympics_endpoint=api_configs["olympics"]["endpoint_url"],
    )

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "sports-rag",
        "version": "1.0.0"
    }

def build_url(base: str, path: str) -> str:
    """Safely join base URL and path."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"

def parse_team_identifier(team_id: str) -> Tuple[str, str, Optional[str]]:
    """
    Parse team identifier into provider, raw id, and optional sport.
    Formats:
        - thesportsdb: "<numeric>"
        - espn: "espn:<sport>:<team_id>"
        - api-football: "api-football:<team_id>"
    """
    if ":" not in team_id:
        return "thesportsdb", team_id, None

    parts = team_id.split(":")
    if len(parts) == 3 and parts[0] == "espn":
        # Only replace the FIRST hyphen with / to separate sport from league
        # e.g., "football-nfl" -> "football/nfl", "football-college-football" -> "football/college-football"
        sport_path = parts[1].replace("-", "/", 1)
        return "espn", parts[2], sport_path
    if len(parts) == 2 and parts[0] == "api-football":
        return "api-football", parts[1], None

    # Olympics events may use "olympics:<sport>:<id>" in future expansion
    if len(parts) >= 2 and parts[0] == "olympics":
        return "olympics", parts[-1], ":".join(parts[1:-1])

    return "thesportsdb", team_id, None

def _normalize_thesportsdb_team(team: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize TheSportsDB team payload."""
    normalized = dict(team)
    normalized["source"] = "thesportsdb"
    return normalized

def _normalize_espn_team(team: Dict[str, Any], sport: str) -> Dict[str, Any]:
    """Normalize ESPN team payload into expected fields."""
    team_id = team.get("id") or team.get("uid", "").split(":")[-1]
    display_name = team.get("displayName") or team.get("name")
    sport_safe = sport.replace("/", "-") if sport else sport
    return {
        "idTeam": f"espn:{sport_safe}:{team_id}",
        "strTeam": display_name,
        "strTeamShort": team.get("abbreviation"),
        "strLeague": sport,
        "strSport": sport.split("/")[0] if sport else None,
        "source": "espn"
    }

def _normalize_api_football_team(team_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize API-Football team payload."""
    team = team_payload.get("team", {})
    return {
        "idTeam": f"api-football:{team.get('id')}",
        "strTeam": team.get("name"),
        "strLeague": team_payload.get("country"),
        "strSport": "soccer",
        "strStadium": team.get("venue", {}).get("name") if isinstance(team.get("venue"), dict) else None,
        "source": "api-football"
    }

def _sort_events_by_date(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Sort events by date, prioritize upcoming; fallback to recent past if no future."""
    now = datetime.now(timezone.utc).date()
    parsed: List[Tuple[datetime.date, Dict[str, Any]]] = []
    for ev in events:
        date_str = ev.get("dateEvent") or ev.get("date") or ""
        try:
            # Support ISO date or datetime
            d = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        except Exception:
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                continue
        parsed.append((d, ev))

    future = [(d, ev) for d, ev in parsed if d >= now]
    past = [(d, ev) for d, ev in parsed if d < now]

    future_sorted = sorted(future, key=lambda x: x[0])
    past_sorted = sorted(past, key=lambda x: x[0], reverse=True)

    ordered = future_sorted or past_sorted
    return [ev for _, ev in ordered]

def _filter_events_window(events: List[Dict[str, Any]], days_ahead: int = 7) -> List[Dict[str, Any]]:
    """Filter events to today through today+days_ahead."""
    now = datetime.now(timezone.utc).date()
    window_end = now + timedelta(days=days_ahead)
    filtered: List[Dict[str, Any]] = []
    for ev in events:
        date_str = ev.get("dateEvent") or ev.get("date")
        try:
            d = datetime.fromisoformat((date_str or "").replace("Z", "+00:00")).date()
        except Exception:
            try:
                d = datetime.strptime(date_str or "", "%Y-%m-%d").date()
            except Exception:
                continue
        if now <= d <= window_end:
            filtered.append(ev)
    return filtered

async def fetch_news(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Fetch sports news headlines using RSS (Google News) with optional GNews API override.
    """
    headlines: List[Dict[str, Any]] = []

    # Prefer GNews API if key available
    if NEWS_GNEWS_API_KEY:
        try:
            resp = await http_client.get(
                "https://gnews.io/api/v4/search",
                params={"q": query, "lang": "en", "max": limit, "token": NEWS_GNEWS_API_KEY},
                timeout=5.0
            )
            resp.raise_for_status()
            data = resp.json()
            for article in data.get("articles", [])[:limit]:
                headlines.append({
                    "title": article.get("title"),
                    "link": article.get("url"),
                    "published": article.get("publishedAt"),
                    "source": article.get("source", {}).get("name", "gnews")
                })
        except Exception as e:
            logger.warning(f"GNews fetch failed, falling back to RSS: {e}")

    if len(headlines) < limit:
        # Fallback to Google News RSS search
        try:
            rss_url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}"
            feed = feedparser.parse(rss_url)
            for entry in feed.entries[:limit]:
                headlines.append({
                    "title": entry.get("title"),
                    "link": entry.get("link"),
                    "published": entry.get("published"),
                    "source": entry.get("source", {}).get("title") if entry.get("source") else "google_news"
                })
        except Exception as e:
            logger.warning(f"RSS fetch failed: {e}")

    return headlines[:limit]

async def fetch_olympics_events(query: str, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Best-effort Olympics events via news headlines (no stable free schedule API).
    """
    enriched_query = f"Olympics {query} schedule"
    headlines = await fetch_news(enriched_query, limit=limit)
    events: List[Dict[str, Any]] = []
    for h in headlines:
        events.append({
            "strEvent": h.get("title"),
            "dateEvent": h.get("published"),
            "strHomeTeam": None,
            "strAwayTeam": None,
            "source": h.get("source", "olympics-news"),
            "link": h.get("link")
        })
    return events

# League to ESPN path mapping
LEAGUE_TO_ESPN_PATH = {
    "nfl": "football/nfl",
    "college-football": "football/college-football",
    "nba": "basketball/nba",
    "college-basketball": "basketball/mens-college-basketball",
    "wnba": "basketball/wnba",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
    "mls": "soccer/usa.1",
    "premier-league": "soccer/eng.1",
    "la-liga": "soccer/esp.1",
    "bundesliga": "soccer/ger.1",
    "serie-a": "soccer/ita.1",
    "ligue-1": "soccer/fra.1",
    "liga-mx": "soccer/mex.1",
    "champions-league": "soccer/uefa.champions",
    "europa-league": "soccer/uefa.europa",
    "international": "soccer/fifa.world",
}

# League season calendar (months when league is active, 1-indexed)
# Used to determine which sport to default to when team name is ambiguous
LEAGUE_SEASONS = {
    "football/nfl": [9, 10, 11, 12, 1, 2],  # Sep - Feb (Super Bowl)
    "football/college-football": [8, 9, 10, 11, 12, 1],  # Aug - Jan (bowl games)
    "basketball/nba": [10, 11, 12, 1, 2, 3, 4, 5, 6],  # Oct - June
    "basketball/mens-college-basketball": [11, 12, 1, 2, 3, 4],  # Nov - Apr (March Madness)
    "basketball/wnba": [5, 6, 7, 8, 9, 10],  # May - Oct
    "baseball/mlb": [3, 4, 5, 6, 7, 8, 9, 10, 11],  # Mar - Nov (World Series)
    "hockey/nhl": [10, 11, 12, 1, 2, 3, 4, 5, 6],  # Oct - June
    "soccer/usa.1": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],  # Feb - Dec
    "soccer/eng.1": [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],  # Aug - May
    "soccer/esp.1": [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],  # Aug - May
    "soccer/ger.1": [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],  # Aug - May
    "soccer/ita.1": [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],  # Aug - May
    "soccer/fra.1": [8, 9, 10, 11, 12, 1, 2, 3, 4, 5],  # Aug - May
    "soccer/mex.1": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],  # Year-round (Apertura/Clausura)
    "soccer/uefa.champions": [9, 10, 11, 12, 1, 2, 3, 4, 5, 6],  # Sep - Jun
    "soccer/uefa.europa": [9, 10, 11, 12, 1, 2, 3, 4, 5, 6],  # Sep - Jun
    "soccer/fifa.world": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],  # Varies, always relevant
}

# Team aliases - map common/short names to search terms and preferred league
# Format: "alias": {"search": "full team name", "league": "preferred-league" or None}
TEAM_ALIASES = {
    # NFL Teams
    "giants": {"search": "Giants", "leagues": ["football/nfl", "baseball/mlb"]},  # NY Giants (NFL) or SF Giants (MLB)
    "jets": {"search": "Jets", "leagues": ["football/nfl"]},
    "bills": {"search": "Bills", "leagues": ["football/nfl"]},
    "pats": {"search": "Patriots", "leagues": ["football/nfl"]},
    "patriots": {"search": "Patriots", "leagues": ["football/nfl"]},
    "dolphins": {"search": "Dolphins", "leagues": ["football/nfl"]},
    "ravens": {"search": "Ravens", "leagues": ["football/nfl"]},
    "steelers": {"search": "Steelers", "leagues": ["football/nfl"]},
    "bengals": {"search": "Bengals", "leagues": ["football/nfl"]},
    "browns": {"search": "Browns", "leagues": ["football/nfl"]},
    "cowboys": {"search": "Cowboys", "leagues": ["football/nfl"]},
    "eagles": {"search": "Eagles", "leagues": ["football/nfl"]},
    "commanders": {"search": "Commanders", "leagues": ["football/nfl"]},
    "niners": {"search": "49ers", "leagues": ["football/nfl"]},
    "49ers": {"search": "49ers", "leagues": ["football/nfl"]},
    "seahawks": {"search": "Seahawks", "leagues": ["football/nfl"]},
    "cardinals": {"search": "Cardinals", "leagues": ["football/nfl", "baseball/mlb"]},  # AZ Cardinals or STL Cardinals
    "rams": {"search": "Rams", "leagues": ["football/nfl"]},
    "chiefs": {"search": "Chiefs", "leagues": ["football/nfl"]},
    "raiders": {"search": "Raiders", "leagues": ["football/nfl"]},
    "broncos": {"search": "Broncos", "leagues": ["football/nfl"]},
    "chargers": {"search": "Chargers", "leagues": ["football/nfl"]},
    "packers": {"search": "Packers", "leagues": ["football/nfl"]},
    "bears": {"search": "Bears", "leagues": ["football/nfl"]},
    "vikings": {"search": "Vikings", "leagues": ["football/nfl"]},
    "lions": {"search": "Lions", "leagues": ["football/nfl"]},
    "falcons": {"search": "Falcons", "leagues": ["football/nfl"]},
    "panthers": {"search": "Panthers", "leagues": ["football/nfl"]},
    "saints": {"search": "Saints", "leagues": ["football/nfl"]},
    "bucs": {"search": "Buccaneers", "leagues": ["football/nfl"]},
    "buccaneers": {"search": "Buccaneers", "leagues": ["football/nfl"]},
    "texans": {"search": "Texans", "leagues": ["football/nfl"]},
    "colts": {"search": "Colts", "leagues": ["football/nfl"]},
    "jaguars": {"search": "Jaguars", "leagues": ["football/nfl"]},
    "titans": {"search": "Titans", "leagues": ["football/nfl"]},

    # College Football (Big Names)
    "michigan": {"search": "Michigan Wolverines", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "wolverines": {"search": "Michigan Wolverines", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "ohio state": {"search": "Ohio State Buckeyes", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "buckeyes": {"search": "Ohio State Buckeyes", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "alabama": {"search": "Alabama Crimson Tide", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "crimson tide": {"search": "Alabama Crimson Tide", "leagues": ["football/college-football"]},
    "georgia": {"search": "Georgia Bulldogs", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "bulldogs": {"search": "Georgia Bulldogs", "leagues": ["football/college-football"]},  # Could be multiple
    "texas": {"search": "Texas Longhorns", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "longhorns": {"search": "Texas Longhorns", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "notre dame": {"search": "Notre Dame Fighting Irish", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "fighting irish": {"search": "Notre Dame Fighting Irish", "leagues": ["football/college-football"]},
    "usc": {"search": "USC Trojans", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "trojans": {"search": "USC Trojans", "leagues": ["football/college-football"]},
    "clemson": {"search": "Clemson Tigers", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "penn state": {"search": "Penn State Nittany Lions", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "nittany lions": {"search": "Penn State Nittany Lions", "leagues": ["football/college-football"]},
    "lsu": {"search": "LSU Tigers", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "florida": {"search": "Florida Gators", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "gators": {"search": "Florida Gators", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "oklahoma": {"search": "Oklahoma Sooners", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "sooners": {"search": "Oklahoma Sooners", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "oregon": {"search": "Oregon Ducks", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "ducks": {"search": "Oregon Ducks", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "tennessee": {"search": "Tennessee Volunteers", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "vols": {"search": "Tennessee Volunteers", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "auburn": {"search": "Auburn Tigers", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "miami": {"search": "Miami Hurricanes", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "hurricanes": {"search": "Miami Hurricanes", "leagues": ["football/college-football"]},
    "wisconsin": {"search": "Wisconsin Badgers", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "badgers": {"search": "Wisconsin Badgers", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "iowa": {"search": "Iowa Hawkeyes", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},
    "hawkeyes": {"search": "Iowa Hawkeyes", "leagues": ["football/college-football", "basketball/mens-college-basketball"]},

    # NBA Teams
    "lakers": {"search": "Lakers", "leagues": ["basketball/nba"]},
    "celtics": {"search": "Celtics", "leagues": ["basketball/nba"]},
    "warriors": {"search": "Warriors", "leagues": ["basketball/nba"]},
    "heat": {"search": "Heat", "leagues": ["basketball/nba"]},
    "nets": {"search": "Nets", "leagues": ["basketball/nba"]},
    "knicks": {"search": "Knicks", "leagues": ["basketball/nba"]},
    "sixers": {"search": "76ers", "leagues": ["basketball/nba"]},
    "76ers": {"search": "76ers", "leagues": ["basketball/nba"]},
    "bulls": {"search": "Bulls", "leagues": ["basketball/nba"]},
    "cavs": {"search": "Cavaliers", "leagues": ["basketball/nba"]},
    "cavaliers": {"search": "Cavaliers", "leagues": ["basketball/nba"]},
    "bucks": {"search": "Bucks", "leagues": ["basketball/nba"]},
    "suns": {"search": "Suns", "leagues": ["basketball/nba"]},
    "mavs": {"search": "Mavericks", "leagues": ["basketball/nba"]},
    "mavericks": {"search": "Mavericks", "leagues": ["basketball/nba"]},
    "spurs": {"search": "Spurs", "leagues": ["basketball/nba"]},
    "rockets": {"search": "Rockets", "leagues": ["basketball/nba"]},
    "nuggets": {"search": "Nuggets", "leagues": ["basketball/nba"]},
    "thunder": {"search": "Thunder", "leagues": ["basketball/nba"]},
    "blazers": {"search": "Trail Blazers", "leagues": ["basketball/nba"]},
    "trail blazers": {"search": "Trail Blazers", "leagues": ["basketball/nba"]},
    "jazz": {"search": "Jazz", "leagues": ["basketball/nba"]},
    "clippers": {"search": "Clippers", "leagues": ["basketball/nba"]},
    "kings": {"search": "Kings", "leagues": ["basketball/nba"]},
    "pelicans": {"search": "Pelicans", "leagues": ["basketball/nba"]},
    "grizzlies": {"search": "Grizzlies", "leagues": ["basketball/nba"]},
    "timberwolves": {"search": "Timberwolves", "leagues": ["basketball/nba"]},
    "wolves": {"search": "Timberwolves", "leagues": ["basketball/nba"]},
    "raptors": {"search": "Raptors", "leagues": ["basketball/nba"]},
    "pacers": {"search": "Pacers", "leagues": ["basketball/nba"]},
    "pistons": {"search": "Pistons", "leagues": ["basketball/nba"]},
    "magic": {"search": "Magic", "leagues": ["basketball/nba"]},
    "wizards": {"search": "Wizards", "leagues": ["basketball/nba"]},
    "hornets": {"search": "Hornets", "leagues": ["basketball/nba"]},
    "hawks": {"search": "Hawks", "leagues": ["basketball/nba"]},

    # MLB Teams
    "yankees": {"search": "Yankees", "leagues": ["baseball/mlb"]},
    "red sox": {"search": "Red Sox", "leagues": ["baseball/mlb"]},
    "sox": {"search": "Sox", "leagues": ["baseball/mlb"]},  # Could be Red Sox or White Sox
    "dodgers": {"search": "Dodgers", "leagues": ["baseball/mlb"]},
    "mets": {"search": "Mets", "leagues": ["baseball/mlb"]},
    "cubs": {"search": "Cubs", "leagues": ["baseball/mlb"]},
    "braves": {"search": "Braves", "leagues": ["baseball/mlb"]},
    "astros": {"search": "Astros", "leagues": ["baseball/mlb"]},
    "phillies": {"search": "Phillies", "leagues": ["baseball/mlb"]},
    "padres": {"search": "Padres", "leagues": ["baseball/mlb"]},
    "mariners": {"search": "Mariners", "leagues": ["baseball/mlb"]},
    "orioles": {"search": "Orioles", "leagues": ["baseball/mlb"]},
    "guardians": {"search": "Guardians", "leagues": ["baseball/mlb"]},
    "twins": {"search": "Twins", "leagues": ["baseball/mlb"]},
    "tigers": {"search": "Tigers", "leagues": ["baseball/mlb"]},  # Also LSU, Clemson, Auburn
    "royals": {"search": "Royals", "leagues": ["baseball/mlb"]},
    "rangers": {"search": "Rangers", "leagues": ["baseball/mlb", "hockey/nhl"]},  # Texas Rangers or NY Rangers
    "athletics": {"search": "Athletics", "leagues": ["baseball/mlb"]},
    "a's": {"search": "Athletics", "leagues": ["baseball/mlb"]},
    "angels": {"search": "Angels", "leagues": ["baseball/mlb"]},
    "white sox": {"search": "White Sox", "leagues": ["baseball/mlb"]},
    "reds": {"search": "Reds", "leagues": ["baseball/mlb"]},
    "brewers": {"search": "Brewers", "leagues": ["baseball/mlb"]},
    "pirates": {"search": "Pirates", "leagues": ["baseball/mlb"]},
    "nationals": {"search": "Nationals", "leagues": ["baseball/mlb"]},
    "marlins": {"search": "Marlins", "leagues": ["baseball/mlb"]},
    "rays": {"search": "Rays", "leagues": ["baseball/mlb"]},
    "blue jays": {"search": "Blue Jays", "leagues": ["baseball/mlb"]},
    "rockies": {"search": "Rockies", "leagues": ["baseball/mlb"]},
    "diamondbacks": {"search": "Diamondbacks", "leagues": ["baseball/mlb"]},
    "d-backs": {"search": "Diamondbacks", "leagues": ["baseball/mlb"]},

    # NHL Teams
    "bruins": {"search": "Bruins", "leagues": ["hockey/nhl"]},
    "canadiens": {"search": "Canadiens", "leagues": ["hockey/nhl"]},
    "habs": {"search": "Canadiens", "leagues": ["hockey/nhl"]},
    "maple leafs": {"search": "Maple Leafs", "leagues": ["hockey/nhl"]},
    "leafs": {"search": "Maple Leafs", "leagues": ["hockey/nhl"]},
    "red wings": {"search": "Red Wings", "leagues": ["hockey/nhl"]},
    "blackhawks": {"search": "Blackhawks", "leagues": ["hockey/nhl"]},
    "penguins": {"search": "Penguins", "leagues": ["hockey/nhl"]},
    "pens": {"search": "Penguins", "leagues": ["hockey/nhl"]},
    "flyers": {"search": "Flyers", "leagues": ["hockey/nhl"]},
    "capitals": {"search": "Capitals", "leagues": ["hockey/nhl"]},
    "caps": {"search": "Capitals", "leagues": ["hockey/nhl"]},
    "oilers": {"search": "Oilers", "leagues": ["hockey/nhl"]},
    "flames": {"search": "Flames", "leagues": ["hockey/nhl"]},
    "canucks": {"search": "Canucks", "leagues": ["hockey/nhl"]},
    "avalanche": {"search": "Avalanche", "leagues": ["hockey/nhl"]},
    "avs": {"search": "Avalanche", "leagues": ["hockey/nhl"]},
    "sharks": {"search": "Sharks", "leagues": ["hockey/nhl"]},
    "ducks": {"search": "Ducks", "leagues": ["hockey/nhl"]},  # Anaheim Ducks, not Oregon
    "coyotes": {"search": "Coyotes", "leagues": ["hockey/nhl"]},
    "golden knights": {"search": "Golden Knights", "leagues": ["hockey/nhl"]},
    "knights": {"search": "Golden Knights", "leagues": ["hockey/nhl"]},
    "kraken": {"search": "Kraken", "leagues": ["hockey/nhl"]},
    "wild": {"search": "Wild", "leagues": ["hockey/nhl"]},
    "predators": {"search": "Predators", "leagues": ["hockey/nhl"]},
    "preds": {"search": "Predators", "leagues": ["hockey/nhl"]},
    "blues": {"search": "Blues", "leagues": ["hockey/nhl"]},
    "stars": {"search": "Stars", "leagues": ["hockey/nhl"]},
    "islanders": {"search": "Islanders", "leagues": ["hockey/nhl"]},
    "devils": {"search": "Devils", "leagues": ["hockey/nhl"]},
    "hurricanes": {"search": "Hurricanes", "leagues": ["hockey/nhl"]},  # Carolina, not Miami
    "lightning": {"search": "Lightning", "leagues": ["hockey/nhl"]},
    "bolts": {"search": "Lightning", "leagues": ["hockey/nhl"]},
    "sabres": {"search": "Sabres", "leagues": ["hockey/nhl"]},
    "senators": {"search": "Senators", "leagues": ["hockey/nhl"]},
    "sens": {"search": "Senators", "leagues": ["hockey/nhl"]},
    "blue jackets": {"search": "Blue Jackets", "leagues": ["hockey/nhl"]},

    # Soccer - Premier League
    "arsenal": {"search": "Arsenal", "leagues": ["soccer/eng.1"]},
    "gunners": {"search": "Arsenal", "leagues": ["soccer/eng.1"]},
    "chelsea": {"search": "Chelsea", "leagues": ["soccer/eng.1"]},
    "liverpool": {"search": "Liverpool", "leagues": ["soccer/eng.1"]},
    "man city": {"search": "Manchester City", "leagues": ["soccer/eng.1"]},
    "manchester city": {"search": "Manchester City", "leagues": ["soccer/eng.1"]},
    "city": {"search": "Manchester City", "leagues": ["soccer/eng.1"]},
    "mc": {"search": "Manchester City", "leagues": ["soccer/eng.1"]},
    "mcfc": {"search": "Manchester City", "leagues": ["soccer/eng.1"]},
    "man united": {"search": "Manchester United", "leagues": ["soccer/eng.1"]},
    "manchester united": {"search": "Manchester United", "leagues": ["soccer/eng.1"]},
    "united": {"search": "Manchester United", "leagues": ["soccer/eng.1"]},
    "mu": {"search": "Manchester United", "leagues": ["soccer/eng.1"]},
    "mufc": {"search": "Manchester United", "leagues": ["soccer/eng.1"]},
    "fulham": {"search": "Fulham", "leagues": ["soccer/eng.1"]},
    "ful": {"search": "Fulham", "leagues": ["soccer/eng.1"]},
    "tottenham": {"search": "Tottenham Hotspur", "leagues": ["soccer/eng.1"]},
    "spurs": {"search": "Tottenham Hotspur", "leagues": ["soccer/eng.1"]},  # Also NBA Spurs
    "newcastle": {"search": "Newcastle United", "leagues": ["soccer/eng.1"]},
    "west ham": {"search": "West Ham United", "leagues": ["soccer/eng.1"]},
    "everton": {"search": "Everton", "leagues": ["soccer/eng.1"]},
    "aston villa": {"search": "Aston Villa", "leagues": ["soccer/eng.1"]},
    "villa": {"search": "Aston Villa", "leagues": ["soccer/eng.1"]},

    # International Soccer
    "usmnt": {"search": "United States", "leagues": ["soccer/fifa.world"]},
    "usa soccer": {"search": "United States", "leagues": ["soccer/fifa.world"]},
    "usa": {"search": "United States", "leagues": ["soccer/fifa.world"]},
    "us soccer": {"search": "United States", "leagues": ["soccer/fifa.world"]},
    "uswnt": {"search": "United States", "leagues": ["soccer/fifa.world"]},
    "england": {"search": "England", "leagues": ["soccer/fifa.world"]},
    "germany": {"search": "Germany", "leagues": ["soccer/fifa.world"]},
    "france": {"search": "France", "leagues": ["soccer/fifa.world"]},
    "brazil": {"search": "Brazil", "leagues": ["soccer/fifa.world"]},
    "argentina": {"search": "Argentina", "leagues": ["soccer/fifa.world"]},
    "spain": {"search": "Spain", "leagues": ["soccer/fifa.world"]},
    "italy": {"search": "Italy", "leagues": ["soccer/fifa.world"]},
    "mexico": {"search": "Mexico", "leagues": ["soccer/fifa.world"]},
}

def get_active_leagues() -> List[str]:
    """Return list of leagues that are currently in season."""
    current_month = datetime.now().month
    active = []
    for league, months in LEAGUE_SEASONS.items():
        if current_month in months:
            active.append(league)
    return active

def resolve_team_alias(query: str) -> Tuple[str, Optional[List[str]]]:
    """
    Resolve team alias to search term and possible leagues.
    Returns (search_term, preferred_leagues or None).

    Also handles matchup patterns like "Michigan vs Ohio State" by extracting first team.
    """
    query_lower = query.lower().strip()

    # Handle matchup patterns: "X vs Y", "X v Y", "X at Y", "X @ Y"
    matchup_patterns = [" vs ", " v ", " at ", " @ ", " versus "]
    for pattern in matchup_patterns:
        if pattern in query_lower:
            # Extract first team from matchup
            first_team = query_lower.split(pattern)[0].strip()
            logger.info(f"Extracted first team from matchup: '{query}' -> '{first_team}'")
            # Recursively resolve the extracted team
            return resolve_team_alias(first_team)

    if query_lower in TEAM_ALIASES:
        alias_info = TEAM_ALIASES[query_lower]
        return alias_info["search"], alias_info.get("leagues")
    return query, None

def filter_leagues_by_season(leagues: List[str]) -> List[str]:
    """Filter leagues to only those currently in season."""
    active = get_active_leagues()
    return [l for l in leagues if l in active]

def pick_best_league_for_team(team_name: str, possible_leagues: List[str]) -> Tuple[Optional[str], List[str]]:
    """
    Given a team that exists in multiple leagues, pick the best one based on season.
    Returns (selected_league, in_season_options).
    - If only one in-season option, returns (league, [league])
    - If multiple in-season options, returns (None, [options...]) to indicate disambiguation needed
    """
    in_season = filter_leagues_by_season(possible_leagues)

    if len(in_season) == 0:
        # No leagues in season, return first one
        return (possible_leagues[0] if possible_leagues else None, possible_leagues)
    elif len(in_season) == 1:
        # Only one in-season option
        return (in_season[0], in_season)
    else:
        # Multiple in-season options - need user clarification
        return (None, in_season)

def get_league_display_name(league_path: str) -> str:
    """Convert league path to human-readable name."""
    display_names = {
        "football/nfl": "NFL Football",
        "football/college-football": "College Football",
        "basketball/nba": "NBA Basketball",
        "basketball/mens-college-basketball": "College Basketball",
        "basketball/wnba": "WNBA Basketball",
        "baseball/mlb": "MLB Baseball",
        "hockey/nhl": "NHL Hockey",
        "soccer/usa.1": "MLS Soccer",
        "soccer/eng.1": "Premier League",
        "soccer/esp.1": "La Liga",
        "soccer/ger.1": "Bundesliga",
        "soccer/ita.1": "Serie A",
        "soccer/fra.1": "Ligue 1",
        "soccer/mex.1": "Liga MX",
        "soccer/uefa.champions": "Champions League",
        "soccer/uefa.europa": "Europa League",
        "soccer/fifa.world": "International Soccer",
    }
    return display_names.get(league_path, league_path)

@cached(ttl=3600, key_prefix="team_search_v4")  # Cache for 1 hour; v4 with alias + season support
async def search_teams_parallel(query: str, league: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Search for teams across providers in parallel and return first provider with data.
    Supports aliases (e.g., "michigan" -> "Michigan Wolverines") and season-aware filtering.
    Optionally filter by explicit league.
    """
    # Step 1: Resolve aliases
    search_term, alias_leagues = resolve_team_alias(query)
    logger.info(f"Resolved alias: '{query}' -> '{search_term}'", alias_leagues=alias_leagues)

    # Step 2: Determine effective league filter
    # If explicit league provided, use it. Otherwise, use alias leagues filtered by season.
    effective_league = league
    if not effective_league and alias_leagues:
        # Filter alias leagues to only in-season ones
        in_season_leagues = filter_leagues_by_season(alias_leagues)
        if len(in_season_leagues) == 1:
            # Single in-season league - use it
            effective_league_path = in_season_leagues[0]
            # Convert path back to league code
            for code, path in LEAGUE_TO_ESPN_PATH.items():
                if path == effective_league_path:
                    effective_league = code
                    break
            logger.info(f"Auto-selected league based on season: {effective_league}")

    async def search_thesportsdb(q: str):
        try:
            url = build_url(api_configs["thesportsdb"]["endpoint_url"], "searchteams.php")
            response = await http_client.get(url, params={"t": q}, timeout=5.0)
            response.raise_for_status()
            data = response.json()
            teams = data.get("teams", []) or []
            teams = [_normalize_thesportsdb_team(t) for t in teams]
            return {"source": "thesportsdb", "teams": teams}
        except Exception as e:
            logger.warning(f"TheSportsDB search failed: {e}")
            return {"source": "thesportsdb", "teams": []}

    async def search_espn(q: str, league_filter: Optional[str] = None):
        try:
            searches = []
            # Determine which leagues to search based on query keywords
            query_lower = q.lower()

            # All available ESPN leagues
            ALL_LEAGUES = {
                # American Football
                "football/nfl": ["nfl", "pro football", "professional football"],
                "football/college-football": ["college football", "ncaa football", "cfb", "wolverines", "buckeyes", "crimson tide", "tigers", "bulldogs", "longhorns", "fighting irish"],
                # Basketball
                "basketball/nba": ["nba", "pro basketball"],
                "basketball/mens-college-basketball": ["college basketball", "ncaa basketball", "march madness", "cbb"],
                "basketball/wnba": ["wnba", "women's basketball"],
                # Other US Sports
                "baseball/mlb": ["mlb", "baseball"],
                "hockey/nhl": ["nhl", "hockey"],
                # Soccer - Domestic
                "soccer/usa.1": ["mls", "major league soccer"],
                "soccer/eng.1": ["premier league", "epl", "english football"],
                "soccer/esp.1": ["la liga", "spanish football"],
                "soccer/ita.1": ["serie a", "italian football"],
                "soccer/ger.1": ["bundesliga", "german football"],
                "soccer/fra.1": ["ligue 1", "french football"],
                "soccer/mex.1": ["liga mx", "mexican football"],
                # Soccer - International/Cups
                "soccer/uefa.champions": ["champions league", "ucl"],
                "soccer/uefa.europa": ["europa league"],
                "soccer/fifa.world": ["world cup", "fifa", "international", "national team", "usa soccer", "usmnt", "uswnt"],
                "soccer/conmebol.america": ["copa america"],
                "soccer/uefa.euro": ["euro", "european championship"],
            }

            # If explicit league filter provided, use only that league
            if league_filter and league_filter in LEAGUE_TO_ESPN_PATH:
                matched_leagues = [LEAGUE_TO_ESPN_PATH[league_filter]]
                logger.info(f"Using explicit league filter: {league_filter} -> {matched_leagues}")
            else:
                # Check if query matches specific league keywords
                matched_leagues = []
                for league_path, keywords in ALL_LEAGUES.items():
                    for keyword in keywords:
                        if keyword in query_lower:
                            matched_leagues.append(league_path)
                            break

                # If no specific league matched, search common leagues
                if not matched_leagues:
                    matched_leagues = [
                        "football/nfl",
                        "football/college-football",
                        "basketball/nba",
                        "basketball/mens-college-basketball",
                        "baseball/mlb",
                        "hockey/nhl",
                        "soccer/eng.1",
                        "soccer/usa.1",
                        "soccer/fifa.world",
                    ]

            for sport in matched_leagues:
                url = build_url(api_configs["espn"]["endpoint_url"], f"{sport}/teams")
                # College sports have many teams, need to request more
                params = {}
                if "college" in sport:
                    params["limit"] = 1000
                searches.append((sport, http_client.get(url, params=params, timeout=10.0)))

            responses = await asyncio.gather(*[s[1] for s in searches], return_exceptions=True)
            all_teams: List[Dict[str, Any]] = []
            for idx, resp in enumerate(responses):
                sport = searches[idx][0]
                if isinstance(resp, httpx.Response):
                    data = resp.json()
                    # ESPN nests teams under sports -> leagues -> teams
                    teams_nested = (
                        data.get("sports", [{}])[0]
                        .get("leagues", [{}])[0]
                        .get("teams", [])
                        or []
                    )
                    # Fallback if structure changes
                    teams = teams_nested or data.get("teams", []) or []
                    matching = []
                    for t in teams:
                        normalized = _normalize_espn_team(t.get("team", t), sport)
                        name = normalized.get("strTeam", "") or ""
                        if q.lower() in name.lower():
                            matching.append(normalized)
                    all_teams.extend(matching)
            return {"source": "espn", "teams": all_teams}
        except Exception as e:
            logger.warning(f"ESPN search failed: {e}")
            return {"source": "espn", "teams": []}

    async def search_api_football(q: str):
        cfg = api_configs.get("api-football", {})
        if not cfg.get("api_key"):
            return {"source": "api-football", "teams": []}

        try:
            url = build_url(cfg["endpoint_url"], "teams")
            response = await http_client.get(
                url,
                headers={"x-apisports-key": cfg["api_key"]},
                params={"search": q},
                timeout=5.0
            )
            response.raise_for_status()
            data = response.json()
            teams = data.get("response", []) or []
            teams = [_normalize_api_football_team(t) for t in teams]
            return {"source": "api-football", "teams": teams}
        except Exception as e:
            logger.warning(f"API-Football search failed: {e}")
            return {"source": "api-football", "teams": []}

    logger.info(f"Searching teams in parallel across providers: {search_term}", league=effective_league, original_query=query)
    results = await asyncio.gather(
        search_thesportsdb(search_term),
        search_espn(search_term, league_filter=effective_league),
        search_api_football(search_term),
        return_exceptions=False
    )

    # Prefer more reliable providers first (ESPN, API-Football) before falling back to TheSportsDB
    results_by_source = {result["source"]: result for result in results if isinstance(result, dict)}
    for source in ["espn", "api-football", "thesportsdb"]:
        provider_result = results_by_source.get(source)
        if provider_result and provider_result.get("teams"):
            logger.info("Using teams from provider", provider=source, count=len(provider_result["teams"]))
            return provider_result["teams"]

    logger.warning(f"No teams found across providers for query: {search_term} (original: {query})")
    return []

async def get_team_info_api(team_id: str) -> Dict[str, Any]:
    """Get team info based on provider identifier."""
    provider, raw_id, sport = parse_team_identifier(team_id)

    if provider == "thesportsdb":
        url = build_url(api_configs["thesportsdb"]["endpoint_url"], "lookupteam.php")
        response = await http_client.get(url, params={"id": raw_id}, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        teams = data.get("teams", [])
        if not teams:
            raise ValueError(f"Team not found: {team_id}")
        team = teams[0]
        team["source"] = "thesportsdb"
        return team

    if provider == "espn":
        url = build_url(api_configs["espn"]["endpoint_url"], f"{sport}/teams/{raw_id}")
        response = await http_client.get(url, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        return _normalize_espn_team(data.get("team", data), sport)

    if provider == "api-football":
        cfg = api_configs.get("api-football", {})
        if not cfg.get("api_key"):
            raise ValueError("API-Football key not configured")
        url = build_url(cfg["endpoint_url"], "teams")
        response = await http_client.get(
            url,
            headers={"x-apisports-key": cfg["api_key"]},
            params={"id": raw_id},
            timeout=5.0
        )
        response.raise_for_status()
        data = response.json()
        if data.get("response"):
            return _normalize_api_football_team(data["response"][0])
        raise ValueError(f"Team not found: {team_id}")

    raise ValueError(f"Unknown provider for team id: {team_id}")

@cached(ttl=600, key_prefix="next_events_v5")  # Cache for 10 minutes; v5 with seasontype fix
async def get_next_events_api(team_id: str) -> List[Dict[str, Any]]:
    """Get next events for a team (provider-aware).

    For ESPN:
    - Tries postseason (type 3) first, then regular season (type 2) as fallback
    - Includes today's games that haven't started yet
    - Returns season status context when season is over
    """
    provider, raw_id, sport = parse_team_identifier(team_id)
    logger.info(f"Fetching next events for team: {team_id}", provider=provider)

    if provider == "thesportsdb":
        url = build_url(api_configs["thesportsdb"]["endpoint_url"], "eventsnext.php")
        response = await http_client.get(url, params={"id": raw_id}, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        events = data.get("events", []) or []
        for event in events:
            event["source"] = "thesportsdb"
        filtered = _filter_events_window(events, days_ahead=7)
        return _sort_events_by_date(filtered)[:5]

    if provider == "espn" and sport:
        # Use Eastern timezone for date display (most US sports)
        eastern = ZoneInfo("America/New_York")
        now_eastern = datetime.now(eastern)
        today_eastern = now_eastern.date()

        async def fetch_espn_schedule(season_type: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
            """Fetch ESPN schedule with optional seasontype parameter."""
            url = build_url(api_configs["espn"]["endpoint_url"], f"{sport}/teams/{raw_id}/schedule")
            params = {}
            if season_type:
                params["seasontype"] = season_type
            response = await http_client.get(url, params=params, timeout=5.0)
            response.raise_for_status()
            data = response.json()

            # Extract season/team metadata
            season_info = data.get("season", {})
            team_info = data.get("team", {})
            metadata = {
                "season_year": season_info.get("year"),
                "season_type": season_info.get("type"),  # 1=preseason, 2=regular, 3=postseason
                "season_name": season_info.get("name"),
                "team_record": team_info.get("recordSummary"),
                "team_standing": team_info.get("standingSummary"),
            }

            events_raw = data.get("events", []) or []
            events_pruned: List[Dict[str, Any]] = []

            for event in events_raw:
                comp = (event.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})

                # Get game status
                status = comp.get("status", {})
                status_type = status.get("type", {})
                game_state = status_type.get("state", "pre")  # pre, in, post

                # Parse start time and convert to Eastern
                start_raw = event.get("date")
                try:
                    if start_raw and "T" in start_raw:
                        start_utc = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
                        start_eastern = start_utc.astimezone(eastern)
                        event_date = start_eastern.date()
                        event_date_str = event_date.isoformat()
                        event_time_str = start_eastern.strftime("%I:%M %p ET")
                    else:
                        event_date = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else None
                        event_date_str = start_raw
                        event_time_str = None
                except Exception:
                    continue

                # Include future games AND today's games that haven't finished
                # (game_state == "pre" means not started, "in" means in progress)
                if event_date:
                    if event_date < today_eastern:
                        continue  # Skip past dates
                    if event_date == today_eastern and game_state == "post":
                        continue  # Skip today's completed games

                events_pruned.append({
                    "strEvent": event.get("name"),
                    "dateEvent": event_date_str,
                    "strTime": event_time_str,
                    "strHomeTeam": home.get("team", {}).get("displayName"),
                    "strAwayTeam": away.get("team", {}).get("displayName"),
                    "game_state": game_state,  # pre, in, post
                    "source": "espn"
                })

            # Sort by date ascending (soonest first)
            events_pruned.sort(key=lambda x: x["dateEvent"])
            return events_pruned, metadata

        # Try fetching schedule - first without seasontype (gets current context)
        events, metadata = await fetch_espn_schedule()

        # If empty and we're in postseason, team might not be in playoffs
        # Fall back to check if there's a next season scheduled
        if not events:
            season_type = metadata.get("season_type")
            season_name = metadata.get("season_name", "")
            team_record = metadata.get("team_record", "")
            team_standing = metadata.get("team_standing", "")

            logger.info(
                f"No upcoming events for {team_id}",
                season_type=season_type,
                season_name=season_name,
                team_record=team_record,
                team_standing=team_standing
            )

            # Return a structured response indicating season is over
            # This helps the orchestrator provide a meaningful response
            return [{
                "strEvent": "Season ended",
                "dateEvent": None,
                "season_status": "ended",
                "team_record": team_record,
                "team_standing": team_standing,
                "season_name": season_name,
                "message": f"The team's {season_name.lower() if season_name else 'season'} has ended with a record of {team_record}. {team_standing}.",
                "source": "espn"
            }]

        return events[:5]

    if provider == "api-football":
        cfg = api_configs.get("api-football", {})
        if not cfg.get("api_key"):
            return []
        url = build_url(cfg["endpoint_url"], "fixtures")
        response = await http_client.get(
            url,
            headers={"x-apisports-key": cfg["api_key"]},
            params={
                "team": raw_id,
                "from": datetime.utcnow().date().isoformat(),
                "to": (datetime.utcnow().date() + timedelta(days=7)).isoformat(),
            },
            timeout=5.0
        )
        response.raise_for_status()
        data = response.json()
        events = []
        for fixture in data.get("response", []) or []:
            start_raw = fixture.get("fixture", {}).get("date")
            try:
                start = datetime.fromisoformat(start_raw.replace("Z", "+00:00")) if start_raw else None
            except Exception:
                start = None
            # API already filters by date range via 'from' and 'to' params
            teams = fixture.get("teams", {})
            events.append({
                "strEvent": fixture.get("fixture", {}).get("status", {}).get("long") or fixture.get("league", {}).get("name") or "Fixture",
                "dateEvent": start.date().isoformat() if start else (fixture.get("fixture", {}).get("date") or "").split("T")[0],
                "strHomeTeam": (teams.get("home") or {}).get("name"),
                "strAwayTeam": (teams.get("away") or {}).get("name"),
                "source": "api-football"
            })
        return _sort_events_by_date(events)[:5]

    return []

@cached(ttl=600, key_prefix="last_events_v3")  # Cache for 10 minutes; v3 with seasontype fix
async def get_last_events_api(team_id: str) -> List[Dict[str, Any]]:
    """Get last events for a team (provider-aware).

    For ESPN:
    - Uses seasontype=2 (regular season) to get past games, even during playoffs
    - Also checks postseason games if team was in playoffs
    - Returns team record and standing context
    """
    provider, raw_id, sport = parse_team_identifier(team_id)
    logger.info(f"Fetching last events for team: {team_id}", provider=provider)

    if provider == "thesportsdb":
        url = build_url(api_configs["thesportsdb"]["endpoint_url"], "eventslast.php")
        response = await http_client.get(url, params={"id": raw_id}, timeout=5.0)
        response.raise_for_status()
        data = response.json()
        events = data.get("results", []) or []
        for event in events:
            event["source"] = "thesportsdb"
        return events

    if provider == "espn" and sport:
        # Use US Eastern timezone for date comparisons (most US sports)
        eastern = ZoneInfo("America/New_York")
        now_eastern = datetime.now(eastern)
        today_eastern = now_eastern.date()

        async def fetch_past_games(season_type: int) -> List[Dict[str, Any]]:
            """Fetch past games for a specific season type."""
            url = build_url(api_configs["espn"]["endpoint_url"], f"{sport}/teams/{raw_id}/schedule")
            response = await http_client.get(url, params={"seasontype": season_type}, timeout=5.0)
            response.raise_for_status()
            data = response.json()
            events = []

            for event in data.get("events", []) or []:
                # Parse full UTC timestamp and convert to Eastern
                event_date_raw = event.get("date") or ""
                try:
                    if "T" in event_date_raw:
                        event_utc = datetime.fromisoformat(event_date_raw.replace("Z", "+00:00"))
                        event_eastern = event_utc.astimezone(eastern)
                        event_date = event_eastern.date()
                        event_date_str = event_date.isoformat()
                    else:
                        event_date = datetime.strptime(event_date_raw, "%Y-%m-%d").date()
                        event_date_str = event_date_raw
                except Exception:
                    continue

                # Get game status to only include completed games
                comp = (event.get("competitions") or [{}])[0]
                status = comp.get("status", {})
                status_type = status.get("type", {})
                game_state = status_type.get("state", "pre")  # pre, in, post

                # Only include completed games (state == "post")
                if game_state != "post":
                    continue

                # Also filter by date to be safe
                if event_date > today_eastern:
                    continue

                competitors = comp.get("competitors", [])
                home = next((c for c in competitors if c.get("homeAway") == "home"), {})
                away = next((c for c in competitors if c.get("homeAway") == "away"), {})

                # Get scores
                home_score_raw = home.get("score")
                away_score_raw = away.get("score")
                home_score = home_score_raw.get("displayValue") if isinstance(home_score_raw, dict) else home_score_raw
                away_score = away_score_raw.get("displayValue") if isinstance(away_score_raw, dict) else away_score_raw
                score_str = f"{home_score}-{away_score}" if home_score and away_score else None

                # Determine winner
                winner = None
                if home_score and away_score:
                    try:
                        if int(home_score) > int(away_score):
                            winner = home.get("team", {}).get("displayName")
                        elif int(away_score) > int(home_score):
                            winner = away.get("team", {}).get("displayName")
                        else:
                            winner = "Tie"
                    except ValueError:
                        pass

                events.append({
                    "strEvent": event.get("name"),
                    "dateEvent": event_date_str,
                    "strHomeTeam": home.get("team", {}).get("displayName"),
                    "strAwayTeam": away.get("team", {}).get("displayName"),
                    "intHomeScore": home_score,
                    "intAwayScore": away_score,
                    "strResult": score_str,
                    "winner": winner,
                    "source": "espn"
                })

            return events

        # Fetch from both regular season (2) and postseason (3) to get all past games
        try:
            regular_games, playoff_games = await asyncio.gather(
                fetch_past_games(season_type=2),  # Regular season
                fetch_past_games(season_type=3),  # Postseason
                return_exceptions=True
            )

            all_events = []
            if isinstance(regular_games, list):
                all_events.extend(regular_games)
            if isinstance(playoff_games, list):
                all_events.extend(playoff_games)

            # Sort by date descending (most recent first) and take top 5
            all_events.sort(key=lambda x: x["dateEvent"], reverse=True)
            return all_events[:5]

        except Exception as e:
            logger.error(f"Error fetching past games for {team_id}: {e}")
            return []

    if provider == "api-football":
        cfg = api_configs.get("api-football", {})
        if not cfg.get("api_key"):
            return []
        url = build_url(cfg["endpoint_url"], "fixtures")
        response = await http_client.get(
            url,
            headers={"x-apisports-key": cfg["api_key"]},
            params={"team": raw_id, "last": 5},
            timeout=5.0
        )
        response.raise_for_status()
        data = response.json()
        events = []
        for fixture in data.get("response", []) or []:
            teams = fixture.get("teams", {})
            events.append({
                "strEvent": fixture.get("fixture", {}).get("status", {}).get("long") or "Fixture",
                "dateEvent": (fixture.get("fixture", {}).get("date") or "").split("T")[0],
                "strHomeTeam": (teams.get("home") or {}).get("name"),
                "strAwayTeam": (teams.get("away") or {}).get("name"),
                "source": "api-football"
            })
        return events

    return []

@app.get("/sports/teams/search")
async def search_teams(
    query: str = Query(..., description="Team name to search"),
    league: Optional[str] = Query(None, description="League filter (e.g., 'college-football', 'nfl', 'premier-league', 'mls', 'international')")
):
    """Search for teams by name, optionally filtered by league."""
    try:
        teams = await search_teams_parallel(query, league=league)

        # Check if disambiguation is needed (only when no explicit league filter)
        needs_disambiguation = False
        disambiguation_options = []

        if not league:
            search_term, alias_leagues = resolve_team_alias(query)
            if alias_leagues:
                selected_league, in_season_options = pick_best_league_for_team(search_term, alias_leagues)
                if selected_league is None and len(in_season_options) > 1:
                    needs_disambiguation = True
                    disambiguation_options = [
                        {
                            "league_code": _get_league_code(l),
                            "league_path": l,
                            "display_name": get_league_display_name(l)
                        }
                        for l in in_season_options
                    ]

        return {
            "query": query,
            "league": league,
            "teams": teams,
            "count": len(teams),
            "needs_disambiguation": needs_disambiguation,
            "disambiguation_options": disambiguation_options
        }
    except httpx.HTTPStatusError as e:
        logger.error(f"TheSportsDB API error: {e}")
        raise HTTPException(status_code=502, detail="Sports service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

def _get_league_code(league_path: str) -> str:
    """Convert league path back to league code for API use."""
    for code, path in LEAGUE_TO_ESPN_PATH.items():
        if path == league_path:
            return code
    return league_path.split("/")[-1]

@app.get("/sports/teams/disambiguate")
async def disambiguate_team(
    query: str = Query(..., description="Team name or alias to disambiguate"),
):
    """
    Check if a team query needs disambiguation (exists in multiple in-season leagues).
    Returns options for the user to choose from if disambiguation is needed.

    Example: "michigan" in December returns options for college football vs college basketball.
    Example: "giants" in December returns only NFL (MLB is not in season).
    """
    search_term, alias_leagues = resolve_team_alias(query)

    if not alias_leagues:
        # Not a known alias, no disambiguation needed
        return {
            "query": query,
            "search_term": search_term,
            "needs_disambiguation": False,
            "auto_selected_league": None,
            "options": []
        }

    selected_league, in_season_options = pick_best_league_for_team(search_term, alias_leagues)

    if selected_league is not None:
        # Single in-season option or no options - auto-selected
        return {
            "query": query,
            "search_term": search_term,
            "needs_disambiguation": False,
            "auto_selected_league": {
                "league_code": _get_league_code(selected_league),
                "league_path": selected_league,
                "display_name": get_league_display_name(selected_league)
            },
            "options": []
        }
    else:
        # Multiple in-season options - need user input
        options = [
            {
                "league_code": _get_league_code(l),
                "league_path": l,
                "display_name": get_league_display_name(l)
            }
            for l in in_season_options
        ]
        return {
            "query": query,
            "search_term": search_term,
            "needs_disambiguation": True,
            "auto_selected_league": None,
            "options": options
        }

@app.get("/sports/teams/{team_id}")
async def get_team(
    team_id: str = Path(..., description="Team ID")
):
    """Get team details by ID."""
    try:
        team = await get_team_info_api(team_id)
        return team
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        logger.error(f"TheSportsDB API error: {e}")
        raise HTTPException(status_code=502, detail="Sports service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/sports/events/{team_id}/next")
async def get_next_events(
    team_id: str = Path(..., description="Team ID")
):
    """Get next events for a team."""
    try:
        events = await get_next_events_api(team_id)
        return {"team_id": team_id, "events": events}
    except httpx.HTTPStatusError as e:
        logger.error(f"TheSportsDB API error: {e}")
        raise HTTPException(status_code=502, detail="Sports service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/sports/events/{team_id}/last")
async def get_last_events(
    team_id: str = Path(..., description="Team ID")
):
    """Get last events for a team."""
    try:
        events = await get_last_events_api(team_id)
        return {"team_id": team_id, "events": events}
    except httpx.HTTPStatusError as e:
        logger.error(f"TheSportsDB API error: {e}")
        raise HTTPException(status_code=502, detail="Sports service unavailable")
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/sports/news")
async def get_sports_news(
    query: str = Query(..., description="Team, sport, or event to search news for"),
    limit: int = Query(5, le=10, ge=1)
):
    """Fetch sports news headlines."""
    try:
        headlines = await fetch_news(query, limit=limit)
        if not headlines:
            raise HTTPException(status_code=404, detail="No news found")
        return {"query": query, "headlines": headlines, "count": len(headlines)}
    except Exception as e:
        logger.error(f"Unexpected error fetching news: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/sports/scores/live")
async def get_live_scores(
    league: str = Query("premier-league", description="League code (e.g., 'premier-league', 'nfl', 'nba', 'mlb', 'nhl')"),
    team: Optional[str] = Query(None, description="Optional team name filter")
):
    """
    Get live/current scores for games in progress or today's games.

    Uses ESPN scoreboard API for real-time game data.
    """
    try:
        # Map league code to ESPN path
        league_path = LEAGUE_TO_ESPN_PATH.get(league, league)
        if "/" not in league_path:
            # Try common mappings
            league_path = {
                "epl": "soccer/eng.1",
                "premier-league": "soccer/eng.1",
                "nfl": "football/nfl",
                "nba": "basketball/nba",
                "mlb": "baseball/mlb",
                "nhl": "hockey/nhl",
                "mls": "soccer/usa.1",
                "la-liga": "soccer/esp.1",
                "bundesliga": "soccer/ger.1",
                "serie-a": "soccer/ita.1",
                "champions-league": "soccer/uefa.champions",
            }.get(league, f"soccer/{league}")

        # Fetch scoreboard from ESPN
        url = build_url(api_configs["espn"]["endpoint_url"], f"{league_path}/scoreboard")
        logger.info(f"Fetching live scores from ESPN", url=url, league=league)

        response = await http_client.get(url, timeout=10.0)
        response.raise_for_status()
        data = response.json()

        games = []
        eastern = ZoneInfo("America/New_York")

        for event in data.get("events", []):
            competitions = event.get("competitions", [{}])
            if not competitions:
                continue

            comp = competitions[0]
            competitors = comp.get("competitors", [])

            home = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away = next((c for c in competitors if c.get("homeAway") == "away"), {})

            home_team = home.get("team", {}).get("displayName", "Unknown")
            away_team = away.get("team", {}).get("displayName", "Unknown")

            # Filter by team name if provided
            if team:
                team_lower = team.lower()
                if team_lower not in home_team.lower() and team_lower not in away_team.lower():
                    continue

            # Get scores
            home_score = home.get("score", "0")
            away_score = away.get("score", "0")

            # Get game status
            status = comp.get("status", {})
            status_type = status.get("type", {})
            state = status_type.get("state", "pre")  # pre, in, post
            detail = status_type.get("detail", "")
            short_detail = status_type.get("shortDetail", "")
            clock = status.get("displayClock", "")
            period = status.get("period", 0)

            # Determine status display
            if state == "in":
                if clock:
                    status_display = f"LIVE - {clock}" + (f" ({short_detail})" if short_detail else "")
                else:
                    status_display = f"LIVE - {short_detail}" if short_detail else "IN PROGRESS"
            elif state == "post":
                status_display = "FINAL"
            else:
                # Pre-game - show start time
                start_time = event.get("date", "")
                try:
                    if start_time:
                        start_utc = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                        start_local = start_utc.astimezone(eastern)
                        status_display = f"Starts {start_local.strftime('%I:%M %p ET')}"
                    else:
                        status_display = detail or "Scheduled"
                except:
                    status_display = detail or "Scheduled"

            games.append({
                "home_team": home_team,
                "away_team": away_team,
                "home_score": home_score,
                "away_score": away_score,
                "status": status_display,
                "state": state,  # pre, in, post
                "period": period,
                "clock": clock,
                "event_name": event.get("name", f"{away_team} @ {home_team}"),
                "venue": comp.get("venue", {}).get("fullName", ""),
            })

        # Sort: live games first, then upcoming, then completed
        state_order = {"in": 0, "pre": 1, "post": 2}
        games.sort(key=lambda g: state_order.get(g["state"], 3))

        return {
            "league": league,
            "league_path": league_path,
            "games": games,
            "count": len(games),
            "live_count": sum(1 for g in games if g["state"] == "in"),
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"ESPN scoreboard API error: {e}")
        raise HTTPException(status_code=502, detail="Sports scoreboard unavailable")
    except Exception as e:
        logger.error(f"Unexpected error fetching live scores: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/sports/standings")
async def get_standings(
    league: str = Query("nfl", description="League code (e.g., 'nfl', 'nba', 'mlb', 'nhl', 'premier-league')"),
    limit: int = Query(10, description="Number of top teams to return (default 10)")
):
    """
    Get current standings for a league.

    Returns teams ranked by wins/points with records.
    Use this to answer questions like "best team", "top teams", "standings", "rankings".
    """
    # ESPN standings API uses v2 endpoint
    espn_standings_base = "https://site.api.espn.com/apis/v2/sports"

    # Map league codes to ESPN paths
    league_map = {
        "nfl": "football/nfl",
        "nba": "basketball/nba",
        "mlb": "baseball/mlb",
        "nhl": "hockey/nhl",
        "premier-league": "soccer/eng.1",
        "eng.1": "soccer/eng.1",
        "la-liga": "soccer/esp.1",
        "esp.1": "soccer/esp.1",
        "bundesliga": "soccer/ger.1",
        "ger.1": "soccer/ger.1",
        "serie-a": "soccer/ita.1",
        "ita.1": "soccer/ita.1",
        "ligue-1": "soccer/fra.1",
        "fra.1": "soccer/fra.1",
        "mls": "soccer/usa.1",
        "usa.1": "soccer/usa.1",
        "ncaaf": "football/college-football",
        "ncaab": "basketball/mens-college-basketball",
    }

    league_lower = league.lower()
    league_path = league_map.get(league_lower)

    if not league_path:
        raise HTTPException(status_code=400, detail=f"Unknown league: {league}. Supported: {list(league_map.keys())}")

    url = f"{espn_standings_base}/{league_path}/standings"

    try:
        logger.info(f"Fetching standings from ESPN: {url}")
        response = await http_client.get(url)
        response.raise_for_status()
        data = response.json()

        teams = []

        # ESPN standings structure varies by sport
        # Look for standings entries in the response
        children = data.get("children", [])

        for division in children:
            div_name = division.get("name", "")
            standings = division.get("standings", {}).get("entries", [])

            for entry in standings:
                team_info = entry.get("team", {})
                stats = entry.get("stats", [])

                # Build stats dict
                stats_dict = {}
                for stat in stats:
                    stats_dict[stat.get("name", "")] = stat.get("displayValue", stat.get("value", ""))

                # Extract common stats
                wins = stats_dict.get("wins", stats_dict.get("W", "0"))
                losses = stats_dict.get("losses", stats_dict.get("L", "0"))
                ties = stats_dict.get("ties", stats_dict.get("T", ""))
                points = stats_dict.get("points", stats_dict.get("PTS", ""))
                win_pct = stats_dict.get("winPercent", stats_dict.get("PCT", ""))
                games_behind = stats_dict.get("gamesBehind", stats_dict.get("GB", ""))

                # Build record string
                if ties:
                    record = f"{wins}-{losses}-{ties}"
                else:
                    record = f"{wins}-{losses}"

                team_data = {
                    "rank": len(teams) + 1,  # Will be sorted later
                    "team_name": team_info.get("displayName", team_info.get("name", "Unknown")),
                    "team_abbreviation": team_info.get("abbreviation", ""),
                    "division": div_name,
                    "record": record,
                    "wins": int(wins) if str(wins).isdigit() else 0,
                    "losses": int(losses) if str(losses).isdigit() else 0,
                }

                if points:
                    team_data["points"] = points
                if win_pct:
                    team_data["win_percentage"] = win_pct
                if games_behind:
                    team_data["games_behind"] = games_behind

                teams.append(team_data)

        # Sort by wins descending (or points for soccer)
        is_soccer = "soccer" in league_path
        if is_soccer:
            teams.sort(key=lambda x: int(x.get("points", 0)) if str(x.get("points", "0")).isdigit() else 0, reverse=True)
        else:
            teams.sort(key=lambda x: (x["wins"], -x["losses"]), reverse=True)

        # Assign ranks after sorting
        for i, team in enumerate(teams):
            team["rank"] = i + 1

        # Limit results
        top_teams = teams[:limit]

        # Generate summary
        if top_teams:
            best_team = top_teams[0]
            summary = f"The best team in {league.upper()} is the {best_team['team_name']} with a record of {best_team['record']}."
            if len(top_teams) > 1:
                summary += f" Top 3: 1) {top_teams[0]['team_name']}"
                if len(top_teams) > 1:
                    summary += f", 2) {top_teams[1]['team_name']}"
                if len(top_teams) > 2:
                    summary += f", 3) {top_teams[2]['team_name']}"
        else:
            summary = f"No standings data available for {league}"

        return {
            "league": league,
            "standings": top_teams,
            "total_teams": len(teams),
            "summary": summary,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

    except httpx.HTTPStatusError as e:
        logger.error(f"ESPN standings API error: {e}")
        raise HTTPException(status_code=502, detail=f"Standings unavailable for {league}")
    except Exception as e:
        logger.error(f"Unexpected error fetching standings: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/sports/olympics/events")
async def get_olympics_events(
    query: str = Query(..., description="Sport or event, e.g., '100m', 'swimming'"),
    date: Optional[str] = Query(None, description="ISO date filter (YYYY-MM-DD)")
):
    """
    Olympics events (best-effort via news headlines).
    Note: No stable free Olympics schedule API in use; returns curated headlines as stand-ins.
    """
    try:
        events = await fetch_olympics_events(query, limit=5)
        if not events:
            raise HTTPException(status_code=404, detail="No Olympics events found")
        return {"query": query, "events": events, "count": len(events)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Olympics fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to fetch Olympics events")

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting Sports RAG service on port {SERVICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=SERVICE_PORT)
