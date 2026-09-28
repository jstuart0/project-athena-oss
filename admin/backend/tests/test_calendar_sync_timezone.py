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

        # Non-default times, so a parser that ignores the kwargs and falls
        # back to 16:00/11:00 fails here.
        events = parse_ical_events(
            _ICAL_DATE_ONLY, "generic_ical", checkin_time="15:00", checkout_time="10:00"
        )
        assert len(events) == 1
        e = events[0]
        assert e["checkin"].isoformat() == "2026-07-01T19:00:00+00:00"
        assert e["checkout"].isoformat() == "2026-07-05T14:00:00+00:00"

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


_ICAL_UPSERT = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:reclass-1@example.com
DTSTART;VALUE=DATE:20260801
DTEND;VALUE=DATE:20260803
SUMMARY:Blocked
END:VEVENT
BEGIN:VEVENT
UID:owner-cancelled-1@example.com
DTSTART;VALUE=DATE:20260810
DTEND;VALUE=DATE:20260812
SUMMARY:Reserved
END:VEVENT
BEGIN:VEVENT
UID:soft-deleted-1@example.com
DTSTART;VALUE=DATE:20260820
DTEND;VALUE=DATE:20260822
SUMMARY:Reserved
END:VEVENT
BEGIN:VEVENT
UID:new-stay-1@example.com
DTSTART;VALUE=DATE:20260901
DTEND;VALUE=DATE:20260905
SUMMARY:Reserved
END:VEVENT
END:VCALENDAR
"""


def _seed_upsert_rows(db):
    """A source with non-default times plus three existing rows the feed
    re-lists: a confirmed row the feed now marks Blocked, an owner-cancelled
    row, and an owner soft-deleted row."""
    source = CalendarSource(
        name="Upsert", source_type="generic_ical", ical_url="https://example.com/u.ics",
        default_checkin_time="15:00", default_checkout_time="10:00",
    )
    db.add(source)
    db.commit()
    db.refresh(source)

    def row(uid, status, deleted_at=None):
        event = CalendarEvent(
            external_id=uid, source="generic_ical", source_id=source.id, title="Guest",
            checkin=datetime(2026, 1, 1, tzinfo=timezone.utc),
            checkout=datetime(2026, 1, 2, tzinfo=timezone.utc),
            status=status, created_by="ical_sync", deleted_at=deleted_at,
        )
        db.add(event)
        return event

    reclass = row("reclass-1@example.com", "confirmed")
    cancelled = row("owner-cancelled-1@example.com", "cancelled")
    deleted = row("soft-deleted-1@example.com", "confirmed", deleted_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    db.commit()
    return source, reclass, cancelled, deleted


def _assert_upsert_outcome(db, reclass, cancelled, deleted):
    from shared.booking_window import db_value_to_utc

    for event in (reclass, cancelled, deleted):
        db.refresh(event)
    assert reclass.status == "blocked"
    assert cancelled.status == "cancelled"
    assert deleted.deleted_at is not None
    # The feed still rewrites times on existing rows, with the source's own
    # 15:00/10:00 in New York (EDT, UTC-4).
    assert db_value_to_utc(cancelled.checkin).isoformat() == "2026-08-10T19:00:00+00:00"
    assert db_value_to_utc(cancelled.checkout).isoformat() == "2026-08-12T14:00:00+00:00"

    new_row = db.query(CalendarEvent).filter(CalendarEvent.external_id == "new-stay-1@example.com").one()
    assert new_row.status == "confirmed"
    assert db_value_to_utc(new_row.checkin).isoformat() == "2026-09-01T19:00:00+00:00"
    assert db_value_to_utc(new_row.checkout).isoformat() == "2026-09-05T14:00:00+00:00"


class TestRealUpsertStatusRule:
    """D11 through the two real upsert loops (not a re-implementation of
    the rule): confirmed -> blocked is reclassified, an owner-set cancelled
    is never overwritten, a soft-deleted row stays deleted."""

    @pytest.mark.asyncio
    async def test_sync_single_source(self, db, monkeypatch):
        from app.services.calendar_sync import sync_single_source

        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()
        source, reclass, cancelled, deleted = _seed_upsert_rows(db)

        with patch("app.routes.calendar_sources.fetch_ical_data", new=AsyncMock(return_value=_ICAL_UPSERT)):
            assert await sync_single_source(source.id, db) is True

        _assert_upsert_outcome(db, reclass, cancelled, deleted)

    def test_sync_route(self, owner_client, db, monkeypatch):
        monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
        config_module._clear_cache_for_tests()
        source, reclass, cancelled, deleted = _seed_upsert_rows(db)

        with patch("app.routes.calendar_sources.fetch_ical_data", new=AsyncMock(return_value=_ICAL_UPSERT)):
            resp = owner_client.post(f"/api/calendar-sources/{source.id}/sync")
        assert resp.status_code == 200
        assert resp.json()["success"] is True, resp.json()

        _assert_upsert_outcome(db, reclass, cancelled, deleted)


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
