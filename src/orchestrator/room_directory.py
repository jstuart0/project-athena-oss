"""The one owner of the room vocabulary.

``RoomDirectoryProvider`` builds a ``room_context.RoomDirectory`` from the
Home Assistant area registry snapshot, ``HA_LIGHT_GROUPS``,
``HA_SATELLITE_ROOM_MAP`` and the admin room groups, and hands the same
directory to every reader (the classify node, the controller, the intent
classifier) through ``current()``.

``current()`` is synchronous and never does I/O. The directory is rebuilt only
when one of its inputs changes: a new registry snapshot, a changed config
string, or a changed room-group list. The room groups are fetched in the
background (``GROUP_TTL_SECONDS``).

Logs carry counts only; a room or area name never appears in a log line.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Callable, Iterable, List, Optional, Tuple

import structlog

from orchestrator.room_context import RoomDirectory
from shared.config import get_config
from shared.ha_registry import parse_satellite_room_map, room_key

logger = structlog.get_logger(__name__)

GROUP_TTL_SECONDS = 300
GROUP_FETCH_TIMEOUT_SECONDS = 5.0


def _light_group_keys(raw: str) -> Tuple[str, ...]:
    """The room keys of ``HA_LIGHT_GROUPS`` (a JSON object); empty when unset
    or malformed (the controller reports a malformed value)."""
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ()
    if not isinstance(parsed, dict):
        return ()
    return tuple(sorted({k for k in (room_key(str(name)) for name in parsed) if k}))


class RoomDirectoryProvider:
    def __init__(
        self,
        registry: Any,
        admin_client: Any,
        *,
        light_group_names: Optional[Callable[[], Iterable[str]]] = None,
        monotonic: Callable[[], float] = time.monotonic,
        config: Callable[[], Any] = get_config,
    ):
        self._registry = registry
        self._admin = admin_client
        self._light_group_names = light_group_names
        self._monotonic = monotonic
        self._config = config
        self._groups: List[dict] = []
        self._groups_version = 0
        self._groups_fetched_at: Optional[float] = None
        self._task: Optional["asyncio.Task[None]"] = None
        self._memo_key: Optional[tuple] = None
        self._directory: RoomDirectory = RoomDirectory.empty()

    # -- reads ------------------------------------------------------------

    def current(self) -> RoomDirectory:
        """The directory for the inputs as they are now. Starts a background
        room-group refresh when that list is older than the TTL."""
        self._maybe_refresh_groups()
        snapshot = self._registry.current() if self._registry is not None else None
        cfg = self._config()
        group_names: Tuple[str, ...] = ()
        if snapshot is None and self._light_group_names is not None:
            group_names = tuple(self._light_group_names())
        key = (
            snapshot.fetched_at if snapshot is not None else None,
            cfg.ha_light_groups or "",
            cfg.ha_satellite_room_map or "",
            self._groups_version,
            group_names,
        )
        if key != self._memo_key:
            self._directory = self._build(snapshot, cfg, group_names)
            self._memo_key = key
        return self._directory

    def _build(self, snapshot: Any, cfg: Any, group_names: Tuple[str, ...]) -> RoomDirectory:
        satellite_map = parse_satellite_room_map(cfg.ha_satellite_room_map or "")
        directory = RoomDirectory.build(
            areas=snapshot.areas if snapshot is not None else (),
            light_group_keys=_light_group_keys(cfg.ha_light_groups or ""),
            room_groups=self._groups,
            satellite_rooms=tuple(satellite_map.entity_rooms.values()) + tuple(satellite_map.device_rooms.values()),
            group_friendly_names=group_names,
        )
        logger.info(
            "room_directory_built",
            rooms=len(directory),
            unmatchable=directory.unmatchable_count,
            collisions=directory.collision_count,
            from_registry=snapshot is not None,
        )
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
