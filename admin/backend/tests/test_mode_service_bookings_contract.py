"""ATHENA-127 Phase 3 -- seam contract test (tessa H1).

Runs the real mode-service `BookingSources.refresh()` against the real
admin-backend FastAPI `app` over `httpx.ASGITransport`, proving the two
sides actually agree on the wire contract rather than each side's own
mocked idea of it.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.models import CalendarEvent
from shared.config import _clear_cache_for_tests, get_config

import mode_service.bookings as ms_bookings

# Pinned clock and zone: the iCal twin's day pair and the fetch window must
# not depend on when the suite runs.
_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _utc_property_zone(monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "UTC")
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def _make_event(db, **kwargs):
    defaults = dict(
        source="lodgify",
        status="confirmed",
        created_by="lodgify_api_sync",
        is_test=False,
        title="Booking",
    )
    defaults.update(kwargs)
    event = CalendarEvent(**defaults)
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def _admin_asgi_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://admin.test"
    )


class TestSeamContract:
    def test_snapshot_matches_seeded_rows(self, client, db):
        """`client` fixture (conftest) already wired `app.dependency_overrides[get_db]`
        to this test's `db` session -- the same app instance is reused here."""
        from main import app

        _clear_cache_for_tests()
        service_key = get_config().service_api_key

        now = _NOW
        confirmed = _make_event(
            db, external_id="lodgify_1", checkin=now, checkout=now + timedelta(days=1)
        )
        blocked = _make_event(
            db,
            external_id="blocked_1",
            checkin=now,
            checkout=now + timedelta(days=1),
            status="blocked",
        )
        deleted = _make_event(
            db,
            external_id="deleted_1",
            checkin=now,
            checkout=now + timedelta(days=1),
            deleted_at=datetime.utcnow(),
        )
        test_row = _make_event(
            db,
            external_id="lodgify_test_1",
            checkin=now,
            checkout=now + timedelta(days=1),
            is_test=True,
        )

        bs = ms_bookings.BookingSources()
        admin_client = _admin_asgi_client(app)
        config = {"enabled": True}

        asyncio.run(bs.refresh(config, now=now, admin_client=admin_client))

        assert bs._admin.last_attempt_ok is True
        ids = {b.id for b in bs._admin.last_good}
        assert ids == {confirmed.id, test_row.id}

        by_id = {b.id: b for b in bs._admin.last_good}
        assert by_id[test_row.id].is_test is True
        assert by_id[confirmed.id].is_test is False
        for booking in by_id.values():
            assert booking.start == _NOW
            assert booking.end == _NOW + timedelta(days=1)

        # The soft-deleted row only (blocked doesn't suppress), as UTC instants.
        assert bs._suppressed_rows == [
            {"checkin": _NOW.isoformat(), "checkout": (_NOW + timedelta(days=1)).isoformat()}
        ]

    def test_wrong_key_is_recorded_as_failed_not_empty(self, client, db, monkeypatch):
        """Both sides read SERVICE_API_KEY from the same process env, so an
        env-var change would flip what admin-backend itself expects too.
        Monkeypatching bookings.py's own `get_config` reference is the only
        way to make just the mode-service SIDE of this seam send a wrong
        key while the real admin app keeps expecting the real one."""
        from main import app

        _clear_cache_for_tests()
        now = _NOW
        _make_event(db, external_id="lodgify_2", checkin=now, checkout=now + timedelta(days=1))

        real_cfg = get_config()
        fake_cfg = SimpleNamespace(**{**real_cfg.model_dump(), "service_api_key": "wrong-key"})
        monkeypatch.setattr(ms_bookings, "get_config", lambda: fake_cfg)

        bs = ms_bookings.BookingSources()
        admin_client = _admin_asgi_client(app)
        asyncio.run(bs.refresh({"enabled": True}, now=now, admin_client=admin_client))

        assert bs._admin.last_attempt_ok is False
        assert bs._admin.last_good == []


class TestEndToEndSuppression:
    """codex r2 High: a synced admin row soft-deleted or cancelled through
    the REAL app must suppress its legacy-iCal twin (same local day pair,
    different UID) end-to-end -- admin over ASGI, iCal over MockTransport."""

    def _seed_twin(self, owner_client, db):
        now = _NOW
        admin_row = _make_event(
            db,
            external_id="lodgify_9",
            checkin=now,
            checkout=now + timedelta(days=1),
            created_by="lodgify_api_sync",
        )
        checkin_date = now.strftime("%Y%m%d")
        checkout_date = (now + timedelta(days=1)).strftime("%Y%m%d")
        ical = (
            "BEGIN:VCALENDAR\r\n"
            "BEGIN:VEVENT\r\n"
            "UID:legacy-twin-uid@example.com\r\n"
            f"DTSTART;VALUE=DATE:{checkin_date}\r\n"
            f"DTEND;VALUE=DATE:{checkout_date}\r\n"
            "SUMMARY:Reserved\r\n"
            "END:VEVENT\r\n"
            "END:VCALENDAR\r\n"
        ).encode()
        return admin_row, now, ical

    def _run_refresh(self, app, now, ical_bytes):
        bs = ms_bookings.BookingSources()
        admin_client = _admin_asgi_client(app)

        def ical_handler(request):
            return httpx.Response(200, content=ical_bytes)

        def ical_factory(timeout=30.0):
            return httpx.AsyncClient(transport=httpx.MockTransport(ical_handler), timeout=timeout)

        config = {"enabled": True, "calendar_url": "https://example.com/x.ics"}
        asyncio.run(bs.refresh(config, now=now, admin_client=admin_client, ical_client_factory=ical_factory))
        snapshot = bs.snapshot(config, now=now, now_monotonic=0.0)
        return bs, snapshot

    def test_soft_delete_suppresses_ical_twin(self, owner_client, db):
        from main import app

        _clear_cache_for_tests()
        admin_row, now, ical_bytes = self._seed_twin(owner_client, db)

        resp = owner_client.delete(f"/api/guest-mode/events/{admin_row.id}")
        assert resp.status_code == 200

        bs, snapshot = self._run_refresh(app, now, ical_bytes)
        assert bs._admin.last_good == []
        assert len(bs._ical.last_good) == 1  # the twin was loaded, so its absence below is suppression
        assert snapshot.bookings == []

    def test_cancel_via_patch_suppresses_ical_twin(self, owner_client, db):
        from main import app

        _clear_cache_for_tests()
        admin_row, now, ical_bytes = self._seed_twin(owner_client, db)

        resp = owner_client.patch(f"/api/guest-mode/events/{admin_row.id}", json={"status": "cancelled"})
        assert resp.status_code == 200

        bs, snapshot = self._run_refresh(app, now, ical_bytes)
        assert bs._admin.last_good == []
        assert len(bs._ical.last_good) == 1
        assert snapshot.bookings == []

    def test_control_without_delete_is_guest_with_one_merged_booking(self, owner_client, db):
        """Control: without the delete/cancel, the merged snapshot holds
        exactly one booking (the admin row and its iCal twin collapse via
        D13's day-pair union) and it's active `now` -- i.e. guest."""
        from main import app
        from datetime import timedelta as _td
        from shared.booking_window import active_booking

        _clear_cache_for_tests()
        admin_row, now, ical_bytes = self._seed_twin(owner_client, db)

        bs, snapshot = self._run_refresh(app, now, ical_bytes)
        assert len(bs._ical.last_good) == 1
        assert len(snapshot.bookings) == 1
        assert active_booking(snapshot.bookings, now, _td(hours=2), _td(hours=1)) is not None
