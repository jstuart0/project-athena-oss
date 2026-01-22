"""
Follow-Me Audio Service for Project Athena.

Automatically transfers music playback to where the user is based on
motion sensor events from Home Assistant.

Features:
- Motion-triggered room detection
- Debounced transfers (prevents rapid switching)
- Grace period when leaving rooms
- Multi-person awareness (optional)
- Integration with music_handler for playback control
"""

import asyncio
import time
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import structlog

logger = structlog.get_logger()


class FollowMeMode(Enum):
    """Follow-me operation modes."""
    OFF = "off"  # Feature disabled
    SINGLE_USER = "single"  # Follow single user (primary)
    PARTY = "party"  # Keep playing in all rooms with motion


@dataclass
class FollowMeConfig:
    """Configuration for follow-me audio behavior."""
    enabled: bool = True
    mode: FollowMeMode = FollowMeMode.SINGLE_USER
    debounce_seconds: float = 5.0  # Min time between transfers
    grace_period_seconds: float = 30.0  # Keep playing after leaving
    min_motion_duration_seconds: float = 2.0  # Minimum motion to trigger
    excluded_rooms: List[str] = field(default_factory=list)  # Rooms to ignore
    quiet_hours_start: Optional[int] = 23  # Hour (24h format) to stop following
    quiet_hours_end: Optional[int] = 7  # Hour to resume following


@dataclass
class RoomPresence:
    """Tracks presence state for a room."""
    room_name: str
    motion_entity_id: str
    last_motion_at: Optional[float] = None
    motion_started_at: Optional[float] = None
    is_occupied: bool = False
    occupancy_confidence: float = 0.0  # 0-1, higher = more confident


class FollowMeAudioService:
    """
    Service that manages follow-me audio behavior.

    Listens to motion sensor events and triggers music transfers
    when the user moves between rooms.
    """

    def __init__(
        self,
        ha_client: Any,  # HomeAssistantClient
        music_handler: Any,  # MusicHandler
        config: Optional[FollowMeConfig] = None
    ):
        """
        Initialize the follow-me service.

        Args:
            ha_client: Home Assistant client for motion events
            music_handler: Music handler for playback control
            config: Follow-me configuration
        """
        self.ha = ha_client
        self.music = music_handler
        self.config = config or FollowMeConfig()

        # Room presence tracking
        self._room_presence: Dict[str, RoomPresence] = {}

        # State
        self._current_room: Optional[str] = None
        self._last_transfer_at: float = 0
        self._active_rooms: Set[str] = set()

        # Event tracking
        self._motion_tasks: Dict[str, asyncio.Task] = {}

        # Callbacks
        self.on_room_change: Optional[callable] = None
        self.on_transfer: Optional[callable] = None

    async def initialize(self, room_motion_mapping: Dict[str, str]):
        """
        Initialize with room-to-motion-entity mapping.

        Args:
            room_motion_mapping: Dict mapping room_name -> motion sensor entity_id
                Example: {"office": "binary_sensor.office_motion"}
        """
        for room_name, entity_id in room_motion_mapping.items():
            if room_name not in self.config.excluded_rooms:
                self._room_presence[room_name] = RoomPresence(
                    room_name=room_name,
                    motion_entity_id=entity_id
                )

        logger.info(
            "follow_me_initialized",
            room_count=len(self._room_presence),
            rooms=list(self._room_presence.keys())
        )

    async def handle_motion_event(
        self,
        room_name: str,
        motion_detected: bool,
        timestamp: Optional[float] = None
    ):
        """
        Handle motion event from Home Assistant.

        Args:
            room_name: Room where motion was detected
            motion_detected: True if motion started, False if cleared
            timestamp: Event timestamp (defaults to now)
        """
        if not self.config.enabled:
            return

        if room_name not in self._room_presence:
            logger.debug("motion_event_unknown_room", room=room_name)
            return

        if room_name in self.config.excluded_rooms:
            return

        # Check quiet hours
        if self._is_quiet_hours():
            return

        now = timestamp or time.time()
        presence = self._room_presence[room_name]

        if motion_detected:
            await self._handle_motion_start(presence, now)
        else:
            await self._handle_motion_end(presence, now)

    async def _handle_motion_start(self, presence: RoomPresence, now: float):
        """Handle motion detected in a room."""
        # Cancel any pending leave timer
        if presence.room_name in self._motion_tasks:
            self._motion_tasks[presence.room_name].cancel()

        if not presence.is_occupied:
            presence.motion_started_at = now

        presence.last_motion_at = now
        presence.is_occupied = True
        presence.occupancy_confidence = 1.0

        self._active_rooms.add(presence.room_name)

        logger.debug(
            "motion_detected",
            room=presence.room_name,
            current_room=self._current_room
        )

        # Check if we should transfer
        await self._evaluate_transfer(presence.room_name, now)

    async def _handle_motion_end(self, presence: RoomPresence, now: float):
        """Handle motion cleared in a room."""
        # Start grace period timer
        task = asyncio.create_task(
            self._grace_period_expired(presence.room_name)
        )
        self._motion_tasks[presence.room_name] = task

        logger.debug(
            "motion_cleared",
            room=presence.room_name,
            grace_period=self.config.grace_period_seconds
        )

    async def _grace_period_expired(self, room_name: str):
        """Called when grace period expires after motion ends."""
        try:
            await asyncio.sleep(self.config.grace_period_seconds)

            presence = self._room_presence.get(room_name)
            if presence:
                presence.is_occupied = False
                presence.occupancy_confidence = 0.0
                self._active_rooms.discard(room_name)

                logger.debug("grace_period_expired", room=room_name)

                # If this was the current room, find a new one
                if self._current_room == room_name:
                    await self._find_new_current_room()

        except asyncio.CancelledError:
            # Motion resumed before grace period ended
            pass

    async def _evaluate_transfer(self, to_room: str, now: float):
        """Evaluate whether to transfer playback to a new room."""
        # Check debounce
        time_since_last = now - self._last_transfer_at
        if time_since_last < self.config.debounce_seconds:
            logger.debug(
                "transfer_debounced",
                seconds_remaining=self.config.debounce_seconds - time_since_last
            )
            return

        # Check if music is currently playing - query HA directly
        # This catches music started from ANY source (HA, MA app, voice, etc.)
        playing_rooms = await self.music.get_playing_rooms_from_ha()
        if not playing_rooms:
            # Also check internal state as fallback
            internal_playing = self.music.playback_manager.get_playing_rooms()
            if not internal_playing:
                logger.debug("no_music_playing_anywhere")
                return
            # Use internal state to build playing_rooms
            playing_rooms = {r: "" for r in internal_playing}

        # Set current room if not already set
        if self._current_room is None and playing_rooms:
            self._current_room = list(playing_rooms.keys())[0]
            logger.info("follow_me_current_room_detected", room=self._current_room)

        # Already in this room?
        if self._current_room == to_room:
            return

        # Check minimum motion duration
        presence = self._room_presence[to_room]
        if presence.motion_started_at:
            duration = now - presence.motion_started_at
            if duration < self.config.min_motion_duration_seconds:
                # Schedule re-check after min duration
                asyncio.create_task(
                    self._delayed_transfer_check(to_room, self.config.min_motion_duration_seconds - duration)
                )
                return

        # Mode-specific behavior
        if self.config.mode == FollowMeMode.SINGLE_USER:
            await self._transfer_to_room(to_room, now)
        elif self.config.mode == FollowMeMode.PARTY:
            await self._expand_to_room(to_room, now)

    async def _delayed_transfer_check(self, room_name: str, delay: float):
        """Re-check transfer after delay."""
        await asyncio.sleep(delay)
        presence = self._room_presence.get(room_name)
        if presence and presence.is_occupied:
            await self._evaluate_transfer(room_name, time.time())

    async def _transfer_to_room(self, to_room: str, now: float):
        """Transfer playback from current room to new room."""
        from_room = self._current_room

        try:
            # Use music handler's transfer function
            result = await self.music.handle_transfer(
                target_room=to_room,
                source_room=from_room
            )

            self._current_room = to_room
            self._last_transfer_at = now

            logger.info(
                "follow_me_transfer",
                from_room=from_room,
                to_room=to_room,
                result=result
            )

            if self.on_transfer:
                await self.on_transfer(from_room, to_room)

            if self.on_room_change:
                await self.on_room_change(to_room)

        except Exception as e:
            logger.error("follow_me_transfer_failed", error=str(e))

    async def _expand_to_room(self, room_name: str, now: float):
        """Party mode: add room to currently playing group."""
        try:
            entity = await self.music._get_entity_for_room(room_name)
            if not entity:
                return

            # Get current playing room to find group - query HA directly
            playing_rooms_dict = await self.music.get_playing_rooms_from_ha()
            if not playing_rooms_dict:
                # Fallback to internal state
                playing_rooms_list = self.music.playback_manager.get_playing_rooms()
                if not playing_rooms_list:
                    return
                playing_rooms_dict = {r: "" for r in playing_rooms_list}

            # Join this room to the group
            primary_room = list(playing_rooms_dict.keys())[0]
            primary_entity = await self.music._get_entity_for_room(primary_room)

            if primary_entity:
                await self.music.ha.call_service(
                    domain="media_player",
                    service="join",
                    service_data={
                        "entity_id": primary_entity,
                        "group_members": [entity]
                    }
                )

                state = self.music.playback_manager.get_room_state(primary_room)
                self.music.playback_manager.set_playing(room_name, entity, state.current_media or "")

                logger.info("follow_me_expanded", added_room=room_name)

        except Exception as e:
            logger.error("follow_me_expand_failed", error=str(e))

    async def _find_new_current_room(self):
        """Find a new current room when the current one becomes unoccupied."""
        # Find the room with most recent motion
        best_room = None
        best_time = 0

        for room_name, presence in self._room_presence.items():
            if presence.is_occupied and presence.last_motion_at:
                if presence.last_motion_at > best_time:
                    best_time = presence.last_motion_at
                    best_room = room_name

        if best_room and best_room != self._current_room:
            await self._transfer_to_room(best_room, time.time())
        elif not best_room:
            # No occupied rooms - music continues in last room
            logger.debug("no_occupied_rooms", keeping=self._current_room)

    def _is_quiet_hours(self) -> bool:
        """Check if current time is during quiet hours."""
        if self.config.quiet_hours_start is None or self.config.quiet_hours_end is None:
            return False

        current_hour = datetime.now().hour
        start = self.config.quiet_hours_start
        end = self.config.quiet_hours_end

        if start < end:
            # Simple case: e.g., 23:00 to 07:00 (crosses midnight)
            return current_hour >= start or current_hour < end
        else:
            # e.g., 08:00 to 20:00 (same day)
            return start <= current_hour < end

    def set_mode(self, mode: FollowMeMode):
        """Change follow-me mode."""
        self.config.mode = mode
        logger.info("follow_me_mode_changed", mode=mode.value)

    def set_enabled(self, enabled: bool):
        """Enable or disable follow-me."""
        self.config.enabled = enabled
        logger.info("follow_me_enabled_changed", enabled=enabled)

    def get_status(self) -> Dict[str, Any]:
        """Get current follow-me status."""
        return {
            "enabled": self.config.enabled,
            "mode": self.config.mode.value,
            "current_room": self._current_room,
            "active_rooms": list(self._active_rooms),
            "room_presence": {
                name: {
                    "occupied": p.is_occupied,
                    "confidence": p.occupancy_confidence,
                    "last_motion": p.last_motion_at
                }
                for name, p in self._room_presence.items()
            },
            "quiet_hours_active": self._is_quiet_hours()
        }


# Singleton instance
_follow_me_service: Optional[FollowMeAudioService] = None


def get_follow_me_service() -> Optional[FollowMeAudioService]:
    """Get the follow-me service instance."""
    return _follow_me_service


def get_most_recent_room(default: str = "office") -> str:
    """
    Get the room with most recent motion activity.

    This is useful for determining where to play music when the request
    comes from an interface without room context (like the web UI).

    Args:
        default: Default room if no motion data available

    Returns:
        Room name with most recent motion, or default
    """
    if not _follow_me_service:
        return default

    # First check if there's a current room being tracked
    if _follow_me_service._current_room:
        return _follow_me_service._current_room

    # Otherwise find the room with most recent motion
    best_room = None
    best_time = 0

    for room_name, presence in _follow_me_service._room_presence.items():
        if presence.last_motion_at and presence.last_motion_at > best_time:
            best_time = presence.last_motion_at
            best_room = room_name

    return best_room or default


async def initialize_follow_me(
    ha_client: Any,
    music_handler: Any,
    room_motion_mapping: Dict[str, str],
    config: Optional[FollowMeConfig] = None
) -> FollowMeAudioService:
    """
    Initialize and start the follow-me audio service.

    Args:
        ha_client: Home Assistant client
        music_handler: Music handler
        room_motion_mapping: Room name to motion sensor entity mapping
        config: Optional configuration

    Returns:
        Initialized FollowMeAudioService
    """
    global _follow_me_service

    _follow_me_service = FollowMeAudioService(ha_client, music_handler, config)
    await _follow_me_service.initialize(room_motion_mapping)

    return _follow_me_service
