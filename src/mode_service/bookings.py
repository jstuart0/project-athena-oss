"""ATHENA-127 -- booking sources for the mode service: admin (required) and
legacy iCal (advisory-additive in `auto`), fused into one merged/suppressed
snapshot `determine_mode` can read.

Import form (D10): this module is `mode_service.bookings` in both the real
image (`/app/mode_service/bookings.py`, `/app` on `sys.path` via the uvicorn
cwd) and the tests (`sys.path.insert(0, "src")`). It imports `shared.*`
directly -- never a relative `.booking_window` or a bare `booking_window`.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import structlog
from icalendar import Calendar

from shared.admin_url import get_admin_url
from shared.config import get_config
from shared.booking_window import (
    DEFAULT_CHECKIN_TIME,
    DEFAULT_CHECKOUT_TIME,
    Booking,
    classify_summary,
    clamp_buffer_hours,
    db_value_to_utc,
    feed_value_to_utc,
    merge,
    resolve_property_tz,
)

logger = structlog.get_logger()

ADMIN_API_URL = get_admin_url()

# Lazily-created HTTP client for the admin bookings fetch, module-level so
# tests can inject a MockTransport-backed one before calling refresh()
# (mirrors main.py's `_get_admin_http_client` pattern, D9).
_bookings_http_client: Optional[httpx.AsyncClient] = None


def get_bookings_http_client() -> httpx.AsyncClient:
    global _bookings_http_client
    if _bookings_http_client is None:
        _bookings_http_client = httpx.AsyncClient(timeout=3.0)
    return _bookings_http_client


async def aclose_bookings_http_client() -> None:
    global _bookings_http_client
    if _bookings_http_client is not None:
        await _bookings_http_client.aclose()
        _bookings_http_client = None


@dataclass
class BookingSnapshot:
    bookings: List[Booking]
    required: str
    advisory: Tuple[str, ...]
    label: str
    statuses: Dict[str, str]
    age_seconds: Optional[float]
    counts: Dict[str, int]
    property_timezone: str
    property_timezone_valid: bool
    window: Optional[Tuple[datetime, datetime]] = None


class _SourceState:
    """Last-good bookings plus freshness bookkeeping for one source (D6).
    Clocks are `time.monotonic()`, matching main.py's existing config-load
    tracking."""

    def __init__(self) -> None:
        self.last_good: List[Booking] = []
        self.last_success_at: Optional[float] = None
        self.last_attempt_at: Optional[float] = None
        self.last_attempt_ok: bool = False
        self.lock: asyncio.Lock = asyncio.Lock()


class BookingSources:
    """Owns both source fetches, their freshness state, and the merged
    snapshot determine_mode() reads. One instance per process (main.py
    holds `booking_sources = BookingSources()`)."""

    def __init__(self) -> None:
        self._admin = _SourceState()
        self._ical = _SourceState()
        self._suppressed_rows: List[Dict[str, Any]] = []
        self._source_mode = "auto"
        self._ical_misconfigured_warned = False
        self._unknown_source_warned = False
        self._last_admin_window: Optional[Tuple[datetime, datetime]] = None

    # ------------------------------------------------------------------
    # D6 windowing / freshness
    # ------------------------------------------------------------------

    def _max_age_seconds(self, config: Dict[str, Any]) -> int:
        cfg = get_config()
        ical_poll_seconds = int(config.get("calendar_poll_interval_minutes", 10) or 10) * 60
        lo = max(300, 2 * ical_poll_seconds)
        hi = 604800
        raw = cfg.mode_bookings_max_age_seconds
        return int(min(max(raw, lo), hi))

    def _fetch_window(
        self, config: Dict[str, Any], now: datetime
    ) -> Tuple[datetime, datetime, timedelta, timedelta, int]:
        buffer_before = clamp_buffer_hours(config.get("buffer_before_checkin_hours", 2))
        buffer_after = clamp_buffer_hours(config.get("buffer_after_checkout_hours", 1))
        max_age = self._max_age_seconds(config)
        lookback = max(timedelta(days=2), timedelta(hours=buffer_after) + timedelta(days=1))
        start = now - lookback
        end = now + timedelta(seconds=max_age) + timedelta(hours=buffer_before) + timedelta(hours=1)
        return start, end, timedelta(hours=buffer_before), timedelta(hours=buffer_after), max_age

    # ------------------------------------------------------------------
    # Refresh (D9)
    # ------------------------------------------------------------------

    async def refresh(
        self,
        config: Dict[str, Any],
        *,
        now: datetime,
        admin_client: httpx.AsyncClient,
        ical_client_factory=httpx.AsyncClient,
    ) -> None:
        cfg = get_config()
        source_mode = cfg.mode_bookings_source
        if source_mode not in ("auto", "admin", "ical"):
            if not self._unknown_source_warned:
                logger.error("mode_bookings_source_unknown", value=source_mode)
                self._unknown_source_warned = True
            source_mode = "auto"
        self._source_mode = source_mode

        calendar_url = config.get("calendar_url")
        ical_active = source_mode == "ical" or (source_mode == "auto" and bool(calendar_url))

        tasks = []
        if source_mode in ("auto", "admin"):
            tasks.append(self._maybe_fetch_admin(config, now, admin_client))

        if ical_active:
            if not calendar_url:
                if not self._ical_misconfigured_warned:
                    logger.error("mode_bookings_source_misconfigured", source="ical")
                    self._ical_misconfigured_warned = True
            else:
                tasks.append(self._maybe_fetch_ical(config, now, ical_client_factory))

        if tasks:
            await asyncio.gather(*tasks)

    async def _maybe_fetch_admin(
        self, config: Dict[str, Any], now: datetime, admin_client: httpx.AsyncClient
    ) -> None:
        if self._admin.lock.locked():
            logger.debug("mode_bookings_fetch_skipped_in_flight", source="admin")
            return
        async with self._admin.lock:
            await self._fetch_admin(config, now, admin_client)

    async def _fetch_admin(
        self, config: Dict[str, Any], now: datetime, admin_client: httpx.AsyncClient
    ) -> None:
        start, end, *_ = self._fetch_window(config, now)
        self._admin.last_attempt_at = time.monotonic()
        key = get_config().service_api_key
        headers = {"X-Service-Key": key} if key else {}

        try:
            response = await admin_client.get(
                f"{ADMIN_API_URL}/api/internal/guest-mode/bookings",
                params={"start": start.isoformat(), "end": end.isoformat()},
                headers=headers,
            )
            if response.status_code != 200:
                raise ValueError(f"admin bookings endpoint returned {response.status_code}")
            body = response.json()
            rows = body["bookings"]
            suppressed_rows = body.get("suppressed", [])
            bookings = [
                Booking(
                    id=row["id"],
                    key=row["key"],
                    source="admin",
                    label=f"admin #{row['id']}",
                    start=db_value_to_utc(datetime.fromisoformat(row["checkin"])),
                    end=db_value_to_utc(datetime.fromisoformat(row["checkout"])),
                    is_test=bool(row.get("is_test", False)),
                )
                for row in rows
            ]
        except Exception as e:
            # Any non-200 (incl. 404), malformed body, or transport error is
            # a FAILED attempt -- never treated as "zero bookings" (R5).
            self._admin.last_attempt_ok = False
            logger.error("mode_bookings_admin_fetch_failed", error=str(e))
            return

        self._admin.last_good = bookings
        self._admin.last_success_at = time.monotonic()
        self._admin.last_attempt_ok = True
        self._suppressed_rows = suppressed_rows
        self._last_admin_window = (start, end)

    async def _maybe_fetch_ical(
        self, config: Dict[str, Any], now: datetime, ical_client_factory
    ) -> None:
        if self._ical.lock.locked():
            logger.debug("mode_bookings_fetch_skipped_in_flight", source="ical")
            return
        poll_seconds = int(config.get("calendar_poll_interval_minutes", 10) or 10) * 60
        never_attempted = self._ical.last_attempt_at is None
        due = never_attempted or (time.monotonic() - self._ical.last_attempt_at) >= poll_seconds
        if not due:
            return
        async with self._ical.lock:
            await self._fetch_ical(config, now, ical_client_factory)

    async def _fetch_ical(
        self, config: Dict[str, Any], now: datetime, ical_client_factory
    ) -> None:
        self._ical.last_attempt_at = time.monotonic()
        calendar_url = config["calendar_url"]
        property_tz, _ = resolve_property_tz(get_config().default_timezone)
        start, end, *_ = self._fetch_window(config, now)

        try:
            async with ical_client_factory(timeout=30.0) as client:
                response = await client.get(calendar_url)
                response.raise_for_status()
                cal = Calendar.from_ical(response.content)

                bookings: List[Booking] = []
                for component in cal.walk():
                    if component.name != "VEVENT":
                        continue
                    try:
                        uid = str(component.get("uid", ""))
                        summary = str(component.get("summary", ""))
                        dtstart = component.get("dtstart")
                        dtend = component.get("dtend")
                        if not dtstart or not dtend:
                            logger.debug("mode_bookings_ical_event_skipped", reason="missing_dtend")
                            continue

                        if classify_summary(summary) == "blocked":
                            continue

                        checkin = feed_value_to_utc(
                            dtstart.dt, default_hhmm=DEFAULT_CHECKIN_TIME, tz=property_tz
                        )
                        checkout = feed_value_to_utc(
                            dtend.dt, default_hhmm=DEFAULT_CHECKOUT_TIME, tz=property_tz
                        )
                        # D6: filter to the fetch window (no server side to
                        # do this for us, unlike the admin endpoint).
                        if checkout <= start or checkin >= end:
                            continue

                        booking_key = hashlib.sha256(f"ical|{uid}".encode()).hexdigest()[:16]
                        bookings.append(
                            Booking(
                                id=None,
                                key=booking_key,
                                source="ical",
                                label=f"ical {booking_key[:8]}",
                                start=checkin,
                                end=checkout,
                                is_test=False,
                            )
                        )
                    except Exception:
                        logger.debug("mode_bookings_ical_event_skipped", reason="parse_error")
                        continue
        except Exception as e:
            self._ical.last_attempt_ok = False
            logger.error("mode_bookings_ical_fetch_failed", error=str(e))
            return

        self._ical.last_good = bookings
        self._ical.last_success_at = time.monotonic()
        self._ical.last_attempt_ok = True

    # ------------------------------------------------------------------
    # Snapshot (D6/D13)
    # ------------------------------------------------------------------

    def _classify(self, state: _SourceState, now_monotonic: float, max_age: int) -> str:
        if state.last_success_at is None:
            return "never_loaded"
        age = now_monotonic - state.last_success_at
        if age > max_age:
            return "expired"
        if state.last_attempt_ok:
            return "fresh"
        return "stale"

    def _suppressed_pairs(self, tz):
        pairs = set()
        for row in self._suppressed_rows:
            try:
                checkin = datetime.fromisoformat(row["checkin"])
                checkout = datetime.fromisoformat(row["checkout"])
            except (KeyError, ValueError, TypeError):
                continue
            pairs.add((checkin.astimezone(tz).date(), checkout.astimezone(tz).date()))
        return pairs

    def snapshot(
        self, config: Dict[str, Any], *, now: datetime, now_monotonic: float
    ) -> BookingSnapshot:
        cfg = get_config()
        source_mode = self._source_mode if self._source_mode in ("auto", "admin", "ical") else cfg.mode_bookings_source
        if source_mode not in ("auto", "admin", "ical"):
            source_mode = "auto"

        calendar_url = config.get("calendar_url")
        property_tz, tz_valid = resolve_property_tz(cfg.default_timezone)
        max_age = self._max_age_seconds(config)

        if source_mode == "ical":
            required_name = "ical"
            advisory_names: Tuple[str, ...] = ()
        else:
            required_name = "admin"
            advisory_names = ("ical",) if (source_mode == "auto" and calendar_url) else ()

        required_state = self._ical if required_name == "ical" else self._admin
        required_status = self._classify(required_state, now_monotonic, max_age)

        statuses = {required_name: required_status}
        counts = {required_name: len(required_state.last_good)}

        # Rule 1: the required source is considered in every state that has
        # data at all (fresh/stale/expired) -- guest-direction only, since an
        # active booking from stale-but-present data must still flip to
        # guest (D6 residual notwithstanding; the alternative -- ignoring
        # expired data entirely -- would only ever narrow guest time, never
        # widen it, so it's still fail-safe to include it here).
        considered_primary: List[Booking] = []
        if required_status != "never_loaded":
            considered_primary = list(required_state.last_good)

        considered_advisory: List[Booking] = []
        for name in advisory_names:
            state = self._ical if name == "ical" else self._admin
            status = self._classify(state, now_monotonic, max_age)
            statuses[name] = status
            counts[name] = len(state.last_good)
            if status in ("fresh", "stale"):
                considered_advisory.extend(state.last_good)

        merged = merge(
            considered_primary,
            considered_advisory,
            suppressed_pairs=self._suppressed_pairs(property_tz),
            tz=property_tz,
        )

        age_seconds = None
        if required_state.last_success_at is not None:
            age_seconds = now_monotonic - required_state.last_success_at

        label = "+".join([required_name, *advisory_names])

        return BookingSnapshot(
            bookings=merged,
            required=required_name,
            advisory=advisory_names,
            label=label,
            statuses=statuses,
            age_seconds=age_seconds,
            counts=counts,
            property_timezone=cfg.default_timezone or "UTC",
            property_timezone_valid=tz_valid,
            window=self._last_admin_window,
        )
