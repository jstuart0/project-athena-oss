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
- ``HARegistry``: owns the cache, the single in-flight fetch, rate limiting
  and the auth-refusal latch. It never raises and never blocks ``current()``.
- ``websocket_fetcher``: the real adapter. It can only send the frames in
  ``_ALLOWED_TYPES``, asserted before every send, so this module stays a
  read-only channel beside the permission-enforcing HA client (ATHENA-69).

No token or URL appears in any log line or exception text here; failures log
the exception class and counts only.
"""
from __future__ import annotations

import asyncio
import json
import re
import ssl
import time
import unicodedata
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Tuple

import structlog

from shared.ha_credentials import legacy_marker_digest, websocket_url

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

WS_OPEN_TIMEOUT = 3
WS_CLOSE_TIMEOUT = 2
WS_MAX_SIZE = 32 * 1024 * 1024
DEFAULT_FETCH_TIMEOUT = 5.0


class AuthInvalid(Exception):
    """Home Assistant answered ``auth_invalid`` to the token."""


class RegistryFetchError(Exception):
    """A protocol-level failure (unexpected frame, ``success: false``)."""


class ForbiddenFrameType(Exception):
    """An attempt to send a frame type outside the read-only allowlist."""


RegistryFetcher = Callable[[], Awaitable[Mapping[str, Any]]]


async def _send(ws: Any, payload: Dict[str, Any]) -> None:
    if payload.get("type") not in _ALLOWED_TYPES:
        raise ForbiddenFrameType(str(payload.get("type"))[:40])
    await ws.send(json.dumps(payload))


def _silent_logger():
    import logging

    quiet = logging.getLogger("athena.ha_registry.websocket")
    quiet.disabled = True
    quiet.propagate = False
    return quiet


def websocket_fetcher(ha_url: str, token: str) -> RegistryFetcher:
    """A fetcher that reads the three registry lists over one WebSocket
    connection. TLS verification is off for ``wss://``, as for every other
    Home Assistant client here. The library's own frame logging is silenced:
    the auth frame carries the token."""
    url = websocket_url(ha_url)

    async def fetch() -> Mapping[str, Any]:
        import websockets

        ssl_context: Optional[ssl.SSLContext] = None
        if url.startswith("wss://"):
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        async with websockets.connect(
            url,
            ssl=ssl_context,
            open_timeout=WS_OPEN_TIMEOUT,
            close_timeout=WS_CLOSE_TIMEOUT,
            max_size=WS_MAX_SIZE,
            ping_interval=None,
            logger=_silent_logger(),
        ) as ws:
            first = json.loads(await ws.recv())
            if first.get("type") != "auth_required":
                raise RegistryFetchError("unexpected first frame")
            await _send(ws, {"type": "auth", "access_token": token})
            verdict = json.loads(await ws.recv()).get("type")
            if verdict == "auth_invalid":
                raise AuthInvalid()
            if verdict != "auth_ok":
                raise RegistryFetchError("unexpected auth reply")
            for index, (_, frame_type) in enumerate(_LIST_TYPES, start=1):
                await _send(ws, {"id": index, "type": frame_type})
            results: Dict[int, Any] = {}
            while len(results) < len(_LIST_TYPES):
                frame = json.loads(await ws.recv())
                if frame.get("type") != "result" or frame.get("id") not in (1, 2, 3):
                    continue
                if not frame.get("success"):
                    raise RegistryFetchError("list command failed")
                results[frame["id"]] = frame.get("result")
            return {name: results[index] for index, (name, _) in enumerate(_LIST_TYPES, start=1)}

    return fetch


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------

REFUSED_MARKER_TTL_SECONDS = 24 * 60 * 60
FORCED_REFRESH_MIN_INTERVAL = 5.0
_MARKER_PREFIX = "athena:ha_registry:refused:"

STATE_DISABLED = "disabled"
STATE_REFUSED = "refused"
STATE_UNAVAILABLE = "unavailable"
STATE_OK = "ok"


token_marker_key = legacy_marker_digest


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

    ``current()`` is synchronous and never touches the network. A refresh
    runs as a background task, one at a time, at most once per
    ``min_refresh_interval``. A failed refresh keeps the last good snapshot
    and reports ``unavailable``. ``auth_invalid`` latches ``refused`` for the
    process and, with Redis and a marker key, for 24 hours across restarts.
    Nothing here raises to a caller.
    """

    def __init__(
        self,
        fetcher: Optional[RegistryFetcher],
        *,
        token_marker_key: Optional[str] = None,
        redis: Any = None,
        ttl: float = 300,
        min_refresh_interval: float = 60,
        fetch_timeout: float = DEFAULT_FETCH_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self._fetcher = fetcher
        self._marker = f"{_MARKER_PREFIX}{token_marker_key}" if token_marker_key else None
        self._redis = redis
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

    @property
    def state(self) -> str:
        return self._state

    def current(self) -> Optional[RegistrySnapshot]:
        return self._snapshot

    def link_summary(self) -> Dict[str, Any]:
        """Closed-vocabulary facts for the Home Assistant link status: the
        state, the area count, and when (wall-clock UTC) a fetch last worked
        or failed and what class of failure it was. No names, no text."""
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
        return self._fetcher is not None and self._state not in (STATE_DISABLED, STATE_REFUSED)

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
        except AuthInvalid:
            await self._latch_refused()
            return
        except Exception as exc:
            self._state = STATE_UNAVAILABLE
            self._note_failure(_failure_class(exc))
            logger.warning("ha_registry_fetch_failed", error_class=type(exc).__name__)
            return
        self._snapshot = snapshot
        self._state = STATE_OK
        self._last_success_at = self._wall()
        logger.info("ha_registry_refreshed", **snapshot.counts())
        for key, area_ids in snapshot.collisions:
            logger.warning("ha_registry_area_name_collision", area_count=len(area_ids))

    async def _latch_refused(self) -> None:
        self._state = STATE_REFUSED
        self._note_failure("auth")
        logger.error("ha_registry_auth_refused")
        if self._redis is not None and self._marker:
            try:
                await self._redis.set(self._marker, "1", ex=REFUSED_MARKER_TTL_SECONDS)
            except Exception as exc:
                logger.warning("ha_registry_marker_write_failed", error_class=type(exc).__name__)

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        """Check the refusal marker, then begin the first fetch in the
        background (startup doesn't wait for Home Assistant)."""
        if not self._may_fetch():
            return
        if self._redis is not None and self._marker:
            try:
                present = await self._redis.exists(self._marker)
            except Exception as exc:
                present = False
                logger.warning("ha_registry_marker_read_failed", error_class=type(exc).__name__)
            if present:
                self._state = STATE_REFUSED
                self._note_failure("auth")
                logger.error("ha_registry_auth_refused_marker_present")
                return
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
    ha_url: Optional[str],
    ha_token: Optional[str],
    mode: str,
    service_api_key: Optional[str] = None,
    redis: Any = None,
) -> HARegistry:
    """The registry a service builds in its lifespan: disabled when
    ``mode`` isn't ``"auto"`` or the URL or token is missing."""
    if mode != "auto" or not ha_url or not ha_token:
        return HARegistry(None)
    return HARegistry(
        websocket_fetcher(ha_url, ha_token),
        token_marker_key=token_marker_key(ha_token, service_api_key),
        redis=redis,
    )
