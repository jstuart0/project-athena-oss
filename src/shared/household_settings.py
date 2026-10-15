"""Household settings: the typed section registry, validators, masking and the cached reader.

Preferences that used to come only from environment variables (city, Amtrak
station, transit feeds, lamp exclusions, bed warmer, jarvis-web entities, ...)
are stored by admin-backend as one ``system_settings`` row per *section*
(``household.<name>``). This module is the one place that knows the sections'
shapes, the rules every stored value must pass, how a URL is shown to a user,
and how a process caches what admin-backend answers.

Pure: stdlib plus pydantic (2.10 and later) only. It never imports ``shared.*``,
so jarvis-web (which has no ``shared`` package) can run a copy of this single
file.

Stored shape (version 1), one row per section::

    {"v": 1, "revision": <int>, "value": {<field>: <value or null>, ...}}

A field that is ``null`` or absent is *unset* (the reader falls back to the
environment, then the built-in default). An empty list is an answer: "none".

P1 interface notes
------------------
* ``SECTIONS`` is built from ``_REGISTRY``; another campaign adds a section by
  appending a ``SectionSpec`` to that tuple and a resolver to
  ``shared.household.RESOLVERS``.
* ``Snapshot.loaded_at`` is the cache clock's reading (``time.monotonic()`` by
  default) at the successful admin fetch that produced the snapshot. It is
  ``0.0`` for a snapshot that has never loaded (env-only or admin unreachable
  since boot). A failed refresh keeps the last good snapshot *and* its
  ``loaded_at``; a successful one advances it.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import math
import re
import socket
import time
import unicodedata
import weakref
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import (
    Annotated, Any, Awaitable, Callable, Dict, List, Literal, Mapping, Optional, Tuple, Type,
)
from urllib.parse import urlsplit

from pydantic import (
    BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator, model_validator,
)
from pydantic_core import PydanticCustomError

logger = logging.getLogger(__name__)

STORED_VERSION = 1
KEY_PREFIX = "household."
SETTING_CATEGORY = "household"
MASK_TOKEN = "•••"

# Limits (decisions D7, D8, D15).
MAX_FEEDS = 20
MAX_STATIC_SERVICES = 20
MAX_SOURCES = 20
MAX_STOPS = 500
MAX_GLOBS = 50
MAX_GLOB_LENGTH = 128
MAX_GLOB_WILDCARDS = 3
MIN_FEED_BYTES = 2**20
MAX_FEED_BYTES = 100 * 2**20
MAX_TEXT_LENGTH = 100
MAX_TEXT_ITEMS = 20
MAX_URL_LENGTH = 2048
MAX_ENTITY_LENGTH = 255
_HARD_LIST_LIMIT = 1000  # refuse before validating a list this long, whatever the field

SectionPermission = Literal["write", "manage_infrastructure"]
AuditMode = Literal["values", "fields", "hosts"]


class SettingsValidationError(Exception):
    """A value failed validation. ``errors`` is ``[{"loc": tuple, "type": str}]``
    and never carries the submitted input."""

    def __init__(self, errors: List[Dict[str, Any]]):
        super().__init__("settings validation failed")
        self.errors = errors


def _err(kind: str) -> PydanticCustomError:
    return PydanticCustomError(kind, "invalid value")


# ---------------------------------------------------------------------------
# Room-name helpers (D20)
# ---------------------------------------------------------------------------

_TOKEN_SPLIT = re.compile(r"[\W_]+")

RESERVED_ROOM_KEYS = frozenset({
    "whole_house", "house", "all", "everywhere", "unknown", "multi_room",
    "here", "this_room", "my_room", "the_room", "in_here",
    # The other wordings room_context's whole-house pattern and speaker-relative
    # phrases recognise, so no room can be named like one (tests pin both lists).
    "the_whole_house", "entire_house", "every_room", "all_rooms", "all_the_rooms",
    "throughout_the_house", "all_over_the_house", "my_current_room",
})


def room_key(name: str) -> str:
    """A room's tokens joined with underscores ("Living Room" -> "living_room").
    Pinned equal to ``shared.ha_registry.room_key`` by a vector test."""
    folded = unicodedata.normalize("NFKC", name or "").casefold()
    return "_".join(t for t in _TOKEN_SPLIT.split(folded) if t)


def is_reserved_room(name: str) -> bool:
    return room_key(name) in RESERVED_ROOM_KEYS


# ---------------------------------------------------------------------------
# Scalar validators
# ---------------------------------------------------------------------------

_BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}


def _clean_text(value: Any, max_len: int = MAX_TEXT_LENGTH) -> Optional[str]:
    """NFC, stripped, no control or format characters; blank is ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise _err("string_type")
    text = unicodedata.normalize("NFC", value).strip()
    if any(unicodedata.category(c) in _BAD_CATEGORIES for c in text):
        raise _err("bad_characters")
    if len(text) > max_len:
        raise _err("string_too_long")
    return text or None


def _required_text(value: Any) -> str:
    text = _clean_text(value)
    if text is None:
        raise _err("string_too_short")
    return text


def _long_text(value: Any) -> Optional[str]:
    return _clean_text(value, 300)


OptText = Annotated[Optional[str], BeforeValidator(_clean_text)]
OptLongText = Annotated[Optional[str], BeforeValidator(_long_text)]
Name = Annotated[str, BeforeValidator(_required_text)]

_ID_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


def _item_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise _err("bad_id")
    return value


ItemId = Annotated[str, BeforeValidator(_item_id)]

_TYPE_RE = re.compile(r"[a-z_]{1,32}")


def _type_word(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _TYPE_RE.fullmatch(value):
        raise _err("bad_type")
    return value


TypeWord = Annotated[Optional[str], BeforeValidator(_type_word)]

_ENTITY_RE = re.compile(r"[a-z_]+\.[a-z0-9_]+")
_APPLIANCE_BAD_TOKENS = ("hot_water", "boiler", "tank", "cylinder", "geyser")
_SWITCH_DENY_TOKENS = (
    "door", "gate", "garage", "lock", "unlock", "opener", "water_heater",
    "boiler", "alarm", "siren", "valve",
)


def _entity_checker(domains: Tuple[str, ...], *, appliance: bool = False, deny: Tuple[str, ...] = ()):
    def check(value: Any) -> Optional[str]:
        if value is None:
            return None
        if not isinstance(value, str) or len(value) > MAX_ENTITY_LENGTH or not _ENTITY_RE.fullmatch(value):
            raise _err("bad_entity_id")
        domain, object_id = value.split(".", 1)
        if domain not in domains:
            raise _err("bad_entity_domain")
        if appliance and (object_id == "water_heater" or any(t in object_id for t in _APPLIANCE_BAD_TOKENS)):
            raise _err("entity_not_allowed")
        if any(t in object_id for t in deny):
            raise _err("entity_not_allowed")
        return value
    return check


ClimateEntity = Annotated[Optional[str], BeforeValidator(_entity_checker(("climate",)))]
ApplianceEntity = Annotated[Optional[str], BeforeValidator(_entity_checker(("water_heater",), appliance=True))]
SensorEntity = Annotated[Optional[str], BeforeValidator(_entity_checker(("sensor",)))]
DoorSensorEntity = Annotated[Optional[str], BeforeValidator(_entity_checker(("binary_sensor", "sensor")))]
BedSwitch = Annotated[Optional[str], BeforeValidator(_entity_checker(("switch",), deny=_SWITCH_DENY_TOKENS))]
BedSelect = Annotated[Optional[str], BeforeValidator(_entity_checker(("select",)))]


def _station(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _err("string_type")
    text = value.strip()
    if not (text.isascii() and re.fullmatch(r"[A-Za-z]{3}", text)):
        raise _err("bad_station")
    return text.upper()


Station = Annotated[Optional[str], BeforeValidator(_station)]


def _country_code(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _err("string_type")
    text = value[1:] if value.startswith("+") else value
    if not re.fullmatch(r"[0-9]{1,3}", text) or int(text) == 0:
        raise _err("bad_country_code")
    return text


CountryCode = Annotated[Optional[str], BeforeValidator(_country_code)]

_MODEL_RE = re.compile(r"[A-Za-z0-9._:/-]{1,255}")


def _model_name(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _MODEL_RE.fullmatch(value):
        raise _err("bad_model_name")
    return value


ModelName = Annotated[Optional[str], BeforeValidator(_model_name)]

_ROOM_RE = re.compile(r"[A-Za-z0-9 _'.&-]{1,64}")


def _room(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _ROOM_RE.fullmatch(value) or is_reserved_room(value):
        raise _err("bad_room")
    return value


Room = Annotated[Optional[str], BeforeValidator(_room)]

_HHMM_RE = re.compile(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]")


def _hhmm(value: Any) -> str:
    if not isinstance(value, str) or not _HHMM_RE.fullmatch(value):
        raise _err("bad_time")
    return value


Hhmm = Annotated[str, BeforeValidator(_hhmm)]


def _link_path(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _err("string_type")
    if (
        not value.startswith("/") or "//" in value or "\\" in value or len(value) > 200
        or any(ord(c) <= 32 or ord(c) == 127 for c in value)
    ):
        raise _err("bad_link_path")
    return value


LinkPath = Annotated[Optional[str], BeforeValidator(_link_path)]


def _glob(value: Any) -> str:
    if not isinstance(value, str):
        raise _err("string_type")
    if len(value) > MAX_GLOB_LENGTH:
        raise _err("glob_too_long")
    if not value.startswith("light."):
        raise _err("glob_invalid")
    body = value[len("light."):]
    if not body or not re.fullmatch(r"[a-z0-9_*]+", body) or "**" in body:
        raise _err("glob_invalid")
    if body.startswith("*"):
        raise _err("glob_too_broad")
    if body.count("*") > MAX_GLOB_WILDCARDS:
        raise _err("glob_too_many_wildcards")
    return value


Glob = Annotated[str, BeforeValidator(_glob)]


def glob_to_regex(item: str) -> str:
    """The anchored regex for a validated lamp-exclusion glob. Everything is
    escaped except ``*``, which becomes a run of entity-id characters."""
    _glob(item)
    return "^" + re.escape(item).replace(r"\*", "[a-z0-9_.]*") + r"\Z"


# ---------------------------------------------------------------------------
# URLs (D8, D9)
# ---------------------------------------------------------------------------

def _is_numeric_host(host: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-FxX.]+", host)) and any(c.isdigit() for c in host)


def _literal_ip(host: str) -> Optional[ipaddress._BaseAddress]:
    """The IP a host spells, including decimal, hex and octal IPv4 forms and
    bracketed IPv6; ``None`` for a name. Raises ``ValueError`` for a malformed
    IPv6 literal."""
    if ":" in host:
        return ipaddress.ip_address(host)
    if _is_numeric_host(host):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


# IPv6 ranges that embed an IPv4 address (or translate to one) and that
# ``is_global`` does not refuse: IPv4-compatible, 6to4 and both NAT64 prefixes.
_EMBEDDING_V6_NETWORKS = tuple(
    ipaddress.ip_network(n) for n in ("::/96", "2002::/16", "64:ff9b::/96", "64:ff9b:1::/48")
)
_PRIVATE_NAME_SUFFIXES = (
    ".localhost", ".svc", ".cluster.local", ".internal", ".local", ".lan", ".home.arpa",
)


def _is_private_literal(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _EMBEDDING_V6_NETWORKS):
            return True
    return not ip.is_global or ip.is_multicast


def _split_url(url: Any):
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise _err("url_invalid")
    if MASK_TOKEN in url:
        raise _err("url_mask_token")
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        raise _err("url_invalid")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _err("url_invalid")
    if parts.scheme.lower() not in ("http", "https"):
        raise _err("url_scheme")
    if "@" in parts.netloc:
        raise _err("url_userinfo")
    if parts.fragment or "#" in url:
        raise _err("url_fragment")
    host = (parts.hostname or "").lower()
    if not host or (port is not None and not 0 < port < 65536):
        raise _err("url_invalid")
    return parts, host


def check_fetch_url(url: Any) -> str:
    """Raise a ``PydanticCustomError`` unless ``url`` may be fetched from an
    admin-supplied setting. A name that *resolves* privately passes here and is
    blocked at fetch time."""
    _parts, host = _split_url(url)
    try:
        ip = _literal_ip(host)
    except ValueError:
        raise _err("url_invalid")
    if ip is not None:
        if _is_private_literal(ip):
            raise _err("url_private_host")
        return url
    name = host.rstrip(".")
    labels = name.split(".")
    if (
        not name.isascii() or not re.fullmatch(r"[a-z0-9._-]+", name)
        or any(not 1 <= len(l) <= 63 or l.startswith("-") or l.endswith("-") for l in labels)
        or not any(c.isalpha() for c in labels[-1])
    ):
        raise _err("url_invalid")
    if len(labels) < 2 or name == "localhost" or name.endswith(_PRIVATE_NAME_SUFFIXES):
        raise _err("url_private_host")
    return url


def _url_checker(value: Any) -> Optional[str]:
    if value is None:
        return None
    return check_fetch_url(value)


FetchUrl = Annotated[str, BeforeValidator(check_fetch_url)]
OptFetchUrl = Annotated[Optional[str], BeforeValidator(_url_checker)]


def url_host(url: Any) -> str:
    """The host of a URL and nothing else; ``"unknown"`` when it can't be read.
    The one renderer for errors, logs and health text."""
    try:
        host = urlsplit(url).hostname if isinstance(url, str) else None
    except ValueError:
        host = None
    return host.lower() if host else "unknown"


def mask_url(url: Any) -> str:
    """``scheme://host[:port]`` of a URL: what a user is shown. ``""`` when unreadable."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except (ValueError, AttributeError, TypeError):
        return ""
    if not host or not parts.scheme:
        return ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme.lower()}://{host}" + (f":{port}" if port else "")


# ---------------------------------------------------------------------------
# Item models
# ---------------------------------------------------------------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _text_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return None
    if not isinstance(value, list):
        raise _err("list_type")
    if len(value) > MAX_TEXT_ITEMS:
        raise _err("too_long")
    return [_required_text(item) for item in value]


TextList = Annotated[Optional[List[str]], BeforeValidator(_text_list)]

Lat = Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]
Lon = Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]


class Bounds(_Strict):
    min_lat: Lat
    max_lat: Lat
    min_lon: Lon
    max_lon: Lon

    @model_validator(mode="after")
    def _ordered(self) -> "Bounds":
        if not (self.min_lat < self.max_lat and self.min_lon < self.max_lon):
            raise _err("bounds_order")
        return self


class Feed(_Strict):
    id: ItemId
    name: Name
    url: FetchUrl
    agency: OptText = None
    type: TypeWord = None
    free: Optional[bool] = None
    description: OptLongText = None
    bounds: Optional[Bounds] = None
    max_bytes: Optional[int] = Field(default=None, ge=MIN_FEED_BYTES, le=MAX_FEED_BYTES)


class Stop(_Strict):
    name: Name
    lat: Lat
    lon: Lon


class HoursRange(_Strict):
    start: Hhmm
    end: Hhmm


class Hours(_Strict):
    weekday: Optional[HoursRange] = None
    weekend: Optional[HoursRange] = None


class StaticService(_Strict):
    id: ItemId
    name: Name
    frequency_minutes: int = Field(ge=1, le=1440)
    stops: List[Stop] = Field(max_length=MAX_STOPS)
    type: TypeWord = None
    free: Optional[bool] = None
    hours: Optional[Hours] = None
    agency_name: OptText = None
    description: OptLongText = None


SOURCE_TYPES = ("link_scan", "event_cards", "tribe_events_api", "squarespace_eventlist")


class Source(_Strict):
    id: ItemId
    name: Name
    type: Literal["link_scan", "event_cards", "tribe_events_api", "squarespace_eventlist"]
    url: FetchUrl
    base_url: OptFetchUrl = None
    link_path: LinkPath = None
    default_location: OptText = None
    default_address: OptText = None
    default_city: OptText = None
    default_state: OptText = None
    is_free: Optional[bool] = None
    category: OptText = None
    location_match: TextList = None
    regions: TextList = None
    max_bytes: Optional[int] = Field(default=None, ge=MIN_FEED_BYTES, le=MAX_FEED_BYTES)


def _unique_ids(items: Optional[list]) -> Optional[list]:
    if items is None:
        return None
    ids = [getattr(i, "id", None) for i in items]
    if len(ids) != len(set(ids)):
        raise _err("duplicate_id")
    return items


# ---------------------------------------------------------------------------
# Section models: every field Optional, extra forbidden
# ---------------------------------------------------------------------------

class LocationSection(_Strict):
    """Read-only: built by admin-backend from the Base Knowledge location."""
    city: OptText = None
    state: OptText = None
    text: OptLongText = None


class AmtrakSection(_Strict):
    default_station: Station = None


class TransitSection(_Strict):
    region_name: OptText = None
    gtfs_feeds: Optional[List[Feed]] = Field(default=None, max_length=MAX_FEEDS)
    static_services: Optional[List[StaticService]] = Field(default=None, max_length=MAX_STATIC_SERVICES)

    _feed_ids = field_validator("gtfs_feeds", "static_services", mode="after")(_unique_ids)


class CommunityEventsSection(_Strict):
    sources: Optional[List[Source]] = Field(default=None, max_length=MAX_SOURCES)

    _source_ids = field_validator("sources", mode="after")(_unique_ids)


class GatewaySection(_Strict):
    fallback_model: ModelName = None


class SmsSection(_Strict):
    default_country_code: CountryCode = None


class LightsSection(_Strict):
    room_light_exclude: Optional[List[Glob]] = Field(default=None, max_length=MAX_GLOBS)


class BedWarmerSection(_Strict):
    level_left: BedSelect = None
    level_right: BedSelect = None
    power_main: BedSwitch = None
    power_side_a: BedSwitch = None
    power_side_b: BedSwitch = None


class JarvisWebSection(_Strict):
    default_room: Room = None
    climate_entity: ClimateEntity = None
    min_temp: Optional[int] = Field(default=None, ge=40, le=95)
    max_temp: Optional[int] = Field(default=None, ge=40, le=95)
    oven_entity: ApplianceEntity = None
    fridge_entity: ApplianceEntity = None
    freezer_entity: ApplianceEntity = None
    stove_cook_mode_sensor: SensorEntity = None
    stove_display_temp_sensor: SensorEntity = None
    stove_timer_sensor: SensorEntity = None
    fridge_door_sensor: DoorSensorEntity = None

    @field_validator("max_temp", mode="after")
    @classmethod
    def _max_not_below_min(cls, value: Optional[int], info) -> Optional[int]:
        low = info.data.get("min_temp")
        if value is not None and low is not None and value < low:
            raise _err("temperature_order")
        return value

    @model_validator(mode="after")
    def _both_bounds_or_neither(self) -> "JarvisWebSection":
        if (self.min_temp is None) != (self.max_temp is None):
            raise _err("temperature_pair_incomplete")
        return self


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SectionSpec:
    name: str
    key: str
    model: Type[BaseModel]
    writable: bool
    required_permission: SectionPermission
    list_fields: Tuple[str, ...] = ()
    units: Tuple[Tuple[str, ...], ...] = ()
    audit: AuditMode = "values"
    audit_redact: Tuple[str, ...] = ()


def _spec(name: str, model: Type[BaseModel], *, writable: bool = True,
          permission: SectionPermission = "write", list_fields: Tuple[str, ...] = (),
          units: Tuple[Tuple[str, ...], ...] = (), audit: AuditMode = "values",
          audit_redact: Tuple[str, ...] = ()) -> SectionSpec:
    if not re.fullmatch(r"[a-z_]+", name):
        raise ValueError("section names are lowercase letters and underscores")
    return SectionSpec(name, KEY_PREFIX + name, model, writable, permission, list_fields, units, audit, audit_redact)


_BED_FIELDS = ("level_left", "level_right", "power_main", "power_side_a", "power_side_b")

_REGISTRY: Tuple[SectionSpec, ...] = (
    _spec("location", LocationSection, writable=False, audit="fields"),
    _spec("amtrak", AmtrakSection, audit="fields"),
    _spec("transit", TransitSection, list_fields=("gtfs_feeds", "static_services"), audit="hosts"),
    _spec("community_events", CommunityEventsSection, list_fields=("sources",), audit="hosts"),
    _spec("gateway", GatewaySection),
    _spec("sms", SmsSection, permission="manage_infrastructure"),
    _spec("lights", LightsSection, permission="manage_infrastructure", list_fields=("room_light_exclude",)),
    _spec("bed_warmer", BedWarmerSection, permission="manage_infrastructure", units=(_BED_FIELDS,)),
    _spec("jarvis_web", JarvisWebSection, permission="manage_infrastructure",
          units=(("min_temp", "max_temp"),), audit_redact=("default_room",)),
)

SECTIONS: Mapping[str, SectionSpec] = MappingProxyType({s.name: s for s in _REGISTRY})
assert len(SECTIONS) == len(_REGISTRY), "duplicate section name"

# Fields that hold a URL a user must never read in full (D9).
_URL_FIELDS: Mapping[str, Mapping[str, Tuple[str, ...]]] = MappingProxyType({
    "transit": {"gtfs_feeds": ("url",)},
    "community_events": {"sources": ("url", "base_url")},
})


# ---------------------------------------------------------------------------
# Stored form
# ---------------------------------------------------------------------------

def _clean_loc(loc: Tuple[Any, ...]) -> Tuple[Any, ...]:
    return tuple(p[:64] if isinstance(p, str) else p for p in loc)


def _errors_of(exc: ValidationError) -> List[Dict[str, Any]]:
    return [
        {"loc": _clean_loc(tuple(e["loc"])), "type": e["type"]}
        for e in exc.errors(include_input=False, include_url=False, include_context=False)
    ]


def _dump(model: Optional[BaseModel], spec: SectionSpec) -> Dict[str, Any]:
    if model is None:
        return {name: None for name in spec.model.model_fields}
    return model.model_dump(mode="json")


def dump(name: str, model: Optional[BaseModel]) -> Optional[Dict[str, Any]]:
    """A section's value as plain JSON (URLs whole); ``None`` when nothing is stored."""
    return None if model is None else model.model_dump(mode="json")


def stored_flags(name: str, model: Optional[BaseModel]) -> Dict[str, bool]:
    """Which fields hold a value (not ``null``) in the stored section."""
    spec = SECTIONS[name]
    return {f: model is not None and getattr(model, f) is not None for f in spec.model.model_fields}


def stored_value(name: str, model: BaseModel, revision: int) -> str:
    """The text written to ``system_settings.value``."""
    return json.dumps(
        {"v": STORED_VERSION, "revision": revision, "value": model.model_dump(mode="json")},
        ensure_ascii=False, separators=(",", ":"),
    )


def _dedupe_ids(section: str, field_name: str, items: list) -> list:
    seen = set()
    kept = []
    for index, item in enumerate(items):
        ident = item.get("id") if isinstance(item, dict) else None
        if ident is not None and ident in seen:
            logger.error("household_setting_item_dropped section=%s field=%s index=%d reason=duplicate_id",
                         section, field_name, index)
            continue
        seen.add(ident)
        kept.append(item)
    return kept


def parse_value(name: str, value: Mapping[str, Any]) -> BaseModel:
    """Tolerant read of a section's value: an invalid field becomes ``None``
    (logged by field name), invalid list items are dropped by index, unknown
    fields are ignored. Never raises."""
    spec = SECTIONS[name]
    model = spec.model
    data: Dict[str, Any] = {}
    for key, item in dict(value).items():
        if key in model.model_fields:
            data[key] = item
        else:
            logger.error("household_setting_field_unknown section=%s", name)
    for list_field in spec.list_fields:
        if isinstance(data.get(list_field), list):
            data[list_field] = _dedupe_ids(name, list_field, data[list_field])
    for _ in range(200):
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            changed = False
            drops: Dict[str, set] = {}
            for err in exc.errors(include_input=False, include_url=False, include_context=False):
                loc = err["loc"]
                if not loc:
                    for group in spec.units:
                        for unit_field in group:
                            if data.get(unit_field) is not None:
                                data[unit_field] = None
                                changed = True
                    logger.error("household_setting_unit_invalid section=%s", name)
                    continue
                field_name = loc[0]
                if len(loc) > 1 and isinstance(loc[1], int) and isinstance(data.get(field_name), list):
                    drops.setdefault(field_name, set()).add(loc[1])
                elif data.get(field_name) is not None:
                    data[field_name] = None
                    changed = True
                    logger.error("household_setting_field_invalid section=%s field=%s", name, field_name)
            for field_name, indexes in drops.items():
                for index in sorted(indexes):
                    logger.error("household_setting_item_dropped section=%s field=%s index=%d",
                                 name, field_name, index)
                data[field_name] = [x for i, x in enumerate(data[field_name]) if i not in indexes]
                changed = True
            if not changed:
                break
    logger.error("household_setting_unreadable section=%s", name)
    return model()


def parse_stored(name: str, raw: Optional[str]) -> Tuple[Optional[BaseModel], int]:
    """``(model, revision)`` from the stored text. Missing gives ``(None, 0)``.
    An unreadable blob gives ``(None, 0)``; an unknown ``v`` gives
    ``(None, revision)``; both log an ERROR with no content."""
    if raw is None:
        return None, 0
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.error("household_setting_unreadable section=%s length=%d", name,
                     len(raw) if isinstance(raw, str) else 0)
        return None, 0
    if not isinstance(data, dict):
        logger.error("household_setting_unreadable section=%s length=%d", name, len(raw))
        return None, 0
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        revision = 0
    if data.get("v") != STORED_VERSION:
        logger.error("household_setting_unknown_version section=%s", name)
        return None, revision
    value = data.get("value")
    if not isinstance(value, dict):
        return None, revision
    return parse_value(name, value), revision


# ---------------------------------------------------------------------------
# Writing: validation, masking, audit
# ---------------------------------------------------------------------------

def validate_update(name: str, value: Any) -> BaseModel:
    """The validated model for a submitted section value. Raises
    ``SettingsValidationError``; the errors carry ``loc`` and ``type`` only."""
    spec = SECTIONS.get(name)
    if spec is None:
        raise SettingsValidationError([{"loc": ("section",), "type": "unknown_section"}])
    if not isinstance(value, dict):
        raise SettingsValidationError([{"loc": (), "type": "dict_type"}])
    oversized = [
        {"loc": _clean_loc((key,)), "type": "too_long"}
        for key, item in value.items() if isinstance(item, list) and len(item) > _HARD_LIST_LIMIT
    ]
    if oversized:
        raise SettingsValidationError(oversized)
    try:
        return spec.model.model_validate(value)
    except ValidationError as exc:
        raise SettingsValidationError(_errors_of(exc)) from None


def mask_for_user(name: str, model: Optional[BaseModel]) -> Optional[Dict[str, Any]]:
    """A section's value for a signed-in user: every URL shown as scheme and
    host, with ``<field>_masked: true`` beside it (D9)."""
    if model is None:
        return None
    out = model.model_dump(mode="json")
    for list_field, url_keys in _URL_FIELDS.get(name, {}).items():
        for item in out.get(list_field) or []:
            for key in url_keys:
                if item.get(key):
                    item[key] = mask_url(item[key])
                    item[f"{key}_masked"] = True
    return out


def merge_masked(name: str, submitted: Any, stored: Optional[BaseModel]) -> Any:
    """Resolve the masked URLs in a submitted value. An item carrying
    ``<field>_masked: true`` keeps the stored URL when the submitted URL is
    still exactly the masked form for the same item ``id``; any other use of
    the mask is refused (``url_masked_edit``). A new full URL passes through."""
    url_fields = _URL_FIELDS.get(name)
    if not url_fields or not isinstance(submitted, dict):
        return submitted
    out = dict(submitted)
    errors: List[Dict[str, Any]] = []
    for list_field, url_keys in url_fields.items():
        items = out.get(list_field)
        if not isinstance(items, list):
            continue
        stored_by_id = {getattr(i, "id", None): i for i in (getattr(stored, list_field, None) or [])}
        merged = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                merged.append(item)
                continue
            item = dict(item)
            for key in url_keys:
                flag = item.pop(f"{key}_masked", None)
                current = item.get(key)
                loc = (list_field, index, key)
                if flag is True:
                    old = getattr(stored_by_id.get(item.get("id")), key, None)
                    if old and isinstance(current, str) and current == mask_url(old):
                        item[key] = old
                    else:
                        errors.append({"loc": loc, "type": "url_masked_edit"})
                elif flag not in (None, False):
                    errors.append({"loc": loc, "type": "bool_type"})
                elif isinstance(current, str) and MASK_TOKEN in current:
                    errors.append({"loc": loc, "type": "url_masked_edit"})
            merged.append(item)
        out[list_field] = merged
    if errors:
        raise SettingsValidationError(errors)
    return out


def _hosts_of(name: str, data: Dict[str, Any]) -> set:
    hosts = set()
    for list_field, url_keys in _URL_FIELDS.get(name, {}).items():
        for item in data.get(list_field) or []:
            for key in url_keys:
                if item.get(key):
                    hosts.add(url_host(item[key]))
    return hosts


def audit_payload(name: str, old: Optional[BaseModel], new: BaseModel) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """``(old_value, new_value)`` for the audit row of a committed save (D18).
    ``values`` sections carry the values (redacted fields appear only as
    changed); ``fields`` sections carry changed field names; ``hosts`` sections
    carry names, counts and added/removed hosts, never a URL, path or name."""
    spec = SECTIONS[name]
    old_d, new_d = _dump(old, spec), _dump(new, spec)
    changed = sorted(f for f in spec.model.model_fields if old_d.get(f) != new_d.get(f))
    if spec.audit == "values":
        keep = [f for f in spec.model.model_fields if f not in spec.audit_redact]
        return (
            {"section": name, **{f: old_d.get(f) for f in keep}},
            {"section": name, **{f: new_d.get(f) for f in keep}, "changed_fields": changed},
        )
    if spec.audit == "fields":
        return {"section": name}, {"section": name, "changed_fields": changed}
    old_hosts, new_hosts = _hosts_of(name, old_d), _hosts_of(name, new_d)
    counts_old = {f: len(old_d.get(f) or []) for f in spec.list_fields}
    counts_new = {f: len(new_d.get(f) or []) for f in spec.list_fields}
    return (
        {"section": name, "counts": counts_old},
        {
            "section": name, "changed_fields": changed, "counts": counts_new,
            "hosts_added": sorted(new_hosts - old_hosts), "hosts_removed": sorted(old_hosts - new_hosts),
        },
    )


# ---------------------------------------------------------------------------
# Value objects shared by the resolvers
# ---------------------------------------------------------------------------

class SourcedStr(str):
    """A string that remembers where it came from: ``"admin"``, ``"env"`` or ``"default"``."""

    __slots__ = ("source",)

    def __new__(cls, value: str, source: str) -> "SourcedStr":
        obj = str.__new__(cls, value)
        object.__setattr__(obj, "source", source)
        return obj

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("SourcedStr is immutable")


@dataclass(frozen=True)
class Location:
    city: str
    state: str
    text: str
    source: str


SOURCE_ADMIN, SOURCE_ENV, SOURCE_DEFAULT = "admin", "env", "default"


def combine_sources(sources: Mapping[str, str]) -> str:
    values = set(sources.values())
    if SOURCE_ADMIN in values:
        return SOURCE_ADMIN
    return SOURCE_ENV if SOURCE_ENV in values else SOURCE_DEFAULT


@dataclass(frozen=True)
class JarvisWeb:
    default_room: str
    climate_entity: str
    min_temp: int
    max_temp: int
    oven_entity: str
    fridge_entity: str
    freezer_entity: str
    stove_cook_mode_sensor: str
    stove_display_temp_sensor: str
    stove_timer_sensor: str
    fridge_door_sensor: str
    sources: Mapping[str, str]

    @property
    def source(self) -> str:
        return combine_sources(self.sources)


_JARVIS_ENV: Mapping[str, Tuple[str, Any]] = MappingProxyType({
    "default_room": ("DEFAULT_ROOM", "guest"),
    "climate_entity": ("CLIMATE_ENTITY", "climate.thermostat"),
    "oven_entity": ("OVEN_ENTITY_ID", ""),
    "fridge_entity": ("FRIDGE_ENTITY_ID", ""),
    "freezer_entity": ("FREEZER_ENTITY_ID", ""),
    "stove_cook_mode_sensor": ("STOVE_COOK_MODE_SENSOR_ID", ""),
    "stove_display_temp_sensor": ("STOVE_DISPLAY_TEMP_SENSOR_ID", ""),
    "stove_timer_sensor": ("STOVE_TIMER_SENSOR_ID", ""),
    "fridge_door_sensor": ("FRIDGE_DOOR_SENSOR_ID", ""),
})
_JARVIS_DEFAULT_TEMPS = (65, 75)


def resolve_jarvis_web(section: Optional[JarvisWebSection], env: Mapping[str, str]) -> JarvisWeb:
    """Admin value, then ``env``, then today's default, per field; the
    thermostat bounds resolve as a unit (both stored, or both from env/default).
    Pure so jarvis-web (no ``shared``) can use this file's copy."""
    values: Dict[str, Any] = {}
    sources: Dict[str, str] = {}
    for fname, (env_name, default) in _JARVIS_ENV.items():
        admin_value = getattr(section, fname, None) if section is not None else None
        env_value = env.get(env_name, "")
        if admin_value is not None:
            values[fname], sources[fname] = admin_value, SOURCE_ADMIN
        elif env_value:
            values[fname], sources[fname] = env_value, SOURCE_ENV
        else:
            values[fname], sources[fname] = default, SOURCE_DEFAULT
    if section is not None and section.min_temp is not None and section.max_temp is not None:
        values["min_temp"], values["max_temp"] = section.min_temp, section.max_temp
        sources["min_temp"] = sources["max_temp"] = SOURCE_ADMIN
    else:
        try:
            env_pair = [env.get("MIN_TEMP", ""), env.get("MAX_TEMP", "")]
            low = int(env_pair[0]) if env_pair[0] else None
            high = int(env_pair[1]) if env_pair[1] else None
        except ValueError:
            low = high = None
        low = _JARVIS_DEFAULT_TEMPS[0] if low is None else low
        high = _JARVIS_DEFAULT_TEMPS[1] if high is None else high
        values["min_temp"], values["max_temp"] = low, high
        from_env = bool(env.get("MIN_TEMP") or env.get("MAX_TEMP"))
        sources["min_temp"] = sources["max_temp"] = SOURCE_ENV if from_env else SOURCE_DEFAULT
    return JarvisWeb(sources=MappingProxyType(sources), **values)


# ---------------------------------------------------------------------------
# The cached reader
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    """What admin-backend last said. ``sections`` holds stored sections only;
    a section missing from the response means "admin has no value".
    ``loaded_at`` is the cache clock at the successful fetch (0.0 = never)."""
    sections: Mapping[str, BaseModel] = field(default_factory=lambda: MappingProxyType({}))
    revisions: Mapping[str, int] = field(default_factory=lambda: MappingProxyType({}))
    loaded: bool = False
    loaded_at: float = 0.0


def snapshot_from_body(body: Mapping[str, Any], loaded_at: float = 0.0) -> Snapshot:
    """Build a snapshot from the GET body (unknown section names are ignored)."""
    sections: Dict[str, BaseModel] = {}
    revisions: Dict[str, int] = {}
    for name, entry in body.items():
        if name not in SECTIONS or not isinstance(entry, dict):
            continue
        revision = entry.get("revision")
        revisions[name] = revision if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0 else 0
        value = entry.get("value")
        if isinstance(value, dict):
            sections[name] = parse_value(name, value)
    return Snapshot(MappingProxyType(sections), MappingProxyType(revisions), True, loaded_at)


Fetch = Callable[[], Awaitable[Optional[Dict[str, Any]]]]
Spawn = Callable[[Awaitable[Any]], Any]


class HouseholdSettingsCache:
    """One cached snapshot per process.

    ``await current()`` refreshes when stale (``ttl`` seconds), with at most one
    fetch in flight; a caller that finds a fetch in flight returns the snapshot
    it has instead of waiting. A failed fetch keeps the last good snapshot and
    stamps the attempt time, so no caller queues behind an outage. ``peek()``
    returns the snapshot in O(1) and schedules at most one background refresh
    through ``spawn`` (never from outside a running loop, never after
    ``aclose``)."""

    def __init__(self, fetch: Fetch, *, ttl: float = 60.0, spawn: Optional[Spawn] = None,
                 clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self._ttl = ttl
        self._spawn = spawn
        self._clock = clock
        self._snapshot = Snapshot()
        self._checked_at: Optional[float] = None
        self._locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = weakref.WeakKeyDictionary()
        self._fetching = False
        self._scheduled_loop: Optional["weakref.ref[asyncio.AbstractEventLoop]"] = None
        self._task: Optional["asyncio.Future[Any]"] = None
        self._closed = False
        self._pinned = False

    def preload(self, snapshot: Snapshot) -> None:
        """Test seam: serve ``snapshot`` as fresh forever (no fetch is ever made)."""
        self._snapshot = snapshot
        self._pinned = True

    # -- state -----------------------------------------------------------------------------

    def _fresh(self) -> bool:
        if self._pinned:
            return True
        return self._checked_at is not None and self._clock() - self._checked_at < self._ttl

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    # -- reading ---------------------------------------------------------------------------

    async def current(self) -> Snapshot:
        if self._closed or self._fresh():
            return self._snapshot
        lock = self._lock()
        if lock.locked() or self._fetching:
            return self._snapshot
        async with lock:
            if self._closed or self._fresh():
                return self._snapshot
            self._fetching = True
            try:
                try:
                    body = await self._fetch()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("household_settings_fetch_error error_type=%s", type(exc).__name__)
                    body = None
                self._checked_at = self._clock()
                if isinstance(body, dict):
                    self._snapshot = snapshot_from_body(body, self._checked_at)
                    logger.info("household_settings_loaded sections=%d", len(self._snapshot.sections))
                elif body is not None:
                    logger.warning("household_settings_unexpected_body")
            finally:
                self._fetching = False
        return self._snapshot

    def peek(self) -> Snapshot:
        if self._closed or self._fresh() or self._fetching:
            return self._snapshot
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return self._snapshot
        pending = self._scheduled_loop() if self._scheduled_loop is not None else None
        if pending is loop:
            return self._snapshot
        self._scheduled_loop = weakref.ref(loop)
        coro = self._refresh()
        try:
            task = self._spawn(coro) if self._spawn is not None else loop.create_task(coro)
        except Exception:
            coro.close()
            self._scheduled_loop = None
            logger.warning("household_settings_spawn_failed")
            return self._snapshot
        self._task = task if isinstance(task, asyncio.Future) else None
        return self._snapshot

    async def _refresh(self) -> None:
        try:
            await self.current()
        finally:
            self._scheduled_loop = None
            self._task = None

    async def aclose(self) -> None:
        """Stop for good: cancel an in-flight background refresh; later ``peek``
        calls schedule nothing and ``current`` returns what it has."""
        self._closed = True
        task, self._task = self._task, None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
