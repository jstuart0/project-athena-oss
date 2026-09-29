"""ATHENA-127: shared stay-window math for admin-backend and the mode service.

Hides three things behind one small surface: timezone/DST correctness for a
feed or DB instant, block-vs-stay classification, and the active/merge/
suppression logic both services need to agree on. Neither side should
duplicate this math -- see the plan's "Pattern parity" section.

Implementation constraint (D5): DST folding is done by constructing
``datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(name))`` directly and converting
with ``.astimezone(timezone.utc)``. No pytz, no ``localize()``, no
normalize round-trip -- those give different answers for a nonexistent
(spring-forward gap) or ambiguous (fall-back) local time than PEP 495
``fold=0`` (the default), which is what every helper below relies on.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo as TZInfo
from typing import Iterable, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

logger = structlog.get_logger()

DEFAULT_CHECKIN_TIME = "16:00"
DEFAULT_CHECKOUT_TIME = "11:00"

# D11: markers recognised as a feed block rather than a real stay, drawn
# from the guest-name heuristics already in calendar_sources.py.
BLOCK_SUMMARY_MARKERS = ("blocked", "closed period", "not available", "unavailable")

MIN_BUFFER_HOURS = 0
MAX_BUFFER_HOURS = 168

# These helpers run on every /mode read, so their warnings fire once per
# process per distinct (event, value) rather than on every call (D5).
# Bounded: once full it starts over, so memory stays capped and a value
# seen again after a reset logs once more.
_LOG_LATCH_CAP = 1024
_logged_once: set[tuple] = set()


def _log_once(level: str, event: str, value, **fields) -> None:
    marker = (event, value)
    if marker in _logged_once:
        return
    if len(_logged_once) >= _LOG_LATCH_CAP:
        _logged_once.clear()
    _logged_once.add(marker)
    getattr(logger, level)(event, **fields)


def _reset_log_latches_for_tests() -> None:
    _logged_once.clear()


def booking_key(source: str, external_id: str) -> str:
    """D3: the opaque booking key both services put on the wire. A hash, so
    no provider UID (which can embed a guest email or token) is exposed."""
    return hashlib.sha256(f"{source}|{external_id}".encode()).hexdigest()[:16]


def resolve_property_tz(name: Optional[str]) -> tuple[TZInfo, bool]:
    """Resolve an IANA zone name to a tzinfo. Empty/unknown -> UTC, plus one
    ERROR log, and a False validity flag the caller can surface as
    ``property_timezone_valid``.
    """
    if not name:
        _log_once("error", "booking_timezone_invalid", name, name=name)
        return timezone.utc, False
    try:
        return ZoneInfo(name), True
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        _log_once("error", "booking_timezone_invalid", name, name=name)
        return timezone.utc, False


def _parse_hhmm(hhmm: str) -> tuple[int, int]:
    try:
        hour_str, minute_str = hhmm.split(":")
        return int(hour_str), int(minute_str)
    except (ValueError, AttributeError):
        return 16, 0


def localize_local(d: date, hhmm: str, tz: TZInfo) -> datetime:
    """A date + "HH:MM" in the property zone -> aware UTC datetime."""
    hour, minute = _parse_hhmm(hhmm)
    local_dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def localize_stay(
    arrival: date,
    departure: date,
    checkin_hhmm: str,
    checkout_hhmm: str,
    tz: TZInfo,
) -> tuple[datetime, datetime]:
    return (
        localize_local(arrival, checkin_hhmm, tz),
        localize_local(departure, checkout_hhmm, tz),
    )


def feed_value_to_utc(v, *, default_hhmm: str, tz: TZInfo) -> datetime:
    """A raw feed value (date-only, floating DATE-TIME, or an already-aware
    DATE-TIME with a TZID/Z) -> aware UTC datetime (D5)."""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            # Floating DATE-TIME from a feed: RFC 5545 SS3.3.5 -- local time
            # in the property zone, not UTC.
            local_dt = datetime(v.year, v.month, v.day, v.hour, v.minute, v.second, tzinfo=tz)
            return local_dt.astimezone(timezone.utc)
        return v.astimezone(timezone.utc)
    # date-only value.
    return localize_local(v, default_hhmm, tz)


def db_value_to_utc(v: datetime) -> datetime:
    """A value read back from the DB (SQLite tests may hand back a naive
    datetime) -> aware UTC. A naive value here is UTC by construction: every
    write path stores real UTC instants (D4)."""
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v.astimezone(timezone.utc)


def classify_summary(summary: str) -> Literal["blocked", "confirmed"]:
    """D11: case-insensitive, trimmed, substring match against
    BLOCK_SUMMARY_MARKERS."""
    normalized = (summary or "").strip().lower()
    for marker in BLOCK_SUMMARY_MARKERS:
        if marker in normalized:
            return "blocked"
    return "confirmed"


def clamp_buffer_hours(hours) -> float:
    """Clamp an operator-configured buffer to [0, 168] hours (D5), logging
    once per distinct raw value that needed clamping. Used at read time for
    both is_active/active_booking and the D6 fetch-window formula, so a
    raw out-of-range buffer can't widen the fetch beyond what is_active
    itself will honour."""
    try:
        raw = float(hours)
    except (TypeError, ValueError):
        raw = 0.0
    clamped = min(max(raw, MIN_BUFFER_HOURS), MAX_BUFFER_HOURS)
    if clamped != raw:
        _log_once("warning", "mode_booking_buffer_clamped", raw, requested=raw, clamped=clamped)
    return clamped


@dataclass(frozen=True)
class Booking:
    id: Optional[int]
    key: str
    source: str
    label: str
    start: datetime
    end: datetime
    is_test: bool = False


def is_active(b: Booking, now: datetime, before: timedelta, after: timedelta) -> bool:
    """Half-open: ``checkin - before <= now < checkout + after``. A booking
    whose raw window is inverted (``end <= start``) is invalid, never
    active, and logged once per booking (D5)."""
    if b.end <= b.start:
        _log_once("warning", "mode_booking_invalid_window", (b.source, b.key), key=b.key, source=b.source)
        return False
    return (b.start - before) <= now < (b.end + after)


def active_booking(
    bookings: Iterable[Booking], now: datetime, before: timedelta, after: timedelta
) -> Optional[Booking]:
    """The active booking with the earliest checkout, breaking ties by key."""
    candidates = [b for b in bookings if is_active(b, now, before, after)]
    if not candidates:
        return None
    return min(candidates, key=lambda b: (b.end, b.key))


def stay_day_pair(b: Booking, tz: TZInfo) -> tuple[date, date]:
    """The (checkin, checkout) local-date pair in the property zone, used
    for the fallback dedupe/suppression key (D13)."""
    return (b.start.astimezone(tz).date(), b.end.astimezone(tz).date())


def _merge_two(a: Booking, b: Booking) -> Booking:
    """Union two bookings that represent the same stay: widest window,
    never-`ical` label preferred, is_test only if both are test rows."""
    non_ical = a if a.source != "ical" else (b if b.source != "ical" else a)
    return Booking(
        id=non_ical.id,
        key=non_ical.key,
        source=non_ical.source,
        label=non_ical.label,
        start=min(a.start, b.start),
        end=max(a.end, b.end),
        is_test=a.is_test and b.is_test,
    )


def merge(
    primary: list[Booking],
    advisory: list[Booking],
    *,
    suppressed_pairs: set[tuple[date, date]],
    tz: TZInfo,
) -> list[Booking]:
    """D13: primary dedupe on (source, key), then a fallback union on the
    local (checkin date, checkout date) pair. Suppression (soft-deleted or
    cancelled admin day pairs) is applied to `advisory` only -- an admin
    row is never suppressed by its own deletion status here, since the
    admin endpoint already excludes deleted/cancelled rows from `bookings`
    at the source."""
    suppressed_pairs = suppressed_pairs or set()
    filtered_advisory = [b for b in advisory if stay_day_pair(b, tz) not in suppressed_pairs]

    all_bookings = list(primary) + filtered_advisory

    by_source_key: dict[tuple[str, str], Booking] = {}
    key_order: list[tuple[str, str]] = []
    for b in all_bookings:
        sk = (b.source, b.key)
        if sk in by_source_key:
            by_source_key[sk] = _merge_two(by_source_key[sk], b)
        else:
            by_source_key[sk] = b
            key_order.append(sk)
    deduped = [by_source_key[sk] for sk in key_order]

    by_day_pair: dict[tuple[date, date], Booking] = {}
    pair_order: list[tuple[date, date]] = []
    for b in deduped:
        dp = stay_day_pair(b, tz)
        if dp in by_day_pair:
            by_day_pair[dp] = _merge_two(by_day_pair[dp], b)
        else:
            by_day_pair[dp] = b
            pair_order.append(dp)

    return [by_day_pair[dp] for dp in pair_order]
