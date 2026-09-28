"""ATHENA-127 Phase 2 -- GET /api/internal/guest-mode/bookings (D2/D3), and
the step-6b synced-row soft-delete/cancel that feeds D13's suppression.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.models import CalendarEvent
from shared.config import _clear_cache_for_tests, get_config

BOOKINGS_URL = "/api/internal/guest-mode/bookings"


def _service_key():
    _clear_cache_for_tests()
    return get_config().service_api_key


def _headers():
    return {"X-Service-Key": _service_key()}


def _iso(dt):
    return dt.isoformat()


def _default_window(now=None):
    now = now or datetime.now(timezone.utc)
    return {"start": _iso(now - timedelta(days=1)), "end": _iso(now + timedelta(days=14))}


def _make_event(db, **kwargs):
    defaults = dict(
        source="lodgify",
        status="confirmed",
        created_by="lodgify_api_sync",
        is_test=False,
        title="Booking",
        guest_name=None,
        guest_email=None,
    )
    defaults.update(kwargs)
    event = CalendarEvent(**defaults)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


class TestAuth:
    def test_missing_key_is_401(self, client):
        resp = client.get(BOOKINGS_URL, params=_default_window())
        assert resp.status_code == 401

    def test_wrong_key_is_401(self, client):
        resp = client.get(BOOKINGS_URL, params=_default_window(), headers={"X-Service-Key": "wrong"})
        assert resp.status_code == 401

    def test_unset_service_key_is_503(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_API_KEY", "")
        _clear_cache_for_tests()
        resp = client.get(BOOKINGS_URL, params=_default_window(), headers={"X-Service-Key": "anything"})
        assert resp.status_code == 503
        _clear_cache_for_tests()


class TestContractValidation:
    def test_naive_start_is_422(self, client):
        now = datetime.now(timezone.utc)
        resp = client.get(
            BOOKINGS_URL,
            params={"start": "2026-07-01T00:00:00", "end": _iso(now)},
            headers=_headers(),
        )
        assert resp.status_code == 422

    def test_end_equals_start_is_422(self, client):
        now = datetime.now(timezone.utc)
        resp = client.get(
            BOOKINGS_URL, params={"start": _iso(now), "end": _iso(now)}, headers=_headers()
        )
        assert resp.status_code == 422

    def test_end_before_start_is_422(self, client):
        now = datetime.now(timezone.utc)
        resp = client.get(
            BOOKINGS_URL,
            params={"start": _iso(now), "end": _iso(now - timedelta(hours=1))},
            headers=_headers(),
        )
        assert resp.status_code == 422

    def test_span_over_62_days_is_422(self, client):
        now = datetime.now(timezone.utc)
        resp = client.get(
            BOOKINGS_URL,
            params={"start": _iso(now), "end": _iso(now + timedelta(days=63))},
            headers=_headers(),
        )
        assert resp.status_code == 422


class TestBookingsFilter:
    def test_overlap_edges_excluded(self, client, db):
        now = datetime.now(timezone.utc)
        window_start = now
        window_end = now + timedelta(days=5)

        ends_at_start = _make_event(
            db,
            external_id="ends-at-start",
            checkin=window_start - timedelta(days=2),
            checkout=window_start,
        )
        starts_at_end = _make_event(
            db,
            external_id="starts-at-end",
            checkin=window_end,
            checkout=window_end + timedelta(days=2),
        )
        inside = _make_event(
            db,
            external_id="inside",
            checkin=window_start + timedelta(hours=1),
            checkout=window_start + timedelta(hours=5),
        )

        resp = client.get(
            BOOKINGS_URL,
            params={"start": _iso(window_start), "end": _iso(window_end)},
            headers=_headers(),
        )
        assert resp.status_code == 200
        ids = {b["id"] for b in resp.json()["bookings"]}
        assert ids == {inside.id}

    def test_blocked_cancelled_deleted_excluded_from_bookings(self, client, db):
        now = datetime.now(timezone.utc)
        confirmed = _make_event(
            db, external_id="confirmed-1", checkin=now, checkout=now + timedelta(days=1)
        )
        blocked = _make_event(
            db,
            external_id="blocked-1",
            checkin=now,
            checkout=now + timedelta(days=1),
            status="blocked",
        )
        cancelled = _make_event(
            db,
            external_id="cancelled-1",
            checkin=now,
            checkout=now + timedelta(days=1),
            status="cancelled",
        )
        pending = _make_event(
            db,
            external_id="pending-1",
            checkin=now,
            checkout=now + timedelta(days=1),
            status="pending",
        )
        deleted = _make_event(
            db,
            external_id="deleted-1",
            checkin=now,
            checkout=now + timedelta(days=1),
            deleted_at=datetime.utcnow(),
        )

        resp = client.get(BOOKINGS_URL, params=_default_window(now), headers=_headers())
        body = resp.json()
        # Only 'confirmed' rows appear in bookings -- blocked, cancelled,
        # pending, and soft-deleted are all excluded.
        booking_ids = {b["id"] for b in body["bookings"]}
        assert booking_ids == {confirmed.id}

        # Only soft-deleted and cancelled appear in suppressed -- blocked
        # and pending don't suppress (D13).
        assert len(body["suppressed"]) == 2

    def test_is_test_flagged(self, client, db):
        now = datetime.now(timezone.utc)
        _make_event(
            db, external_id="test-1", checkin=now, checkout=now + timedelta(days=1), is_test=True
        )
        resp = client.get(BOOKINGS_URL, params=_default_window(now), headers=_headers())
        assert resp.json()["bookings"][0]["is_test"] is True

    def test_key_is_hashed_and_opaque(self, client, db):
        now = datetime.now(timezone.utc)
        event = _make_event(
            db, external_id="lodgify_9001", source="lodgify", checkin=now, checkout=now + timedelta(days=1)
        )
        resp = client.get(BOOKINGS_URL, params=_default_window(now), headers=_headers())
        key = resp.json()["bookings"][0]["key"]
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)
        assert key != event.external_id
        expected = hashlib.sha256(f"lodgify|{event.external_id}".encode()).hexdigest()[:16]
        assert key == expected

    def test_pii_negative(self, client, db):
        now = datetime.now(timezone.utc)
        _make_event(
            db,
            external_id="1418fb94e984-zq@example.org@airbnb.com",
            source="airbnb",
            checkin=now,
            checkout=now + timedelta(days=1),
            guest_name="Zelda Quux",
            guest_email="zq@example.org",
            title="Lodgify Booking - Zelda Quux",
        )
        resp = client.get(BOOKINGS_URL, params=_default_window(now), headers=_headers())
        text = resp.text
        for forbidden in ("Zelda", "Quux", "zq@example.org", "1418fb94e984"):
            assert forbidden not in text


class TestSyncedRowDelete:
    def test_delete_synced_row_via_owner_client(self, owner_client, db):
        now = datetime.now(timezone.utc)
        event = _make_event(
            db,
            external_id="lodgify_1",
            source="lodgify",
            checkin=now,
            checkout=now + timedelta(days=1),
            created_by="lodgify_api_sync",
        )
        resp = owner_client.delete(f"/api/guest-mode/events/{event.id}")
        assert resp.status_code == 200
        db.refresh(event)
        assert event.deleted_at is not None

        bookings_resp = owner_client.get(
            BOOKINGS_URL, params=_default_window(now), headers=_headers()
        )
        booking_ids = {b["id"] for b in bookings_resp.json()["bookings"]}
        assert event.id not in booking_ids
        assert len(bookings_resp.json()["suppressed"]) == 1

    def test_resync_does_not_resurrect_deleted_row(self, owner_client, db):
        """Drives the real sync upsert over a soft-deleted synced row the
        feed still lists: it stays deleted and out of `bookings`."""
        import asyncio
        from unittest.mock import AsyncMock, patch

        from app.models import CalendarSource
        from app.services.calendar_sync import sync_single_source

        source = CalendarSource(name="Feed", source_type="generic_ical", ical_url="https://example.com/f.ics")
        db.add(source)
        db.commit()
        db.refresh(source)

        checkin = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        event = _make_event(
            db,
            external_id="resurrect-me@example.com",
            source="generic_ical",
            source_id=source.id,
            checkin=checkin,
            checkout=checkin + timedelta(days=2),
            created_by="ical_sync",
        )
        assert owner_client.delete(f"/api/guest-mode/events/{event.id}").status_code == 200

        feed = (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:resurrect-me@example.com\n"
            f"DTSTART;VALUE=DATE:{checkin:%Y%m%d}\n"
            f"DTEND;VALUE=DATE:{checkin + timedelta(days=2):%Y%m%d}\n"
            "SUMMARY:Reserved\nEND:VEVENT\nEND:VCALENDAR\n"
        )
        with patch("app.routes.calendar_sources.fetch_ical_data", new=AsyncMock(return_value=feed)):
            assert asyncio.run(sync_single_source(source.id, db)) is True

        db.refresh(event)
        assert event.deleted_at is not None
        assert event.synced_at is not None
        body = owner_client.get(BOOKINGS_URL, params=_default_window(checkin), headers=_headers()).json()
        assert event.id not in {b["id"] for b in body["bookings"]}
        assert len(body["suppressed"]) == 1

    def test_viewer_client_forbidden(self, viewer_client, db):
        now = datetime.now(timezone.utc)
        event = _make_event(
            db, external_id="lodgify_3", source="lodgify", checkin=now, checkout=now + timedelta(days=1)
        )
        resp = viewer_client.delete(f"/api/guest-mode/events/{event.id}")
        assert resp.status_code == 403

    def test_unknown_id_is_404(self, owner_client):
        resp = owner_client.delete("/api/guest-mode/events/999999")
        assert resp.status_code == 404


class TestCancelViaPatch:
    def test_cancel_via_patch_suppresses(self, owner_client, db):
        now = datetime.now(timezone.utc)
        event = _make_event(
            db,
            external_id="lodgify_4",
            source="lodgify",
            checkin=now,
            checkout=now + timedelta(days=1),
            created_by="lodgify_api_sync",
        )
        resp = owner_client.patch(f"/api/guest-mode/events/{event.id}", json={"status": "cancelled"})
        assert resp.status_code == 200

        bookings_resp = owner_client.get(
            BOOKINGS_URL, params=_default_window(now), headers=_headers()
        )
        booking_ids = {b["id"] for b in bookings_resp.json()["bookings"]}
        assert event.id not in booking_ids
        assert len(bookings_resp.json()["suppressed"]) == 1


class TestQueryBounds:
    """The stored columns are timestamptz on Postgres: a naive bound there
    is read in the session TimeZone, so only SQLite (naive storage) may get
    the tz stripped."""

    def test_postgres_bound_stays_aware_utc(self):
        from zoneinfo import ZoneInfo
        from app.routes.internal import _utc_query_bound

        local = datetime(2026, 7, 1, 16, 0, tzinfo=ZoneInfo("America/New_York"))
        bound = _utc_query_bound(local, "postgresql")
        assert bound.tzinfo is not None
        assert bound.utcoffset() == timedelta(0)
        assert bound == datetime(2026, 7, 1, 20, 0, tzinfo=timezone.utc)

    def test_sqlite_bound_is_naive_utc(self):
        from zoneinfo import ZoneInfo
        from app.routes.internal import _utc_query_bound

        local = datetime(2026, 7, 1, 16, 0, tzinfo=ZoneInfo("America/New_York"))
        assert _utc_query_bound(local, "sqlite") == datetime(2026, 7, 1, 20, 0)


class TestOverlapStraddlingStart:
    def test_row_starting_before_window_and_ending_inside_is_returned(self, client, db):
        start = datetime(2026, 7, 10, 12, 0, tzinfo=timezone.utc)
        end = start + timedelta(days=5)
        straddler = _make_event(
            db,
            external_id="straddles-start",
            checkin=start - timedelta(days=3),
            checkout=start + timedelta(days=1),
        )
        resp = client.get(BOOKINGS_URL, params={"start": _iso(start), "end": _iso(end)}, headers=_headers())
        assert resp.status_code == 200
        assert [b["id"] for b in resp.json()["bookings"]] == [straddler.id]


class TestEditStatusValidation:
    def _synced_row(self, db, status="confirmed"):
        now = datetime.now(timezone.utc)
        return _make_event(
            db, external_id=f"lodgify_status_{status}", checkin=now, checkout=now + timedelta(days=1),
            status=status,
        )

    @pytest.mark.parametrize("bad", ["", "bogus", "Confirmed"])
    def test_invalid_status_is_422_and_row_unchanged(self, owner_client, db, bad):
        event = self._synced_row(db)
        resp = owner_client.patch(f"/api/guest-mode/events/{event.id}", json={"status": bad})
        assert resp.status_code == 422
        db.refresh(event)
        assert event.status == "confirmed"

    def test_blocked_row_round_trips(self, owner_client, db):
        event = self._synced_row(db, status="blocked")
        resp = owner_client.patch(
            f"/api/guest-mode/events/{event.id}", json={"status": "blocked", "notes": "owner note"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "blocked"
        db.refresh(event)
        assert event.status == "blocked"
        assert event.notes == "owner note"


class TestContractBoundaries:
    def test_naive_end_is_422(self, client):
        now = datetime.now(timezone.utc)
        resp = client.get(
            BOOKINGS_URL, params={"start": _iso(now), "end": "2099-07-01T00:00:00"}, headers=_headers()
        )
        assert resp.status_code == 422

    def test_exactly_62_days_is_allowed_and_one_second_more_is_not(self, client):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        ok = client.get(BOOKINGS_URL, params={"start": _iso(start), "end": _iso(start + timedelta(days=62))}, headers=_headers())
        over = client.get(
            BOOKINGS_URL,
            params={"start": _iso(start), "end": _iso(start + timedelta(days=62, seconds=1))},
            headers=_headers(),
        )
        assert ok.status_code == 200
        assert over.status_code == 422
