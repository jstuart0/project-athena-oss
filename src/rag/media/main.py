"""
Media Requests RAG Service.

Provides natural language interface to Overseerr for media requests.
OWNER MODE ONLY - not available in guest mode.
"""

import os
from datetime import datetime
from typing import Optional, Dict, Any, List
from fastapi import FastAPI, Query, HTTPException, Request
from pydantic import BaseModel
import structlog
import httpx
from shared.metrics import setup_metrics_endpoint

logger = structlog.get_logger()

SERVICE_NAME = "media-requests-rag"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8029"))

app = FastAPI(
    title="Media Requests RAG",
    description="Natural language interface to Overseerr media requests (Owner Mode Only)",
    version="1.0.0"
)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

# Configuration
OVERSEERR_URL = os.getenv("OVERSEERR_URL", "http://localhost:5055")
OVERSEERR_API_KEY = os.getenv("OVERSEERR_API_KEY", "")
ADMIN_API_URL = os.getenv("ADMIN_API_URL", "http://localhost:8080")

# HTTP client
http_client: Optional[httpx.AsyncClient] = None


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
    global http_client, OVERSEERR_API_KEY
    http_client = httpx.AsyncClient(timeout=30.0)

    # Fetch API key from admin if not set
    if not OVERSEERR_API_KEY:
        try:
            resp = await http_client.get(
                f"{ADMIN_API_URL}/api/external-api-keys/public/overseerr/key"
            )
            if resp.status_code == 200:
                data = resp.json()
                OVERSEERR_API_KEY = data.get("api_key", "")
                logger.info("overseerr_api_key_loaded", key_length=len(OVERSEERR_API_KEY))
        except Exception as e:
            logger.warning("failed_to_load_overseerr_key", error=str(e))


@app.on_event("shutdown")
async def shutdown():
    if http_client:
        await http_client.aclose()


@app.get("/health")
async def health():
    """Health check endpoint."""
    overseerr_ok = False
    try:
        resp = await http_client.get(
            f"{OVERSEERR_URL}/api/v1/status",
            headers={"X-Api-Key": OVERSEERR_API_KEY}
        )
        overseerr_ok = resp.status_code == 200
    except:
        pass

    return {
        "status": "healthy" if overseerr_ok else "degraded",
        "service": "media-requests-rag",
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
    if request_result:
        return {
            "success": True,
            "answer": f"I've requested '{media.title}' ({media.year}). It will be downloaded automatically when available.",
            "data": {"media": media.dict(), "request": request_result}
        }
    else:
        return {
            "success": False,
            "answer": f"Failed to create request for '{media.title}'. Please try again or use Overseerr at your-overseerr-url."
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
    query_lower = query.lower()

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
    """Search Overseerr for media."""
    from urllib.parse import quote
    try:
        # URL encode the query properly (Overseerr requires %20 not +)
        encoded_query = quote(query, safe='')
        resp = await http_client.get(
            f"{OVERSEERR_URL}/api/v1/search?query={encoded_query}&page=1&language=en",
            headers={"X-Api-Key": OVERSEERR_API_KEY}
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data.get("results", [])[:10]:
            media_type = item.get("mediaType", "movie")

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

        return results
    except Exception as e:
        logger.error("search_failed", error=str(e))
        return []


async def create_request(media_id: int, media_type: str) -> Optional[Dict]:
    """Create a media request in Overseerr."""
    try:
        resp = await http_client.post(
            f"{OVERSEERR_URL}/api/v1/request",
            json={"mediaId": media_id, "mediaType": media_type},
            headers={"X-Api-Key": OVERSEERR_API_KEY}
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error("create_request_failed", error=str(e))
        return None


async def get_media_title(tmdb_id: int, media_type: str) -> str:
    """Fetch media title from Overseerr by TMDB ID."""
    try:
        endpoint = "movie" if media_type == "movie" else "tv"
        resp = await http_client.get(
            f"{OVERSEERR_URL}/api/v1/{endpoint}/{tmdb_id}",
            headers={"X-Api-Key": OVERSEERR_API_KEY}
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("title") or data.get("name") or data.get("originalTitle") or "Unknown"
    except Exception as e:
        logger.warning("get_media_title_failed", tmdb_id=tmdb_id, error=str(e))
    return "Unknown"


async def get_requests() -> List[RequestResult]:
    """Get list of media requests."""
    import asyncio
    try:
        resp = await http_client.get(
            f"{OVERSEERR_URL}/api/v1/request",
            params={"take": 50, "skip": 0},
            headers={"X-Api-Key": OVERSEERR_API_KEY}
        )
        resp.raise_for_status()
        data = resp.json()

        status_map = {
            1: "pending",
            2: "approved",
            3: "declined",
            4: "available"
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
    except Exception as e:
        logger.error("get_requests_failed", error=str(e))
        return []


# Additional endpoints for direct access

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
    if result:
        return {"success": True, "request": result}
    raise HTTPException(status_code=500, detail="Failed to create request")
