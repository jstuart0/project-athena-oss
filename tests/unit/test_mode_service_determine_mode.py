"""ATHENA-127 Phase 3 -- src/mode_service/main.py's determine_mode()/
determine_mode_reason()/get_current_event(): precedence, D5 turnover, D6
freshness/stale-lookahead/residual, mixed-freshness `auto`, D13 suppression,
and PII (both the admin and the legacy-iCal path).

Every case uses an injected `now`/`now_monotonic` -- no wall clock.
"""
from __future__ import annotations

import asyncio
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient

from shared import config as config_module
from shared.booking_window import Booking

_SERVICE_KEY = "test-mode-service-key"
_HEADERS = {"X-Service-Key": _SERVICE_KEY}
NY = ZoneInfo("America/New_York")


@pytest.fixture(scope="module", autouse=True)
def _restore_structlog_after_module():
    snapshot = structlog.get_config()
    yield
    structlog.configure(**snapshot)


@pytest.fixture(autouse=True)
def _mode_service_env(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", _SERVICE_KEY)
    monkeypatch.setenv("DEV_MODE", "true")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


@pytest.fixture
def ms(_mode_service_env):
    from mode_service import main as ms_main
    import mode_service.bookings as ms_bookings

    ms_main.ADMIN_API_URL = "http://admin.test"
    ms_bookings.ADMIN_API_URL = "http://admin.test"
    ms_main.current_config = {"enabled": True}
    ms_main.current_mode = "owner"
    ms_main.active_override = None
    ms_main._config_loaded = True
    ms_main._last_load_ok = True
    ms_main._admin_http_client = None
    ms_main.booking_sources = ms_main.BookingSources()
    yield ms_main
    if ms_main._admin_http_client is not None:
        asyncio.run(ms_main._admin_http_client.aclose())
        ms_main._admin_http_client = None
    if ms_bookings._bookings_http_client is not None:
        asyncio.run(ms_bookings._bookings_http_client.aclose())
        ms_bookings._bookings_http_client = None


@pytest.fixture
def client(ms):
    return TestClient(ms.app)


def _fresh(state, now_monotonic, bookings=None, age=10):
    state.last_good = bookings or []
    state.last_success_at = now_monotonic - age
    state.last_attempt_at = now_monotonic - age
    state.last_attempt_ok = True


def _stale(state, now_monotonic, bookings=None, age=10):
    state.last_good = bookings or []
    state.last_success_at = now_monotonic - (age + 1000)
    state.last_attempt_at = now_monotonic - age
    state.last_attempt_ok = False


def _expired(state, now_monotonic, max_age, bookings=None):
    state.last_good = bookings or []
    state.last_success_at = now_monotonic - (max_age + 100)
    state.last_attempt_at = now_monotonic - (max_age + 100)
    state.last_attempt_ok = True


def _never_loaded(state):
    state.last_good = []
    state.last_success_at = None
    state.last_attempt_at = None
    state.last_attempt_ok = False


class TestPrecedence:
    def test_owner_override_during_active_booking_wins(self, ms):
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        booking = Booking(id=1, key="k1", source="admin", label="admin #1",
                           start=now - timedelta(hours=1), end=now + timedelta(hours=1), is_test=False)
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[booking])
        ms.active_override = {"mode": "owner", "activated_at": now, "expires_at": now + timedelta(minutes=30), "voice_device_id": None}
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "owner"

    def test_guest_override_without_booking_wins(self, ms):
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        ms.current_config = {"enabled": False}
        ms.active_override = {"mode": "guest", "activated_at": now, "expires_at": now + timedelta(minutes=30), "voice_device_id": None}
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "guest"

    def test_invalid_stored_override_falls_through(self, ms):
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        ms.current_config = {"enabled": False}
        ms.active_override = {"mode": "bogus", "activated_at": now, "expires_at": now + timedelta(minutes=30), "voice_device_id": None}
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "owner"
        assert ms.active_override is None

    def test_expired_override_cleared(self, ms):
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        ms.current_config = {"enabled": False}
        ms.active_override = {"mode": "guest", "activated_at": now - timedelta(hours=1), "expires_at": now - timedelta(minutes=1), "voice_device_id": None}
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "owner"
        assert ms.active_override is None

    def test_disabled_and_never_loaded_is_owner(self, ms):
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        ms.current_config = {"enabled": False}
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "owner"

    def test_enabled_and_never_loaded_is_degraded(self, ms):
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        ms.current_config = {"enabled": True}
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "degraded"


class TestTurnover:
    def test_turnover_owner_gap_then_guest(self, ms):
        now_monotonic = time.monotonic()
        a_end = datetime(2026, 7, 5, 11, 0, tzinfo=NY).astimezone(timezone.utc)
        b_start = datetime(2026, 7, 5, 16, 0, tzinfo=NY).astimezone(timezone.utc)
        a = Booking(id=1, key="a", source="admin", label="admin #1", start=a_end - timedelta(days=4), end=a_end, is_test=False)
        b = Booking(id=2, key="b", source="admin", label="admin #2", start=b_start, end=b_start + timedelta(days=4), is_test=False)
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[a, b])
        ms.current_config = {"enabled": True, "buffer_before_checkin_hours": 2, "buffer_after_checkout_hours": 1}

        owner_at = a_end + timedelta(hours=1, minutes=30)  # 12:30 local
        guest_at = a_end + timedelta(hours=3)  # 14:00 local, b's buffered start
        assert ms.determine_mode(now=owner_at, now_monotonic=now_monotonic) == "owner"
        assert ms.determine_mode(now=guest_at, now_monotonic=now_monotonic) == "guest"


class TestStaleLookaheadAndResidual:
    def test_stale_lookahead(self, ms, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "21600")
        config_module._clear_cache_for_tests()
        T0 = time.monotonic()
        real_now = datetime.now(timezone.utc)
        booking_checkin = real_now + timedelta(hours=3)
        booking = Booking(id=1, key="k1", source="admin", label="admin #1",
                           start=booking_checkin, end=booking_checkin + timedelta(days=3), is_test=False)
        state = ms.booking_sources._admin
        state.last_good = [booking]
        state.last_success_at = T0
        state.last_attempt_at = T0
        state.last_attempt_ok = False  # failing since T0, but within max_age -> stale
        ms.current_config = {"enabled": True, "buffer_before_checkin_hours": 2, "buffer_after_checkout_hours": 1}

        owner_at = real_now + timedelta(minutes=30)
        assert ms.determine_mode(now=owner_at, now_monotonic=T0 + 1800) == "owner"

        guest_at = real_now + timedelta(hours=1)  # booking_checkin - 2h buffer
        guest_at_monotonic = T0 + 3600
        assert ms.determine_mode(now=guest_at, now_monotonic=guest_at_monotonic) == "guest"

        snapshot = ms.booking_sources.snapshot(ms.current_config, now=guest_at, now_monotonic=guest_at_monotonic)
        assert snapshot.statuses["admin"] == "stale"

    def test_residual_absent_booking_stays_owner_while_stale(self, ms):
        T0 = time.monotonic()
        real_now = datetime.now(timezone.utc)
        state = ms.booking_sources._admin
        state.last_good = []  # the new/moved booking simply isn't in the snapshot
        state.last_success_at = T0
        state.last_attempt_at = T0
        state.last_attempt_ok = False
        ms.current_config = {"enabled": True}
        assert ms.determine_mode(now=real_now, now_monotonic=T0 + 60) == "owner"

    def test_expired_and_no_active_is_degraded(self, ms, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "300")
        config_module._clear_cache_for_tests()
        T0 = time.monotonic()
        state = ms.booking_sources._admin
        state.last_good = []
        state.last_success_at = T0
        state.last_attempt_at = T0
        state.last_attempt_ok = True
        ms.current_config = {"enabled": True, "calendar_poll_interval_minutes": 1}
        max_age = ms.booking_sources._max_age_seconds(ms.current_config)
        assert ms.determine_mode(now=datetime.now(timezone.utc), now_monotonic=T0 + max_age + 10) == "degraded"


class TestMixedFreshnessAuto:
    def _with_ical(self, ms):
        ms.current_config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}

    def test_admin_fresh_no_active_ical_stale_active_is_guest(self, ms):
        self._with_ical(ms)
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        booking = Booking(id=None, key="ical1", source="ical", label="ical abcdef01",
                           start=now - timedelta(hours=1), end=now + timedelta(hours=1), is_test=False)
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[])
        _stale(ms.booking_sources._ical, now_monotonic, bookings=[booking])
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "guest"

    def test_admin_fresh_no_active_ical_expired_active_is_owner(self, ms):
        self._with_ical(ms)
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        max_age = ms.booking_sources._max_age_seconds(ms.current_config)
        booking = Booking(id=None, key="ical1", source="ical", label="ical abcdef01",
                           start=now - timedelta(hours=1), end=now + timedelta(hours=1), is_test=False)
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[])
        _expired(ms.booking_sources._ical, now_monotonic, max_age, bookings=[booking])
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "owner"

    def test_admin_fresh_ical_never_loaded_is_owner(self, ms):
        self._with_ical(ms)
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[])
        _never_loaded(ms.booking_sources._ical)
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "owner"

    def test_admin_expired_ical_fresh_no_active_is_degraded(self, ms):
        self._with_ical(ms)
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        max_age = ms.booking_sources._max_age_seconds(ms.current_config)
        _expired(ms.booking_sources._admin, now_monotonic, max_age, bookings=[])
        _fresh(ms.booking_sources._ical, now_monotonic, bookings=[])
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "degraded"

    def test_admin_expired_ical_fresh_active_is_guest(self, ms):
        self._with_ical(ms)
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        max_age = ms.booking_sources._max_age_seconds(ms.current_config)
        booking = Booking(id=None, key="ical1", source="ical", label="ical abcdef01",
                           start=now - timedelta(hours=1), end=now + timedelta(hours=1), is_test=False)
        _expired(ms.booking_sources._admin, now_monotonic, max_age, bookings=[])
        _fresh(ms.booking_sources._ical, now_monotonic, bookings=[booking])
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "guest"

    def test_admin_expired_but_own_last_good_active_is_guest(self, ms):
        ms.current_config = {"enabled": True}  # no calendar_url: no advisory at all
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        max_age = ms.booking_sources._max_age_seconds(ms.current_config)
        booking = Booking(id=1, key="k1", source="admin", label="admin #1",
                           start=now - timedelta(hours=1), end=now + timedelta(hours=1), is_test=False)
        _expired(ms.booking_sources._admin, now_monotonic, max_age, bookings=[booking])
        assert ms.determine_mode(now=now, now_monotonic=now_monotonic) == "guest"


class TestSuppression:
    def test_soft_deleted_admin_pair_drops_matching_ical_booking(self, ms, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()
        ms.current_config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()

        checkin = datetime(2026, 7, 1, 16, 0, tzinfo=NY).astimezone(timezone.utc)
        checkout = datetime(2026, 7, 5, 11, 0, tzinfo=NY).astimezone(timezone.utc)

        ical_booking = Booking(id=None, key="icalkey", source="ical", label="ical abcdef01", start=checkin, end=checkout, is_test=False)
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[])
        ms.booking_sources._suppressed_rows = [{"checkin": checkin.isoformat(), "checkout": checkout.isoformat()}]
        _fresh(ms.booking_sources._ical, now_monotonic, bookings=[ical_booking])

        snapshot = ms.booking_sources.snapshot(ms.current_config, now=now, now_monotonic=now_monotonic)
        assert snapshot.bookings == []

    def test_cross_source_duplicate_counts_once(self, ms, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()
        ms.current_config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()

        checkin = datetime(2026, 7, 1, 16, 0, tzinfo=NY).astimezone(timezone.utc)
        checkout = datetime(2026, 7, 5, 11, 0, tzinfo=NY).astimezone(timezone.utc)

        admin_booking = Booking(id=1, key="lodgify_1", source="admin", label="admin #1", start=checkin, end=checkout, is_test=False)
        ical_booking = Booking(id=None, key="icalkey", source="ical", label="ical abcdef01", start=checkin, end=checkout, is_test=False)
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[admin_booking])
        _fresh(ms.booking_sources._ical, now_monotonic, bookings=[ical_booking])

        snapshot = ms.booking_sources.snapshot(ms.current_config, now=now, now_monotonic=now_monotonic)
        assert len(snapshot.bookings) == 1


class TestPII:
    def test_admin_path_health_and_mode_clean(self, ms, client):
        now = datetime.now(timezone.utc)
        now_monotonic = time.monotonic()
        booking = Booking(id=1, key="deadbeef01234567", source="admin", label="admin #1",
                           start=now - timedelta(hours=1), end=now + timedelta(hours=1), is_test=False)
        _fresh(ms.booking_sources._admin, now_monotonic, bookings=[booking])
        ms.current_config = {"enabled": True}

        health_text = client.get("/health").text
        mode_text = client.get("/mode", headers=_HEADERS).text
        combined = health_text + mode_text
        assert "Zelda" not in combined
        assert "@" not in combined

    def test_legacy_ical_path_no_leak(self, ms, client, monkeypatch):
        """codex r2 Medium: this fails on base behaviour --
        determine_mode_reason returns event['summary'] verbatim
        (main.py:676-678) and get_current_event returns the raw uid/summary
        (main.py:694-699)."""
        monkeypatch.setenv("MODE_BOOKINGS_SOURCE", "ical")
        config_module._clear_cache_for_tests()
        ms.current_config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}

        now = datetime.now(timezone.utc)
        checkin_date = (now - timedelta(days=1)).strftime("%Y%m%d")
        checkout_date = (now + timedelta(days=1)).strftime("%Y%m%d")

        ical = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:1418fb94e984-zq@example.org@airbnb.com\r\n"
            f"DTSTART;VALUE=DATE:{checkin_date}\r\n"
            f"DTEND;VALUE=DATE:{checkout_date}\r\n"
            "SUMMARY:Reserved - Zelda Quux\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        ).encode()

        def handler(request):
            return httpx.Response(200, content=ical)

        def factory(timeout=30.0):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

        with structlog.testing.capture_logs() as logs:
            asyncio.run(ms.booking_sources.refresh(ms.current_config, now=now, admin_client=None, ical_client_factory=factory))
            health_resp = client.get("/health")
            mode_resp = client.get("/mode", headers=_HEADERS)
            events_resp = client.get("/mode/events", headers=_HEADERS)

        bodies = health_resp.text + mode_resp.text + events_resp.text
        log_text = " ".join(repr(e) for e in logs)
        for forbidden in ("Zelda", "Quux", "zq@example.org", "1418fb94e984"):
            assert forbidden not in bodies, forbidden
            assert forbidden not in log_text, forbidden

        mode_json = mode_resp.json()
        assert mode_json["mode"] == "guest"
        assert re.match(r"^Active booking: ical [0-9a-f]{8}", mode_json["reason"])
        assert re.fullmatch(r"[0-9a-f]{16}", mode_json["current_event"]["uid"])


class TestDegradedPermissionsMatch:
    def test_degraded_by_bookings_permissions_equals_degraded_response(self, ms, client):
        ms.current_config = {"enabled": True}  # admin never_loaded -> degraded
        resp = client.get("/mode/permissions", headers=_HEADERS)
        expected = ms._degraded_permissions_response()
        assert resp.json() == expected.model_dump()
