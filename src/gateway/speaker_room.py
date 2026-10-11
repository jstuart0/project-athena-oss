"""Which room the speaker is in, as far as the gateway can tell.

The gateway learns a Home Assistant device id from the request (the
`athena_device_id:` line the HA conversation agent puts in its prompt, or
`/ha/conversation`'s `device_id`) and turns it into a room key through the
area registry. A device id is believed only from a *trusted voice caller*:

- a request that presented the gateway's own API key (when one is set), or
- a client whose resolved address is in `HA_DEVICE_CLIENT_NETWORKS`.

Anything else gets no room from a device id, no follow-up surface, and no
registry wait. The one-active-satellite guess is also limited to trusted
callers and only ever names the speaker's room; it can never choose the whole
house.

Logs carry the source and timing; a room key is logged only at DEBUG and a
device id never.
"""
from __future__ import annotations

import hmac
import re
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import structlog

from shared.client_throttle import (
    IPNetwork,
    in_networks,
    parse_ip,
    read_forwarded_for,
    resolve_rate_client,
)
from shared.ha_registry import SatelliteRoomMap, room_key

logger = structlog.get_logger(__name__)

DEVICE_ID_RE = re.compile(r"[0-9a-f]{32}")
MARKER_NAME = "athena_device_id:"
_MARKER_LINE = re.compile(r"[ \t]*athena_device_id:[ \t]*([0-9a-f]{32})[ \t]*")
MARKER_SCAN_BYTES = 64 * 1024
MARKER_ROLES = frozenset({"system", "developer"})

NEGATIVE_CACHE_SECONDS = 60.0
UNKNOWN_CACHE_MAX = 1000
COLD_ENSURE_SECONDS = 1.0
UNKNOWN_DEVICE_ENSURE_SECONDS = 0.5
DUMMY_KEY = "dummy-key"


@dataclass(frozen=True)
class SpeakerRoom:
    """``key`` is the room key, or None when the speaker's room is unknown.
    ``source``: ``map``, ``area``, ``guess`` or ``none``."""
    key: Optional[str]
    source: str


UNKNOWN_ROOM = SpeakerRoom(None, "none")


# ---------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------

def trusted_voice_caller(
    raw_request: Any,
    *,
    api_key: Optional[str],
    device_networks: Sequence[IPNetwork],
    trusted_proxies: Sequence[IPNetwork],
) -> bool:
    """Whether a device id on this request may be believed.

    (a) the gateway's API key is set (not empty, not ``dummy-key``) and the
    request presented it as a Bearer token, compared in constant time; or
    (b) the client address, resolved the way the new-conversation limiter
    resolves it (nearest ``X-Forwarded-For`` hop outside the trusted proxies),
    is in ``device_networks``.
    """
    if api_key and api_key != DUMMY_KEY:
        header = raw_request.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(
            header[len("Bearer "):].encode(), api_key.encode()
        ):
            return True
    if device_networks:
        peer = raw_request.client.host if getattr(raw_request, "client", None) else None
        client = resolve_rate_client(
            peer, read_forwarded_for(raw_request.headers), None, trusted_proxies, trust_cf=False
        )
        return in_networks(parse_ip(client.ip), device_networks)
    return False


# ---------------------------------------------------------------------------
# The prompt marker
# ---------------------------------------------------------------------------

def _get(message: Any, name: str) -> Any:
    return message.get(name) if isinstance(message, dict) else getattr(message, name, None)


def _with_content(message: Any, content: str) -> Any:
    if isinstance(message, dict):
        return {**message, "content": content}
    return message.model_copy(update={"content": content})


def _strip_marker_lines(text: str, budget: List[int]) -> Tuple[str, List[str]]:
    """The text without its marker lines, and the marker lines found inside
    the scan budget (bytes still to scan, shared across texts). Marker lines
    past the budget are removed but not honoured."""
    kept: List[str] = []
    found: List[str] = []
    changed = False
    for line in text.split("\n"):
        budget[0] -= len(line) + 1
        if line.lstrip(" \t").startswith(MARKER_NAME):
            changed = True
            if budget[0] >= 0:
                found.append(line)
            continue
        kept.append(line)
    return ("\n".join(kept) if changed else text), found


def extract_device_marker(
    messages: Sequence[Any], instructions: Optional[str] = None,
) -> Tuple[Optional[str], List[Any], Optional[str], bool]:
    """Read the `athena_device_id: <32 hex>` line from the system and
    developer messages (and the Responses API `instructions`) only.

    Exactly one marker line in all of them gives a device id; none, several,
    or a malformed value (uppercase, short, long) gives None. The last item
    is True when a marker line was present but gave no id (several, or
    malformed): a rejected marker, which must not fall back to guessing the
    room from the active satellite. Every marker
    line is removed from the returned messages either way, so it never
    reaches the orchestrator's prompt. User, assistant and tool messages are
    never read or changed. At most 64 KiB is scanned.
    """
    budget = [MARKER_SCAN_BYTES]
    found: List[str] = []
    cleaned: List[Any] = []
    for message in messages:
        content = _get(message, "content")
        if _get(message, "role") in MARKER_ROLES and isinstance(content, str):
            text, lines = _strip_marker_lines(content, budget)
            found.extend(lines)
            cleaned.append(message if text is content else _with_content(message, text))
        else:
            cleaned.append(message)
    cleaned_instructions = instructions
    if isinstance(instructions, str):
        cleaned_instructions, lines = _strip_marker_lines(instructions, budget)
        found.extend(lines)
    device_id = None
    if len(found) == 1:
        match = _MARKER_LINE.fullmatch(found[0])
        device_id = match.group(1) if match else None
    return device_id, cleaned, cleaned_instructions, bool(found) and device_id is None


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------

_unknown_until: Dict[str, float] = {}


def reset_for_test() -> None:
    _unknown_until.clear()


def _remember_unknown(device_id: str, now: float) -> None:
    """Remember an unknown device id for NEGATIVE_CACHE_SECONDS. Expired
    entries are dropped first; at the cap the entry closest to expiry goes, so
    a flood of made-up ids cannot grow the table."""
    if len(_unknown_until) >= UNKNOWN_CACHE_MAX:
        for key in [k for k, until in _unknown_until.items() if until <= now]:
            del _unknown_until[key]
    if len(_unknown_until) >= UNKNOWN_CACHE_MAX:
        del _unknown_until[min(_unknown_until, key=_unknown_until.get)]
    _unknown_until[device_id] = now + NEGATIVE_CACHE_SECONDS


ReadStates = Callable[[], Awaitable[Iterable[Dict[str, Any]]]]


def _log(source: str, area_known: bool, started: float) -> None:
    logger.info(
        "speaker_room_resolved",
        source=source,
        area_known=area_known,
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


async def resolve_speaker_room(
    *,
    device_id: Optional[str],
    trusted: bool,
    registry: Any,
    satellite_map: SatelliteRoomMap = SatelliteRoomMap(),
    read_states: Optional[ReadStates] = None,
    marker_rejected: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
) -> SpeakerRoom:
    """The speaker's room, in order:

    1. ``HA_SATELLITE_ROOM_MAP``, by the device id or by the satellite
       entities on that device (or its children);
    2. the registry's area for the device id. An id the registry doesn't know
       waits once (0.5 s) for a forced refresh and is then remembered as
       unknown for 60 s; the first request before any snapshot waits up to
       1 s;
    3. the guess: exactly one assist satellite is not idle (trusted callers
       only, only when no device id was given, and never after a marker was
       present but rejected);
    4. unknown.

    A device id from a caller that isn't trusted is ignored entirely: no
    room, no registry wait.
    """
    started = time.monotonic()
    valid_id = device_id if trusted and device_id and DEVICE_ID_RE.fullmatch(device_id) else None

    if valid_id is not None:
        mapped = satellite_map.device_rooms.get(valid_id)
        snapshot = registry.current() if registry is not None else None
        if mapped is None and snapshot is not None:
            for entity in snapshot.satellite_entities(valid_id):
                mapped = satellite_map.entity_rooms.get(entity)
                if mapped:
                    break
        if mapped:
            _log("map", False, started)
            return SpeakerRoom(room_key(mapped), "map")

        if registry is not None:
            if snapshot is None:
                snapshot = await registry.ensure(COLD_ENSURE_SECONDS)
            elif registry.stale:
                registry.request_refresh()
            if snapshot is not None and not snapshot.knows_device(valid_id):
                if _unknown_until.get(valid_id, 0.0) <= monotonic():
                    _remember_unknown(valid_id, monotonic())
                    snapshot = await registry.ensure(UNKNOWN_DEVICE_ENSURE_SECONDS, refresh=True)
            if snapshot is not None:
                area = snapshot.area_for(valid_id)
                if area is not None:
                    _log("area", True, started)
                    return SpeakerRoom(area.key, "area")
                for entity in snapshot.satellite_entities(valid_id):
                    area = snapshot.area_for(entity)
                    if area is not None:
                        _log("area", True, started)
                        return SpeakerRoom(area.key, "area")

    if trusted and valid_id is None and not marker_rejected and read_states is not None:
        guessed = await _guess_from_active_satellite(read_states, registry, satellite_map)
        if guessed is not None:
            _log("guess", False, started)
            return SpeakerRoom(guessed, "guess")

    _log("none", False, started)
    return UNKNOWN_ROOM


async def _guess_from_active_satellite(
    read_states: ReadStates, registry: Any, satellite_map: SatelliteRoomMap,
) -> Optional[str]:
    try:
        states = list(await read_states())
    except Exception as exc:
        logger.warning("speaker_room_states_failed", error_class=type(exc).__name__)
        return None
    active = [
        s.get("entity_id", "") for s in states
        if str(s.get("entity_id", "")).startswith("assist_satellite.") and s.get("state") not in ("idle", None)
    ]
    if len(active) != 1:
        return None
    entity = active[0]
    mapped = satellite_map.entity_rooms.get(entity)
    if mapped:
        return room_key(mapped)
    snapshot = registry.current() if registry is not None else None
    area = snapshot.area_for(entity) if snapshot is not None else None
    return area.key if area is not None else None
