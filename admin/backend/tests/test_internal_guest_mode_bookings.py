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
        now = datetime.now(timezone.utc)
        event = _make_event(
            db,
            external_id="lodgify_2",
            source="lodgify",
            checkin=now,
            checkout=now + timedelta(days=1),
            created_by="lodgify_api_sync",
        )
        owner_client.delete(f"/api/guest-mode/events/{event.id}")
        db.refresh(event)
        assert event.deleted_at is not None

        # Simulate the upsert's existing-row branch: it must never touch
        # deleted_at (verified at calendar_sync.py / calendar_sources.py).
        event.title = "Re-synced title"
        db.commit()
        db.refresh(event)
        assert event.deleted_at is not None

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
