"""The property's local clock, independent of the process ``TZ``.

Every prompt clock, "today", date window and "wait until" reads the time
through this module, in ``DEFAULT_TIMEZONE`` (``get_config().default_timezone``).
A pod whose process zone is UTC still gets the property's wall clock.

Invariants:

- Nothing here reads the process ``TZ``: every value is derived from an
  aware UTC instant converted into the configured zone.
- Every returned ``datetime`` is aware.
- Real elapsed time must be computed between UTC-converted values
  (``a.astimezone(timezone.utc) - b.astimezone(timezone.utc)``). Subtracting
  two aware values that share a ``ZoneInfo`` is wall-clock arithmetic in
  Python and is off by an hour across a DST change.

An empty or unknown zone falls back to UTC and logs one
``local_timezone_invalid`` ERROR per distinct name. The image must ship the
zone database (``tzdata``) for a named zone to resolve.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

from shared.config import get_config

logger = structlog.get_logger()

_invalid_zone_logged: set[str] = set()


def _utc_now() -> datetime:
    """Current instant; a module-level seam tests replace to freeze time."""
    return datetime.now(timezone.utc)


def local_tz() -> tzinfo:
    """The configured property zone, or UTC when it's empty or unknown."""
    name = (get_config().default_timezone or "").strip()
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    if name not in _invalid_zone_logged:
        _invalid_zone_logged.add(name)
        logger.error("local_timezone_invalid", timezone=name, fallback="UTC")
    return timezone.utc


def local_now() -> datetime:
    """The current time as an aware datetime in the property zone."""
    return _utc_now().astimezone(local_tz())


def local_today() -> date:
    """The property's current calendar date."""
    return local_now().date()


def local_day_bounds_utc(day: Optional[date] = None, days: int = 1) -> tuple[datetime, datetime]:
    """UTC instants of ``[local midnight of day, local midnight of day + days)``.

    ``day`` defaults to the property's today. A DST-change day spans 23 or
    25 hours, which is why the bounds are built from local midnights rather
    than by adding hours to a UTC value.
    """
    tz = local_tz()
    start_day = day if day is not None else local_today()
    start = datetime.combine(start_day, time(0), tzinfo=tz)
    end = datetime.combine(start_day + timedelta(days=days), time(0), tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _reset_for_tests() -> None:
    """Clear the invalid-zone log latch. Tests only."""
    _invalid_zone_logged.clear()
