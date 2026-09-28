"""ATHENA-127 Phase 3 -- src/mode_service/bookings.py: source selection,
fetch/window/freshness (D6), and cadence/single-flight (D9).

The admin backend is faked with httpx.MockTransport (module-level
ADMIN_API_URL is monkeypatched to a dummy host, matching
test_mode_service_config_and_pin.py's pattern for main.py's ADMIN_API_URL).
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

import httpx
import pytest
import structlog

from shared import config as config_module
from shared.booking_window import Booking

NY = ZoneInfo("America/New_York")


@pytest.fixture(scope="module", autouse=True)
def _restore_structlog_after_module():
    snapshot = structlog.get_config()
    yield
    structlog.configure(**snapshot)


@pytest.fixture(autouse=True)
def _mode_service_env(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "test-mode-service-key")
    monkeypatch.setenv("DEV_MODE", "true")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


@pytest.fixture
def bs(_mode_service_env):
    from mode_service.bookings import BookingSources
    import mode_service.bookings as bookings_module

    bookings_module.ADMIN_API_URL = "http://admin.test"
    yield BookingSources()


def _admin_handler(payload, status_code=200):
    def handler(request):
        return httpx.Response(status_code, json=payload)
    return handler


def _admin_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ical_factory(handler):
    def factory(timeout=30.0):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    return factory


def _bookings_payload(rows, suppressed=None):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "property_timezone": "America/New_York",
        "property_timezone_valid": True,
        "bookings": rows,
        "suppressed": suppressed or [],
    }


def _row(id_, key, checkin, checkout, is_test=False, source="admin"):
    return {
        "id": id_, "key": key, "source": source,
        "checkin": checkin.isoformat(), "checkout": checkout.isoformat(), "is_test": is_test,
    }


class TestSourceSelection:
    def test_unknown_source_falls_back_to_auto_with_one_error(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_SOURCE", "bogus")
        config_module._clear_cache_for_tests()

        now = datetime.now(timezone.utc)
        admin_client = _admin_client(_admin_handler(_bookings_payload([])))

        with structlog.testing.capture_logs() as logs:
            asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=admin_client))
            asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=admin_client))

        errors = [e for e in logs if e.get("event") == "mode_bookings_source_unknown"]
        assert len(errors) == 1

        snapshot = bs.snapshot({"enabled": True}, now=now, now_monotonic=time.monotonic())
        assert snapshot.required == "admin"

    def test_auto_without_calendar_url_has_no_advisory(self, bs):
        now = datetime.now(timezone.utc)
        admin_client = _admin_client(_admin_handler(_bookings_payload([])))
        asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=admin_client))
        snapshot = bs.snapshot({"enabled": True}, now=now, now_monotonic=time.monotonic())
        assert snapshot.required == "admin"
        assert snapshot.advisory == ()

    def test_auto_with_calendar_url_has_ical_advisory(self, bs):
        now = datetime.now(timezone.utc)
        admin_client = _admin_client(_admin_handler(_bookings_payload([])))
        ical_client_factory = _ical_factory(lambda r: httpx.Response(200, content=b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"))
        config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}
        asyncio.run(bs.refresh(config, now=now, admin_client=admin_client, ical_client_factory=ical_client_factory))
        snapshot = bs.snapshot(config, now=now, now_monotonic=time.monotonic())
        assert snapshot.advisory == ("ical",)

    def test_admin_only_mode_never_fetches_ical(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_SOURCE", "admin")
        config_module._clear_cache_for_tests()

        now = datetime.now(timezone.utc)
        admin_client = _admin_client(_admin_handler(_bookings_payload([])))
        called = []

        def factory(timeout=30.0):
            called.append(1)
            raise AssertionError("ical should not be fetched in admin mode")

        config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}
        asyncio.run(bs.refresh(config, now=now, admin_client=admin_client, ical_client_factory=factory))
        assert called == []
        snapshot = bs.snapshot(config, now=now, now_monotonic=time.monotonic())
        assert snapshot.required == "admin"
        assert snapshot.advisory == ()

    def test_ical_only_mode_required_source_is_ical(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_SOURCE", "ical")
        config_module._clear_cache_for_tests()

        now = datetime.now(timezone.utc)
        ical_client_factory = _ical_factory(lambda r: httpx.Response(200, content=b"BEGIN:VCALENDAR\nEND:VCALENDAR\n"))
        config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}
        asyncio.run(bs.refresh(config, now=now, admin_client=None, ical_client_factory=ical_client_factory))
        snapshot = bs.snapshot(config, now=now, now_monotonic=time.monotonic())
        assert snapshot.required == "ical"

    def test_ical_only_mode_empty_url_is_misconfigured_never_loaded(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_SOURCE", "ical")
        config_module._clear_cache_for_tests()

        now = datetime.now(timezone.utc)
        config = {"enabled": True, "calendar_url": ""}

        with structlog.testing.capture_logs() as logs:
            asyncio.run(bs.refresh(config, now=now, admin_client=None, ical_client_factory=_ical_factory(lambda r: httpx.Response(200))))

        errors = [e for e in logs if e.get("event") == "mode_bookings_source_misconfigured"]
        assert len(errors) == 1
        snapshot = bs.snapshot(config, now=now, now_monotonic=time.monotonic())
        assert snapshot.statuses["ical"] == "never_loaded"


class TestAdminFetch:
    def test_request_carries_service_key_and_window(self, bs):
        captured = {}

        def handler(request):
            captured["headers"] = dict(request.headers)
            captured["params"] = dict(request.url.params)
            return httpx.Response(200, json=_bookings_payload([]))

        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        config = {
            "enabled": True,
            "buffer_before_checkin_hours": 2,
            "buffer_after_checkout_hours": 1,
            "calendar_poll_interval_minutes": 10,
        }
        asyncio.run(bs.refresh(config, now=now, admin_client=_admin_client(handler)))

        assert captured["headers"].get("x-service-key") == "test-mode-service-key"

        start = datetime.fromisoformat(captured["params"]["start"])
        end = datetime.fromisoformat(captured["params"]["end"])
        max_age = bs._max_age_seconds(config)
        expected_end = now + timedelta(seconds=max_age) + timedelta(hours=2) + timedelta(hours=1)
        assert end == expected_end

    def test_non_200_is_failed_attempt_last_good_retained(self, bs):
        now = datetime.now(timezone.utc)
        good_row = _row(1, "k1", now - timedelta(hours=1), now + timedelta(hours=1))
        asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([good_row])))))

        for status_code in (500, 404):
            asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=_admin_client(_admin_handler({}, status_code=status_code))))
            snapshot = bs.snapshot({"enabled": True}, now=now, now_monotonic=time.monotonic())
            assert len(snapshot.bookings) == 1, f"status {status_code} should not clear last-good"
            assert snapshot.statuses["admin"] == "stale"

    def test_timeout_is_failed_attempt(self, bs):
        now = datetime.now(timezone.utc)

        def handler(request):
            raise httpx.TimeoutException("timed out")

        asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=_admin_client(handler)))
        snapshot = bs.snapshot({"enabled": True}, now=now, now_monotonic=time.monotonic())
        assert snapshot.statuses["admin"] == "never_loaded"

    def test_malformed_body_is_failed_attempt(self, bs):
        now = datetime.now(timezone.utc)

        def handler(request):
            return httpx.Response(200, text="not json")

        asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=_admin_client(handler)))
        snapshot = bs.snapshot({"enabled": True}, now=now, now_monotonic=time.monotonic())
        assert snapshot.statuses["admin"] == "never_loaded"


class TestFreshnessClassification:
    def test_success_long_ago_no_retry_is_expired(self, bs, monkeypatch):
        # The clamp floor is max(300, 2 x ical_poll_seconds); with the
        # default 10-minute poll interval that floor is 1200s, so elapsing
        # past it (not just past the requested "100") is what proves the
        # clamp, not just the raw config value.
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "100")
        config_module._clear_cache_for_tests()
        config = {"enabled": True, "calendar_poll_interval_minutes": 10}
        now = datetime.now(timezone.utc)
        asyncio.run(bs.refresh(config, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([])))))
        effective_max_age = bs._max_age_seconds(config)
        assert effective_max_age == 1200  # clamp floor, not the raw "100"

        # Simulate time passing past the clamped max_age with no retry.
        bs._admin.last_success_at -= (effective_max_age + 50)
        bs._admin.last_attempt_at -= (effective_max_age + 50)
        snapshot = bs.snapshot(config, now=now, now_monotonic=time.monotonic())
        assert snapshot.statuses["admin"] == "expired"

    def test_failed_with_recent_success_is_stale(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "21600")
        config_module._clear_cache_for_tests()
        now = datetime.now(timezone.utc)
        asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([])))))
        asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=_admin_client(_admin_handler({}, status_code=500))))
        snapshot = bs.snapshot({"enabled": True}, now=now, now_monotonic=time.monotonic())
        assert snapshot.statuses["admin"] == "stale"

    def test_max_age_clamps(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "1")
        config_module._clear_cache_for_tests()
        config = {"calendar_poll_interval_minutes": 10}
        assert bs._max_age_seconds(config) == max(300, 2 * 600)

        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "99999999")
        config_module._clear_cache_for_tests()
        assert bs._max_age_seconds(config) == 604800

    def test_lookback_formula(self, bs):
        now = datetime.now(timezone.utc)
        config = {"buffer_after_checkout_hours": 72}
        start, end, before, after, max_age = bs._fetch_window(config, now)
        expected_lookback = max(timedelta(days=2), timedelta(hours=72) + timedelta(days=1))
        assert now - start == expected_lookback
        assert expected_lookback == timedelta(days=4)


class TestIcalFetch:
    # A wide config (max clamped buffers/max_age) so the D6 fetch window
    # comfortably covers `now` plus a few days either side -- these tests
    # are about parsing/classification, not the window formula itself
    # (covered by TestFreshnessClassification.test_lookback_formula).
    _WIDE_CONFIG = {
        "enabled": True,
        "calendar_url": "https://example.com/x.ics",
        "buffer_before_checkin_hours": 168,
        "buffer_after_checkout_hours": 168,
    }

    def test_date_only_localised_to_new_york(self, bs, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "604800")
        config_module._clear_cache_for_tests()

        ical = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:date-only@example.com\r\n"
            "DTSTART;VALUE=DATE:20260701\r\n"
            "DTEND;VALUE=DATE:20260705\r\n"
            "SUMMARY:Reserved\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        ).encode()

        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        factory = _ical_factory(lambda r: httpx.Response(200, content=ical))
        asyncio.run(bs.refresh(self._WIDE_CONFIG, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([]))), ical_client_factory=factory))

        assert len(bs._ical.last_good) == 1
        assert bs._ical.last_good[0].start.isoformat() == "2026-07-01T20:00:00+00:00"
        assert bs._ical.last_good[0].end.isoformat() == "2026-07-05T15:00:00+00:00"

    def test_floating_datetime_localised_to_new_york(self, bs, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "604800")
        config_module._clear_cache_for_tests()

        ical = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:floating@example.com\r\n"
            "DTSTART:20260701T160000\r\n"
            "DTEND:20260705T110000\r\n"
            "SUMMARY:Reserved\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        ).encode()

        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        factory = _ical_factory(lambda r: httpx.Response(200, content=ical))
        asyncio.run(bs.refresh(self._WIDE_CONFIG, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([]))), ical_client_factory=factory))

        assert len(bs._ical.last_good) == 1
        assert bs._ical.last_good[0].start.isoformat() == "2026-07-01T20:00:00+00:00"
        assert bs._ical.last_good[0].end.isoformat() == "2026-07-05T15:00:00+00:00"

    def test_blocked_vevent_dropped(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "604800")
        config_module._clear_cache_for_tests()
        ical = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:blocked@example.com\r\n"
            "DTSTART;VALUE=DATE:20260701\r\n"
            "DTEND;VALUE=DATE:20260705\r\n"
            "SUMMARY:Blocked\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        ).encode()
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        factory = _ical_factory(lambda r: httpx.Response(200, content=ical))
        asyncio.run(bs.refresh(self._WIDE_CONFIG, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([]))), ical_client_factory=factory))
        assert bs._ical.last_good == []
        assert bs._ical.last_attempt_ok is True

    def test_vevent_without_dtend_skipped_not_feed_fatal(self, bs, monkeypatch):
        monkeypatch.setenv("MODE_BOOKINGS_MAX_AGE_SECONDS", "604800")
        config_module._clear_cache_for_tests()
        ical = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:no-dtend@example.com\r\n"
            "DTSTART;VALUE=DATE:20260701\r\n"
            "SUMMARY:Reserved\r\n"
            "END:VEVENT\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:good@example.com\r\n"
            "DTSTART;VALUE=DATE:20260701\r\n"
            "DTEND;VALUE=DATE:20260705\r\n"
            "SUMMARY:Reserved\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        ).encode()
        now = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        factory = _ical_factory(lambda r: httpx.Response(200, content=ical))
        asyncio.run(bs.refresh(self._WIDE_CONFIG, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([]))), ical_client_factory=factory))
        assert len(bs._ical.last_good) == 1
        assert bs._ical.last_attempt_ok is True


class TestSingleFlightAndCadence:
    def test_second_refresh_while_first_blocked_skips_request(self, bs):
        call_count = {"n": 0}
        release = asyncio.Event()

        async def handler(request):
            call_count["n"] += 1
            await release.wait()
            return httpx.Response(200, json=_bookings_payload([]))

        now = datetime.now(timezone.utc)
        admin_client = _admin_client(handler)

        async def run():
            first = asyncio.create_task(bs.refresh({"enabled": True}, now=now, admin_client=admin_client))
            await asyncio.sleep(0.01)
            second = asyncio.create_task(bs.refresh({"enabled": True}, now=now, admin_client=admin_client))
            await asyncio.sleep(0.01)
            release.set()
            await first
            await second

        asyncio.run(run())
        assert call_count["n"] == 1

    def test_loop_fetches_when_enabled_is_false(self, bs):
        now = datetime.now(timezone.utc)
        called = {"n": 0}

        def handler(request):
            called["n"] += 1
            return httpx.Response(200, json=_bookings_payload([]))

        asyncio.run(bs.refresh({"enabled": False}, now=now, admin_client=_admin_client(handler)))
        assert called["n"] == 1

    def test_first_tick_fetches_ical_immediately(self, bs):
        now = datetime.now(timezone.utc)
        called = {"n": 0}

        def handler(request):
            called["n"] += 1
            return httpx.Response(200, content=b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")

        config = {"enabled": True, "calendar_url": "https://example.com/x.ics", "calendar_poll_interval_minutes": 60}
        asyncio.run(bs.refresh(config, now=now, admin_client=_admin_client(_admin_handler(_bookings_payload([]))), ical_client_factory=_ical_factory(handler)))
        assert called["n"] == 1
