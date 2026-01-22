import os
import sys

# Add parent directory to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from contextlib import asynccontextmanager
from typing import Any, Dict, Optional
import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query, Path
from fastapi.responses import JSONResponse
from shared.cache import cached
from shared.service_registry import register_service, unregister_service
from shared.logging_config import setup_logging
from shared.admin_config import get_admin_client
from shared.metrics import setup_metrics_endpoint

setup_logging(service_name="streaming-rag")
logger = structlog.get_logger()

SERVICE_NAME = "streaming"
SERVICE_PORT = int(os.getenv("SERVICE_PORT", "8015"))
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE_URL = "https://api.themoviedb.org/3"
http_client: Optional[httpx.AsyncClient] = None
admin_client = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, admin_client, TMDB_API_KEY
    logger.info("streaming_service.startup")

    # Initialize admin client
    admin_client = get_admin_client()

    # Try to fetch API key from Admin API (overrides env var)
    try:
        api_config = await admin_client.get_external_api_key("tmdb")
        if api_config and api_config.get("api_key"):
            TMDB_API_KEY = api_config["api_key"]
            logger.info("api_key_from_admin", service="tmdb")
        else:
            logger.info("api_key_from_env", service="tmdb")
    except Exception as e:
        logger.warning("admin_api_unavailable", error=str(e), service="tmdb")
        logger.info("api_key_from_env_fallback", service="tmdb")

    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        params={"api_key": TMDB_API_KEY} if TMDB_API_KEY else {}
    )
    yield
    if http_client:
        await http_client.aclose()
    if admin_client:
        await admin_client.close()

app = FastAPI(title="Streaming RAG Service", version="1.0.0", lifespan=lifespan)

# Setup Prometheus metrics
setup_metrics_endpoint(app, SERVICE_NAME, SERVICE_PORT)

@cached(ttl=3600)
async def search_movies(query: str, page: int = 1) -> Dict[str, Any]:
    if not TMDB_API_KEY:
        raise ValueError("TMDB API key not configured")
    response = await http_client.get(f"{TMDB_BASE_URL}/search/movie", params={"query": query, "page": page})
    response.raise_for_status()
    data = response.json()
    movies = [{
        "id": m.get("id"),
        "title": m.get("title"),
        "overview": m.get("overview"),
        "release_date": m.get("release_date"),
        "vote_average": m.get("vote_average"),
        "poster_path": f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}" if m.get("poster_path") else None
    } for m in data.get("results", [])]
    return {"movies": movies, "total_results": data.get("total_results", 0)}

@cached(ttl=3600)
async def search_tv_shows(query: str, page: int = 1) -> Dict[str, Any]:
    if not TMDB_API_KEY:
        raise ValueError("TMDB API key not configured")
    response = await http_client.get(f"{TMDB_BASE_URL}/search/tv", params={"query": query, "page": page})
    response.raise_for_status()
    data = response.json()
    shows = [{
        "id": s.get("id"),
        "name": s.get("name"),
        "overview": s.get("overview"),
        "first_air_date": s.get("first_air_date"),
        "vote_average": s.get("vote_average"),
        "poster_path": f"https://image.tmdb.org/t/p/w500{s.get('poster_path')}" if s.get("poster_path") else None
    } for s in data.get("results", [])]
    return {"tv_shows": shows, "total_results": data.get("total_results", 0)}

@cached(ttl=86400)
async def get_movie_details(movie_id: int) -> Dict[str, Any]:
    if not TMDB_API_KEY:
        raise ValueError("TMDB API key not configured")
    response = await http_client.get(f"{TMDB_BASE_URL}/movie/{movie_id}")
    response.raise_for_status()
    m = response.json()
    return {
        "id": m.get("id"),
        "title": m.get("title"),
        "overview": m.get("overview"),
        "release_date": m.get("release_date"),
        "runtime": m.get("runtime"),
        "genres": [g.get("name") for g in m.get("genres", [])],
        "vote_average": m.get("vote_average"),
        "poster_path": f"https://image.tmdb.org/t/p/w500{m.get('poster_path')}" if m.get("poster_path") else None
    }

@cached(ttl=300)
async def get_trending(media_type: str = "all", time_window: str = "day") -> Dict[str, Any]:
    if not TMDB_API_KEY:
        raise ValueError("TMDB API key not configured")
    response = await http_client.get(f"{TMDB_BASE_URL}/trending/{media_type}/{time_window}")
    response.raise_for_status()
    data = response.json()
    return {"trending": data.get("results", []), "total_results": len(data.get("results", []))}

@app.get("/health")
async def health_check():
    return JSONResponse(status_code=200, content={"status": "healthy", "service": "streaming-rag"})

@app.get("/streaming/movies/search")
async def search_movies_endpoint(query: str = Query(...), page: int = Query(1, ge=1)):
    try:
        return await search_movies(query, page)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"TMDB API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/streaming/tv/search")
async def search_tv_endpoint(query: str = Query(...), page: int = Query(1, ge=1)):
    try:
        return await search_tv_shows(query, page)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"TMDB API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/streaming/movies/{movie_id}")
async def get_movie(movie_id: int = Path(...)):
    try:
        return await get_movie_details(movie_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"TMDB API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/streaming/trending")
async def trending(media_type: str = Query("all"), time_window: str = Query("day")):
    try:
        return await get_trending(media_type, time_window)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"TMDB API error: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal server error")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=SERVICE_PORT, reload=True, log_config=None)
