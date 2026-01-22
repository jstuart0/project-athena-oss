"""
Music Configuration API routes.

Provides endpoints for managing global music playback settings:
- Music Assistant connection configuration
- Spotify account pool management
- Default playback settings
- Genre-to-artist mappings with autocomplete
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import structlog
import httpx
import os

from app.database import get_db
from app.models import MusicConfig, Feature
from app.auth.oidc import get_current_user

logger = structlog.get_logger()
router = APIRouter(prefix="/api/music-config", tags=["music"])

# Home Assistant configuration for artist search - defaults empty, should be configured
HA_URL = os.getenv("HA_URL", "")
HA_TOKEN = os.getenv("HA_TOKEN", "")


# Pydantic models
class MusicConfigUpdate(BaseModel):
    music_assistant_url: Optional[str] = None
    music_assistant_enabled: Optional[bool] = None
    default_volume: Optional[float] = None
    default_radio_mode: Optional[bool] = None
    default_provider: Optional[str] = None
    genre_seed_selection_mode: Optional[str] = None
    stream_health_check_enabled: Optional[bool] = None
    stream_frozen_timeout_seconds: Optional[int] = None
    auto_restart_frozen_streams: Optional[bool] = None


class SpotifyAccount(BaseModel):
    id: str
    name: str


class GenreUpdate(BaseModel):
    genre: str
    artists: List[str]


# Helper function to get or create config
def get_or_create_config(db: Session) -> MusicConfig:
    """Get existing config or create default one."""
    config = db.query(MusicConfig).first()
    if not config:
        config = MusicConfig()
        db.add(config)
        db.commit()
        db.refresh(config)
    return config


# ============================================================================
# Main Configuration Endpoints
# ============================================================================

@router.get("")
async def get_music_config(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get music configuration."""
    config = get_or_create_config(db)

    # Also get the feature flag status
    feature = db.query(Feature).filter(Feature.name == 'music_playback').first()
    feature_enabled = feature.enabled if feature else False

    result = config.to_dict()
    result['feature_enabled'] = feature_enabled

    return result


@router.put("")
async def update_music_config(
    data: MusicConfigUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update music configuration."""
    config = get_or_create_config(db)

    # Update fields that were provided
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(config, field, value)

    db.commit()
    db.refresh(config)

    logger.info("music_config_updated", fields=list(update_data.keys()))

    return config.to_dict()


@router.get("/internal")
async def get_internal_music_config(
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """Get music config for orchestrator (no auth required)."""
    config = get_or_create_config(db)

    # Also check if feature is enabled
    feature = db.query(Feature).filter(Feature.name == 'music_playback').first()
    feature_enabled = feature.enabled if feature else False

    result = config.to_dict()
    result['feature_enabled'] = feature_enabled

    return result


# ============================================================================
# Music Assistant Connection
# ============================================================================

@router.post("/test-connection")
async def test_music_assistant_connection(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Test connection to Music Assistant."""
    config = get_or_create_config(db)

    if not config.music_assistant_url:
        raise HTTPException(status_code=400, detail="Music Assistant URL not configured")

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Music Assistant API endpoint
            response = await client.get(f"{config.music_assistant_url}/api")

            if response.status_code == 200:
                return {
                    "success": True,
                    "message": "Connected to Music Assistant",
                    "url": config.music_assistant_url
                }
            else:
                return {
                    "success": False,
                    "message": f"Music Assistant returned status {response.status_code}",
                    "url": config.music_assistant_url
                }

    except httpx.ConnectError:
        return {
            "success": False,
            "message": "Could not connect to Music Assistant",
            "url": config.music_assistant_url
        }
    except httpx.TimeoutException:
        return {
            "success": False,
            "message": "Connection timed out",
            "url": config.music_assistant_url
        }
    except Exception as e:
        return {
            "success": False,
            "message": str(e),
            "url": config.music_assistant_url
        }


# ============================================================================
# Spotify Account Management
# ============================================================================

@router.get("/spotify-accounts")
async def get_spotify_accounts(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, str]]:
    """Get configured Spotify accounts."""
    config = get_or_create_config(db)
    return config.spotify_accounts or []


@router.post("/spotify-accounts")
async def add_spotify_account(
    account: SpotifyAccount,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Add a Spotify account to the pool."""
    config = get_or_create_config(db)

    accounts = config.spotify_accounts or []

    # Check for duplicate
    if any(a['id'] == account.id for a in accounts):
        raise HTTPException(status_code=400, detail="Account already exists")

    # Limit to 5 accounts for Spotify (single stream per account)
    if len(accounts) >= 5:
        raise HTTPException(status_code=400, detail="Maximum 5 Spotify accounts allowed")

    accounts.append({"id": account.id, "name": account.name})
    config.spotify_accounts = accounts

    db.commit()
    db.refresh(config)

    logger.info("spotify_account_added", account_id=account.id)

    return {"message": "Account added", "accounts": config.spotify_accounts}


@router.delete("/spotify-accounts/{account_id}")
async def remove_spotify_account(
    account_id: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Remove a Spotify account from the pool."""
    config = get_or_create_config(db)

    accounts = config.spotify_accounts or []
    original_count = len(accounts)

    accounts = [a for a in accounts if a['id'] != account_id]

    if len(accounts) == original_count:
        raise HTTPException(status_code=404, detail="Account not found")

    config.spotify_accounts = accounts
    db.commit()
    db.refresh(config)

    logger.info("spotify_account_removed", account_id=account_id)

    return {"message": "Account removed", "accounts": config.spotify_accounts}


# ============================================================================
# Genre Management
# ============================================================================

@router.get("/genres")
async def get_genres(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Get all genre-to-artist mappings."""
    config = get_or_create_config(db)
    return {
        "genres": config.genre_to_artists or {},
        "selection_mode": config.genre_seed_selection_mode
    }


@router.put("/genres")
async def update_genre(
    data: GenreUpdate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Update artists for a genre (or add new genre)."""
    config = get_or_create_config(db)

    genres = config.genre_to_artists or {}
    genres[data.genre.lower()] = data.artists
    config.genre_to_artists = genres

    db.commit()
    db.refresh(config)

    logger.info("genre_updated", genre=data.genre, artist_count=len(data.artists))

    return {"message": "Genre updated", "genre": data.genre, "artists": data.artists}


@router.delete("/genres/{genre}")
async def delete_genre(
    genre: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> Dict[str, Any]:
    """Delete a genre."""
    config = get_or_create_config(db)

    genres = config.genre_to_artists or {}
    genre_lower = genre.lower()

    if genre_lower not in genres:
        raise HTTPException(status_code=404, detail="Genre not found")

    del genres[genre_lower]
    config.genre_to_artists = genres

    db.commit()
    db.refresh(config)

    logger.info("genre_deleted", genre=genre)

    return {"message": "Genre deleted", "genre": genre}


@router.get("/browser-playback")
async def get_browser_playback_config(
    db: Session = Depends(get_db)
) -> Dict[str, Any]:
    """
    Get browser playback configuration (public endpoint for Jarvis Web).

    Returns the configuration needed for browser-based music playback,
    including MA WebSocket URL, stream URL, and browser player settings.
    No authentication required as this is fetched by the browser client.
    """
    config = get_or_create_config(db)

    # Check if feature is enabled
    feature = db.query(Feature).filter(Feature.name == 'music_playback').first()
    feature_enabled = feature.enabled if feature else False

    # Browser playback feature flag
    browser_feature = db.query(Feature).filter(Feature.name == 'browser_music_playback').first()
    browser_enabled = browser_feature.enabled if browser_feature else False

    if not config.music_assistant_enabled or not feature_enabled:
        return {
            "enabled": False,
            "error": "Music playback is disabled"
        }

    if not browser_enabled:
        return {
            "enabled": False,
            "error": "Browser playback is disabled"
        }

    # Extract WebSocket URL from base URL
    # MA server typically runs WebSocket on same port
    ma_url = config.music_assistant_url or os.getenv("MUSIC_ASSISTANT_URL", "")
    ws_url = ma_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"

    return {
        "enabled": True,
        "ws_url": ws_url,
        "stream_base_url": ma_url,
        "browser_player_name": "Jarvis Web Browser",
        "default_volume": config.default_volume or 0.7,
        "default_radio_mode": config.default_radio_mode if config.default_radio_mode is not None else True
    }


@router.get("/artists/search")
async def search_artists(
    q: str,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user)
) -> List[Dict[str, str]]:
    """
    Search for artists using Music Assistant.

    Query params:
    - q: Search query (minimum 2 characters)

    Returns list of matching artists for autocomplete.
    """
    if len(q) < 2:
        return []

    config = get_or_create_config(db)

    # Try Music Assistant search if configured
    if config.music_assistant_url and config.music_assistant_enabled:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Music Assistant search endpoint
                response = await client.get(
                    f"{config.music_assistant_url}/api/search",
                    params={"query": q, "media_type": "artist", "limit": 10}
                )

                if response.status_code == 200:
                    data = response.json()
                    return [
                        {"name": artist.get("name", ""), "id": artist.get("item_id", "")}
                        for artist in data.get("artists", [])
                    ]
        except Exception as e:
            logger.warning("music_assistant_search_failed", error=str(e))

    # Fallback: Search in existing genre artists
    genres = config.genre_to_artists or {}
    matches = set()
    q_lower = q.lower()

    for artists in genres.values():
        for artist in artists:
            if q_lower in artist.lower():
                matches.add(artist)

    return [{"name": name, "id": name} for name in sorted(matches)[:10]]
