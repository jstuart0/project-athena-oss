"""The one owner of the room vocabulary.

``RoomDirectoryProvider`` builds a ``room_context.RoomDirectory`` from the
Home Assistant area registry snapshot, the household's room lights and
speaker rooms (``shared.household``: admin, else ``HA_LIGHT_GROUPS`` /
``HA_SATELLITE_ROOM_MAP``) and the admin room groups, and hands the same
directory to every reader (the classify node, the controller, the intent
classifier) through ``current()``.

``current()`` is synchronous and never does I/O. The directory is rebuilt only
when one of its inputs changes: a new registry snapshot, a new room-lights
or speaker-rooms value, or a changed room-group list. The room groups are fetched in the
background (``GROUP_TTL_SECONDS``).

Logs carry counts only; a room or area name never appears in a log line.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import structlog

from orchestrator.room_context import RoomDirectory
from shared import household
from shared.ha_registry import room_key
from shared.household import RoomLightsValue, SpeakerRoomsValue

logger = structlog.get_logger(__name__)

GROUP_TTL_SECONDS = 300
GROUP_FETCH_TIMEOUT_SECONDS = 5.0


def _speaker_room_keys(speakers: SpeakerRoomsValue) -> Tuple[str, ...]:
    rooms = tuple(speakers.map.entity_rooms.values()) + tuple(speakers.map.device_rooms.values())
    return tuple(sorted({k for k in (room_key(r) for r in rooms) if k}))


def _unmatched(snapshot: Any, keys: Iterable[str]) -> Optional[int]:
    """How many of these room keys no area of the snapshot has (by key or alias)."""
    if snapshot is None:
        return None
    return sum(1 for k in set(keys) if snapshot.area_by_key(k) is None and not snapshot.areas_by_alias(k))


class RoomDirectoryProvider:
    def __init__(
        self,
        registry: Any,
        admin_client: Any,
        *,
        light_group_names: Optional[Callable[[], Iterable[str]]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        room_lights: Callable[[], RoomLightsValue] = household.room_lights,
        speaker_rooms: Callable[[], SpeakerRoomsValue] = household.speaker_rooms,
    ):
        self._registry = registry
        self._admin = admin_client
        self._light_group_names = light_group_names
        self._monotonic = monotonic
        self._room_lights = room_lights
        self._speaker_rooms = speaker_rooms
        self._unmatched_logged: Optional[int] = None
        self._snapshot: Any = None
        self._groups: List[dict] = []
        self._groups_version = 0
        self._groups_fetched_at: Optional[float] = None
        self._task: Optional["asyncio.Task[None]"] = None
        self._memo_key: Optional[tuple] = None
        self._directory: RoomDirectory = RoomDirectory.empty()

    # -- reads ------------------------------------------------------------

    def room_lights(self) -> RoomLightsValue:
        """The effective room-to-lights map (admin, else the environment)."""
        return self._room_lights()

    def speaker_rooms(self) -> SpeakerRoomsValue:
        """The effective voice-device-to-room map (admin, else the environment)."""
        return self._speaker_rooms()

    def current(self) -> RoomDirectory:
        """The directory for the inputs as they are now. Starts a background
        room-group refresh when that list is older than the TTL."""
        self._maybe_refresh_groups()
        snapshot = self._registry.read() if self._registry is not None else None
        self._snapshot = snapshot
        lights = self._room_lights()
        speakers = self._speaker_rooms()
        group_names: Tuple[str, ...] = ()
        if snapshot is None and self._light_group_names is not None:
            group_names = tuple(self._light_group_names())
        key = (
            snapshot.fetched_at if snapshot is not None else None,
            lights.version,
            speakers.version,
            self._groups_version,
            group_names,
        )
        if key != self._memo_key:
            self._directory = self._build(snapshot, lights, speakers, group_names)
            self._memo_key = key
        return self._directory

    def sources(self) -> Dict[str, Dict[str, Any]]:
        """Where each map comes from and how big it is. Counts and words only;
        ``unmatched`` is the number of configured rooms that are no Home
        Assistant area (``None`` before a registry snapshot exists)."""
        self.current()
        snapshot = self._snapshot
        lights = self._room_lights()
        speakers = self._speaker_rooms()
        light_keys = tuple(sorted(k for k in lights.mapping if k))
        speaker_keys = _speaker_room_keys(speakers)
        return {
            "room_lights": {
                "source": lights.source,
                "rooms": len(light_keys),
                "lights": sum(len(ids) for ids in lights.mapping.values()),
                "unmatched": _unmatched(snapshot, light_keys),
            },
            "speaker_rooms": {
                "source": speakers.source,
                "device_keys": len(speakers.map.device_rooms),
                "entity_keys": len(speakers.map.entity_rooms),
                "unmatched": _unmatched(snapshot, speaker_keys),
            },
        }

    def _build(
        self, snapshot: Any, lights: RoomLightsValue, speakers: SpeakerRoomsValue, group_names: Tuple[str, ...],
    ) -> RoomDirectory:
        light_keys = tuple(sorted(k for k in lights.mapping if k))
        satellite_rooms = tuple(speakers.map.entity_rooms.values()) + tuple(speakers.map.device_rooms.values())
        directory = RoomDirectory.build(
            areas=snapshot.areas if snapshot is not None else (),
            light_group_keys=light_keys,
            room_groups=self._groups,
            satellite_rooms=satellite_rooms,
            group_friendly_names=group_names,
        )
        logger.info(
            "room_directory_built",
            rooms=len(directory),
            unmatchable=directory.unmatchable_count,
            collisions=directory.collision_count,
            from_registry=snapshot is not None,
        )
        if snapshot is not None:
            unmatched = _unmatched(snapshot, light_keys + _speaker_room_keys(speakers))
            if unmatched != self._unmatched_logged:
                self._unmatched_logged = unmatched
                logger.info("room_overrides_unmatched", count=unmatched)
        return directory

    # -- room groups ------------------------------------------------------

    def _maybe_refresh_groups(self) -> None:
        if self._admin is None or (self._task is not None and not self._task.done()):
            return
        now = self._monotonic()
        if self._groups_fetched_at is not None and now - self._groups_fetched_at < GROUP_TTL_SECONDS:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._groups_fetched_at = now
        self._task = loop.create_task(self._fetch_groups())

    async def _fetch_groups(self) -> None:
        try:
            groups = await asyncio.wait_for(
                self._admin.get_room_groups(enabled_only=True), GROUP_FETCH_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("room_groups_refresh_failed", error_class=type(exc).__name__)
            return
        if isinstance(groups, list) and groups != self._groups:
            self._groups = groups
            self._groups_version += 1

    async def start(self) -> None:
        """Fetch the room groups once, bounded, so the first turn has them."""
        if self._admin is None:
            return
        self._groups_fetched_at = self._monotonic()
        await self._fetch_groups()

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._task = None


def current_directory() -> Optional[RoomDirectory]:
    """The deployment's room directory, or None before the lifespan has
    registered one (tests, tools). Readers pass the result straight to
    ``room_context``, which falls back to the generic room words."""
    from orchestrator.nodes import _runtime

    provider = _runtime.get_room_directory()
    return provider.current() if provider is not None else None
