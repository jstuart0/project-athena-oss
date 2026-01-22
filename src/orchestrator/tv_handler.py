"""
Apple TV control handler for Athena orchestrator.

Handles TV control intents via Home Assistant's Apple TV integration.
Supports:
- App launching (Netflix, YouTube, Disney+, etc.)
- Power control (on/off)
- Remote navigation (up, down, left, right, select, menu, home)
- Playback control (play, pause)
- YouTube deep links
- Multi-TV control ("open Netflix everywhere")
- Guest mode app filtering
"""
import os
import re
import asyncio
import time
import structlog
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

from shared.ha_client import HomeAssistantClient
from shared.admin_config import AdminConfigClient

logger = structlog.get_logger()

# Fallback room to Apple TV entity mapping
# Used when admin API is unavailable
FALLBACK_ROOM_TO_TV = {
    "master_bedroom": ("media_player.master_bedroom_tv", "remote.master_bedroom_tv"),
    "bedroom": ("media_player.master_bedroom_tv", "remote.master_bedroom_tv"),  # Alias
    "living_room": ("media_player.living_room_tv", "remote.living_room_tv"),
    "office": ("media_player.office_tv", "remote.office_tv"),
}

# Cache for TV configs fetched from admin API
_tv_config_cache: Dict[str, Any] = {}
_tv_config_cache_time: float = 0
_app_config_cache: List[Dict[str, Any]] = []
_app_config_cache_time: float = 0
_feature_flag_cache: Dict[str, bool] = {}
_feature_flag_cache_time: float = 0
TV_CONFIG_CACHE_TTL = 300  # 5 minutes


async def get_tv_configs() -> Dict[str, Dict[str, Any]]:
    """
    Fetch room TV configurations from Admin API.
    Returns dict mapping room_name -> config dict.
    Falls back to hardcoded values if API unavailable.
    """
    import httpx
    global _tv_config_cache, _tv_config_cache_time

    # Check cache
    if _tv_config_cache and (time.time() - _tv_config_cache_time) < TV_CONFIG_CACHE_TTL:
        return _tv_config_cache

    admin_url = os.getenv("ADMIN_API_URL", "http://localhost:8080")

    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            response = await client.get(f"{admin_url}/api/room-tv/internal")
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

                _tv_config_cache = result
                _tv_config_cache_time = time.time()
                logger.info("tv_configs_loaded", count=len(configs), source="admin_api")
                return result
    except Exception as e:
        logger.warning("tv_configs_fetch_failed", error=str(e), fallback="hardcoded")

    # Fallback to hardcoded values
    return {name: {
        "room_name": name,
        "media_player_entity_id": entities[0],
        "remote_entity_id": entities[1]
    } for name, entities in FALLBACK_ROOM_TO_TV.items()}


async def get_app_configs(guest_mode: bool = False) -> List[Dict[str, Any]]:
    """
    Fetch TV app configurations from Admin API.
    Returns list of app configs, optionally filtered for guest mode.
    """
    import httpx
    global _app_config_cache, _app_config_cache_time

    # Check cache (only for full list, guest mode bypasses cache)
    if not guest_mode and _app_config_cache and (time.time() - _app_config_cache_time) < TV_CONFIG_CACHE_TTL:
        return _app_config_cache

    admin_url = os.getenv("ADMIN_API_URL", "http://localhost:8080")

    try:
        params = {"guest_mode": "true"} if guest_mode else {}
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            response = await client.get(f"{admin_url}/api/room-tv/apps", params=params)
            if response.status_code == 200:
                apps = response.json()
                if not guest_mode:
                    _app_config_cache = apps
                    _app_config_cache_time = time.time()
                logger.info("app_configs_loaded", count=len(apps), guest_mode=guest_mode)
                return apps
    except Exception as e:
        logger.warning("app_configs_fetch_failed", error=str(e))

    # Return empty list on failure - apps won't be filtered
    return []


async def get_feature_flag(feature_name: str) -> bool:
    """
    Check if a TV feature flag is enabled.
    """
    import httpx
    global _feature_flag_cache, _feature_flag_cache_time

    # Check cache
    if _feature_flag_cache and (time.time() - _feature_flag_cache_time) < TV_CONFIG_CACHE_TTL:
        return _feature_flag_cache.get(feature_name, False)

    admin_url = os.getenv("ADMIN_API_URL", "http://localhost:8080")

    try:
        async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
            response = await client.get(f"{admin_url}/api/room-tv/features")
            if response.status_code == 200:
                flags = response.json()
                _feature_flag_cache = {f["feature_name"]: f["enabled"] for f in flags}
                _feature_flag_cache_time = time.time()
                return _feature_flag_cache.get(feature_name, False)
    except Exception as e:
        logger.warning("feature_flags_fetch_failed", error=str(e))

    return False


@dataclass
class TVIntent:
    """Parsed TV control intent."""
    action: str  # launch, power, navigate, playback
    app_name: Optional[str] = None
    room: Optional[str] = None
    command: Optional[str] = None  # up, down, left, right, select, menu, home, play, pause
    power_action: Optional[str] = None  # on, off
    youtube_video_id: Optional[str] = None
    all_tvs: bool = False  # "everywhere", "all TVs"


class AppleTVHandler:
    """
    Handles Apple TV control commands via Home Assistant.

    Provides methods for:
    - Launching apps with profile screen handling
    - Power control
    - Remote navigation and playback
    - YouTube deep links
    - Multi-TV control
    """

    def __init__(self, ha_client: HomeAssistantClient, admin_client: AdminConfigClient):
        self.ha = ha_client
        self.admin = admin_client

    async def parse_tv_intent(self, query: str, room: Optional[str] = None, mode: str = "owner") -> TVIntent:
        """
        Parse a natural language query into a TV control intent.

        Examples:
        - "open Netflix" -> launch, app_name=Netflix
        - "turn on the bedroom TV" -> power, power_action=on, room=bedroom
        - "open Netflix everywhere" -> launch, app_name=Netflix, all_tvs=True
        - "go up" -> navigate, command=up
        - "play that video" -> playback, command=play
        """
        query_lower = query.lower()

        intent = TVIntent(action="unknown", room=room)

        # Check for multi-TV commands
        if any(phrase in query_lower for phrase in ["everywhere", "all tvs", "all the tvs", "every tv"]):
            intent.all_tvs = True

        # Parse room from query
        room_match = re.search(r"(?:on|in)\s+(?:the\s+)?(\w+(?:\s+\w+)?)\s*(?:tv|television)", query_lower)
        if room_match:
            room_text = room_match.group(1).strip()
            # Normalize room names
            intent.room = room_text.replace(" ", "_").lower()

        # Power control
        if any(phrase in query_lower for phrase in ["turn on", "power on", "switch on"]):
            intent.action = "power"
            intent.power_action = "on"
            return intent
        elif any(phrase in query_lower for phrase in ["turn off", "power off", "switch off"]):
            intent.action = "power"
            intent.power_action = "off"
            return intent

        # App launching
        app_patterns = [
            r"(?:open|launch|start|play)\s+(.+?)(?:\s+on\s+|\s+everywhere|\s*$)",
            r"(?:put on|go to)\s+(.+?)(?:\s+on\s+|\s*$)",
        ]
        for pattern in app_patterns:
            match = re.search(pattern, query_lower)
            if match:
                app_name = match.group(1).strip()
                # Clean up common phrases
                app_name = re.sub(r"(?:the\s+)?(?:tv|television|app)$", "", app_name).strip()
                if app_name:
                    intent.action = "launch"
                    intent.app_name = self._normalize_app_name(app_name)
                    return intent

        # Navigation commands
        nav_commands = {
            "up": ["go up", "move up", "scroll up", "up"],
            "down": ["go down", "move down", "scroll down", "down"],
            "left": ["go left", "move left", "left"],
            "right": ["go right", "move right", "right"],
            "select": ["select", "ok", "enter", "choose", "click"],
            "menu": ["menu", "back", "go back"],
            "home": ["home", "home screen", "go home"],
        }
        for command, phrases in nav_commands.items():
            if any(phrase in query_lower for phrase in phrases):
                intent.action = "navigate"
                intent.command = command
                return intent

        # Playback commands
        if any(phrase in query_lower for phrase in ["play", "resume", "unpause"]):
            intent.action = "playback"
            intent.command = "play"
            return intent
        elif any(phrase in query_lower for phrase in ["pause", "stop"]):
            intent.action = "playback"
            intent.command = "pause"
            return intent

        return intent

    def _normalize_app_name(self, name: str) -> str:
        """Normalize app name to match source_list."""
        # Common aliases
        aliases = {
            "hbo": "HBO Max",
            "max": "HBO Max",
            "disney": "Disney+",
            "disneyplus": "Disney+",
            "amazon": "Prime Video",
            "amazon prime": "Prime Video",
            "prime": "Prime Video",
            "youtube": "YouTube",
            "yt": "YouTube",
            "netflix": "Netflix",
            "hulu": "Hulu",
            "paramount": "Paramount+",
            "peacock": "Peacock",
            "apple tv": "TV",
            "apple tv plus": "TV",
            "spotify": "Spotify",
            "music": "Music",
            "apple music": "Music",
        }
        return aliases.get(name.lower(), name.title())

    async def handle_launch(
        self,
        app_name: str,
        room: Optional[str] = None,
        guest_mode: bool = False
    ) -> Dict[str, Any]:
        """
        Launch an app on an Apple TV.

        Returns dict with status and response message.
        """
        # Get TV config for room
        tv_configs = await get_tv_configs()

        if not room:
            # Use first available TV
            if tv_configs:
                room = list(tv_configs.keys())[0]
            else:
                return {
                    "success": False,
                    "message": "No Apple TVs configured. Please set up Room TV Config in the admin panel.",
                    "error": "no_tv_configured"
                }

        config = tv_configs.get(room.lower())
        if not config:
            available_rooms = [c.get("display_name", n) for n, c in tv_configs.items() if n not in ("bedroom",)]
            return {
                "success": False,
                "message": f"No Apple TV in {room.replace('_', ' ')}. Available: {', '.join(available_rooms)}",
                "error": "room_not_found"
            }

        # Check app access in guest mode
        if guest_mode:
            apps = await get_app_configs(guest_mode=True)
            allowed_apps = [a["app_name"].lower() for a in apps]
            if app_name.lower() not in allowed_apps:
                return {
                    "success": False,
                    "message": f"Sorry, {app_name} is not available in guest mode.",
                    "error": "app_not_allowed"
                }

        # Get app config for profile screen handling
        apps = await get_app_configs()
        app_config = next((a for a in apps if a["app_name"].lower() == app_name.lower()), None)

        entity_id = config["media_player_entity_id"]
        remote_id = config["remote_entity_id"]

        # Launch the app
        try:
            await self.ha.call_service(
                "media_player",
                "select_source",
                {"entity_id": entity_id, "source": app_name}
            )

            # Handle profile screen if configured
            if app_config and app_config.get("has_profile_screen"):
                auto_select = await get_feature_flag("auto_profile_select")
                if auto_select:
                    delay_ms = app_config.get("profile_select_delay_ms", 1500)
                    await asyncio.sleep(delay_ms / 1000)
                    await self.ha.call_service(
                        "remote",
                        "send_command",
                        {"entity_id": remote_id, "command": "select"}
                    )

            room_display = config.get("display_name", room.replace("_", " "))
            return {
                "success": True,
                "message": f"Opening {app_name} on {room_display} TV.",
                "room": room,
                "app": app_name
            }

        except Exception as e:
            logger.error("tv_launch_failed", app=app_name, room=room, error=str(e))
            return {
                "success": False,
                "message": f"Failed to launch {app_name}. Please try again.",
                "error": str(e)
            }

    async def handle_launch_everywhere(
        self,
        app_name: str,
        guest_mode: bool = False
    ) -> Dict[str, Any]:
        """Launch an app on all Apple TVs."""

        # Check if multi-TV is enabled
        multi_enabled = await get_feature_flag("multi_tv_commands")
        if not multi_enabled:
            return {
                "success": False,
                "message": "Multi-TV commands are disabled. Enable them in the admin panel.",
                "error": "feature_disabled"
            }

        tv_configs = await get_tv_configs()
        if not tv_configs:
            return {
                "success": False,
                "message": "No Apple TVs configured.",
                "error": "no_tv_configured"
            }

        # Filter out aliases (bedroom, etc.)
        rooms = [name for name in tv_configs.keys() if name not in ("bedroom", "basement")]

        # Launch on all TVs
        results = []
        for room in rooms:
            result = await self.handle_launch(app_name, room, guest_mode)
            results.append(result)

        success_count = sum(1 for r in results if r.get("success"))
        return {
            "success": success_count > 0,
            "message": f"Opening {app_name} on {success_count} of {len(rooms)} TVs.",
            "results": results
        }

    async def handle_power(
        self,
        action: str,
        room: Optional[str] = None
    ) -> Dict[str, Any]:
        """Turn TV on or off."""

        tv_configs = await get_tv_configs()

        if not room:
            if tv_configs:
                room = list(tv_configs.keys())[0]
            else:
                return {
                    "success": False,
                    "message": "No Apple TVs configured.",
                    "error": "no_tv_configured"
                }

        config = tv_configs.get(room.lower())
        if not config:
            return {
                "success": False,
                "message": f"No Apple TV in {room.replace('_', ' ')}.",
                "error": "room_not_found"
            }

        entity_id = config["media_player_entity_id"]
        service = "turn_on" if action == "on" else "turn_off"

        try:
            await self.ha.call_service(
                "media_player",
                service,
                {"entity_id": entity_id}
            )

            room_display = config.get("display_name", room.replace("_", " "))
            return {
                "success": True,
                "message": f"Turned {action} {room_display} TV.",
                "room": room,
                "action": action
            }

        except Exception as e:
            logger.error("tv_power_failed", action=action, room=room, error=str(e))
            return {
                "success": False,
                "message": f"Failed to turn {action} the TV.",
                "error": str(e)
            }

    async def handle_navigate(
        self,
        command: str,
        room: Optional[str] = None,
        repeat: int = 1
    ) -> Dict[str, Any]:
        """Send navigation command to TV."""

        tv_configs = await get_tv_configs()

        if not room:
            if tv_configs:
                room = list(tv_configs.keys())[0]
            else:
                return {
                    "success": False,
                    "message": "No Apple TVs configured.",
                    "error": "no_tv_configured"
                }

        config = tv_configs.get(room.lower())
        if not config:
            return {
                "success": False,
                "message": f"No Apple TV in {room.replace('_', ' ')}.",
                "error": "room_not_found"
            }

        remote_id = config["remote_entity_id"]

        try:
            for i in range(repeat):
                await self.ha.call_service(
                    "remote",
                    "send_command",
                    {"entity_id": remote_id, "command": command}
                )
                if i < repeat - 1:
                    await asyncio.sleep(0.3)

            return {
                "success": True,
                "message": "Done.",
                "command": command,
                "repeat": repeat
            }

        except Exception as e:
            logger.error("tv_navigate_failed", command=command, room=room, error=str(e))
            return {
                "success": False,
                "message": "Navigation command failed.",
                "error": str(e)
            }

    async def handle_playback(
        self,
        command: str,
        room: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send playback command (play/pause)."""

        tv_configs = await get_tv_configs()

        if not room:
            if tv_configs:
                room = list(tv_configs.keys())[0]
            else:
                return {
                    "success": False,
                    "message": "No Apple TVs configured.",
                    "error": "no_tv_configured"
                }

        config = tv_configs.get(room.lower())
        if not config:
            return {
                "success": False,
                "message": f"No Apple TV in {room.replace('_', ' ')}.",
                "error": "room_not_found"
            }

        remote_id = config["remote_entity_id"]

        try:
            await self.ha.call_service(
                "remote",
                "send_command",
                {"entity_id": remote_id, "command": command}
            )

            action_word = "Playing" if command == "play" else "Paused"
            return {
                "success": True,
                "message": f"{action_word}.",
                "command": command
            }

        except Exception as e:
            logger.error("tv_playback_failed", command=command, room=room, error=str(e))
            return {
                "success": False,
                "message": "Playback command failed.",
                "error": str(e)
            }

    async def handle_youtube_video(
        self,
        video_id: str,
        room: Optional[str] = None
    ) -> Dict[str, Any]:
        """Play a specific YouTube video using deep link."""

        tv_configs = await get_tv_configs()

        if not room:
            if tv_configs:
                room = list(tv_configs.keys())[0]
            else:
                return {
                    "success": False,
                    "message": "No Apple TVs configured.",
                    "error": "no_tv_configured"
                }

        config = tv_configs.get(room.lower())
        if not config:
            return {
                "success": False,
                "message": f"No Apple TV in {room.replace('_', ' ')}.",
                "error": "room_not_found"
            }

        entity_id = config["media_player_entity_id"]

        try:
            await self.ha.call_service(
                "media_player",
                "play_media",
                {
                    "entity_id": entity_id,
                    "media_content_id": f"youtube://www.youtube.com/watch?v={video_id}",
                    "media_content_type": "url"
                }
            )

            return {
                "success": True,
                "message": "Playing YouTube video.",
                "video_id": video_id
            }

        except Exception as e:
            logger.error("tv_youtube_failed", video_id=video_id, room=room, error=str(e))
            return {
                "success": False,
                "message": "Failed to play YouTube video.",
                "error": str(e)
            }


# Global handler instance
_tv_handler: Optional[AppleTVHandler] = None


def get_tv_handler(
    ha_client: HomeAssistantClient,
    admin_client: AdminConfigClient
) -> AppleTVHandler:
    """Get or create the global TV handler instance."""
    global _tv_handler

    if _tv_handler is None:
        _tv_handler = AppleTVHandler(ha_client, admin_client)
        logger.info("tv_handler_initialized")

    return _tv_handler
