"""Household settings, service side: one reader per process and the typed resolvers.

Every household preference resolves in the same order: the admin value, then
the environment variable that used to be the only source, then today's built-in
default. A deployment that sets only the environment behaves exactly as before.

* ``configure(...)`` is called once in a service's lifespan with the sections
  that process reads. ``reader()`` is the cached snapshot.
* The resolvers are synchronous and read ``reader().peek()``; the env fallback
  reads ``get_config()`` at call time (never cached), so a test's patch applies.
* Each resolver returns a frozen value with ``.source``: ``"admin"``, ``"env"``
  or ``"default"``. Logs name the section and the source, never a value.
* Tests use ``install_for_test`` and ``reset_for_test`` (the autouse fixture in
  ``tests/unit/conftest.py`` resets after every test).

The registry, models and cache live in ``shared.household_settings``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from shared.household_settings import (
    SECTIONS,
    SOURCE_ADMIN,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    HouseholdSettingsCache,
    JarvisWeb,
    Location,
    Snapshot,
    SourcedStr,
    combine_sources,
    glob_to_regex,
    resolve_jarvis_web,
    snapshot_from_body,
    validate_update,
)

logger = logging.getLogger(__name__)

# The pattern a status LED or LED ring matches by entity id; always applied
# unless the environment opts out with "[]" (a pin test keeps this equal to
# orchestrator.ha_entity_manager.DEFAULT_ROOM_LIGHT_EXCLUDE_PATTERNS).
BUILTIN_ROOM_LIGHT_EXCLUDE: Tuple[str, ...] = (r"(?:^|[._])(?:led_ring|status_led)(?:_|$)",)

BED_WARMER_KEYS = ("level_left", "level_right", "power_main", "power_side_a", "power_side_b")


# ---------------------------------------------------------------------------
# The process reader
# ---------------------------------------------------------------------------

_reader: Optional[HouseholdSettingsCache] = None
_configured: Optional[Tuple[str, ...]] = None
_configured_args: Optional[tuple] = None
_seam = False
_warned_unconfigured: set = set()


def _admin_fetch(sections: Tuple[str, ...]) -> Callable[[], Awaitable[Optional[Dict[str, Any]]]]:
    async def fetch() -> Optional[Dict[str, Any]]:
        from shared.admin_url import get_admin_url

        if not get_admin_url():
            return None
        from shared.admin_config import get_admin_client

        return await get_admin_client().fetch_household_settings(sections)

    return fetch


def configure(*, sections: Tuple[str, ...], spawn: Optional[Callable] = None,
              fetch: Optional[Callable] = None) -> HouseholdSettingsCache:
    """Build this process's reader. Call it once in the lifespan, with the sections
    the process reads. Called again with different arguments it replaces the
    reader and logs ``household_reader_reconfigured`` once."""
    global _reader, _configured, _configured_args, _seam
    args = (tuple(sections), spawn, fetch)
    if _reader is not None and _configured_args == args and not _seam and not _reader.closed:
        return _reader
    if _reader is not None and _configured_args is not None and _configured_args != args:
        logger.warning("household_reader_reconfigured")
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown:
        raise ValueError("unknown household section")
    _reader = HouseholdSettingsCache(fetch or _admin_fetch(tuple(sections)), spawn=spawn)
    _configured, _configured_args, _seam = tuple(sections), args, False
    return _reader


WARM_TIMEOUT_SECONDS = 2.5


async def warm(timeout: float = WARM_TIMEOUT_SECONDS) -> None:
    """Load the snapshot once at startup, for at most ``timeout`` seconds in
    total (the fetch's own timeout applies per phase, so a slow admin could
    otherwise hold a service's startup several times over). On a timeout the
    environment stays in force until the next refresh succeeds."""
    try:
        await asyncio.wait_for(reader().current(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("household_settings_startup_timeout")


class ReloadBackoff:
    """Retry timing for a service that reloads config-driven data from the
    network. After a failed reload of ``key`` the same key is held back for a
    doubling delay (``base`` up to ``cap`` seconds); a different key (the admin
    changed something) is never held back. ``succeeded()`` resets it."""

    def __init__(self, *, base: float = 60.0, cap: float = 1800.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._base, self._cap, self._clock = base, cap, clock
        self._key: Any = None
        self._failures = 0
        self._not_before = 0.0

    def failed(self, key: Any) -> float:
        """Record a failed reload of ``key``; returns the delay before the next try."""
        self._failures = self._failures + 1 if key == self._key else 1
        self._key = key
        delay = min(self._base * 2 ** (self._failures - 1), self._cap)
        self._not_before = self._clock() + delay
        return delay

    def succeeded(self) -> None:
        self._key, self._failures, self._not_before = None, 0, 0.0

    def held_back(self, key: Any) -> bool:
        return self._failures > 0 and key == self._key and self._clock() < self._not_before


def reader() -> HouseholdSettingsCache:
    """This process's reader. Before ``configure`` it is a default reader that
    fetches every section and does no I/O when no admin URL is set."""
    global _reader
    if _reader is None:
        _reader = HouseholdSettingsCache(_admin_fetch(tuple(SECTIONS)))
    return _reader


def _inert_fetch() -> Awaitable[None]:
    async def nothing() -> None:
        return None

    return nothing()


def install_for_test(*, snapshot: Optional[Snapshot] = None, fetch: Optional[Callable] = None,
                     sources: Optional[Mapping[str, Optional[dict]]] = None) -> HouseholdSettingsCache:
    """Hand every resolver a fake. ``sources`` maps a section to a plain dict
    (validated through its model) or ``None``; ``snapshot`` is used as given;
    ``fetch`` builds a normal cache over that fetch."""
    global _reader, _configured, _configured_args, _seam
    cache = HouseholdSettingsCache(fetch or _inert_fetch)
    if sources is not None:
        body = {
            name: {"value": validate_update(name, value).model_dump(mode="json"), "revision": 1}
            for name, value in sources.items() if value is not None
        }
        snapshot = snapshot_from_body(body, time.monotonic())
    if snapshot is not None:
        cache.preload(snapshot)
    _reader, _configured, _configured_args, _seam = cache, None, None, True
    return cache


def reset_for_test() -> None:
    """Drop the singleton and the seam."""
    global _reader, _configured, _configured_args, _seam
    _reader, _configured, _configured_args, _seam = None, None, None, False
    _warned_unconfigured.clear()


def _section(name: str):
    """The stored model of a section, or ``None``."""
    if _configured is not None and name not in _configured and name not in _warned_unconfigured:
        _warned_unconfigured.add(name)
        logger.warning("household_section_not_configured section=%s", name)
    return reader().peek().sections.get(name)


def _config():
    from shared.config import get_config

    return get_config()


# ---------------------------------------------------------------------------
# Resolvers
# ---------------------------------------------------------------------------

def location() -> Location:
    """City, state and the text of the home location. An admin value that is
    text only (no matching city) keeps the environment's city and state."""
    section = _section("location")
    env_city = _config().default_city
    env_state = os.getenv("DEFAULT_STATE", "")
    if section is not None and (section.text or section.city):
        if section.city:
            city, state = section.city, section.state or ""
        else:
            city, state = env_city, env_state
        text = section.text or ", ".join(p for p in (city, state) if p)
        return Location(city, state, text, SOURCE_ADMIN)
    text = ", ".join(p for p in (env_city, env_state) if p)
    return Location(env_city, env_state, text, SOURCE_ENV if text else SOURCE_DEFAULT)


def amtrak_station() -> SourcedStr:
    section = _section("amtrak")
    if section is not None and section.default_station:
        return SourcedStr(section.default_station, SOURCE_ADMIN)
    env = _config().default_amtrak_station.strip().upper()
    return SourcedStr(env, SOURCE_ENV if env else SOURCE_DEFAULT)


def gateway_fallback_model() -> SourcedStr:
    section = _section("gateway")
    if section is not None and section.fallback_model:
        return SourcedStr(section.fallback_model, SOURCE_ADMIN)
    config = _config()
    if config.gateway_fallback_model:
        return SourcedStr(config.gateway_fallback_model, SOURCE_ENV)
    return SourcedStr(config.athena_default_model, SOURCE_DEFAULT)


def sms_country_code() -> SourcedStr:
    section = _section("sms")
    if section is not None and section.default_country_code:
        return SourcedStr(section.default_country_code, SOURCE_ADMIN)
    config = _config()
    from_env = "sms_default_country_code" in config.model_fields_set
    return SourcedStr(config.sms_default_country_code, SOURCE_ENV if from_env else SOURCE_DEFAULT)


@dataclass(frozen=True)
class PatternSet:
    """Regexes (``re.search`` on an entity id) that stop a light being a room fixture."""
    patterns: Tuple[str, ...]
    source: str


def room_light_exclude_patterns() -> PatternSet:
    """The built-in LED rule plus the admin globs; or, when admin has no list,
    the environment's regexes exactly as before (env ``[]`` opts out)."""
    section = _section("lights")
    if section is not None and section.room_light_exclude is not None:
        globs = tuple(glob_to_regex(g) for g in section.room_light_exclude)
        return PatternSet(BUILTIN_ROOM_LIGHT_EXCLUDE + globs, SOURCE_ADMIN)
    from shared.guest_policy import parse_json_array_env

    raw = _config().ha_room_light_exclude_entities
    patterns = tuple(parse_json_array_env(raw, list(BUILTIN_ROOM_LIGHT_EXCLUDE)))
    return PatternSet(patterns, SOURCE_ENV if raw else SOURCE_DEFAULT)


@dataclass(frozen=True)
class BedWarmerSetting:
    entities: Optional[Dict[str, str]]
    source: str


def resolved_bed_warmer() -> BedWarmerSetting:
    """The five bed-warmer entities as one unit: admin when all five are stored,
    else the environment's object when it names all five, else none."""
    section = _section("bed_warmer")
    if section is not None:
        stored = {k: getattr(section, k) for k in BED_WARMER_KEYS}
        if all(stored.values()):
            return BedWarmerSetting(dict(stored), SOURCE_ADMIN)
    raw = _config().ha_bed_warmer_entities
    entities: Dict[str, str] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("not an object")
            entities = {k: str(parsed[k]) for k in BED_WARMER_KEYS if k in parsed}
        except Exception as exc:
            logger.error("household_env_invalid section=bed_warmer error_type=%s", type(exc).__name__)
            entities = {}
    complete = all(k in entities for k in BED_WARMER_KEYS)
    return BedWarmerSetting(entities if complete else None, SOURCE_ENV if raw else SOURCE_DEFAULT)


def bed_warmer() -> Optional[Dict[str, str]]:
    return resolved_bed_warmer().entities


@dataclass(frozen=True)
class TransitSettings:
    """Feeds and static services as plain dicts shaped like today's env JSON,
    each with ``id`` and ``origin`` (``"admin"`` or ``"env"``). ``error`` is set
    when an environment value is unreadable (the service reports not configured)."""
    region_name: str
    feeds: Tuple[Dict[str, Any], ...]
    static_services: Tuple[Dict[str, Any], ...]
    error: Optional[str]
    source: str
    sources: Mapping[str, str] = field(default_factory=dict)


def _item_dicts(items, origin: str) -> Tuple[Dict[str, Any], ...]:
    out = []
    for item in items:
        data = item.model_dump(mode="json", exclude_none=True)
        data["origin"] = origin
        out.append(data)
    return tuple(out)


def _env_object(raw: str, name: str) -> Tuple[Tuple[Dict[str, Any], ...], Optional[str]]:
    if not raw:
        return (), None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return (), f"{name}: invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return (), f"{name}: must be a JSON object"
    items = []
    for item_id, item in parsed.items():
        data = dict(item) if isinstance(item, dict) else {}
        data["id"], data["origin"] = item_id, SOURCE_ENV
        items.append(data)
    return tuple(items), None


def transit() -> TransitSettings:
    section = _section("transit")
    config = _config()
    sources: Dict[str, str] = {}
    error: Optional[str] = None

    if section is not None and section.region_name:
        region, sources["region_name"] = section.region_name, SOURCE_ADMIN
    else:
        region = config.transit_region_name
        sources["region_name"] = SOURCE_ENV if region else SOURCE_DEFAULT

    if section is not None and section.gtfs_feeds is not None:
        feeds, sources["gtfs_feeds"] = _item_dicts(section.gtfs_feeds, SOURCE_ADMIN), SOURCE_ADMIN
    else:
        feeds, feeds_error = _env_object(config.transit_gtfs_feeds, "TRANSIT_GTFS_FEEDS")
        error = error or feeds_error
        sources["gtfs_feeds"] = SOURCE_ENV if config.transit_gtfs_feeds else SOURCE_DEFAULT

    if section is not None and section.static_services is not None:
        static, sources["static_services"] = _item_dicts(section.static_services, SOURCE_ADMIN), SOURCE_ADMIN
    else:
        static, static_error = _env_object(config.transit_static_services, "TRANSIT_STATIC_SERVICES")
        error = error or static_error
        sources["static_services"] = SOURCE_ENV if config.transit_static_services else SOURCE_DEFAULT

    if error:
        feeds, static = (), ()
    return TransitSettings(region, feeds, static, error, combine_sources(sources), MappingProxyType(sources))


@dataclass(frozen=True)
class CommunitySettings:
    """Community-event sources as dicts shaped like today's env JSON, each with
    ``origin``; ``error`` as for ``TransitSettings``."""
    sources: Tuple[Dict[str, Any], ...]
    error: Optional[str]
    source: str


def community_sources() -> CommunitySettings:
    section = _section("community_events")
    if section is not None and section.sources is not None:
        return CommunitySettings(_item_dicts(section.sources, SOURCE_ADMIN), None, SOURCE_ADMIN)
    raw = _config().community_events_sources
    name = "COMMUNITY_EVENTS_SOURCES"
    if not raw:
        return CommunitySettings((), None, SOURCE_DEFAULT)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return CommunitySettings((), f"{name}: invalid JSON: {exc}", SOURCE_ENV)
    if not isinstance(parsed, list):
        return CommunitySettings((), f"{name}: must be a JSON array", SOURCE_ENV)
    items = []
    for index, item in enumerate(parsed):
        if not isinstance(item, dict):
            return CommunitySettings((), f"{name}: source at index {index} must be an object", SOURCE_ENV)
        items.append({**item, "origin": SOURCE_ENV})
    return CommunitySettings(tuple(items), None, SOURCE_ENV)


def jarvis_web() -> JarvisWeb:
    """The jarvis-web entities and thermostat range (bounds resolve as a unit).
    jarvis-web itself runs ``resolve_jarvis_web`` from its own copy of the pure module."""
    return resolve_jarvis_web(_section("jarvis_web"), os.environ)


# Every section has a resolver; a campaign that adds a section adds its resolver here.
RESOLVERS: Mapping[str, Callable[[], Any]] = MappingProxyType({
    "location": location,
    "amtrak": amtrak_station,
    "transit": transit,
    "community_events": community_sources,
    "gateway": gateway_fallback_model,
    "sms": sms_country_code,
    "lights": room_light_exclude_patterns,
    "bed_warmer": resolved_bed_warmer,
    "jarvis_web": jarvis_web,
})
