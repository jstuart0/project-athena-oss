"""Home Assistant's area, device and entity registries, read-only.

A service learns which room a Home Assistant device or light is in from
here, never from a hard-coded list. The data comes from three non-admin
WebSocket list commands, is fetched in the background, and is read through a
synchronous, non-blocking ``HARegistry.current()``.

Layout:

- ``tokens`` / ``room_key``: the one room-name normalisation.
- ``parse_satellite_room_map``: ``HA_SATELLITE_ROOM_MAP`` split by key kind.
- ``RegistrySnapshot``: pure. Area resolution (entity, device, parent
  devices), area membership, satellite devices, and which lights are not
  lamps. No I/O.
- ``HARegistry``: owns the cache, the single in-flight fetch and rate limiting.
  It never raises and never blocks ``current()``; ``read()`` is ``current()``
  plus a background refresh when the snapshot is stale.
- ``snapshot_fetcher``: the real adapter. It reads the three lists over one
  ``ha_ws_session`` with the ``REGISTRY_READ`` profile, which can send only
  those list commands, so this module stays a read-only channel beside the
  permission-enforcing HA client (ATHENA-69). The shared latch decides whether
  a refused token may be tried again; this module keeps no refusal marker.

No token or URL appears in any log line or exception text here; failures log
the exception class and counts only.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

import structlog

from shared.ha_ws import (  # ForbiddenFrameType is re-exported for callers that import it from here
    AuthInvalid, ForbiddenFrameType, HABanned, HAWithheld, HomeAssistantNotConfiguredError, REGISTRY_READ,
    RegistryFetchError, ha_ws_session,
)

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Room-name normalisation
# ---------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r'[\W_]+')


def tokens(s: str) -> tuple:
    """Unicode-normalised, case-folded word tokens; NFC and NFD forms agree."""
    folded = unicodedata.normalize("NFKC", s or "").casefold()
    return tuple(t for t in _TOKEN_SPLIT.split(folded) if t)


def room_key(name: str) -> str:
    """The key a room is known by: its tokens joined with underscores
    (``"Living Room"`` -> ``"living_room"``). The same normalisation as the
    keys of ``HA_LIGHT_GROUPS``."""
    return "_".join(tokens(name))


# ---------------------------------------------------------------------------
# HA_SATELLITE_ROOM_MAP
# ---------------------------------------------------------------------------

_ENTITY_KEY = re.compile(r"^assist_satellite\.[a-z0-9_]+$")
_DEVICE_KEY = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True)
class SatelliteRoomMap:
    """``entity_rooms``: assist_satellite entity id -> room.
    ``device_rooms``: Home Assistant device id -> room."""
    entity_rooms: Mapping[str, str] = field(default_factory=dict)
    device_rooms: Mapping[str, str] = field(default_factory=dict)


def parse_satellite_room_map(raw: Any) -> SatelliteRoomMap:
    """Split a ``HA_SATELLITE_ROOM_MAP`` value (a JSON string or an
    already-decoded mapping) into entity-keyed and device-keyed rooms.

    A key that is neither an ``assist_satellite.<id>`` entity id nor a 32-hex
    device id is dropped; one ERROR reports how many (never the keys).
    Empty, unset and malformed values give an empty map (a malformed one
    logs an ERROR with the exception class only).
    """
    if raw is None or raw == "":
        return SatelliteRoomMap()
    parsed = raw
    if isinstance(raw, (str, bytes)):
        try:
            parsed = json.loads(raw)
        except ValueError as exc:
            logger.error("ha_satellite_room_map_invalid_json", error_class=type(exc).__name__)
            return SatelliteRoomMap()
    if not isinstance(parsed, Mapping):
        logger.error("ha_satellite_room_map_invalid_shape")
        return SatelliteRoomMap()
    entity_rooms: Dict[str, str] = {}
    device_rooms: Dict[str, str] = {}
    dropped = 0
    for key, room in parsed.items():
        key, room = str(key), str(room)
        if _ENTITY_KEY.match(key):
            entity_rooms[key] = room
        elif _DEVICE_KEY.match(key):
            device_rooms[key] = room
        else:
            dropped += 1
    if dropped:
        logger.error("ha_satellite_room_map_invalid_keys", count=dropped)
    return SatelliteRoomMap(entity_rooms, device_rooms)


# ---------------------------------------------------------------------------
# The snapshot (pure)
# ---------------------------------------------------------------------------

MAX_PARENT_LEVELS = 4


class RegistryShapeError(ValueError):
    """The registry lists don't have the shape this parser knows."""


@dataclass(frozen=True)
class Area:
    area_id: str
    name: str
    aliases: Tuple[str, ...]
    key: str


def _require_list(value: Any, what: str) -> List[dict]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise RegistryShapeError(what)
    return value


def _opt_str(entry: dict, name: str, what: str) -> Optional[str]:
    value = entry.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RegistryShapeError(what)
    return value


def _req_str(entry: dict, name: str, what: str) -> str:
    value = entry.get(name)
    if not isinstance(value, str) or not value:
        raise RegistryShapeError(what)
    return value


class RegistrySnapshot:
    """One consistent read of the three registries.

    Build it with ``from_raw``. Every method is a dictionary lookup; nothing
    here does I/O or raises on unknown ids.

    Area resolution (``area_for``): an entity's own ``area_id``, else its
    device's, else the first non-null ``area_id`` walking ``parent_device_id``
    up at most ``MAX_PARENT_LEVELS`` levels (cycle-safe). A device id resolves
    through the same chain. Only the fields ``area_id``, ``name``,
    ``aliases``, ``id``, ``parent_device_id``, ``entity_id``, ``device_id``,
    ``entity_category``, ``hidden_by`` and ``disabled_by`` are read; any other
    key (including the deprecated device ones) is ignored.
    """

    def __init__(
        self,
        *,
        areas: Mapping[str, Area],
        device_area: Mapping[str, Optional[str]],
        device_parent: Mapping[str, Optional[str]],
        entities: Mapping[str, dict],
        fetched_at: float,
    ):
        self._areas = dict(areas)
        self._device_area = dict(device_area)
        self._device_parent = dict(device_parent)
        self._entities = dict(entities)
        self.fetched_at = fetched_at

        by_key: Dict[str, List[str]] = {}
        for area in self._areas.values():
            if area.key:
                by_key.setdefault(area.key, []).append(area.area_id)
        self.collisions: Tuple[Tuple[str, Tuple[str, ...]], ...] = tuple(
            sorted((key, tuple(sorted(ids))) for key, ids in by_key.items() if len(ids) > 1)
        )
        self._collided = frozenset(key for key, _ in self.collisions)
        self._by_key = {
            key: self._areas[ids[0]] for key, ids in by_key.items() if len(ids) == 1
        }
        alias_map: Dict[str, List[Area]] = {}
        for area in self._areas.values():
            for alias in area.aliases:
                alias_key = room_key(alias)
                if alias_key and area not in alias_map.setdefault(alias_key, []):
                    alias_map[alias_key].append(area)
        self._by_alias = {k: tuple(sorted(v, key=lambda a: a.area_id)) for k, v in alias_map.items()}

        self._entity_area: Dict[str, Optional[str]] = {
            eid: self._resolve_entity_area(rec) for eid, rec in self._entities.items()
        }
        self._satellite_roots = frozenset(
            root
            for eid, rec in self._entities.items()
            if eid.startswith("assist_satellite.") and rec["device_id"]
            for root in (self._root_device(rec["device_id"]),)
        )
        self.non_lamp_lights: frozenset = frozenset(
            eid for eid, rec in self._entities.items()
            if eid.startswith("light.") and self._is_not_a_lamp(rec)
        )

    # -- construction -----------------------------------------------------

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any], *, fetched_at: float) -> "RegistrySnapshot":
        """Parse the three list results. Raises ``RegistryShapeError`` on any
        shape this parser doesn't know, so the caller reports "unavailable"
        rather than an empty registry."""
        if not isinstance(raw, Mapping):
            raise RegistryShapeError("registry")
        areas: Dict[str, Area] = {}
        for entry in _require_list(raw.get("areas"), "areas"):
            area_id = _req_str(entry, "area_id", "area_id")
            name = _req_str(entry, "name", "area name")
            aliases = entry.get("aliases") or []
            if not isinstance(aliases, list) or not all(isinstance(a, str) for a in aliases):
                raise RegistryShapeError("area aliases")
            areas[area_id] = Area(area_id, name, tuple(aliases), room_key(name))
        device_area: Dict[str, Optional[str]] = {}
        device_parent: Dict[str, Optional[str]] = {}
        for entry in _require_list(raw.get("devices"), "devices"):
            device_id = _req_str(entry, "id", "device id")
            device_area[device_id] = _opt_str(entry, "area_id", "device area_id")
            device_parent[device_id] = _opt_str(entry, "parent_device_id", "device parent")
        entities: Dict[str, dict] = {}
        for entry in _require_list(raw.get("entities"), "entities"):
            entity_id = _req_str(entry, "entity_id", "entity_id")
            entities[entity_id] = {
                "device_id": _opt_str(entry, "device_id", "entity device_id"),
                "area_id": _opt_str(entry, "area_id", "entity area_id"),
                "entity_category": bool(entry.get("entity_category")),
                "hidden": bool(entry.get("hidden_by")),
                "disabled": bool(entry.get("disabled_by")),
            }
        return cls(
            areas=areas,
            device_area=device_area,
            device_parent=device_parent,
            entities=entities,
            fetched_at=fetched_at,
        )

    # -- area resolution --------------------------------------------------

    def _device_chain(self, device_id: str) -> List[str]:
        """The device and its parents, nearest first, at most
        ``MAX_PARENT_LEVELS`` parents, stopping at a repeat."""
        chain: List[str] = []
        current: Optional[str] = device_id
        while current is not None and current not in chain and len(chain) <= MAX_PARENT_LEVELS:
            chain.append(current)
            current = self._device_parent.get(current)
        return chain

    def _resolve_device_area(self, device_id: Optional[str]) -> Optional[str]:
        if not device_id:
            return None
        for device in self._device_chain(device_id):
            area_id = self._device_area.get(device)
            if area_id:
                return area_id
        return None

    def _resolve_entity_area(self, rec: dict) -> Optional[str]:
        return rec["area_id"] or self._resolve_device_area(rec["device_id"])

    def _root_device(self, device_id: str) -> str:
        """The top of a device's parent chain. In a cycle, the smallest id of
        the cycle, so every member agrees."""
        seen: List[str] = []
        current: Optional[str] = device_id
        while current is not None:
            if current in seen:
                return min(seen[seen.index(current):])
            seen.append(current)
            current = self._device_parent.get(current)
        return seen[-1]

    def _is_not_a_lamp(self, rec: dict) -> bool:
        if rec["entity_category"] or rec["hidden"] or rec["disabled"]:
            return True
        device_id = rec["device_id"]
        return bool(device_id) and self._root_device(device_id) in self._satellite_roots

    def area_for(self, ref: str) -> Optional[Area]:
        """The area of an entity id or a device id, or None."""
        if ref in self._entity_area:
            area_id = self._entity_area[ref]
        elif ref in self._device_area:
            area_id = self._resolve_device_area(ref)
        else:
            return None
        return self._areas.get(area_id) if area_id else None

    def knows_device(self, device_id: str) -> bool:
        return device_id in self._device_area

    # -- areas ------------------------------------------------------------

    @property
    def areas(self) -> Tuple[Area, ...]:
        return tuple(sorted(self._areas.values(), key=lambda a: a.area_id))

    def area_by_key(self, key: str) -> Optional[Area]:
        """The area with this room key, or None when none has it or several
        share it (see ``collisions``)."""
        return self._by_key.get(key)

    def areas_by_alias(self, alias: str) -> Tuple[Area, ...]:
        return self._by_alias.get(room_key(alias), ())

    # -- membership -------------------------------------------------------

    def entities_in(self, area_id: str, domain: str) -> Tuple[str, ...]:
        """Entity ids of one domain that resolve to the area, minus hidden
        and disabled entries. A light's lamp-ness is ``non_lamp_lights``."""
        prefix = f"{domain}."
        return tuple(sorted(
            eid for eid, resolved in self._entity_area.items()
            if resolved == area_id and eid.startswith(prefix)
            and not self._entities[eid]["hidden"] and not self._entities[eid]["disabled"]
        ))

    def satellite_entities(self, device_id: str) -> Tuple[str, ...]:
        """assist_satellite entity ids on the device or on any of its
        children."""
        found = []
        for eid, rec in self._entities.items():
            if not eid.startswith("assist_satellite.") or not rec["device_id"]:
                continue
            if device_id in self._device_chain(rec["device_id"]):
                found.append(eid)
        return tuple(sorted(found))

    def counts(self) -> Dict[str, int]:
        return {
            "areas": len(self._areas),
            "devices": len(self._device_area),
            "entities": len(self._entities),
            "collisions": len(self.collisions),
        }


# ---------------------------------------------------------------------------
# The WebSocket adapter
# ---------------------------------------------------------------------------

_LIST_TYPES = (
    ("areas", "config/area_registry/list"),
    ("devices", "config/device_registry/list"),
    ("entities", "config/entity_registry/list"),
)
_ALLOWED_TYPES = frozenset({"auth"} | {frame_type for _, frame_type in _LIST_TYPES})

DEFAULT_FETCH_TIMEOUT = 5.0

RegistryFetcher = Callable[[], Awaitable[Mapping[str, Any]]]


def snapshot_fetcher(credentials: Any, latch: Any, *, caller: str, connect: Optional[Callable[..., Any]] = None) -> RegistryFetcher:
    """A fetcher that reads the three registry lists over one WebSocket session
    (``ha_ws_session`` with ``REGISTRY_READ``), from the credentials as they
    are when it runs. Public so a tool (``room_probe``) uses the same path."""

    async def fetch() -> Mapping[str, Any]:
        snapshot = credentials.current()
        async with ha_ws_session(snapshot, latch, caller=caller, profile=REGISTRY_READ, connect=connect) as ws:
            frames = await ws.commands([{"type": frame_type} for _, frame_type in _LIST_TYPES])
            if not all(frame.get("success") for frame in frames):
                raise RegistryFetchError("list command failed")
            return {name: frame.get("result") for (name, _), frame in zip(_LIST_TYPES, frames)}

    return fetch


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

FORCED_REFRESH_MIN_INTERVAL = 5.0

STATE_DISABLED = "disabled"
STATE_REFUSED = "refused"
STATE_UNAVAILABLE = "unavailable"
STATE_OK = "ok"

# Why the shared latch would not let a fetch go out: the token was rejected, or
# the failed sign-in budget is spent. ``gated`` (Redis unreadable, a wait that
# timed out) is not a verdict on the token.
_REFUSAL_REASONS = frozenset({"latched", "admin_only", "budget"})


def _failure_class(exc: BaseException) -> str:
    """The closed class of a failed fetch: ``TimeoutError`` is an ``OSError``,
    so it is tested first."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "timeout"
    if isinstance(exc, OSError):
        return "unreachable"
    return "other"


class HARegistry:
    """The cached registry snapshot and the one fetch that refreshes it.

    ``current()`` is synchronous and never touches the network; ``read()`` is
    ``current()`` that also asks for a background refresh when the snapshot is
    stale. A refresh runs as a background task, one at a time, at most once per
    ``min_refresh_interval``. A failed refresh keeps the last good snapshot and
    reports ``unavailable``. ``auth_invalid`` (or the shared latch withholding
    the fetch) reports ``refused``, and the registry tries again at the normal
    interval: the latch decides whether anything is sent, so a registry refused
    while REST already works recovers by itself. Nothing here raises to a
    caller.
    """

    def __init__(
        self,
        fetcher: Optional[RegistryFetcher],
        *,
        configured: Callable[[], bool] = lambda: True,
        ttl: float = 300,
        min_refresh_interval: float = 60,
        fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._fetcher = fetcher
        self._configured = configured
        self._ttl = ttl
        self._min_refresh_interval = min_refresh_interval
        self._fetch_timeout = fetch_timeout
        self._monotonic = monotonic
        self._wall = wall
        self._last_success_at: Optional[datetime] = None
        self._last_failure_at: Optional[datetime] = None
        self._last_failure_class: Optional[str] = None
        self._ban_suspected = False
        self._snapshot: Optional[RegistrySnapshot] = None
        self._state = STATE_DISABLED if fetcher is None else STATE_UNAVAILABLE
        self._last_attempt: Optional[float] = None
        self._last_forced: Optional[float] = None
        self._task: Optional["asyncio.Task[None]"] = None

    # -- reads ------------------------------------------------------------

    def _apply_enabled(self) -> None:
        """Disabled while Home Assistant isn't configured; unavailable (and
        fetching) once it is."""
        if self._fetcher is None:
            return
        if not self._configured():
            self._state = STATE_DISABLED
        elif self._state == STATE_DISABLED:
            self._state = STATE_UNAVAILABLE

    @property
    def state(self) -> str:
        self._apply_enabled()
        return self._state

    def current(self) -> Optional[RegistrySnapshot]:
        return self._snapshot

    def read(self) -> Optional[RegistrySnapshot]:
        """The snapshot, and a background refresh when it is stale. Does no I/O
        itself and never raises."""
        self.request_refresh()
        return self._snapshot

    def link_summary(self) -> Dict[str, Any]:
        """Closed-vocabulary facts for the Home Assistant link status: the
        state, the area count, and when (wall-clock UTC) a fetch last worked
        or failed and what class of failure it was. No names, no text."""
        self._apply_enabled()
        snap = self._snapshot
        return {
            "state": self._state,
            "areas": snap.counts()["areas"] if snap is not None else None,
            "last_success_at": self._last_success_at.isoformat() if self._last_success_at else None,
            "last_failure_at": self._last_failure_at.isoformat() if self._last_failure_at else None,
            "last_failure_class": self._last_failure_class,
            "ban_suspected": self._ban_suspected,
        }

    def _note_failure(self, failure_class: str) -> None:
        self._last_failure_at = self._wall()
        self._last_failure_class = failure_class

    @property
    def stale(self) -> bool:
        snap = self._snapshot
        return snap is None or self._monotonic() - snap.fetched_at >= self._ttl

    # -- refreshing -------------------------------------------------------

    def _may_fetch(self) -> bool:
        self._apply_enabled()
        return self._fetcher is not None and self._state != STATE_DISABLED

    def _start_fetch(self, *, forced: bool = False) -> None:
        """Start the background fetch. ``forced`` (a caller that must see new
        data, such as an unknown device id) skips the usual minimum interval,
        but not more than once per ``FORCED_REFRESH_MIN_INTERVAL``."""
        if not self._may_fetch() or (self._task is not None and not self._task.done()):
            return
        now = self._monotonic()
        within_interval = self._last_attempt is not None and now - self._last_attempt < self._min_refresh_interval
        recently_forced = self._last_forced is not None and now - self._last_forced < FORCED_REFRESH_MIN_INTERVAL
        if within_interval and (not forced or recently_forced):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._last_attempt = now
        if forced:
            self._last_forced = now
        self._task = loop.create_task(self._refresh())

    def request_refresh(self, *, force: bool = False) -> None:
        """Ask for a background refresh. A no-op while the snapshot is fresh
        (unless ``force``), while one is in flight, or inside the minimum
        interval."""
        if force or self.stale:
            self._start_fetch()

    def credentials_changed(self) -> None:
        """The token or address was replaced: a refused registry gets a fresh
        try now (the latch keys its stop on the old credentials)."""
        if self._state == STATE_REFUSED:
            self._state = STATE_UNAVAILABLE
        self._last_attempt = None
        self._start_fetch(forced=True)

    async def ensure(self, timeout: float, *, refresh: bool = False) -> Optional[RegistrySnapshot]:
        """Wait up to ``timeout`` seconds for a usable snapshot. Returns the
        current snapshot (possibly stale, possibly None) either way; the fetch
        keeps running past a timeout."""
        if self._may_fetch() and (refresh or self.stale):
            self._start_fetch(forced=refresh)
            task = self._task
            if task is not None and not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout)
                except Exception:
                    pass
        return self._snapshot

    async def _refresh(self) -> None:
        try:
            raw = await asyncio.wait_for(self._fetcher(), self._fetch_timeout)
            snapshot = RegistrySnapshot.from_raw(raw, fetched_at=self._monotonic())
        except asyncio.CancelledError:
            raise
        except HAWithheld as exc:
            self._withheld(exc.reason)
            return
        except AuthInvalid:
            self._refuse()
            logger.error("ha_registry_auth_refused")
            return
        except HABanned:
            self._state = STATE_UNAVAILABLE
            self._ban_suspected = True
            self._note_failure("other")
            logger.warning("ha_registry_address_blocked")
            return
        except HomeAssistantNotConfiguredError:
            self._apply_enabled()
            return
        except Exception as exc:
            self._state = STATE_UNAVAILABLE
            self._note_failure(_failure_class(exc))
            logger.warning("ha_registry_fetch_failed", error_class=type(exc).__name__)
            return
        self._snapshot = snapshot
        self._state = STATE_OK
        self._ban_suspected = False
        self._last_success_at = self._wall()
        logger.info("ha_registry_refreshed", **snapshot.counts())
        for key, area_ids in snapshot.collisions:
            logger.warning("ha_registry_area_name_collision", area_count=len(area_ids))

    def _refuse(self) -> None:
        self._state = STATE_REFUSED
        self._note_failure("auth")

    def _withheld(self, reason: str) -> None:
        """The shared latch sent nothing. A rejected token or a spent budget is
        ``refused``; anything else is a transient ``unavailable``."""
        if reason in _REFUSAL_REASONS:
            if self._state != STATE_REFUSED:
                logger.warning("ha_registry_withheld", reason=reason)
            self._refuse()
        else:
            self._state = STATE_UNAVAILABLE
            self._note_failure("other")

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Begin the first fetch in the background (startup doesn't wait for
        Home Assistant)."""
        self._start_fetch()

    async def close(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        self._task = None


def create_registry(
    *,
    credentials: Any,
    latch: Any,
    caller: str,
    mode: str,
    connect: Optional[Callable[..., Any]] = None,
) -> HARegistry:
    """The registry a service builds in its lifespan: disabled when ``mode``
    isn't ``"auto"``, and otherwise enabled whenever the credentials are
    configured (checked on each use, so a token saved later brings it alive)."""
    if mode != "auto":
        return HARegistry(None)
    return HARegistry(
        snapshot_fetcher(credentials, latch, caller=caller, connect=connect),
        configured=lambda: credentials.current().configured,
    )
