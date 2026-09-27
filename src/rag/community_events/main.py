"""Community Events RAG Service - Region-Configurable Events Aggregator

Scrapes and aggregates community events from operator-configured local
sources (COMMUNITY_EVENTS_SOURCES, a JSON list). Four scraper types are
supported: link_scan, event_cards, tribe_events_api, squarespace_eventlist
(see .env.example for the schema). link_scan and event_cards are best-effort
heuristics, validated only against their reference sites -- a source's HTML
structure can drift without notice.

Events are cached in Redis with 24-hour TTL and refreshed daily.

API Endpoints:
- GET /health - Health check
- GET /events/search - Search community events
- POST /events/refresh - Force refresh of events cache
- GET /events/sources - List configured sources
"""

import os
import sys
import re
import json
import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import zip_longest
from typing import Any, Dict, List, NamedTuple, Optional
from contextlib import asynccontextmanager

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import httpx
import structlog
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks, Depends
from fastapi.responses import JSONResponse

from shared.config import get_config
from shared.logging_config import configure_logging
from shared.metrics import setup_metrics_endpoint
from shared.url_safety import safe_get, SsrfBlockedError

logger = configure_logging("community-events-rag")

SERVICE_NAME = "community-events"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8026"))

# Service-specific Redis URL (path encodes DB 1 to isolate community events cache).
# Falls back to a localhost default for OSS first-boot without Redis configured.
# Do NOT use REDIS_HOST/REDIS_PORT — the kubelet injects REDIS_PORT=tcp://... for
# any K8s Service named "redis", which breaks int() casts at import time.
REDIS_URL = os.getenv("COMMUNITY_EVENTS_REDIS_URL", "redis://localhost:6379/1")
CACHE_TTL = 86400  # 24 hours in seconds

# Redis keys for sorted set storage
REDIS_KEY_BY_DATE = "community_events:by_date"  # Sorted set: score=timestamp, value=event_json
REDIS_KEY_NO_DATE = "community_events:no_date"  # Set for events without parseable dates
REDIS_KEY_METADATA = "community_events:metadata"  # Hash for cache metadata

# Far future timestamp for events without dates (Dec 31, 2099)
NO_DATE_TIMESTAMP = 4102444800

# D3: per-type defaults, copied exactly from today's (house-specific) literals.
# tribe_events_api's is_free is NOT independently configurable -- it's always
# computed per-event from the API's own `cost` field.
_TYPE_DEFAULTS = {
    "link_scan": {"category": "community", "is_free": True},
    "event_cards": {"category": "community", "is_free": False},
    "tribe_events_api": {"category": "downtown", "is_free": False},
    "squarespace_eventlist": {"category": "neighborhood", "is_free": True},
}

# Global clients
redis_client = None

# In-memory cache fallback (when Redis is unavailable)
in_memory_events: List[Dict[str, Any]] = []
in_memory_cache_time: Optional[datetime] = None


@dataclass
class EventSource:
    """One configured community-events source (D3)."""
    name: str
    type: str
    url: str
    base_url: str = ""
    link_path: str = ""
    default_location: str = ""
    default_address: str = ""
    default_city: str = ""
    default_state: str = ""
    is_free: bool = False
    category: str = ""
    location_match: List[str] = field(default_factory=list)
    regions: List[str] = field(default_factory=list)
    max_bytes: Optional[int] = None
    allow_private: bool = False


class CommunityEventsConfig(NamedTuple):
    """Parsed COMMUNITY_EVENTS_SOURCES config. Never raises."""
    configured: bool
    sources: List[EventSource]
    error: Optional[str]


def load_event_sources(cfg) -> CommunityEventsConfig:
    """Parse COMMUNITY_EVENTS_SOURCES. Never raises."""
    raw = cfg.community_events_sources
    if not raw:
        return CommunityEventsConfig(False, [], None)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        return CommunityEventsConfig(False, [], f"COMMUNITY_EVENTS_SOURCES: invalid JSON: {e}")
    if not isinstance(parsed, list):
        return CommunityEventsConfig(False, [], "COMMUNITY_EVENTS_SOURCES: must be a JSON array")

    sources: List[EventSource] = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            return CommunityEventsConfig(False, [], f"COMMUNITY_EVENTS_SOURCES: source at index {i} must be an object")
        name = item.get("name")
        if not name:
            return CommunityEventsConfig(False, [], f"COMMUNITY_EVENTS_SOURCES: source at index {i} missing 'name'")
        source_type = item.get("type")
        if source_type not in _TYPE_DEFAULTS:
            return CommunityEventsConfig(
                False, [], f"COMMUNITY_EVENTS_SOURCES: source '{name}' has unknown type '{source_type}'"
            )
        if not item.get("url"):
            return CommunityEventsConfig(False, [], f"COMMUNITY_EVENTS_SOURCES: source '{name}' missing 'url'")

        defaults = _TYPE_DEFAULTS[source_type]
        if source_type == "tribe_events_api":
            # Not independently configurable (D3): always computed per-event.
            is_free = defaults["is_free"]
        else:
            is_free = item.get("is_free", defaults["is_free"])

        sources.append(EventSource(
            name=name,
            type=source_type,
            url=item["url"],
            base_url=item.get("base_url", ""),
            link_path=item.get("link_path", ""),
            default_location=item.get("default_location", ""),
            default_address=item.get("default_address", ""),
            default_city=item.get("default_city", ""),
            default_state=item.get("default_state", ""),
            is_free=is_free,
            category=item.get("category", defaults["category"]),
            location_match=item.get("location_match") or [],
            regions=item.get("regions") or [],
            max_bytes=item.get("max_bytes"),
            allow_private=bool(item.get("allow_private", False)),
        ))

    return CommunityEventsConfig(True, sources, None)


event_sources_config: CommunityEventsConfig = load_event_sources(get_config())

# Per-source fetch failures (e.g. SSRF-blocked), surfaced in /health's message.
fetch_errors: Dict[str, str] = {}


def _private_kwargs(src: EventSource) -> Dict[str, Any]:
    """D13: a private host is permitted ONLY for this source's own hostname,
    and only when the source opts in. By default no allowed_private_hosts
    is passed, so every hop -- including the first -- is validated."""
    if src.allow_private:
        return {"allowed_private_hosts": [httpx.URL(src.url).host]}
    return {}


async def get_redis_client():
    """Get or create Redis client connection."""
    global redis_client
    if redis_client is None:
        try:
            import redis.asyncio as redis
            # URL path encodes the DB index; do NOT pass db= kwarg (it would be
            # silently overridden by the URL path per redis-py precedence rules).
            redis_client = redis.from_url(REDIS_URL, decode_responses=True)
            # Test connection
            await redis_client.ping()
            logger.info("redis_connected", url=REDIS_URL)
        except Exception as e:
            logger.warning("redis_connection_failed", error=str(e))
            redis_client = None
    return redis_client


def parse_date_string(date_str: str) -> Optional[datetime]:
    """Parse various date formats from scraped content."""
    if not date_str:
        return None

    # Clean up the string
    date_str = date_str.strip()

    # Common formats to try
    formats = [
        "%b %d, %Y",           # Dec 6, 2025
        "%B %d, %Y",           # December 6, 2025
        "%m/%d/%Y",            # 12/6/2025
        "%Y-%m-%d",            # 2025-12-06
        "%a, %b %d, %Y",       # Sat, Dec 6, 2025
        "%A, %B %d, %Y",       # Saturday, December 6, 2025
    ]

    for fmt in formats:
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue

    return None


def date_to_timestamp(date_str: Optional[str]) -> int:
    """Convert date string to Unix timestamp for Redis sorted set scoring."""
    if not date_str:
        return NO_DATE_TIMESTAMP

    dt = parse_date_string(date_str)
    if dt:
        return int(dt.timestamp())

    return NO_DATE_TIMESTAMP


def timestamp_to_date(ts: int) -> Optional[str]:
    """Convert Unix timestamp back to date string."""
    if ts >= NO_DATE_TIMESTAMP:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def extract_date_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract date information from text using regex patterns."""
    if not text:
        return None

    # Pattern: "Nov 7–Jan 30" or "Dec 6–7"
    range_pattern = r'([A-Za-z]+)\s+(\d+)[–-](?:([A-Za-z]+)\s+)?(\d+)'

    # Pattern: "Dec 6, 2025" or "December 6th, 2025"
    single_date_pattern = r'([A-Za-z]+)\s+(\d+)(?:st|nd|rd|th)?,?\s*(\d{4})?'

    # Try range pattern first
    range_match = re.search(range_pattern, text)
    if range_match:
        start_month = range_match.group(1)
        start_day = range_match.group(2)
        end_month = range_match.group(3) or start_month
        end_day = range_match.group(4)

        # Assume current year if not specified
        year = datetime.now().year
        if datetime.now().month > 6 and start_month.lower() in ['jan', 'feb', 'mar', 'january', 'february', 'march']:
            year += 1

        return {
            "start_date": f"{start_month} {start_day}, {year}",
            "end_date": f"{end_month} {end_day}, {year}",
            "is_range": True
        }

    # Try single date pattern
    single_match = re.search(single_date_pattern, text)
    if single_match:
        month = single_match.group(1)
        day = single_match.group(2)
        year = single_match.group(3) or str(datetime.now().year)

        return {
            "start_date": f"{month} {day}, {year}",
            "end_date": None,
            "is_range": False
        }

    return None


async def scrape_link_scan_source(src: EventSource) -> List[Dict[str, Any]]:
    """link_scan: find <a> tags whose href contains src.link_path, then walk
    up to the nearest container for date/location/description. Best-effort
    heuristic (D3), validated only against its reference site."""
    events: List[Dict[str, Any]] = []

    try:
        logger.info("scraping_link_scan_source", name=src.name, url=src.url)

        response = await safe_get(
            src.url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            max_hops=5,
            max_bytes=src.max_bytes or 5 * 2**20,
            timeout=30.0,
            **_private_kwargs(src),
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        event_links = soup.find_all('a', href=lambda x: x and src.link_path and src.link_path in x)
        seen_urls = set()

        for link in event_links:
            try:
                href = link.get('href', '')
                if href in seen_urls or not href:
                    continue
                seen_urls.add(href)

                full_url = href if href.startswith('http') else f"{src.base_url}{href}"

                title = link.get_text(strip=True)
                if not title or len(title) < 3:
                    parent = link.find_parent(['div', 'article', 'li'])
                    if parent:
                        heading = parent.find(['h1', 'h2', 'h3', 'h4', 'h5'])
                        if heading:
                            title = heading.get_text(strip=True)

                if not title or title in ['→', 'View Event', 'View Event →', 'ICS', 'Google Calendar']:
                    continue

                if '?format=ical' in full_url or '?format=gcal' in full_url:
                    continue

                parent = link.find_parent(['div', 'article', 'li'])
                date_text = ""
                location = src.default_location
                description = ""

                if parent:
                    time_elem = parent.find(['time', 'span'], class_=lambda x: x and 'date' in str(x).lower())
                    if time_elem:
                        date_text = time_elem.get_text(strip=True)

                    loc_elem = parent.find(['span', 'div'], class_=lambda x: x and ('location' in str(x).lower() or 'venue' in str(x).lower()))
                    if loc_elem:
                        location = loc_elem.get_text(strip=True)

                    desc_elem = parent.find(['p', 'div'], class_=lambda x: x and ('description' in str(x).lower() or 'excerpt' in str(x).lower()))
                    if desc_elem:
                        description = desc_elem.get_text(strip=True)[:500]

                date_info = extract_date_from_text(date_text) or {}

                events.append({
                    "id": hashlib.md5(full_url.encode()).hexdigest()[:12],
                    "title": title,
                    "url": full_url,
                    "source": src.name,
                    "location": location,
                    "address": src.default_address,
                    "description": description,
                    "date_text": date_text,
                    "start_date": date_info.get("start_date"),
                    "end_date": date_info.get("end_date"),
                    "is_free": src.is_free,
                    "category": src.category,
                    "scraped_at": datetime.now().isoformat()
                })

            except Exception as e:
                logger.warning("event_parse_error", source=src.name, error=str(e))
                continue

        logger.info("link_scan_scrape_complete", name=src.name, events_found=len(events))
        fetch_errors.pop(src.name, None)

    except SsrfBlockedError as e:
        logger.warning("community_source_ssrf_blocked", source=src.name, reason=str(e))
        fetch_errors[src.name] = str(e)
    except Exception as e:
        logger.error("link_scan_scrape_failed", name=src.name, error=str(e))
        fetch_errors[src.name] = str(e)

    return events


async def scrape_event_cards_source(src: EventSource) -> List[Dict[str, Any]]:
    """event_cards: find <a> tags whose href contains src.link_path, using
    each link's parent card for title/date/location/description. Best-effort
    heuristic (D3), validated only against its reference site."""
    events: List[Dict[str, Any]] = []

    try:
        logger.info("scraping_event_cards_source", name=src.name, url=src.url)

        response = await safe_get(
            src.url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            max_hops=5,
            max_bytes=src.max_bytes or 5 * 2**20,
            timeout=30.0,
            **_private_kwargs(src),
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        event_links = soup.find_all('a', href=lambda x: x and src.link_path and src.link_path in x)
        seen_urls = set()

        for link in event_links:
            try:
                href = link.get('href', '')
                if href in seen_urls or not href:
                    continue
                seen_urls.add(href)

                full_url = href if href.startswith('http') else f"{src.base_url}{href}"

                title = link.get_text(strip=True)

                parent = link.find_parent(['article', 'div', 'li'])
                if parent:
                    heading = parent.find(['h2', 'h3', 'h4', 'h5'])
                    if heading:
                        title = heading.get_text(strip=True)

                if not title or len(title) < 3:
                    continue

                if title.lower() in ['read more', 'learn more', 'view all', 'see more']:
                    continue

                date_text = ""
                location = src.default_location
                description = ""

                if parent:
                    date_elem = parent.find(['time', 'span', 'div'], class_=lambda x: x and any(
                        term in str(x).lower() for term in ['date', 'time', 'when']
                    ))
                    if date_elem:
                        date_text = date_elem.get_text(strip=True)

                    all_text = parent.get_text()
                    date_patterns = [
                        r'((?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)[,\s]+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2})',
                        r'((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,?\s*\d{4})?)',
                    ]
                    for pattern in date_patterns:
                        match = re.search(pattern, all_text, re.IGNORECASE)
                        if match and not date_text:
                            date_text = match.group(1)
                            break

                    loc_elem = parent.find(['span', 'div', 'address'], class_=lambda x: x and any(
                        term in str(x).lower() for term in ['location', 'venue', 'address', 'where']
                    ))
                    if loc_elem:
                        location = loc_elem.get_text(strip=True)

                    desc_elem = parent.find(['p', 'div'], class_=lambda x: x and any(
                        term in str(x).lower() for term in ['description', 'excerpt', 'summary', 'content']
                    ))
                    if desc_elem:
                        description = desc_elem.get_text(strip=True)[:500]

                date_info = extract_date_from_text(date_text) or {}

                events.append({
                    "id": hashlib.md5(full_url.encode()).hexdigest()[:12],
                    "title": title,
                    "url": full_url,
                    "source": src.name,
                    "location": location,
                    "address": src.default_address,
                    "description": description,
                    "date_text": date_text,
                    "start_date": date_info.get("start_date"),
                    "end_date": date_info.get("end_date"),
                    "is_free": src.is_free,
                    "category": src.category,
                    "scraped_at": datetime.now().isoformat()
                })

            except Exception as e:
                logger.warning("event_cards_parse_error", source=src.name, error=str(e))
                continue

        logger.info("event_cards_scrape_complete", name=src.name, events_found=len(events))
        fetch_errors.pop(src.name, None)

    except SsrfBlockedError as e:
        logger.warning("community_source_ssrf_blocked", source=src.name, reason=str(e))
        fetch_errors[src.name] = str(e)
    except Exception as e:
        logger.error("event_cards_scrape_failed", name=src.name, error=str(e))
        fetch_errors[src.name] = str(e)

    return events


async def scrape_tribe_events_api_source(src: EventSource) -> List[Dict[str, Any]]:
    """tribe_events_api: The Events Calendar WordPress plugin REST API.
    is_free is always computed per-event from the API's own `cost` field --
    not independently configurable (D3)."""
    events: List[Dict[str, Any]] = []

    try:
        logger.info("fetching_tribe_events_api_source", name=src.name, url=src.url)

        params = {"per_page": 50, "status": "publish"}
        response = await safe_get(
            src.url,
            params=params,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            max_hops=5,
            max_bytes=src.max_bytes or 5 * 2**20,
            timeout=30.0,
            **_private_kwargs(src),
        )
        response.raise_for_status()

        data = response.json()
        api_events = data.get("events", [])

        for api_event in api_events:
            try:
                start_date_str = api_event.get("start_date", "")
                start_date = None
                if start_date_str:
                    try:
                        dt = datetime.strptime(start_date_str.split()[0], "%Y-%m-%d")
                        start_date = dt.strftime("%b %d, %Y")
                    except ValueError:
                        pass

                venue_list = api_event.get("venue", [])
                location = src.default_location
                address = src.default_address
                if venue_list and isinstance(venue_list, list) and len(venue_list) > 0:
                    venue = venue_list[0]
                    location = venue.get("venue", location)
                    address = f"{venue.get('address', '')}, {venue.get('city', src.default_city)}, {venue.get('state', src.default_state)}"

                description = api_event.get("excerpt", "") or ""
                if "<" in description:
                    description = re.sub(r'<[^>]+>', '', description)
                description = description[:500].strip()

                event = {
                    "id": hashlib.md5(api_event.get("url", "").encode()).hexdigest()[:12],
                    "title": api_event.get("title", ""),
                    "url": api_event.get("url", ""),
                    "source": src.name,
                    "location": location,
                    "address": address.strip(", "),
                    "description": description,
                    "date_text": f"{start_date_str.split()[0] if start_date_str else ''} {start_date_str.split()[1] if len(start_date_str.split()) > 1 else ''}".strip(),
                    "start_date": start_date,
                    "end_date": None,
                    "is_free": not api_event.get("cost"),  # Free if no cost listed (not configurable, D3)
                    "category": src.category,
                    "scraped_at": datetime.now().isoformat()
                }

                if event["title"]:
                    events.append(event)

            except Exception as e:
                logger.warning("tribe_events_parse_error", source=src.name, error=str(e))
                continue

        logger.info("tribe_events_api_fetch_complete", name=src.name, events_found=len(events))
        fetch_errors.pop(src.name, None)

    except SsrfBlockedError as e:
        logger.warning("community_source_ssrf_blocked", source=src.name, reason=str(e))
        fetch_errors[src.name] = str(e)
    except Exception as e:
        logger.error("tribe_events_api_fetch_failed", name=src.name, error=str(e))
        fetch_errors[src.name] = str(e)

    return events


async def scrape_squarespace_eventlist_source(src: EventSource) -> List[Dict[str, Any]]:
    """squarespace_eventlist: Squarespace's own eventlist-title/eventlist-meta
    class structure (structural, not source-specific). src.location_match
    names the substrings that make the last meta item usable as a location;
    otherwise src.default_location is used."""
    events: List[Dict[str, Any]] = []

    try:
        logger.info("scraping_squarespace_eventlist_source", name=src.name, url=src.url)

        response = await safe_get(
            src.url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            },
            max_hops=5,
            max_bytes=src.max_bytes or 5 * 2**20,
            timeout=30.0,
            **_private_kwargs(src),
        )
        response.raise_for_status()

        soup = BeautifulSoup(response.text, 'html.parser')
        title_headings = soup.find_all('h1', class_='eventlist-title')
        seen_urls = set()

        for heading in title_headings:
            try:
                link = heading.find('a', href=lambda x: x and '/events/' in x)
                if not link:
                    continue

                href = link.get('href', '')

                if '?format=' in href or 'google.com' in href:
                    continue

                if href.startswith('/'):
                    full_url = f"{src.base_url}{href}"
                elif href.startswith('http'):
                    full_url = href
                else:
                    continue

                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                title = link.get_text(strip=True)
                if not title or len(title) < 3:
                    continue

                parent = heading.find_parent(['article', 'div'], class_=lambda x: x and 'eventlist' in str(x).lower())

                date_text = ""
                start_date = None
                location = src.default_location

                if parent:
                    month_elem = parent.find(['span', 'div'], class_='eventlist-month')
                    day_elem = parent.find(['span', 'div'], class_='eventlist-day')

                    if month_elem and day_elem:
                        month = month_elem.get_text(strip=True)
                        day = day_elem.get_text(strip=True)
                        year = datetime.now().year
                        if datetime.now().month > 9 and month.lower() in ['jan', 'feb', 'mar', 'apr', 'may', 'jun']:
                            year += 1
                        date_text = f"{month} {day}, {year}"
                        start_date = date_text

                    meta_list = parent.find('ul', class_='eventlist-meta')
                    if meta_list:
                        items = meta_list.find_all('li')
                        if items and len(items) > 0:
                            date_text = items[0].get_text(strip=True)
                            date_info = extract_date_from_text(date_text)
                            if date_info:
                                start_date = date_info.get("start_date")
                        if len(items) >= 3:
                            loc_text = items[-1].get_text(strip=True)
                            if any(term in loc_text for term in src.location_match):
                                location = loc_text

                events.append({
                    "id": hashlib.md5(full_url.encode()).hexdigest()[:12],
                    "title": title,
                    "url": full_url,
                    "source": src.name,
                    "location": location,
                    "address": src.default_address,
                    "description": "",
                    "date_text": date_text,
                    "start_date": start_date,
                    "end_date": None,
                    "is_free": src.is_free,
                    "category": src.category,
                    "scraped_at": datetime.now().isoformat()
                })

            except Exception as e:
                logger.warning("squarespace_eventlist_parse_error", source=src.name, error=str(e))
                continue

        logger.info("squarespace_eventlist_scrape_complete", name=src.name, events_found=len(events))
        fetch_errors.pop(src.name, None)

    except SsrfBlockedError as e:
        logger.warning("community_source_ssrf_blocked", source=src.name, reason=str(e))
        fetch_errors[src.name] = str(e)
    except Exception as e:
        logger.error("squarespace_eventlist_scrape_failed", name=src.name, error=str(e))
        fetch_errors[src.name] = str(e)

    return events


SCRAPERS = {
    "link_scan": scrape_link_scan_source,
    "event_cards": scrape_event_cards_source,
    "tribe_events_api": scrape_tribe_events_api_source,
    "squarespace_eventlist": scrape_squarespace_eventlist_source,
}


async def get_cached_events(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> Optional[List[Dict[str, Any]]]:
    """
    Get events from Redis cache, optionally filtered by date range.

    Uses ZRANGEBYSCORE for efficient date-based filtering at Redis level.
    Only fetches events within the specified date range instead of all events.

    Args:
        start_date: Start of date range (YYYY-MM-DD), defaults to today
        end_date: End of date range (YYYY-MM-DD), defaults to +1 year

    Returns:
        List of events within the date range, or None if cache unavailable
    """
    redis = await get_redis_client()
    if not redis:
        # Fallback to in-memory cache with Python-based date filtering
        if in_memory_events:
            # Parse date range
            from datetime import datetime as dt
            filtered = in_memory_events.copy()
            if start_date:
                try:
                    start_dt = dt.strptime(start_date, "%Y-%m-%d")
                    filtered = [e for e in filtered if not e.get("start_date") or
                               dt.strptime(e["start_date"], "%Y-%m-%d") >= start_dt]
                except ValueError:
                    pass
            if end_date:
                try:
                    end_dt = dt.strptime(end_date, "%Y-%m-%d")
                    filtered = [e for e in filtered if not e.get("start_date") or
                               dt.strptime(e["start_date"], "%Y-%m-%d") <= end_dt]
                except ValueError:
                    pass
            logger.info("cache_hit_in_memory_filtered", events_count=len(filtered))
            return filtered
        return None

    try:
        # Check if sorted set exists
        exists = await redis.exists(REDIS_KEY_BY_DATE)
        if not exists:
            logger.info("cache_miss", reason="sorted_set_not_found")
            return None

        # Calculate timestamp range
        if start_date:
            try:
                start_ts = int(datetime.strptime(start_date, "%Y-%m-%d").timestamp())
            except ValueError:
                start_ts = 0  # Beginning of time
        else:
            start_ts = 0  # No start filter - get all

        if end_date:
            try:
                # End of day for end_date
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                end_ts = int(end_dt.timestamp())
            except ValueError:
                end_ts = NO_DATE_TIMESTAMP + 1  # Include events without dates
        else:
            end_ts = NO_DATE_TIMESTAMP + 1  # Include events without dates

        # Query sorted set by score range - this is the efficient part!
        # Only fetches events within the date range from Redis
        event_jsons = await redis.zrangebyscore(
            REDIS_KEY_BY_DATE,
            min=start_ts,
            max=end_ts
        )

        events = [json.loads(e) for e in event_jsons]
        logger.info("cache_hit",
                    events_count=len(events),
                    start_ts=start_ts,
                    end_ts=end_ts,
                    query_type="zrangebyscore")
        return events

    except Exception as e:
        logger.warning("cache_read_error", error=str(e))
        return None


async def get_all_cached_events() -> Optional[List[Dict[str, Any]]]:
    """Get ALL events from cache (no date filtering). Used for text search."""
    redis = await get_redis_client()
    if not redis:
        # Fallback to in-memory cache
        if in_memory_events:
            logger.info("cache_hit_in_memory", events_count=len(in_memory_events))
            return in_memory_events.copy()
        return None

    try:
        # Get all events from sorted set
        event_jsons = await redis.zrange(REDIS_KEY_BY_DATE, 0, -1)
        if event_jsons:
            events = [json.loads(e) for e in event_jsons]
            logger.info("cache_hit_all", events_count=len(events))
            return events
    except Exception as e:
        logger.warning("cache_read_all_error", error=str(e))

    return None


async def cache_events(events: List[Dict[str, Any]]) -> bool:
    """
    Cache events in Redis sorted set with date-based scoring.

    Events are stored in a sorted set where:
    - Score = Unix timestamp of event start date
    - Value = JSON-encoded event data

    This enables efficient ZRANGEBYSCORE queries for date filtering.
    """
    global in_memory_events, in_memory_cache_time

    redis = await get_redis_client()
    if not redis:
        # Fallback to in-memory cache
        in_memory_events = events.copy()
        in_memory_cache_time = datetime.now()
        logger.info("cache_updated_in_memory", events_count=len(events))
        return True

    try:
        # Clear existing data
        await redis.delete(REDIS_KEY_BY_DATE, REDIS_KEY_METADATA)

        # Build sorted set entries: {score: timestamp, member: event_json}
        # Use pipeline for efficiency
        pipe = redis.pipeline()

        dated_count = 0
        undated_count = 0

        for event in events:
            # Get timestamp score from start_date
            timestamp = date_to_timestamp(event.get("start_date"))
            event_json = json.dumps(event)

            # Add to sorted set with timestamp as score
            pipe.zadd(REDIS_KEY_BY_DATE, {event_json: timestamp})

            if timestamp < NO_DATE_TIMESTAMP:
                dated_count += 1
            else:
                undated_count += 1

        # Store metadata
        pipe.hset(REDIS_KEY_METADATA, mapping={
            "cached_at": datetime.now().isoformat(),
            "total_events": len(events),
            "dated_events": dated_count,
            "undated_events": undated_count,
            "source_count": len(set(e.get("source", "") for e in events))
        })

        # Set TTL on sorted set
        pipe.expire(REDIS_KEY_BY_DATE, CACHE_TTL)
        pipe.expire(REDIS_KEY_METADATA, CACHE_TTL)

        await pipe.execute()

        logger.info("cache_updated_sorted_set",
                    events_count=len(events),
                    dated=dated_count,
                    undated=undated_count)
        return True

    except Exception as e:
        logger.warning("cache_write_error", error=str(e))
        return False


async def refresh_events_cache() -> List[Dict[str, Any]]:
    """Refresh the events cache from all configured sources."""
    if not event_sources_config.configured:
        logger.warning("community_events_not_configured")
        return []

    logger.info("refreshing_events_cache")

    tasks = [SCRAPERS[src.type](src) for src in event_sources_config.sources]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    source_lists: List[List[Dict[str, Any]]] = []
    counts: Dict[str, int] = {}
    for src, result in zip(event_sources_config.sources, results):
        if isinstance(result, Exception):
            logger.error("source_scrape_exception", source=src.name, error=str(result))
            result = []
        counts[src.name] = len(result)
        source_lists.append(result)

    # Interleave events from all sources for variety in results
    all_events: List[Dict[str, Any]] = []
    for events_tuple in zip_longest(*source_lists):
        for event in events_tuple:
            if event:
                all_events.append(event)

    logger.info("total_events_scraped", total=len(all_events), **counts)

    # Cache the results
    await cache_events(all_events)

    return all_events


async def search_events(
    query: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    category: Optional[str] = None,
    free_only: bool = False,
    include_past: bool = False,
    size: int = 20
) -> Dict[str, Any]:
    """
    Search community events with filtering.

    OPTIMIZED: When date filters are provided without text query, uses Redis
    ZRANGEBYSCORE to fetch only events in the date range (efficient).
    When text query is provided, fetches all events for text search.

    Args:
        query: Text search in title/description
        start_date: Filter events on or after this date (YYYY-MM-DD)
        end_date: Filter events on or before this date (YYYY-MM-DD)
        category: Event category filter
        free_only: Only return free events
        include_past: Include past events (for historical queries like "when was X")
        size: Maximum number of results

    Returns:
        Dictionary with events and metadata
    """
    cache_status = "miss"
    query_type = "all"

    # Decide query strategy based on filters
    if query:
        # Text search requires all events
        events = await get_all_cached_events()
        query_type = "text_search"
    elif start_date or end_date:
        # Date-only filter - use efficient ZRANGEBYSCORE
        events = await get_cached_events(start_date=start_date, end_date=end_date)
        query_type = "date_range"
    else:
        # No filters - get all events
        events = await get_all_cached_events()
        query_type = "all"

    # If cache miss, refresh and re-query
    if not events:
        await refresh_events_cache()
        if query:
            events = await get_all_cached_events()
        elif start_date or end_date:
            events = await get_cached_events(start_date=start_date, end_date=end_date)
        else:
            events = await get_all_cached_events()

    if events:
        cache_status = "hit"

    # Apply remaining Python filters
    filtered = events or []

    # Filter out past events unless include_past=True
    if not include_past:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        def is_future_event(event: Dict) -> bool:
            """Check if event is today or in the future. Events without dates are included."""
            date_str = event.get("start_date")
            if not date_str:
                return True  # Include events without parseable dates

            # Try to parse the date
            parsed = parse_date_string(date_str)
            if not parsed:
                return True  # Include if we can't parse

            return parsed >= today

        filtered = [e for e in filtered if is_future_event(e)]
        logger.info("filtered_past_events", original_count=len(events or []), future_count=len(filtered))
    else:
        logger.info("including_past_events", total_count=len(filtered))

    # Text search (if provided)
    if query:
        query_lower = query.lower()
        filtered = [
            e for e in filtered
            if query_lower in e.get("title", "").lower()
            or query_lower in e.get("description", "").lower()
            or query_lower in e.get("location", "").lower()
        ]

    if free_only:
        filtered = [e for e in filtered if e.get("is_free", False)]

    if category:
        filtered = [e for e in filtered if e.get("category", "").lower() == category.lower()]

    # Limit results
    filtered = filtered[:size]

    # Format for output
    formatted_events = []
    for e in filtered:
        formatted_events.append({
            "title": e.get("title"),
            "venue": e.get("location", ""),
            "address": e.get("address", ""),
            "time": e.get("date_text", "See website for times"),
            "date": e.get("start_date"),
            "link": e.get("url"),
            "description": e.get("description"),
            "source": e.get("source"),
            "is_free": e.get("is_free", True),
            "category": e.get("category")
        })

    return {
        "events": formatted_events,
        "total_events": len(filtered),
        "sources": [src.name for src in event_sources_config.sources],
        "cache_status": cache_status,
        "query_type": query_type  # Shows whether date filtering was done at Redis level
    }


# Refresh interval in seconds (24 hours)
REFRESH_INTERVAL = 86400

# Background task handle
_refresh_task: Optional[asyncio.Task] = None


async def periodic_refresh_task():
    """Background task that refreshes events cache every 24 hours."""
    while True:
        try:
            await asyncio.sleep(REFRESH_INTERVAL)
            logger.info("periodic_refresh_starting", interval_hours=REFRESH_INTERVAL // 3600)
            await refresh_events_cache()
            logger.info("periodic_refresh_completed")
        except asyncio.CancelledError:
            logger.info("periodic_refresh_cancelled")
            break
        except Exception as e:
            logger.error("periodic_refresh_failed", error=str(e))
            # Continue running even if refresh fails


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan."""
    global _refresh_task

    logger.info("community_events_service.startup", msg="Initializing Community Events RAG service")

    if not event_sources_config.configured:
        logger.warning("community_events_not_configured")
    else:
        # Initial cache population
        try:
            await refresh_events_cache()
        except Exception as e:
            logger.warning("initial_cache_failed", error=str(e))

        # Start periodic refresh background task
        _refresh_task = asyncio.create_task(periodic_refresh_task())
        logger.info("periodic_refresh_scheduled", interval_hours=REFRESH_INTERVAL // 3600)

    logger.info("community_events_service.startup.complete")

    yield

    # Cleanup
    logger.info("community_events_service.shutdown")

    # Cancel periodic refresh task
    if _refresh_task:
        _refresh_task.cancel()
        try:
            await _refresh_task
        except asyncio.CancelledError:
            pass

    if redis_client:
        await redis_client.close()


# Create FastAPI app
app = FastAPI(
    title="Community Events RAG Service",
    description="Region-configurable community events aggregator with web scraping and Redis caching",
    version="1.0.0",
    lifespan=lifespan
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)


async def require_community_configured():
    """FastAPI dependency: 503 when no community-events sources are configured."""
    if not event_sources_config.configured:
        raise HTTPException(
            status_code=503,
            detail="Community events not configured: set COMMUNITY_EVENTS_SOURCES",
        )


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    redis = await get_redis_client()

    message = None
    if not event_sources_config.configured:
        message = event_sources_config.error or "not configured: set COMMUNITY_EVENTS_SOURCES"
    elif fetch_errors:
        message = "; ".join(f"{name}: {reason}" for name, reason in fetch_errors.items())

    content = {
        "status": "healthy",
        "service": "community-events-rag",
        "configured": event_sources_config.configured,
        "config_error": event_sources_config.error,
        "redis_connected": redis is not None,
        "sources": [src.name for src in event_sources_config.sources],
    }
    if message:
        content["message"] = message

    return JSONResponse(status_code=200, content=content)


@app.get("/events/search", dependencies=[Depends(require_community_configured)])
async def search_events_endpoint(
    query: Optional[str] = Query(None, description="Search text"),
    city: Optional[str] = Query(None, description="City filter (informational; not applied to results)"),
    start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    category: Optional[str] = Query(None, description="Event category"),
    free_only: bool = Query(False, description="Only free events"),
    include_past: bool = Query(False, description="Include past events (for historical queries)"),
    size: int = Query(20, description="Max results", ge=1, le=100)
):
    """
    Search for local community events.

    Returns events from the operator-configured sources, cached in Redis.
    """
    try:
        result = await search_events(
            query=query,
            start_date=start_date,
            end_date=end_date,
            category=category,
            free_only=free_only,
            include_past=include_past,
            size=size
        )

        logger.info(
            "community_events.search.success",
            events_count=len(result["events"]),
            query=query
        )

        return result

    except Exception as e:
        logger.error("community_events.search.error", error=str(e), exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/events/refresh", dependencies=[Depends(require_community_configured)])
async def refresh_events_endpoint(background_tasks: BackgroundTasks):
    """
    Force refresh of the events cache.

    Triggers a background scrape of all configured sources and updates Redis cache.
    """
    try:
        # Run refresh in background
        background_tasks.add_task(refresh_events_cache)

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "message": "Cache refresh started in background"
            }
        )

    except Exception as e:
        logger.error("community_events.refresh.error", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to start refresh")


@app.get("/events/sources")
async def get_sources():
    """List configured event sources."""
    return {
        "configured": event_sources_config.configured,
        "sources": [
            {
                "name": src.name,
                "type": src.type,
                "url": src.url,
                "regions": src.regions,
            }
            for src in event_sources_config.sources
        ],
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=SERVICE_PORT,
        reload=True,
        log_config=None
    )
