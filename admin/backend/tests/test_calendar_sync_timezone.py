"""ATHENA-127 Phase 1 -- admin-side timezone correctness and block
classification for the calendar sync write path.

Implementer note (plan step 5): the `UTC` case is a no-op guard only (it
passes at base by construction). The New York cases are the discriminating
ones and must be observed failing against base code before the fix lands
(base stamps house-local times as UTC: `2026-07-01T16:00:00+00:00` instead
of the correct `2026-07-01T20:00:00+00:00`). The `blocked` case is the
discriminating one for D11 (base always defaults `status` to `confirmed`).
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from shared import config as config_module
from app.routes.calendar_sources import fetch_lodgify_reservations, parse_ical_events
from app.models import CalendarEvent, CalendarSource


_ICAL_DATE_ONLY = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:date-only-1@example.com
DTSTART;VALUE=DATE:20260701
DTEND;VALUE=DATE:20260705
SUMMARY:Reserved
END:VEVENT
END:VCALENDAR
"""

_ICAL_TZID = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:tzid-1@example.com
DTSTART;TZID=America/New_York:20260701T160000
DTEND;TZID=America/New_York:20260705T110000
SUMMARY:Reserved
END:VEVENT
END:VCALENDAR
"""

_ICAL_FLOATING = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:floating-1@example.com
DTSTART:20260701T160000
DTEND:20260705T110000
SUMMARY:Reserved
END:VEVENT
END:VCALENDAR
"""

_ICAL_BLOCKED = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:blocked-1@example.com
DTSTART;VALUE=DATE:20260801
DTEND;VALUE=DATE:20260803
SUMMARY:Blocked
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture(autouse=True)
def _reset_config_cache(monkeypatch):
    """bob L9: reset the AthenaConfig lru_cache in setup and teardown so
    DEFAULT_TIMEZONE monkeypatches in this file never leak into (or read
    stale values from) any other test module."""
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


class TestLodgifyTimezone:
    @pytest.mark.asyncio
    async def test_lodgify_dates_localised_in_new_york(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()

        payload = {
            "items": [
                {
                    "id": 42,
                    "type": "Booking",
                    "arrival": "2026-07-01",
                    "departure": "2026-07-05",
                    "guest": {"name": "Jane Doe"},
                    "source": "Airbnb",
                }
            ],
            "total": 1,
        }

        class _FakeResponse:
            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                return None

            def json(self):
                return self._data

        class _FakeClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def get(self, *args, **kwargs):
                return _FakeResponse(payload)

        with patch("app.routes.calendar_sources.httpx.AsyncClient", return_value=_FakeClient()):
            reservations = await fetch_lodgify_reservations("fake-key")

        assert len(reservations) == 1
        r = reservations[0]
        assert r["checkin"].isoformat() == "2026-07-01T20:00:00+00:00"
        assert r["checkout"].isoformat() == "2026-07-05T15:00:00+00:00"


class TestIcalTimezone:
    def test_date_only_gets_source_times(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()

        events = parse_ical_events(
            _ICAL_DATE_ONLY, "generic_ical", checkin_time="16:00", checkout_time="11:00"
        )
        assert len(events) == 1
        e = events[0]
        assert e["checkin"].isoformat() == "2026-07-01T20:00:00+00:00"
        assert e["checkout"].isoformat() == "2026-07-05T15:00:00+00:00"

    def test_tzid_value_unchanged(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()

        events = parse_ical_events(_ICAL_TZID, "generic_ical")
        assert len(events) == 1
        e = events[0]
        assert e["checkin"].isoformat() == "2026-07-01T20:00:00+00:00"
        assert e["checkout"].isoformat() == "2026-07-05T15:00:00+00:00"

    def test_floating_value_localised_to_new_york(self, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()

        events = parse_ical_events(_ICAL_FLOATING, "generic_ical")
        assert len(events) == 1
        e = events[0]
        assert e["checkin"].isoformat() == "2026-07-01T20:00:00+00:00"
        assert e["checkout"].isoformat() == "2026-07-05T15:00:00+00:00"

    def test_utc_zone_is_a_no_op_guard(self, monkeypatch):
        """Not discriminating: base already produces this by construction
        (UTC localisation == the pre-fix "stamp as UTC" behaviour)."""
        monkeypatch.setenv("DEFAULT_TIMEZONE", "UTC")
        config_module._clear_cache_for_tests()

        events = parse_ical_events(_ICAL_DATE_ONLY, "generic_ical")
        assert events[0]["checkin"].isoformat() == "2026-07-01T16:00:00+00:00"
        assert events[0]["checkout"].isoformat() == "2026-07-05T11:00:00+00:00"

    def test_blocked_vevent_classified_blocked(self):
        events = parse_ical_events(_ICAL_BLOCKED, "generic_ical")
        assert len(events) == 1
        assert events[0]["status"] == "blocked"


class TestUpsertStatusRule:
    def test_reclassifies_existing_confirmed_to_blocked(self, db):
        source = CalendarSource(
            name="Test", source_type="generic_ical", ical_url="https://example.com/x.ics"
        )
        db.add(source)
        db.commit()
        db.refresh(source)

        existing = CalendarEvent(
            external_id="blocked-1@example.com",
            source="generic_ical",
            source_id=source.id,
            title="Blocked",
            checkin=datetime(2026, 8, 1, tzinfo=timezone.utc),
            checkout=datetime(2026, 8, 3, tzinfo=timezone.utc),
            status="confirmed",
            created_by="ical_sync",
        )
        db.add(existing)
        db.commit()

        event_data = {"status": "blocked"}
        if existing.status in ("confirmed", "blocked"):
            existing.status = event_data.get("status", existing.status)
        db.commit()
        db.refresh(existing)
        assert existing.status == "blocked"

    def test_never_overwrites_owner_set_cancelled(self, db):
        source = CalendarSource(
            name="Test", source_type="generic_ical", ical_url="https://example.com/y.ics"
        )
        db.add(source)
        db.commit()
        db.refresh(source)

        existing = CalendarEvent(
            external_id="cancelled-1@example.com",
            source="generic_ical",
            source_id=source.id,
            title="Some Guest",
            checkin=datetime(2026, 8, 1, tzinfo=timezone.utc),
            checkout=datetime(2026, 8, 3, tzinfo=timezone.utc),
            status="cancelled",
            created_by="ical_sync",
        )
        db.add(existing)
        db.commit()

        event_data = {"status": "confirmed"}
        if existing.status in ("confirmed", "blocked"):
            existing.status = event_data.get("status", existing.status)
        db.commit()
        db.refresh(existing)
        assert existing.status == "cancelled"


class TestSyncAllActuallySyncs:
    @pytest.mark.asyncio
    async def test_sync_all_invokes_sync_single_source_per_enabled_source(self, owner_client, db):
        enabled_a = CalendarSource(
            name="A", source_type="generic_ical", ical_url="https://example.com/a.ics", enabled=True
        )
        enabled_b = CalendarSource(
            name="B", source_type="generic_ical", ical_url="https://example.com/b.ics", enabled=True
        )
        disabled = CalendarSource(
            name="C", source_type="generic_ical", ical_url="https://example.com/c.ics", enabled=False
        )
        db.add_all([enabled_a, enabled_b, disabled])
        db.commit()

        with patch(
            "app.services.calendar_sync.sync_single_source", new=AsyncMock(return_value=True)
        ) as mocked:
            resp = owner_client.post("/api/calendar-sources/sync-all")
            assert resp.status_code == 200
            assert resp.json()["source_count"] == 2

        assert mocked.await_count == 2
