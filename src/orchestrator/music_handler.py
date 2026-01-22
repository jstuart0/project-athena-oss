"""
Music playback handler for Athena orchestrator.

Handles music intents via Music Assistant integration in Home Assistant.
Supports:
- Basic playback (play artist, album, playlist, genre)
- Music controls (pause, next, volume)
- Multi-room independent playback (2 Spotify accounts)
- Queue management (add to queue, what's next)
- Music transfer between rooms
- Room group synced playback
- Room exclusion ("play everywhere except...")
- Per-room pause/resume with state tracking
"""
import os
import re
import time
import structlog
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from shared.ha_client import HomeAssistantClient
from shared.admin_config import AdminConfigClient
from orchestrator.follow_me_audio import get_most_recent_room

logger = structlog.get_logger()

# Music Assistant configuration status cache
_ma_config_checked: bool = False
_ma_has_players: bool = False


async def check_music_assistant_players(ha_client: HomeAssistantClient) -> bool:
    """
    Check if Music Assistant has any players configured.

    Music Assistant creates media_player.mass_* entities when players are linked.
    If no such entities exist, play_media calls will silently fail.

    Returns:
        True if MA has players configured, False otherwise
    """
    global _ma_config_checked, _ma_has_players

    # Only check once per session
    if _ma_config_checked:
        return _ma_has_players

    try:
        # Get all HA states and look for MA player entities
        import httpx
        ha_url = ha_client.url
        async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
            response = await client.get(
                f"{ha_url}/api/states",
                headers=ha_client.headers
            )
            if response.status_code == 200:
                states = response.json()
                # MA players have mass_player_type attribute (player or group)
                ma_players = [
                    e for e in states
                    if e.get("entity_id", "").startswith("media_player.")
                    and e.get("attributes", {}).get("mass_player_type")
                ]
                _ma_has_players = len(ma_players) > 0
                _ma_config_checked = True

                if not _ma_has_players:
                    logger.warning(
                        "music_assistant_no_players",
                        msg="Music Assistant has no players configured. "
                            "Music playback will not work. Please add players "
                            "in Home Assistant → Music Assistant → Settings → Players"
                    )
                else:
                    logger.info(
                        "music_assistant_players_found",
                        count=len(ma_players),
                        players=[e["entity_id"] for e in ma_players]
                    )

                return _ma_has_players
    except Exception as e:
        logger.error("music_assistant_check_failed", error=str(e))

    return False


class RoomPlaybackState(Enum):
    """Playback state for a room."""
    IDLE = "idle"           # Not playing anything
    PLAYING = "playing"     # Actively playing
    PAUSED = "paused"       # Paused mid-song
    EXCLUDED = "excluded"   # Excluded from current group playback


@dataclass
class RoomState:
    """Tracks playback state for a single room."""
    room_name: str
    state: RoomPlaybackState = RoomPlaybackState.IDLE
    entity_id: Optional[str] = None
    current_media: Optional[str] = None  # Track/artist being played
    volume_level: float = 0.5
    position_ms: int = 0  # Position in current track
    paused_at: Optional[float] = None  # Timestamp when paused
    resumed_at: Optional[float] = None
    part_of_group: Optional[str] = None  # Group name if in synced group
    excluded_from: Optional[str] = None  # Group name if excluded


class PlaybackStateManager:
    """
    Manages per-room playback state for independent and group playback.

    Tracks which rooms are playing, paused, or excluded to enable:
    - "Play everywhere except the office"
    - "Pause just the kitchen"
    - "Resume the living room"
    """

    def __init__(self):
        self._states: Dict[str, RoomState] = {}
        self._groups: Dict[str, Set[str]] = {}  # group_name -> set of room_names

    def get_room_state(self, room_name: str) -> RoomState:
        """Get or create room state."""
        normalized = room_name.lower().strip().replace(" ", "_")
        if normalized not in self._states:
            self._states[normalized] = RoomState(room_name=normalized)
        return self._states[normalized]

    def set_playing(self, room_name: str, entity_id: str, media: str):
        """Mark room as playing."""
        state = self.get_room_state(room_name)
        state.state = RoomPlaybackState.PLAYING
        state.entity_id = entity_id
        state.current_media = media
        state.paused_at = None
        state.resumed_at = time.time()
        logger.debug("room_playing", room=room_name, media=media[:50] if media else None)

    def set_paused(self, room_name: str, position_ms: int = 0):
        """Mark room as paused."""
        state = self.get_room_state(room_name)
        state.state = RoomPlaybackState.PAUSED
        state.position_ms = position_ms
        state.paused_at = time.time()
        logger.debug("room_paused", room=room_name, position_ms=position_ms)

    def set_idle(self, room_name: str):
        """Mark room as idle (stopped)."""
        state = self.get_room_state(room_name)
        state.state = RoomPlaybackState.IDLE
        state.current_media = None
        state.position_ms = 0
        state.paused_at = None
        state.part_of_group = None

    def set_excluded(self, room_name: str, from_group: str):
        """Mark room as excluded from a group."""
        state = self.get_room_state(room_name)
        state.state = RoomPlaybackState.EXCLUDED
        state.excluded_from = from_group
        logger.debug("room_excluded", room=room_name, from_group=from_group)

    def add_to_group(self, room_name: str, group_name: str):
        """Add room to a sync group."""
        if group_name not in self._groups:
            self._groups[group_name] = set()
        self._groups[group_name].add(room_name)
        state = self.get_room_state(room_name)
        state.part_of_group = group_name

    def remove_from_group(self, room_name: str, group_name: str):
        """Remove room from a sync group."""
        if group_name in self._groups:
            self._groups[group_name].discard(room_name)
        state = self.get_room_state(room_name)
        if state.part_of_group == group_name:
            state.part_of_group = None
            state.excluded_from = None

    def get_group_rooms(self, group_name: str) -> Set[str]:
        """Get rooms in a group."""
        return self._groups.get(group_name, set()).copy()

    def get_playing_rooms(self) -> List[str]:
        """Get list of rooms currently playing."""
        return [name for name, state in self._states.items()
                if state.state == RoomPlaybackState.PLAYING]

    def get_paused_rooms(self) -> List[str]:
        """Get list of rooms currently paused."""
        return [name for name, state in self._states.items()
                if state.state == RoomPlaybackState.PAUSED]

    def is_playing(self, room_name: str) -> bool:
        """Check if room is currently playing."""
        state = self.get_room_state(room_name)
        return state.state == RoomPlaybackState.PLAYING

    def is_paused(self, room_name: str) -> bool:
        """Check if room is currently paused."""
        state = self.get_room_state(room_name)
        return state.state == RoomPlaybackState.PAUSED


# Singleton playback state manager
_playback_manager: Optional[PlaybackStateManager] = None


def get_playback_manager() -> PlaybackStateManager:
    """Get or create playback state manager singleton."""
    global _playback_manager
    if _playback_manager is None:
        _playback_manager = PlaybackStateManager()
    return _playback_manager

# Fallback room to Music Assistant media player entity mapping
# Used when admin API is unavailable
# Source of truth is now the Admin backend's room_audio_config table
# Updated 2026-01-05 to use Music Assistant entities (mass_player_type: player)
# NOT the AirPlay entities (office_3, kitchen_2, etc.)
FALLBACK_ROOM_TO_PLAYER = {
    "living_room": "media_player.living_room_2_2",  # MA player
    "kitchen": "media_player.kitchen",              # MA player
    "office": "media_player.office_group",          # MA stereo group (both HomePods)
    "bedroom": "media_player.master_bedroom",       # Alias - MA player
    "master_bedroom": "media_player.master_bedroom", # MA player
    "master_bathroom": "media_player.master_bathroom", # MA player
    "alpha": "media_player.alpha",                  # MA player
    "beta": "media_player.beta",                    # MA player
    "dining_room": "media_player.living_room_2_2",  # MA player (shared with living room)
    "basement": "media_player.basement_bathroom",   # Alias - MA player
    "basement_bathroom": "media_player.basement_bathroom", # MA player
    "main_bathroom": "media_player.main_bathroom_2", # MA player
    "home": "media_player.office_group",            # Default - office stereo pair
}

# Cache for room configs fetched from admin API
_room_config_cache: Dict[str, Any] = {}
_room_config_cache_time: float = 0
ROOM_CONFIG_CACHE_TTL = 300  # 5 minutes


async def get_room_configs() -> Dict[str, Dict[str, Any]]:
    """
    Fetch room audio configurations from Admin API.
    Returns dict mapping room_name -> config dict.
    Falls back to hardcoded values if API unavailable.
    """
    import httpx
    import time
    global _room_config_cache, _room_config_cache_time

    # Check cache
    if _room_config_cache and (time.time() - _room_config_cache_time) < ROOM_CONFIG_CACHE_TTL:
        return _room_config_cache

    admin_url = os.getenv("ADMIN_API_URL", "http://localhost:8080")

    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            response = await client.get(f"{admin_url}/api/room-audio/internal")
            if response.status_code == 200:
                configs = response.json()
                # Convert list to dict keyed by room_name
                result = {}
                for config in configs:
                    room_name = config.get("room_name", "").lower()
                    result[room_name] = config
                    # Add common aliases
                    if room_name == "master_bedroom":
                        result["bedroom"] = config
                    if room_name == "basement_bathroom":
                        result["basement"] = config

                _room_config_cache = result
                _room_config_cache_time = time.time()
                logger.info("room_configs_loaded", count=len(configs), source="admin_api")
                return result
    except Exception as e:
        logger.warning("room_configs_fetch_failed", error=str(e), fallback="hardcoded")

    # Fallback to hardcoded values
    return {name: {"room_name": name, "primary_entity_id": entity}
            for name, entity in FALLBACK_ROOM_TO_PLAYER.items()}


def get_room_entity(room_name: str, room_configs: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Get the primary entity ID for a room from config."""
    config = room_configs.get(room_name.lower())
    if config:
        return config.get("primary_entity_id")
    return None


def get_room_secondary_entity(room_name: str, room_configs: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Get the secondary entity ID for a room (e.g., second HomePod in stereo pair)."""
    config = room_configs.get(room_name.lower())
    if config:
        return config.get("secondary_entity_id")
    return None


def get_room_display_names(room_configs: Dict[str, Dict[str, Any]]) -> List[str]:
    """Get list of room display names for voice responses."""
    # Filter out aliases (bedroom, basement, home)
    display_names = []
    seen = set()
    for name, config in room_configs.items():
        if name in ("bedroom", "basement", "home"):
            continue
        display = config.get("display_name") or name.replace("_", " ").title()
        if display not in seen:
            display_names.append(display)
            seen.add(display)
    return sorted(display_names)

# Genre to artist mapping for radio mode (seeds)
# When user says "play jazz", we randomly select an artist as the radio seed
# Each genre has multiple artists for variety
import random

GENRE_TO_ARTISTS = {
    "jazz": ["Miles Davis", "John Coltrane", "Thelonious Monk", "Charlie Parker", "Duke Ellington", "Herbie Hancock", "Bill Evans", "Chet Baker"],
    "rock": ["Led Zeppelin", "The Rolling Stones", "Pink Floyd", "Queen", "AC/DC", "The Beatles", "Nirvana", "Guns N' Roses"],
    "classical": ["Ludwig van Beethoven", "Wolfgang Amadeus Mozart", "Johann Sebastian Bach", "Frédéric Chopin", "Pyotr Tchaikovsky"],
    "electronic": ["Daft Punk", "Deadmau5", "Aphex Twin", "The Chemical Brothers", "Kraftwerk", "Boards of Canada"],
    "hip hop": ["Kendrick Lamar", "J. Cole", "Drake", "Kanye West", "Jay-Z", "Nas", "Tyler the Creator"],
    "country": ["Johnny Cash", "Willie Nelson", "Dolly Parton", "Chris Stapleton", "Zach Bryan", "Luke Combs"],
    "r&b": ["Marvin Gaye", "Stevie Wonder", "Prince", "D'Angelo", "Frank Ocean", "SZA", "The Weeknd"],
    "pop": ["Michael Jackson", "Taylor Swift", "Dua Lipa", "Bruno Mars", "Beyoncé", "Lady Gaga"],
    "indie": ["Arcade Fire", "Bon Iver", "Vampire Weekend", "Tame Impala", "Fleet Foxes", "The National"],
    "metal": ["Metallica", "Iron Maiden", "Black Sabbath", "Slayer", "Pantera", "Tool"],
    "blues": ["B.B. King", "Muddy Waters", "Robert Johnson", "John Lee Hooker", "Stevie Ray Vaughan"],
    "reggae": ["Bob Marley", "Peter Tosh", "Jimmy Cliff", "Toots and the Maytals", "Lee Scratch Perry"],
    "folk": ["Bob Dylan", "Joni Mitchell", "Simon & Garfunkel", "Neil Young", "James Taylor", "Cat Stevens"],
    "soul": ["Aretha Franklin", "Otis Redding", "Sam Cooke", "Al Green", "Marvin Gaye"],
    "funk": ["James Brown", "Parliament-Funkadelic", "Sly and the Family Stone", "Earth Wind & Fire", "Prince"],
    "alternative": ["Radiohead", "The Smiths", "R.E.M.", "Pixies", "The Cure", "Depeche Mode"],
    "punk": ["The Ramones", "The Clash", "Sex Pistols", "Green Day", "Bad Religion", "Black Flag"],
    "disco": ["Bee Gees", "Donna Summer", "Chic", "Gloria Gaynor", "ABBA"],
    "techno": ["Carl Cox", "Richie Hawtin", "Jeff Mills", "Derrick May", "Nina Kraviz"],
    "house": ["Frankie Knuckles", "Larry Heard", "Kerri Chandler", "Disclosure", "Fisher"],
}


def get_genre_artist(genre: str) -> str:
    """Get a random artist for a genre to seed radio mode."""
    genre_lower = genre.lower()
    artists = GENRE_TO_ARTISTS.get(genre_lower, [])
    if artists:
        return random.choice(artists)
    return None


# Legacy dict for backward compatibility (returns first artist)
GENRE_TO_ARTIST = {genre: artists[0] for genre, artists in GENRE_TO_ARTISTS.items()}


@dataclass
class AccountAssignment:
    """Tracks which Spotify account is assigned to which room."""
    account_id: str
    room: str
    assigned_at: datetime
    last_queue_state: dict = field(default_factory=dict)


class SpotifyAccountPool:
    """
    Manages Spotify accounts for independent multi-room playback.

    With 2 accounts, we can play different music in 2 rooms simultaneously.
    """

    def __init__(self, accounts: Optional[List[str]] = None):
        """
        Initialize with list of Spotify account identifiers.

        Args:
            accounts: List of Spotify account IDs from Music Assistant.
                      If None, uses default placeholder accounts.
        """
        # Default to 2 placeholder accounts - update with real IDs from Music Assistant
        if accounts is None:
            accounts = [
                os.getenv("SPOTIFY_ACCOUNT_1", "spotify_primary"),
                os.getenv("SPOTIFY_ACCOUNT_2", "spotify_secondary"),
            ]

        self.accounts = set(accounts)
        self.available = set(accounts)
        self.assignments: Dict[str, AccountAssignment] = {}  # room -> assignment

    def get_account_for_room(self, room: str) -> Optional[str]:
        """
        Get or assign a Spotify account for a room.

        Returns:
            Account ID if available, None if all accounts in use
        """
        # Room already has an account? Reuse it
        if room in self.assignments:
            logger.debug(
                "spotify_account_reused",
                account=self.assignments[room].account_id,
                room=room
            )
            return self.assignments[room].account_id

        # Try to assign next available account
        if not self.available:
            logger.warning(
                "spotify_accounts_exhausted",
                room=room,
                active_rooms=list(self.assignments.keys())
            )
            return None

        account = self.available.pop()
        self.assignments[room] = AccountAssignment(
            account_id=account,
            room=room,
            assigned_at=datetime.now()
        )
        logger.info(
            "spotify_account_assigned",
            account=account,
            room=room
        )
        return account

    def release_room(self, room: str, queue_state: Optional[dict] = None) -> bool:
        """
        Release a room's account back to the pool.

        Args:
            room: Room to release
            queue_state: Optional queue state to preserve for resume

        Returns:
            True if room was released, False if room had no assignment
        """
        if room not in self.assignments:
            return False

        assignment = self.assignments.pop(room)

        # Save queue state for potential resume
        if queue_state:
            assignment.last_queue_state = queue_state

        self.available.add(assignment.account_id)
        logger.info(
            "spotify_account_released",
            account=assignment.account_id,
            room=room
        )
        return True

    def get_active_rooms(self) -> List[str]:
        """Get list of rooms with active playback."""
        return list(self.assignments.keys())

    def get_room_account(self, room: str) -> Optional[str]:
        """Get the account assigned to a room, if any."""
        if room in self.assignments:
            return self.assignments[room].account_id
        return None

    def is_room_active(self, room: str) -> bool:
        """Check if a room has an active account assignment."""
        return room in self.assignments

    def get_available_count(self) -> int:
        """Get number of available accounts."""
        return len(self.available)

    def can_start_new_stream(self) -> bool:
        """Check if we can start a new independent stream."""
        return len(self.available) > 0

    def transfer_assignment(self, source_room: str, target_room: str) -> bool:
        """
        Transfer an account assignment from one room to another.

        Used when transferring music playback between rooms.
        """
        if source_room not in self.assignments:
            return False

        assignment = self.assignments.pop(source_room)
        assignment.room = target_room
        assignment.assigned_at = datetime.now()
        self.assignments[target_room] = assignment

        logger.info(
            "spotify_account_transferred",
            account=assignment.account_id,
            from_room=source_room,
            to_room=target_room
        )
        return True


class MusicHandler:
    """
    Handles music playback intents via Music Assistant.

    Integrates with Home Assistant's Music Assistant add-on for:
    - Spotify playback to HomePods via AirPlay 2
    - Multi-room independent or synced playback
    - Queue management and music transfer
    - Room exclusion ("play everywhere except the office")
    - Per-room pause/resume with state tracking
    """

    def __init__(
        self,
        ha_client: HomeAssistantClient,
        admin_client: Optional[AdminConfigClient] = None,
        spotify_accounts: Optional[List[str]] = None
    ):
        """
        Initialize the music handler.

        Args:
            ha_client: Home Assistant client for API calls
            admin_client: Admin config client for room groups
            spotify_accounts: List of Spotify account IDs (default: 2 accounts)
        """
        self.ha = ha_client
        self.admin = admin_client
        self.account_pool = SpotifyAccountPool(spotify_accounts)
        self.default_room = "home"  # Fallback if no room specified
        self.playback_manager = get_playback_manager()  # Per-room state tracking

    def _normalize_room(self, room: Optional[str]) -> str:
        """Normalize room name to match entity keys."""
        if not room:
            return self.default_room

        # Normalize: lowercase, replace spaces with underscores
        normalized = room.lower().strip().replace(" ", "_")

        # Handle common aliases
        room_aliases = {
            "master": "master_bedroom",
            "living": "living_room",
            "dining": "dining_room",
            "main_bedroom": "master_bedroom",
        }

        # For web/unknown interfaces, use motion detection to find current room
        if normalized in ("web", "jarvis-web", "unknown"):
            detected_room = get_most_recent_room(default="office")
            logger.info(
                "room_detected_from_motion",
                source=normalized,
                detected_room=detected_room
            )
            return detected_room

        return room_aliases.get(normalized, normalized)

    async def _get_entity_for_room(self, room: str) -> Optional[str]:
        """Get the media player entity ID for a room from admin config."""
        normalized = self._normalize_room(room)
        room_configs = await get_room_configs()
        return get_room_entity(normalized, room_configs)

    async def get_playing_rooms_from_ha(self) -> Dict[str, str]:
        """
        Query Home Assistant directly to find which rooms have music playing.

        This is essential for follow-me audio to work with music started
        from any source (HA, Music Assistant app, voice commands, etc.)

        Returns:
            Dict mapping room_name -> entity_id for rooms with playing media
        """
        import httpx

        playing_rooms = {}

        try:
            # Get room configs to know which entities to look for
            room_configs = await get_room_configs()
            # Build reverse mapping: entity_id -> room_name
            # room_configs values are dicts with 'primary_entity_id' key
            entity_to_room = {}
            for room_name, config in room_configs.items():
                if isinstance(config, dict):
                    entity_id = config.get("primary_entity_id")
                    if entity_id:
                        entity_to_room[entity_id] = room_name
                elif isinstance(config, str) and config:
                    # Fallback format: direct entity string
                    entity_to_room[config] = room_name

            # Query HA for all media player states
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                response = await client.get(
                    f"{self.ha.url}/api/states",
                    headers=self.ha.headers
                )

                if response.status_code == 200:
                    states = response.json()

                    for entity in states:
                        entity_id = entity.get("entity_id", "")
                        state = entity.get("state", "")

                        # Check if this entity is playing
                        if entity_id.startswith("media_player.") and state == "playing":
                            # Check if we know which room this is
                            if entity_id in entity_to_room:
                                room = entity_to_room[entity_id]
                                playing_rooms[room] = entity_id
                                logger.debug(
                                    "ha_playing_detected",
                                    room=room,
                                    entity_id=entity_id
                                )
                            # Also check group entities by room name in entity_id
                            elif "group" in entity_id.lower():
                                # Try to match by room name appearing in entity_id
                                for room_name in room_configs.keys():
                                    if room_name in entity_id.lower().replace("_", ""):
                                        playing_rooms[room_name] = entity_id
                                        break
                                    # Check without underscores
                                    if room_name.replace("_", "") in entity_id.lower().replace("_", ""):
                                        playing_rooms[room_name] = entity_id
                                        break

            if playing_rooms:
                logger.debug(
                    "ha_playing_rooms_found",
                    rooms=list(playing_rooms.keys()),
                    count=len(playing_rooms)
                )

        except Exception as e:
            logger.error("get_playing_rooms_from_ha_failed", error=str(e))

        return playing_rooms

    async def parse_music_play_intent(
        self,
        query: str,
        room: Optional[str] = None,
        interface_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Parse a music play query into structured intent data.

        Args:
            query: The user's query (e.g., "play jazz in the kitchen")
            room: Optional room from context
            interface_type: Optional interface type ("voice", "text", "jarvis_web")

        Returns:
            Dict with media_type, media_id, room, radio_mode, is_room_group, play_in_browser
        """
        # Strip leading/trailing whitespace to ensure regex matching works
        query_lower = query.lower().strip()

        # Detect browser playback patterns
        # User says "play X here" or "play X on this device" from Jarvis Web
        play_in_browser = False
        browser_patterns = [
            r'\b(?:on|in)\s+(?:this|the)\s+(?:device|browser|phone|app)\b',
            r'\bhere\b(?:\s|$)',  # "play X here" but not "hear"
            r'\bplay\s+(?:it\s+)?(?:on\s+)?here\b',
        ]

        for pattern in browser_patterns:
            if re.search(pattern, query_lower):
                play_in_browser = True
                logger.info("browser_playback_detected", pattern=pattern, query=query[:50])
                break

        # Also enable browser playback if interface is jarvis_web and room is "jarvis_web"
        if interface_type == "jarvis_web" or room == "jarvis_web":
            play_in_browser = True
            logger.info("browser_playback_from_interface", interface=interface_type, room=room)

        # Get room configs from admin API
        room_configs = await get_room_configs()
        room_keys = list(room_configs.keys())

        # Build room group lookup: term -> group_name
        # This includes group names and all aliases
        room_group_lookup = {}
        is_room_group = False
        try:
            admin_client = AdminConfigClient()
            room_groups = await admin_client.get_room_groups(enabled_only=True)
            for group in room_groups:
                group_name = group.get("name", "")
                display_name = group.get("display_name", "")
                aliases = group.get("aliases", [])

                # Add group name variations
                if group_name:
                    room_group_lookup[group_name.replace("_", " ")] = group_name
                    room_group_lookup[group_name] = group_name
                if display_name:
                    room_group_lookup[display_name.lower()] = group_name

                # Add all aliases
                for alias in aliases:
                    if alias:
                        room_group_lookup[alias.lower()] = group_name

            logger.debug(
                "room_group_lookup_built",
                group_count=len(room_groups),
                term_count=len(room_group_lookup),
                terms=list(room_group_lookup.keys())[:10]  # Log first 10 for debugging
            )
        except Exception as e:
            logger.warning("room_group_lookup_failed", error=str(e))

        # Extract room/room group from query if present
        room_match = None

        # First, check for room group patterns (higher priority)
        # Check patterns: "on the X", "in the X", "on X", "in X", or just X at end
        group_patterns = [
            r'(?:on|in)\s+the\s+(.+?)(?:\s+room)?$',  # "on the first floor"
            r'(?:on|in)\s+(.+?)(?:\s+room)?$',         # "on first floor"
            r'(?:play\s+\w+\s+)?(?:on|in)\s+the\s+(.+?)(?:\s|$)',  # mid-sentence
            r'(?:play\s+\w+\s+)?(?:on|in)\s+(.+?)(?:\s|$)',        # mid-sentence without "the"
        ]

        for pattern in group_patterns:
            match = re.search(pattern, query_lower)
            if match:
                potential_term = match.group(1).strip()
                # Check if this matches a room group or alias
                if potential_term in room_group_lookup:
                    room_match = room_group_lookup[potential_term]
                    is_room_group = True
                    logger.info(
                        "room_group_matched",
                        query_term=potential_term,
                        group_name=room_match
                    )
                    break

        # Also check for direct alias mentions (e.g., "play jazz downstairs", "play music everywhere")
        if not room_match:
            for alias, group_name in room_group_lookup.items():
                # Check if alias appears as a word (not substring) in query
                if re.search(r'\b' + re.escape(alias) + r'\b', query_lower):
                    room_match = group_name
                    is_room_group = True
                    logger.info(
                        "room_group_alias_direct_match",
                        alias=alias,
                        group_name=group_name
                    )
                    break

        # If no room group match, check for individual rooms
        if not room_match:
            for room_key in room_keys:
                room_pattern = room_key.replace("_", " ")
                if room_pattern in query_lower:
                    room_match = room_key
                    break

        # Also check for "in the X" pattern for individual rooms
        if not room_match:
            in_the_match = re.search(r'in the (\w+(?:\s+\w+)?)', query_lower)
            if in_the_match:
                extracted_room = in_the_match.group(1).replace(" ", "_")
                if extracted_room in room_configs or extracted_room + "_room" in room_configs:
                    room_match = extracted_room if extracted_room in room_configs else extracted_room + "_room"

        # Use extracted room, fallback to provided room, then default
        target_room = room_match or room or self.default_room

        # Detect media type and ID
        media_type = "artist"  # Default
        media_id = ""
        radio_mode = True  # Default to radio mode for variety

        # Check for genre - use random artist selection for variety
        for genre in GENRE_TO_ARTISTS.keys():
            if genre in query_lower:
                media_type = "artist"  # Use artist as seed for genre radio
                media_id = get_genre_artist(genre)  # Random artist selection
                radio_mode = True
                break

        # Check for playlist references
        playlist_patterns = [
            r"my (\w+) playlist",
            r"playlist (\w+)",
            r"the (\w+) playlist"
        ]
        for pattern in playlist_patterns:
            match = re.search(pattern, query_lower)
            if match:
                media_type = "playlist"
                media_id = match.group(1) + " playlist"
                radio_mode = False
                break

        # If no specific match, extract artist/album name after "play"
        if not media_id:
            play_match = re.match(r'play\s+(.+?)(?:\s+in\s+the|\s+on\s+the|$)', query_lower)
            if play_match:
                content = play_match.group(1).strip()
                # Remove common words
                content = re.sub(r'\b(some|music|by|the)\b', '', content).strip()
                # Remove browser playback indicators (here, on this device, etc.)
                content = re.sub(r'\s+here\s*$', '', content).strip()
                content = re.sub(r'\s+(?:on|in)\s+(?:this|the)\s+(?:device|browser|phone|app)\s*$', '', content).strip()
                if content:
                    media_id = content.title()  # Capitalize for artist names
                    media_type = "artist"

        # Fallback to generic "music" if nothing parsed
        if not media_id:
            media_id = "Lo-Fi Beats"  # Pleasant default
            media_type = "artist"

        # Check for exclusion patterns ("everywhere except...", "all rooms but...")
        excluded_rooms = await self._parse_exclusion_pattern(query_lower, room_configs)

        return {
            "media_type": media_type,
            "media_id": media_id,
            "room": target_room,
            "radio_mode": radio_mode,
            "is_room_group": is_room_group,
            "excluded_rooms": excluded_rooms,  # List of rooms to skip
            "play_in_browser": play_in_browser  # True if user wants browser playback
        }

    async def _parse_exclusion_pattern(
        self,
        query: str,
        room_configs: Dict[str, Dict[str, Any]]
    ) -> List[str]:
        """
        Parse "except X" or "but not X" patterns from query.

        Examples:
        - "play jazz everywhere except the office" -> ["office"]
        - "play in all rooms but the kitchen and bedroom" -> ["kitchen", "bedroom"]
        """
        excluded = []

        # Patterns for exclusion
        patterns = [
            r'(?:everywhere|all rooms?)\s+(?:except|but|but not|other than)\s+(?:the\s+)?(.+?)(?:\s+play|\s+$|$)',
            r'(?:except|but|but not|other than)\s+(?:the\s+)?(.+?)(?:\s+play|\s|$)',
        ]

        for pattern in patterns:
            match = re.search(pattern, query)
            if match:
                excluded_text = match.group(1).strip()
                # Parse room list (could be comma or "and" separated)
                room_parts = re.split(r',\s*|\s+and\s+', excluded_text)
                for part in room_parts:
                    part = part.strip().lower().replace("the ", "")
                    # Try to match to a known room
                    normalized = part.replace(" ", "_")
                    if normalized in room_configs:
                        excluded.append(normalized)
                    elif normalized + "_room" in room_configs:
                        excluded.append(normalized + "_room")
                    else:
                        # Check display names
                        for room_key, config in room_configs.items():
                            display = config.get("display_name", "").lower()
                            if part == display or part == room_key.replace("_", " "):
                                excluded.append(room_key)
                                break

                if excluded:
                    logger.info("exclusion_pattern_matched", excluded=excluded, query=query[:50])
                break

        return excluded

    async def parse_music_control_intent(self, query: str, room: Optional[str] = None) -> Dict[str, Any]:
        """
        Parse a music control query into structured intent data.

        Args:
            query: The user's query (e.g., "pause the music")
            room: Optional room from context

        Returns:
            Dict with action, room, volume_level (if applicable)
        """
        # Strip leading/trailing whitespace to ensure pattern matching works
        query_lower = query.lower().strip()

        # Determine action
        action = None
        volume_level = None

        # Now playing / what's playing queries - check FIRST before pause/stop
        if any(p in query_lower for p in [
            "what song", "what's playing", "whats playing", "what is playing",
            "what track", "what artist", "who is this", "who's playing",
            "now playing", "currently playing", "what am i listening",
            "what are we listening", "what music", "song called", "song name",
            "whats that song", "what is that song", "whats this song",
            "what is this song", "name of this song", "name of the song",
            "whats jammin", "what's jammin", "whats jamming", "what's jamming",  # Round 13
            "whats playin", "what's playin", "playin rn", "playing rn",  # Round 14
            # Round 17: "is music playing" status check queries
            "is music playing", "is anything playing", "is something playing",
            "is there music", "music on right now", "music going",
            "any music on", "any music playing", "is the music on",
            "is there anything playing", "anything playing right now"
        ]):
            action = "now_playing"
        elif any(p in query_lower for p in ["pause", "stop", "hold up", "hold on"]):
            action = "pause"
        elif any(p in query_lower for p in ["resume", "unpause", "continue"]):
            action = "play"
        elif query_lower.strip() == "next" or any(p in query_lower for p in [
            "next song", "next track", "skip song", "skip track", "skip this", "skip",
            "play the next one", "next one", "play the next",
            "put on the next track", "put on the next one", "put on next"  # Round 12
        ]):
            action = "next"
        elif query_lower.strip() == "previous" or any(p in query_lower for p in [
            "previous song", "previous track", "go back", "go back one song",
            "back one song", "one song back", "play the last one"
        ]):
            action = "previous"
        elif any(p in query_lower for p in [
            "shuffle", "shuffle on", "enable shuffle", "shuffle my", "shuffle music"
        ]):
            action = "shuffle"
        elif any(p in query_lower for p in [
            "repeat", "repeat this", "repeat song", "repeat on", "enable repeat", "loop"
        ]):
            action = "repeat"
        elif any(p in query_lower for p in [
            "volume up", "turn it up", "louder", "turn up the volume",
            "turn the volume up", "increase volume", "raise the volume",
            "volume higher", "crank it up", "make it louder", "crank up",
            "pump up", "pump it up", "blast it", "louder please",
            "turn up the music", "cant hear it", "can't hear it", "cant hear",
            "can't hear", "i cant hear", "i can't hear", "volume way up",
            "turn the volume way up", "turn it way up",
            "give it more volume", "more volume", "pump up the jam",  # Round 12
            "crank this", "crank the", "crank that",  # Round 13/15 - "crank this/that music"
            "crank the music"  # Round 15
        ]):
            action = "volume_up"
        elif any(p in query_lower for p in [
            "volume down", "turn it down", "quieter", "turn down the volume",
            "turn the volume down", "decrease volume", "lower the volume",
            "volume lower", "make it quieter", "softer", "quiet down",
            "turn down", "lower please", "not so loud", "too loud",
            "mad loud", "way too loud", "hella loud",  # Round 14
            "noise down", "that noise down", "less volume", "need less volume",  # Round 15
            "damn loud", "too damn loud", "so damn loud"  # Round 17
        ]):
            action = "volume_down"
        elif "mute" in query_lower:
            action = "mute" if "unmute" not in query_lower else "unmute"

        # Check for explicit volume level
        volume_match = re.search(r'volume (?:to |at )?(\d+)(?:\s*%)?', query_lower)
        if volume_match:
            action = "volume_set"
            volume_level = int(volume_match.group(1)) / 100.0  # Convert to 0.0-1.0

        # Get room configs from admin API
        room_configs = await get_room_configs()
        room_keys = list(room_configs.keys())

        # Check for house-wide patterns first (Round 17: "is music playing in the house")
        house_wide_patterns = [
            "in the house", "in my house", "in the home", "in my home",
            "anywhere in the", "around the house", "in this house"
        ]
        is_house_wide = any(p in query_lower for p in house_wide_patterns)

        # Extract room if present
        room_match = None
        for room_key in room_keys:
            room_pattern = room_key.replace("_", " ")
            if room_pattern in query_lower:
                room_match = room_key
                break

        # For house-wide queries with now_playing, use "all_rooms" to check everywhere
        if is_house_wide and action == "now_playing":
            target_room = "all_rooms"
        else:
            target_room = room_match or room or self.default_room

        return {
            "action": action or "pause",  # Default to pause
            "room": target_room,
            "volume_level": volume_level
        }

    async def handle_play(
        self,
        media_type: str,
        media_id: str,
        room: Optional[str] = None,
        radio_mode: bool = True
    ) -> str:
        """
        Handle music play intent with account pooling.

        Args:
            media_type: Type of media (artist, album, playlist, track, genre)
            media_id: Name or search term
            room: Target room (optional, defaults to home)
            radio_mode: Generate similar tracks after (default True)

        Returns:
            Response message for the user
        """
        # Check if Music Assistant has players configured
        has_ma_players = await check_music_assistant_players(self.ha)
        if not has_ma_players:
            logger.error(
                "music_play_blocked_no_ma_players",
                media_id=media_id,
                room=room
            )
            return (
                "Music playback is not available. Music Assistant has no players configured. "
                "Please add your speakers as players in Home Assistant → Music Assistant → Settings → Players."
            )

        # Get room configs from admin API
        room_configs = await get_room_configs()

        target_room = self._normalize_room(room)
        entity_id = get_room_entity(target_room, room_configs)

        if not entity_id:
            available_rooms = ", ".join(get_room_display_names(room_configs))
            return f"I don't know how to play music in {target_room.replace('_', ' ')}. Available rooms: {available_rooms}"

        # Get account from pool
        account = self.account_pool.get_account_for_room(target_room)
        if not account:
            active_rooms = [r.replace("_", " ") for r in self.account_pool.get_active_rooms()]
            return (
                f"Both Spotify accounts are in use ({', '.join(active_rooms)}). "
                f"Stop music in one room first, or ask me to play in one of those rooms."
            )

        # Handle genre -> artist conversion for radio mode (random selection)
        if media_type == "genre":
            artist = get_genre_artist(media_id)
            if artist:
                media_type = "artist"
                media_id = artist
                radio_mode = True
            else:
                return f"I don't have a good radio seed for {media_id} genre. Try asking for a specific artist."

        try:
            # Check for secondary entity (e.g., stereo HomePod pair)
            secondary_entity = get_room_secondary_entity(target_room, room_configs)

            if secondary_entity:
                # Try to join secondary to primary for stereo playback (HomePod pairs)
                # Non-fatal if join fails - will still play to primary speaker
                try:
                    logger.info(
                        "joining_stereo_pair",
                        primary=entity_id,
                        secondary=secondary_entity,
                        room=target_room
                    )
                    await self.ha.call_service(
                        domain="media_player",
                        service="join",
                        service_data={
                            "entity_id": secondary_entity,
                            "group_members": [entity_id]
                        }
                    )
                except Exception as join_error:
                    logger.warning(
                        "stereo_join_failed",
                        primary=entity_id,
                        secondary=secondary_entity,
                        error=str(join_error)
                    )
                    # Continue with single speaker playback

            # Call Music Assistant play_media service
            await self.ha.call_service(
                domain="music_assistant",
                service="play_media",
                service_data={
                    "entity_id": entity_id,
                    "media_id": media_id,
                    "media_type": media_type,
                    "radio_mode": radio_mode
                }
            )

            # Update playback manager so follow-me audio knows music is playing
            self.playback_manager.set_playing(target_room, entity_id, media_id)

            room_name = target_room.replace("_", " ")
            logger.info(
                "music_play_success",
                media_id=media_id,
                media_type=media_type,
                room=target_room,
                entity_id=entity_id,
                secondary_entity=secondary_entity
            )
            return f"Playing {media_id} in the {room_name}."

        except Exception as e:
            logger.error(
                "music_play_failed",
                error=str(e),
                media_id=media_id,
                room=target_room
            )
            # Release the account on failure
            self.account_pool.release_room(target_room)
            return f"Sorry, I couldn't play that. {str(e)}"

    async def handle_control(
        self,
        action: str,
        room: Optional[str] = None,
        volume_level: Optional[float] = None
    ) -> str:
        """
        Handle music control intent (pause, next, volume, etc.).

        Args:
            action: Control action (pause, play, next, previous, volume_up, etc.)
            room: Target room (optional)
            volume_level: Volume level 0.0-1.0 for volume_set action

        Returns:
            Response message for the user
        """
        # Round 17: Handle house-wide now_playing queries FIRST (before entity lookup)
        # "is there music playing in the house" should check all rooms
        if action == "now_playing" and room == "all_rooms":
            try:
                room_configs = await get_room_configs()
                playing_rooms = []

                for room_key in room_configs.keys():
                    room_entity = get_room_entity(room_key, room_configs)
                    if not room_entity:
                        continue

                    try:
                        player_state = await self.ha.get_state(room_entity)
                        if player_state and player_state.get('state') in ['playing', 'paused']:
                            attrs = player_state.get('attributes', {})
                            media_title = attrs.get('media_title', '')
                            media_artist = attrs.get('media_artist', '')
                            state_value = player_state.get('state')
                            room_display = room_key.replace('_', ' ')

                            if media_artist and media_title:
                                status = "Playing" if state_value == 'playing' else "Paused"
                                playing_rooms.append(f"{room_display}: {status} - {media_title} by {media_artist}")
                            elif media_title:
                                status = "Playing" if state_value == 'playing' else "Paused"
                                playing_rooms.append(f"{room_display}: {status} - {media_title}")
                            else:
                                playing_rooms.append(f"{room_display}: Something is playing")
                    except Exception as e:
                        logger.debug(f"Error checking room {room_key}: {e}")
                        continue

                if playing_rooms:
                    if len(playing_rooms) == 1:
                        return f"Music is playing in the house. {playing_rooms[0]}."
                    else:
                        return "Music is playing in the house. " + "; ".join(playing_rooms) + "."
                else:
                    return "No music is playing in the house right now."
            except Exception as e:
                logger.error(f"Error checking house-wide music: {e}")
                return f"I couldn't check what's playing in the house. Error: {str(e)}"

        target_room = self._normalize_room(room)
        entity_id = await self._get_entity_for_room(target_room)

        if not entity_id:
            return f"No music player found for {target_room.replace('_', ' ')}."

        service_map = {
            "pause": ("media_player", "media_pause"),
            "play": ("media_player", "media_play"),
            "stop": ("media_player", "media_stop"),
            "next": ("media_player", "media_next_track"),
            "previous": ("media_player", "media_previous_track"),
            "volume_up": ("media_player", "volume_up"),
            "volume_down": ("media_player", "volume_down"),
            "mute": ("media_player", "volume_mute"),
            "unmute": ("media_player", "volume_mute"),  # Toggle
            "shuffle": ("media_player", "shuffle_set"),
            "repeat": ("media_player", "repeat_set"),
        }

        try:
            # Handle now_playing - get current track info from HA
            if action == "now_playing":
                try:
                    # Get the specific media player state from Home Assistant
                    player_state = await self.ha.get_state(entity_id)

                    if player_state:
                        attrs = player_state.get('attributes', {})
                        media_title = attrs.get('media_title', '')
                        media_artist = attrs.get('media_artist', '')
                        media_album = attrs.get('media_album', '')
                        state_value = player_state.get('state', 'unknown')

                        if state_value in ['playing', 'paused']:
                            if media_artist and media_title:
                                status = "Currently playing" if state_value == 'playing' else "Paused"
                                response = f"{status}: {media_title} by {media_artist}"
                                if media_album:
                                    response += f" from {media_album}"
                                return response + f" in the {target_room.replace('_', ' ')}."
                            elif media_title:
                                status = "Currently playing" if state_value == 'playing' else "Paused"
                                return f"{status}: {media_title} in the {target_room.replace('_', ' ')}."
                            else:
                                return f"Something is playing in the {target_room.replace('_', ' ')}, but I don't have track details."
                        else:
                            return f"Nothing is playing in the {target_room.replace('_', ' ')} right now."
                    else:
                        return f"I couldn't find the music player for {target_room.replace('_', ' ')}."
                except Exception as e:
                    logger.error(f"Error getting now playing: {e}")
                    return f"I couldn't check what's playing. Error: {str(e)}"

            if action == "volume_set" and volume_level is not None:
                await self.ha.call_service(
                    domain="media_player",
                    service="volume_set",
                    service_data={
                        "entity_id": entity_id,
                        "volume_level": volume_level
                    }
                )
                logger.info(
                    "music_volume_set",
                    room=target_room,
                    volume=volume_level
                )
                return f"Volume set to {int(volume_level * 100)}%."

            elif action in service_map:
                domain, service = service_map[action]
                service_data = {"entity_id": entity_id}

                # For mute/unmute, we need to specify the mute state
                if action == "mute":
                    service_data["is_volume_muted"] = True
                elif action == "unmute":
                    service_data["is_volume_muted"] = False
                # For shuffle, we need to enable shuffle mode
                elif action == "shuffle":
                    service_data["shuffle"] = True
                # For repeat, we need to set repeat mode (one, all, or off)
                elif action == "repeat":
                    service_data["repeat"] = "one"  # Repeat current track

                await self.ha.call_service(
                    domain=domain,
                    service=service,
                    service_data=service_data
                )

                # Release account when stopping
                if action in ["stop", "pause"]:
                    # Note: We don't release on pause - user might resume
                    # Only release on explicit stop
                    if action == "stop":
                        self.account_pool.release_room(target_room)

                logger.info(
                    "music_control_success",
                    action=action,
                    room=target_room
                )
                # Provide contextual response based on action
                action_descriptions = {
                    "pause": "paused the music",
                    "stop": "stopped the music",
                    "resume": "resumed the music",
                    "next": "skipped to the next track",
                    "previous": "went back to the previous track",
                    "mute": "muted the music",
                    "unmute": "unmuted the music",
                    "shuffle": "enabled shuffle",
                    "repeat": "enabled repeat for the current song",
                }
                desc = action_descriptions.get(action, f"performed {action}")
                room_display = target_room.replace('_', ' ') if target_room else "the speaker"
                return f"Done! I've {desc} in {room_display}."

            else:
                return f"I don't know how to {action} music."

        except Exception as e:
            logger.error(
                "music_control_failed",
                error=str(e),
                action=action,
                room=target_room
            )
            return f"Sorry, I couldn't {action}. {str(e)}"

    async def handle_queue(
        self,
        action: str,
        media_type: Optional[str] = None,
        media_id: Optional[str] = None,
        room: Optional[str] = None
    ) -> str:
        """
        Handle queue management intents.

        Args:
            action: Queue action (add, what_next, clear, shuffle)
            media_type: Type of media to add (for "add" action)
            media_id: Media to add (for "add" action)
            room: Target room

        Returns:
            Response message for the user
        """
        # Check if Music Assistant has players configured (for add/clear actions)
        if action in ("add", "clear"):
            has_ma_players = await check_music_assistant_players(self.ha)
            if not has_ma_players:
                return (
                    "Music queue management is not available. Music Assistant has no players configured. "
                    "Please add your speakers as players in Home Assistant → Music Assistant → Settings → Players."
                )

        target_room = self._normalize_room(room)
        entity_id = await self._get_entity_for_room(target_room)

        if not entity_id:
            return f"No music player found for {target_room.replace('_', ' ')}."

        try:
            if action == "add" and media_id:
                # Add to queue using enqueue parameter
                await self.ha.call_service(
                    domain="music_assistant",
                    service="play_media",
                    service_data={
                        "entity_id": entity_id,
                        "media_id": media_id,
                        "media_type": media_type or "track",
                        "enqueue": "add"  # Add to end of queue
                    }
                )
                logger.info(
                    "music_queue_add",
                    media_id=media_id,
                    room=target_room
                )
                return f"Added {media_id} to the queue."

            elif action == "what_next":
                # Get queue state from Music Assistant
                state = await self.ha.get_state(entity_id)
                queue = state.get("attributes", {}).get("queue_items", [])
                if queue and len(queue) > 1:
                    next_track = queue[1]  # Index 0 is current
                    artist = next_track.get("artist", "Unknown")
                    title = next_track.get("name", next_track.get("title", "Unknown"))
                    return f"Next up: {title} by {artist}"
                return "Nothing else in the queue."

            elif action == "clear":
                await self.ha.call_service(
                    domain="music_assistant",
                    service="play_media",
                    service_data={
                        "entity_id": entity_id,
                        "media_id": "",
                        "enqueue": "replace"  # Clears queue
                    }
                )
                logger.info("music_queue_cleared", room=target_room)
                return "Queue cleared."

            elif action == "shuffle":
                await self.ha.call_service(
                    domain="media_player",
                    service="shuffle_set",
                    service_data={
                        "entity_id": entity_id,
                        "shuffle": True
                    }
                )
                logger.info("music_shuffle_enabled", room=target_room)
                return "Shuffle is on."

            else:
                return f"I don't know how to {action} the queue."

        except Exception as e:
            logger.error(
                "music_queue_failed",
                error=str(e),
                action=action,
                room=target_room
            )
            return f"Sorry, I couldn't {action} the queue. {str(e)}"

    async def handle_transfer(
        self,
        target_room: str,
        source_room: Optional[str] = None
    ) -> str:
        """
        Transfer music playback to another room.

        Args:
            target_room: Room to transfer music to
            source_room: Room to transfer from (optional, defaults to active room)

        Returns:
            Response message for the user
        """
        source = self._normalize_room(source_room) if source_room else None
        target = self._normalize_room(target_room)

        source_entity = await self._get_entity_for_room(source) if source else None
        target_entity = await self._get_entity_for_room(target)

        # If no source specified, find the active room
        if not source:
            active_rooms = self.account_pool.get_active_rooms()
            if active_rooms:
                source = active_rooms[0]
                source_entity = await self._get_entity_for_room(source)
            else:
                return "No music is currently playing to transfer."

        if not source_entity:
            return f"No music playing in {source.replace('_', ' ')}."
        if not target_entity:
            return f"I don't know how to play music in {target.replace('_', ' ')}."

        try:
            # Use Music Assistant's transfer_queue service
            await self.ha.call_service(
                domain="music_assistant",
                service="transfer_queue",
                service_data={
                    "entity_id": target_entity,
                    "source_player": source_entity,
                    "auto_play": True
                }
            )

            # Update account pool - transfer the assignment
            self.account_pool.transfer_assignment(source, target)

            source_name = source.replace("_", " ")
            target_name = target.replace("_", " ")

            logger.info(
                "music_transfer_success",
                from_room=source,
                to_room=target
            )
            return f"Moved music from {source_name} to {target_name}."

        except Exception as e:
            logger.error(
                "music_transfer_failed",
                error=str(e),
                from_room=source,
                to_room=target
            )
            return f"Sorry, I couldn't transfer the music. {str(e)}"

    async def handle_room_group_play(
        self,
        group_name: str,
        media_type: str,
        media_id: str,
        radio_mode: bool = True
    ) -> str:
        """
        Play synced music to a room group (uses 1 account for all rooms).

        Args:
            group_name: Name of the room group (e.g., "upstairs", "first_floor")
            media_type: Type of media to play
            media_id: Media to play
            radio_mode: Generate similar tracks

        Returns:
            Response message for the user
        """
        # Check if Music Assistant has players configured
        has_ma_players = await check_music_assistant_players(self.ha)
        if not has_ma_players:
            return (
                "Music playback is not available. Music Assistant has no players configured. "
                "Please add your speakers as players in Home Assistant → Music Assistant → Settings → Players."
            )

        if not self.admin:
            return "Room groups are not configured. Try specifying a specific room."

        try:
            # Resolve room group from admin API
            group_data = await self.admin.resolve_room_group(group_name)

            if not group_data:
                return f"I don't have a room group called {group_name}. Try specifying a specific room."

            # Get member rooms
            members = group_data.get("members", [])
            if not members:
                return f"The {group_name} group has no rooms configured."

            # Get entity IDs for all group members
            entities = []
            room_names = []
            for member in members:
                room_name = member.get("room_name") or member
                entity = await self._get_entity_for_room(room_name)
                if entity:
                    entities.append(entity)
                    room_names.append(room_name.replace("_", " "))

            if not entities:
                return f"No speakers found for the {group_name} group."

            # Get account for the group (treated as single allocation)
            group_key = f"group:{group_name}"
            account = self.account_pool.get_account_for_room(group_key)
            if not account:
                active = self.account_pool.get_active_rooms()
                return f"Both Spotify accounts are in use. Stop music first."

            # Handle genre conversion (random artist selection)
            if media_type == "genre":
                artist = get_genre_artist(media_id)
                if artist:
                    media_type = "artist"
                    media_id = artist
                    radio_mode = True

            # Create sync group with first speaker as leader
            primary = entities[0]
            others = entities[1:]

            if others:
                await self.ha.call_service(
                    domain="media_player",
                    service="join",
                    service_data={
                        "entity_id": primary,
                        "group_members": others
                    }
                )

            # Play to the synced group
            await self.ha.call_service(
                domain="music_assistant",
                service="play_media",
                service_data={
                    "entity_id": primary,
                    "media_id": media_id,
                    "media_type": media_type,
                    "radio_mode": radio_mode
                }
            )

            room_list = ", ".join(room_names)
            display_name = group_data.get("display_name", group_name)

            logger.info(
                "music_group_play_success",
                group=group_name,
                media_id=media_id,
                rooms=room_names
            )
            return f"Playing {media_id} in {display_name} ({room_list})."

        except Exception as e:
            logger.error(
                "music_group_play_failed",
                error=str(e),
                group=group_name
            )
            # Release account on failure
            self.account_pool.release_room(f"group:{group_name}")
            return f"Sorry, I couldn't play to {group_name}. {str(e)}"

    async def handle_everywhere_except(
        self,
        excluded_rooms: List[str],
        media_type: str,
        media_id: str,
        radio_mode: bool = True
    ) -> str:
        """
        Play to all configured rooms except specified ones.

        Args:
            excluded_rooms: Rooms to skip (normalized names)
            media_type: Type of media to play
            media_id: Media to play
            radio_mode: Generate similar tracks

        Returns:
            Response message for the user
        """
        # Check if Music Assistant has players configured
        has_ma_players = await check_music_assistant_players(self.ha)
        if not has_ma_players:
            return (
                "Music playback is not available. Music Assistant has no players configured. "
                "Please add your speakers as players in Home Assistant → Music Assistant → Settings → Players."
            )

        try:
            room_configs = await get_room_configs()

            # Get all room entities except excluded and aliases
            target_rooms = []
            entities = []

            for room_name, config in room_configs.items():
                # Skip aliases
                if room_name in ("bedroom", "basement", "home"):
                    continue
                # Skip excluded rooms
                if room_name in excluded_rooms:
                    self.playback_manager.set_excluded(room_name, "everywhere")
                    continue

                entity = config.get("primary_entity_id")
                if entity:
                    target_rooms.append(room_name)
                    entities.append(entity)

            if not entities:
                return "No rooms available after exclusions."

            # Get account for the group
            group_key = "group:everywhere"
            account = self.account_pool.get_account_for_room(group_key)
            if not account:
                return "Both Spotify accounts are in use. Stop music first."

            # Handle genre conversion (random artist selection)
            if media_type == "genre":
                artist = get_genre_artist(media_id)
                if artist:
                    media_type = "artist"
                    media_id = artist
                    radio_mode = True

            # Create sync group with first speaker as leader
            primary = entities[0]
            others = entities[1:]

            if others:
                await self.ha.call_service(
                    domain="media_player",
                    service="join",
                    service_data={
                        "entity_id": primary,
                        "group_members": others
                    }
                )

            # Play to the synced group
            await self.ha.call_service(
                domain="music_assistant",
                service="play_media",
                service_data={
                    "entity_id": primary,
                    "media_id": media_id,
                    "media_type": media_type,
                    "radio_mode": radio_mode
                }
            )

            # Update playback state
            for room in target_rooms:
                entity = await self._get_entity_for_room(room)
                if entity:
                    self.playback_manager.set_playing(room, entity, media_id)
                    self.playback_manager.add_to_group(room, "everywhere")

            excluded_display = ", ".join(r.replace("_", " ") for r in excluded_rooms)
            room_count = len(target_rooms)

            logger.info(
                "music_everywhere_except_success",
                media_id=media_id,
                excluded=excluded_rooms,
                playing_rooms=target_rooms
            )
            return f"Playing {media_id} in {room_count} rooms (excluding {excluded_display})."

        except Exception as e:
            logger.error(
                "music_everywhere_except_failed",
                error=str(e),
                excluded=excluded_rooms
            )
            self.account_pool.release_room("group:everywhere")
            return f"Sorry, I couldn't start playback. {str(e)}"

    async def handle_room_specific_pause(self, room: str) -> str:
        """
        Pause playback in a specific room while keeping others playing.

        Args:
            room: Room to pause

        Returns:
            Response message
        """
        target_room = self._normalize_room(room)
        entity_id = await self._get_entity_for_room(target_room)

        if not entity_id:
            return f"No music player found for {room}."

        state = self.playback_manager.get_room_state(target_room)
        if not self.playback_manager.is_playing(target_room):
            return f"Nothing is playing in {room}."

        try:
            # If part of a group, unjoin first
            if state.part_of_group:
                await self.ha.call_service(
                    domain="media_player",
                    service="unjoin",
                    service_data={"entity_id": entity_id}
                )

            # Pause the room
            await self.ha.call_service(
                domain="media_player",
                service="media_pause",
                service_data={"entity_id": entity_id}
            )

            # Get position for resume
            ha_state = await self.ha.get_state(entity_id)
            position_ms = int((ha_state.get("attributes", {}).get("media_position", 0)) * 1000)

            self.playback_manager.set_paused(target_room, position_ms)

            room_display = target_room.replace("_", " ")
            logger.info("room_specific_pause", room=target_room, position_ms=position_ms)
            return f"Paused in {room_display}."

        except Exception as e:
            logger.error("room_specific_pause_failed", room=target_room, error=str(e))
            return f"Sorry, I couldn't pause in {room}. {str(e)}"

    async def handle_room_specific_resume(self, room: str) -> str:
        """
        Resume playback in a specific room.

        Args:
            room: Room to resume

        Returns:
            Response message
        """
        target_room = self._normalize_room(room)
        entity_id = await self._get_entity_for_room(target_room)

        if not entity_id:
            return f"No music player found for {room}."

        if not self.playback_manager.is_paused(target_room):
            return f"Nothing is paused in {room}."

        try:
            state = self.playback_manager.get_room_state(target_room)

            # Resume playback
            await self.ha.call_service(
                domain="media_player",
                service="media_play",
                service_data={"entity_id": entity_id}
            )

            # Seek to position if we have it
            if state.position_ms > 0:
                await self.ha.call_service(
                    domain="media_player",
                    service="media_seek",
                    service_data={
                        "entity_id": entity_id,
                        "seek_position": state.position_ms / 1000
                    }
                )

            self.playback_manager.set_playing(target_room, entity_id, state.current_media or "")

            room_display = target_room.replace("_", " ")
            logger.info("room_specific_resume", room=target_room)
            return f"Resumed in {room_display}."

        except Exception as e:
            logger.error("room_specific_resume_failed", room=target_room, error=str(e))
            return f"Sorry, I couldn't resume in {room}. {str(e)}"


# Singleton instance
_music_handler: Optional[MusicHandler] = None


def get_music_handler(
    ha_client: HomeAssistantClient,
    admin_client: Optional[AdminConfigClient] = None
) -> MusicHandler:
    """
    Get or create music handler singleton.

    Args:
        ha_client: Home Assistant client
        admin_client: Admin config client (optional)

    Returns:
        MusicHandler instance
    """
    global _music_handler
    if _music_handler is None:
        _music_handler = MusicHandler(ha_client, admin_client)
    return _music_handler
