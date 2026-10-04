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
import json
import os
import re
import asyncio
import time
import structlog
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass

from shared.ha_client import HomeAssistantClient
from shared.admin_config import AdminConfigClient
from shared.admin_url import get_admin_url
from shared.config import get_config
from shared.service_key import note_admin_refusal, service_key_headers
# ATHENA-69: orchestrator.mode_permission is imported lazily inside
# TVHandler.__init__ (not at module scope) -- see the identical note in
# sequence_executor.py.

logger = structlog.get_logger()

# Fallback room -> Apple TV entity mapping, used only when the admin API's
# Room TV Config is unreachable. Configured via HA_TV_ENTITIES (DC14 item
# 1b, OSS-First) rather than hardcoded here; empty means no fallback --
# every handler below already treats an empty tv_configs dict as "no TV
# entity configured" (see e.g. handle_launch's early "No Apple TVs
# configured" return), so this degrades cleanly.
_fallback_room_to_tv_cache: Optional[Dict[str, Tuple[str, str]]] = None
_fallback_room_to_tv_warned = False


def _parse_ha_tv_entities(raw: str) -> Dict[str, Tuple[str, str]]:
    """room_name -> (media_player_entity_id, remote_entity_id).

    Accepts a JSON array of {"room", "media_player_entity_id",
    "remote_entity_id"} objects, or a comma-separated list of
    "room:media_player_entity_id[:remote_entity_id]" triples.
    """
    result: Dict[str, Tuple[str, str]] = {}
    raw = raw.strip()
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
            for item in parsed:
                room = str(item["room"]).lower()
                media_player = str(item["media_player_entity_id"])
                remote = str(item.get("remote_entity_id", ""))
                result[room] = (media_player, remote)
        except Exception as e:
            logger.error("ha_tv_entities_invalid_json", error=str(e))
            return {}
    else:
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            parts = token.split(":")
            if len(parts) < 2:
                logger.error("ha_tv_entities_invalid_entry", entry=token)
                continue
            room = parts[0].lower()
            media_player = parts[1]
            remote = parts[2] if len(parts) > 2 else ""
            result[room] = (media_player, remote)
    return result


def _get_fallback_room_to_tv() -> Dict[str, Tuple[str, str]]:
    global _fallback_room_to_tv_cache, _fallback_room_to_tv_warned
    if _fallback_room_to_tv_cache is not None:
        return _fallback_room_to_tv_cache

    raw = get_config().ha_tv_entities
    if not raw:
        if not _fallback_room_to_tv_warned:
            logger.info("ha_tv_entities_unset_no_fallback_tv_configured")
            _fallback_room_to_tv_warned = True
        _fallback_room_to_tv_cache = {}
        return _fallback_room_to_tv_cache

    _fallback_room_to_tv_cache = _parse_ha_tv_entities(raw)
    return _fallback_room_to_tv_cache

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

    admin_url = get_admin_url()

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{admin_url}/api/room-tv/internal", headers=service_key_headers())
            note_admin_refusal(response.status_code, "/api/room-tv/internal")
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
    } for name, entities in _get_fallback_room_to_tv().items()}


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

    admin_url = get_admin_url()

    try:
        params = {"guest_mode": "true"} if guest_mode else {}
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{admin_url}/api/room-tv/apps", params=params, headers=service_key_headers()
            )
            note_admin_refusal(response.status_code, "/api/room-tv/apps")
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

    admin_url = get_admin_url()

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{admin_url}/api/room-tv/features", headers=service_key_headers())
            note_admin_refusal(response.status_code, "/api/room-tv/features")
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
        from orchestrator.mode_permission import ensure_permission_enforcing

        self.ha = ensure_permission_enforcing(ha_client)
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

        from orchestrator.mode_permission import HAWritePermissionDenied, current_ha_scope

        # "Not a guest" isn't "the owner": a degraded house reports mode
        # "owner" with the degraded permission set. Only permissions that
        # positively say owner get entered into the first profile.
        scope = current_ha_scope()
        is_owner = scope is not None and scope.permissions.get("mode") == "owner"

        # Launch the app
        try:
            await self.ha.call_service(
                "media_player",
                "select_source",
                {"entity_id": entity_id, "source": app_name}
            )

            # Anyone else stops at the profile screen and picks by hand. For
            # a guest the press would also be a denied write: the guest
            # baseline has no `remote` domain.
            if is_owner and app_config and app_config.get("has_profile_screen"):
                if await get_feature_flag("auto_profile_select"):
                    await self._press_profile_select(app_name, room, remote_id, app_config)

            room_display = config.get("display_name", room.replace("_", " "))
            return {
                "success": True,
                "message": f"Opening {app_name} on {room_display} TV.",
                "room": room,
                "app": app_name
            }

        except HAWritePermissionDenied:
            # Not a failed launch. The guard has recorded and logged the
            # denial on the request's scope; route_tv_node answers from it.
            raise
        except Exception as e:
            logger.error("tv_launch_failed", app=app_name, room=room, error=str(e))
            return {
                "success": False,
                "message": f"Failed to launch {app_name}. Please try again.",
                "error": str(e)
            }

    async def _press_profile_select(
        self, app_name: str, room: str, remote_id: str, app_config: Dict[str, Any]
    ) -> None:
        """Press select on the app's profile screen, once it has had time to
        appear. Optional: the app is already open, so a press that fails is
        logged and the launch still succeeds. A press the permission guard
        refuses is not a failure of that kind: it is raised, through
        handle_launch, with the denial recorded on the request's scope."""
        from orchestrator.mode_permission import HAWritePermissionDenied

        await asyncio.sleep(app_config.get("profile_select_delay_ms", 1500) / 1000)
        try:
            await self.ha.call_service(
                "remote",
                "send_command",
                {"entity_id": remote_id, "command": "select"}
            )
        except HAWritePermissionDenied:
            raise
        except Exception as e:
            logger.error("tv_profile_select_failed", app=app_name, room=room, error_type=type(e).__name__)

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

        # Launch on all TVs. A write the permission guard refuses stops the
        # loop (it raises through handle_launch): the scope is latched shut,
        # so every later TV would only be refused as well.
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

    @staticmethod
    def _resolve_tv(tv_configs: Dict[str, Dict[str, Any]], room: Optional[str]):
        """(room, config, None) for the TV a room names -- the first
        configured TV when no room is given -- or (room, None, failure)."""
        if not room:
            if not tv_configs:
                return room, None, {
                    "success": False,
                    "message": "No Apple TVs configured.",
                    "error": "no_tv_configured"
                }
            room = list(tv_configs.keys())[0]
        config = tv_configs.get(room.lower())
        if not config:
            return room, None, {
                "success": False,
                "message": f"No Apple TV in {room.replace('_', ' ')}.",
                "error": "room_not_found"
            }
        return room, config, None

    _TV_STATE_PHRASES = {
        "on": "on", "playing": "playing", "paused": "paused", "idle": "on and idle",
        "standby": "off", "off": "off", "unavailable": "unavailable",
    }

    async def handle_status(self, room: Optional[str] = None, all_tvs: bool = False) -> Dict[str, Any]:
        """Answer a TV state question from the resolved TV's Home Assistant
        state (a read: get_state passes the permission guard untouched)."""
        tv_configs = await get_tv_configs()
        if all_tvs:
            targets = [(r, c) for r, c in tv_configs.items()]
            if not targets:
                return {"success": False, "message": "No Apple TVs configured.", "error": "no_tv_configured"}
        else:
            room, config, failure = self._resolve_tv(tv_configs, room)
            if failure:
                return failure
            targets = [(room, config)]

        lines = []
        for target_room, config in targets:
            display = config.get("display_name", target_room.replace("_", " "))
            try:
                state = await self.ha.get_state(config["media_player_entity_id"])
                value = (state or {}).get("state")
            except Exception as e:
                logger.error("tv_status_failed", room=target_room, error=str(e))
                value = None
            if not value:
                lines.append(f"I couldn't check the {display} TV right now.")
            else:
                lines.append(f"The {display} TV is {self._TV_STATE_PHRASES.get(value, value)}.")
        return {"success": True, "message": " ".join(lines), "room": room}

    async def handle_power(
        self,
        action: str,
        room: Optional[str] = None
    ) -> Dict[str, Any]:
        """Turn TV on or off."""

        tv_configs = await get_tv_configs()
        room, config, failure = self._resolve_tv(tv_configs, room)
        if failure:
            return failure

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
