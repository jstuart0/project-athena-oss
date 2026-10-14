"""
Media Requests RAG Service.

Provides natural language interface to Overseerr for media requests.
OWNER MODE ONLY - not available in guest mode.
"""

import os
import time
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import httpx
from shared.metrics import setup_metrics_endpoint
from shared.logging_config import configure_logging
from shared.admin_url import get_admin_url
from shared.service_key import note_admin_refusal, service_key_headers

logger = configure_logging("media-rag")

SERVICE_NAME = "media-requests-rag"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8029"))

app = FastAPI(
    title="Media Requests RAG",
    description="Natural language interface to Overseerr media requests (Owner Mode Only)",
    version="1.0.0"
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

# Configuration. The admin panel's "overseerr" External API Key (its endpoint
# URL and key) wins; OVERSEERR_URL / OVERSEERR_API_KEY are the fallback for a
# deployment without one. There is no built-in address.
ENV_OVERSEERR_URL = os.getenv("OVERSEERR_URL", "")
ENV_OVERSEERR_API_KEY = os.getenv("OVERSEERR_API_KEY", "")
ADMIN_API_URL = get_admin_url()
SETTINGS_TTL_SECONDS = 60

NOT_CONFIGURED_ANSWER = (
    "Media requests aren't set up yet. Add the Overseerr address and API key "
    "in the admin panel's External API Keys."
)
UNAVAILABLE_ANSWER = (
    "I can't reach the media request service right now, so I couldn't check "
    "or request that. Please try again in a little while."
)

# HTTP client
http_client: Optional[httpx.AsyncClient] = None

# Last settings read from admin-backend; kept while admin-backend is down.
_admin_settings: Dict[str, str] = {}
_admin_settings_read_at: Optional[float] = None


class SeerrNotConfigured(Exception):
    """No Overseerr address or key from the admin panel or the environment."""


class SeerrUnavailable(Exception):
    """Overseerr couldn't be reached or refused the call."""


def base_url_from_endpoint(endpoint_url: str) -> str:
    """The server root of an Overseerr endpoint, with or without ``/api/v1``."""
    url = (endpoint_url or "").strip().rstrip("/")
    if url.endswith("/api/v1"):
        url = url[: -len("/api/v1")]
    return url.rstrip("/")


async def _refresh_admin_settings() -> None:
    global _admin_settings, _admin_settings_read_at
    _admin_settings_read_at = time.monotonic()
    try:
        resp = await http_client.get(
            f"{ADMIN_API_URL}/api/external-api-keys/public/overseerr/key",
            headers=service_key_headers(),
        )
    except httpx.HTTPError as e:
        logger.warning("overseerr_settings_fetch_failed", error_type=type(e).__name__)
        return
    if resp.status_code == 200:
        data = resp.json()
        _admin_settings = {
            "base_url": base_url_from_endpoint(data.get("endpoint_url") or ""),
            "api_key": data.get("api_key") or "",
        }
        logger.info(
            "overseerr_settings_loaded",
            url_set=bool(_admin_settings["base_url"]),
            key_length=len(_admin_settings["api_key"]),
        )
    elif resp.status_code == 404:
        _admin_settings = {}  # removed in the admin panel
    else:
        note_admin_refusal(resp.status_code, "/api/external-api-keys/public/overseerr/key")
        logger.warning("overseerr_settings_fetch_failed", status=resp.status_code)


async def seerr_settings() -> tuple[str, str]:
    """``(base_url, api_key)``; raises ``SeerrNotConfigured`` when either is missing."""
    if (
        _admin_settings_read_at is None
        or time.monotonic() - _admin_settings_read_at >= SETTINGS_TTL_SECONDS
    ):
        await _refresh_admin_settings()
    base_url = _admin_settings.get("base_url") or base_url_from_endpoint(ENV_OVERSEERR_URL)
    api_key = _admin_settings.get("api_key") or ENV_OVERSEERR_API_KEY
    if not base_url or not api_key:
        raise SeerrNotConfigured()
    return base_url, api_key


async def seerr_call(method: str, path: str, **kwargs) -> httpx.Response:
    """One Overseerr API call; raises ``SeerrUnavailable`` on any failure."""
    base_url, api_key = await seerr_settings()
    try:
        resp = await http_client.request(
            method, f"{base_url}/api/v1{path}", headers={"X-Api-Key": api_key}, **kwargs
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error("overseerr_call_failed", path=path, status=e.response.status_code)
        raise SeerrUnavailable() from e
    except httpx.HTTPError as e:
        logger.error("overseerr_call_failed", path=path, error_type=type(e).__name__)
        raise SeerrUnavailable() from e
    return resp


class MediaSearchResult(BaseModel):
    id: int
    title: str
    media_type: str  # "movie" or "tv"
    year: Optional[int] = None
    overview: Optional[str] = None
    status: str  # "available", "pending", "unknown"
    poster_path: Optional[str] = None


class RequestResult(BaseModel):
    id: int
    media_title: str
    media_type: str
    status: str
    requested_date: datetime
    requested_by: Optional[str] = None


@app.on_event("startup")
async def startup():
    global http_client
    http_client = httpx.AsyncClient(timeout=30.0)
    await _refresh_admin_settings()


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()


@app.get("/health")
async def health():
    """Health check endpoint."""
    overseerr_ok = False
    configured = True
    try:
        await seerr_call("GET", "/status")
        overseerr_ok = True
    except SeerrNotConfigured:
        configured = False
    except SeerrUnavailable:
        pass

    return {
        "status": "healthy" if overseerr_ok else "degraded",
        "service": "media-requests-rag",
        "overseerr_configured": configured,
        "overseerr_connected": overseerr_ok,
        "owner_mode_only": True,
        "timestamp": datetime.utcnow().isoformat()
    }


@app.api_route("/query", methods=["GET", "POST"])
async def query(q: str = Query(None, description="Natural language query"), request: Request = None):
    """
    Process natural language media queries.

    Examples:
    - "Add the movie Inception"
    - "Request Dune Part 2"
    - "What movies have I requested?"
    - "Is Breaking Bad available?"
    """
    # Handle POST body for orchestrator compatibility
    query_text = q
    if request and request.method == "POST":
        try:
            body = await request.json()
            query_text = body.get("query", q)
        except:
            pass

    if not query_text:
        return {"answer": "Please provide a query about media requests."}

    try:
        return await dispatch_query(query_text)
    except SeerrNotConfigured:
        return {"success": False, "answer": NOT_CONFIGURED_ANSWER}
    except SeerrUnavailable:
        return {"success": False, "answer": UNAVAILABLE_ANSWER}


async def dispatch_query(query_text: str) -> Dict[str, Any]:
    """Pick the handler for a query by its wording."""
    query_lower = query_text.lower()

    # Parse intent - check more specific patterns first
    # List requests must come before "request" check since "pending requests" contains "request"
    list_requests_patterns = [
        "my requests", "my pending", "pending requests", "what have i requested", "show requests",
        "what did i request", "what i requested", "movies have i requested", "shows have i requested",
        "what movies have i", "what shows have i", "requested movies", "requested shows",
        "list my requests", "list requests", "all my requests"
    ]
    if any(word in query_lower for word in list_requests_patterns):
        return await handle_list_requests(query_text)
    elif any(word in query_lower for word in ["status of", "check on", "check status"]):
        return await handle_status_query(query_text)
    elif any(word in query_lower for word in ["available", "have we got", "can i watch", "is there", "in the library", "on plex", "on jellyfin", "do we have"]):
        return await handle_availability_query(query_text)
    elif any(word in query_lower for word in ["add", "request", "want to watch", "get me", "download"]):
        return await handle_request_query(query_text)
    else:
        # Default to search
        return await handle_search_query(query_text)


async def handle_request_query(query: str) -> Dict[str, Any]:
    """Handle media request queries."""
    # Extract title from query
    title = extract_title(query)
    if not title:
        return {"answer": "I couldn't determine what media you want to request. Please specify a movie or TV show title."}

    # Search for media
    results = await search_media(title)
    if not results:
        return {"answer": f"I couldn't find '{title}' in the database. Please check the spelling or try a different title."}

    # Get first result
    media = results[0]

    # Check if already available
    if media.status == "available":
        return {
            "success": True,
            "answer": f"'{media.title}' ({media.year}) is already available in your library. You can watch it now on Plex or Jellyfin!",
            "data": media.dict()
        }

    # Check if already requested
    if media.status == "pending":
        return {
            "success": True,
            "answer": f"'{media.title}' ({media.year}) has already been requested and is pending download.",
            "data": media.dict()
        }

    # Create request
    request_result = await create_request(media.id, media.media_type)
    return {
        "success": True,
        "answer": f"I've requested '{media.title}' ({media.year}). It will be downloaded automatically when available.",
        "data": {"media": media.dict(), "request": request_result}
    }


async def handle_list_requests(query: str) -> Dict[str, Any]:
    """List user's media requests."""
    requests = await get_requests()
    if not requests:
        return {"success": True, "answer": "You don't have any media requests."}

    pending = [r for r in requests if r.status in ["pending", "approved", "processing"]]
    if not pending:
        return {
            "success": True,
            "answer": "All your media requests have been completed and are available in the library!",
            "data": {"total_requests": len(requests)}
        }

    request_list = "\n".join([f"- {r.media_title} ({r.status})" for r in pending[:10]])
    return {
        "success": True,
        "answer": f"You have {len(pending)} pending request(s):\n{request_list}",
        "data": {"requests": [r.dict() for r in pending]}
    }


async def handle_availability_query(query: str) -> Dict[str, Any]:
    """Check media availability."""
    title = extract_title(query)
    if not title:
        return {"success": True, "answer": "Please specify a movie or TV show to check."}

    results = await search_media(title)
    if not results:
        return {"success": True, "answer": f"I couldn't find '{title}' in the database."}

    media = results[0]
    if media.status == "available":
        return {
            "success": True,
            "answer": f"Yes! '{media.title}' ({media.year}) is available in your library. You can watch it on Plex or Jellyfin.",
            "data": media.dict()
        }
    elif media.status == "pending":
        return {
            "success": True,
            "answer": f"'{media.title}' ({media.year}) has been requested and is pending download.",
            "data": media.dict()
        }
    else:
        return {
            "success": True,
            "answer": f"'{media.title}' ({media.year}) is not in your library yet. Would you like me to request it?",
            "data": media.dict()
        }


async def handle_status_query(query: str) -> Dict[str, Any]:
    """Check status of a specific request."""
    title = extract_title(query)
    requests = await get_requests()

    if title:
        # Find matching request
        matching = [r for r in requests if title.lower() in r.media_title.lower()]
        if matching:
            r = matching[0]
            return {
                "success": True,
                "answer": f"'{r.media_title}' is currently {r.status}.",
                "data": r.dict()
            }

    # Show recent requests
    recent = requests[:5] if requests else []
    if recent:
        status_list = "\n".join([f"- {r.media_title}: {r.status}" for r in recent])
        return {
            "success": True,
            "answer": f"Recent request statuses:\n{status_list}",
            "data": {"requests": [r.dict() for r in recent]}
        }

    return {"success": True, "answer": "No requests found."}


async def handle_search_query(query: str) -> Dict[str, Any]:
    """General search query."""
    title = extract_title(query)
    if not title:
        title = query

    results = await search_media(title)
    if not results:
        return {"success": True, "answer": f"No results found for '{title}'."}

    result_list = "\n".join([f"- {r.title} ({r.year}) - {r.status}" for r in results[:5]])
    return {
        "success": True,
        "answer": f"Search results for '{title}':\n{result_list}",
        "data": {"results": [r.dict() for r in results[:5]]}
    }


def extract_title(query: str) -> Optional[str]:
    """Extract media title from natural language query."""
    import re
    query_lower = query.lower().strip().rstrip("?!.")

    # Remove common command phrases (only at start or as complete phrases)
    command_patterns = [
        r"^add the movie\s+",
        r"^add movie\s+",
        r"^request the movie\s+",
        r"^request movie\s+",
        r"^add the show\s+",
        r"^add show\s+",
        r"^request the show\s+",
        r"^request show\s+",
        r"^add the tv show\s+",
        r"^request the tv show\s+",
        r"^add tv show\s+",
        r"^i want to watch\s+",
        r"^can i watch\s+",
        r"^is there\s+",
        r"^do we have\s+",
        r"^have we got\s+",
        r"^is\s+",
        r"^check on my request for\s+",
        r"^status of my request for\s+",
        r"^status of\s+",
        r"^check status of\s+",
        r"^check the status of\s+",
        r"^get me\s+",
        r"^download\s+",
        r"^add\s+",
        r"^request\s+",
    ]

    result = query_lower
    for pattern in command_patterns:
        result = re.sub(pattern, "", result)

    # Remove trailing location phrases
    trailing_patterns = [
        r"\s+on plex$",
        r"\s+on jellyfin$",
        r"\s+in the library$",
        r"\s+in my library$",
        r"\s+to the library$",
        r"\s+to my library$",
        r"\s+available$",
    ]

    for pattern in trailing_patterns:
        result = re.sub(pattern, "", result)

    # Clean up whitespace
    result = " ".join(result.split()).strip()
    return result if result else None


async def search_media(query: str) -> List[MediaSearchResult]:
    """Search Overseerr for movies and TV shows (people are skipped)."""
    from urllib.parse import quote
    # URL encode the query properly (Overseerr requires %20 not +)
    encoded_query = quote(query, safe='')
    resp = await seerr_call("GET", f"/search?query={encoded_query}&page=1&language=en")
    data = resp.json()

    results = []
    for item in data.get("results", []):
        media_type = item.get("mediaType", "movie")
        if media_type not in ("movie", "tv"):
            continue

        # Determine status from mediaInfo
        media_info = item.get("mediaInfo")
        if media_info:
            status_code = media_info.get("status")
            if status_code == 5:  # Available
                status = "available"
            elif status_code in [2, 3, 4]:  # Pending/Processing/Partially Available
                status = "pending"
            else:
                status = "not requested"
        else:
            status = "not requested"

        # Get year from releaseDate or firstAirDate
        year = None
        release_date = item.get("releaseDate") or item.get("firstAirDate")
        if release_date and len(release_date) >= 4:
            try:
                year = int(release_date[:4])
            except ValueError:
                pass

        results.append(MediaSearchResult(
            id=item.get("id"),
            title=item.get("title") or item.get("name") or "Unknown",
            media_type=media_type,
            year=year,
            overview=item.get("overview"),
            status=status,
            poster_path=item.get("posterPath")
        ))
        if len(results) == 10:
            break

    return results


async def create_request(media_id: int, media_type: str) -> Dict:
    """Create a media request in Overseerr; a TV request asks for every season."""
    body: Dict[str, Any] = {"mediaId": media_id, "mediaType": media_type}
    if media_type == "tv":
        body["seasons"] = "all"
    resp = await seerr_call("POST", "/request", json=body)
    return resp.json()


async def get_media_title(tmdb_id: int, media_type: str) -> str:
    """Fetch media title from Overseerr by TMDB ID."""
    endpoint = "movie" if media_type == "movie" else "tv"
    try:
        resp = await seerr_call("GET", f"/{endpoint}/{tmdb_id}")
    except SeerrUnavailable:
        return "Unknown"
    data = resp.json()
    return data.get("title") or data.get("name") or data.get("originalTitle") or "Unknown"


async def get_requests() -> List[RequestResult]:
    """Get list of media requests."""
    import asyncio
    resp = await seerr_call("GET", "/request", params={"take": 50, "skip": 0})
    data = resp.json()

    # Overseerr's MediaRequestStatus
    status_map = {
        1: "pending",
        2: "approved",
        3: "declined",
        4: "failed",
        5: "completed",
    }

    # Collect items and fetch titles in parallel
    items = data.get("results", [])

    # Build list of title lookup tasks
    async def return_unknown():
        return "Unknown"

    title_tasks = []
    for item in items:
        media = item.get("media", {})
        tmdb_id = media.get("tmdbId")
        media_type = item.get("type", "movie")
        if tmdb_id:
            title_tasks.append(get_media_title(tmdb_id, media_type))
        else:
            title_tasks.append(return_unknown())

    # Fetch all titles in parallel (limit to first 20 to avoid rate limiting)
    titles = await asyncio.gather(*title_tasks[:20], return_exceptions=True)
    titles = [t if isinstance(t, str) else "Unknown" for t in titles]
    # Extend with "Unknown" for remaining items
    titles.extend(["Unknown"] * (len(items) - len(titles)))

    results = []
    for i, item in enumerate(items):
        created_at = item.get("createdAt", "2024-01-01T00:00:00.000Z")
        try:
            requested_date = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except:
            requested_date = datetime.utcnow()

        results.append(RequestResult(
            id=item.get("id"),
            media_title=titles[i] if i < len(titles) else "Unknown",
            media_type=item.get("type", "movie"),
            status=status_map.get(item.get("status"), "unknown"),
            requested_date=requested_date,
            requested_by=item.get("requestedBy", {}).get("displayName") if item.get("requestedBy") else None
        ))

    return results


# Additional endpoints for direct access

@app.exception_handler(SeerrNotConfigured)
async def seerr_not_configured_handler(request: Request, exc: SeerrNotConfigured):
    return JSONResponse(status_code=503, content={"detail": "overseerr_not_configured"})


@app.exception_handler(SeerrUnavailable)
async def seerr_unavailable_handler(request: Request, exc: SeerrUnavailable):
    return JSONResponse(status_code=502, content={"detail": "overseerr_unavailable"})


@app.get("/search")
async def search_endpoint(query: str = Query(..., description="Search query")):
    """Direct search endpoint."""
    results = await search_media(query)
    return {"results": [r.dict() for r in results]}


@app.get("/requests")
async def list_requests():
    """List all requests."""
    requests = await get_requests()
    return {"requests": [r.dict() for r in requests]}


@app.post("/request")
async def make_request(media_id: int, media_type: str = "movie"):
    """Create a media request."""
    result = await create_request(media_id, media_type)
    return {"success": True, "request": result}
