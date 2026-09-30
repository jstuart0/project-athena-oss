"""Guest sessions follow their Lodgify event: a deleted, blocked, cancelled
or pending event never creates a session and cancels an upcoming/active
one; restoring the event restores the session. Manual sessions and
completed sessions are never touched."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

import pytest

from app.models import CalendarEvent, CalendarSource, GuestSession

TODAY = date(2026, 9, 10)


@pytest.fixture(autouse=True)
def _pinned_today(monkeypatch):
    monkeypatch.setattr("app.routes.calendar_sources._today", lambda: TODAY)


def _source(db, source_type="lodgify"):
    s = CalendarSource(name=source_type, source_type=source_type, ical_url=f"https://{source_type}.example/x.ics")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _event(db, source, ext_id, start_offset, nights, **kw):
    start = TODAY + timedelta(days=start_offset)
    end = start + timedelta(days=nights)
    defaults = dict(
        external_id=ext_id, source="lodgify", source_id=source.id, status="confirmed",
        created_by="lodgify_api_sync", title="Guest", guest_name="Guest",
        checkin=datetime.combine(start, time(20), tzinfo=timezone.utc),
        checkout=datetime.combine(end, time(15), tzinfo=timezone.utc),
    )
    defaults.update(kw)
    e = CalendarEvent(**defaults)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


async def _sync(db):
    from app.routes.calendar_sources import sync_lodgify_to_guest_sessions

    return await sync_lodgify_to_guest_sessions(db)


def _session(db, ext_id):
    db.expire_all()
    return db.query(GuestSession).filter(GuestSession.lodgify_booking_id == ext_id).first()


@pytest.mark.asyncio
async def test_a_deleted_event_creates_no_session(db):
    source = _source(db)
    _event(db, source, "lodgify_1", -1, 3, deleted_at=datetime.now(timezone.utc))
    await _sync(db)
    assert _session(db, "lodgify_1") is None


@pytest.mark.asyncio
async def test_b_session_follows_delete_and_restore(db):
    source = _source(db)
    event = _event(db, source, "lodgify_2", -1, 3)
    await _sync(db)
    assert _session(db, "lodgify_2").status == "active"

    event.deleted_at = datetime.now(timezone.utc)
    db.commit()
    await _sync(db)
    assert _session(db, "lodgify_2").status == "cancelled"

    event.deleted_at = None
    db.commit()
    await _sync(db)
    assert _session(db, "lodgify_2").status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("gone_status", ["blocked", "cancelled", "pending"])
async def test_c_non_confirmed_event_cancels_upcoming_session(db, gone_status):
    source = _source(db)
    event = _event(db, source, "lodgify_3", 5, 2)
    await _sync(db)
    assert _session(db, "lodgify_3").status == "upcoming"
    event.status = gone_status
    db.commit()
    await _sync(db)
    assert _session(db, "lodgify_3").status == "cancelled"


@pytest.mark.asyncio
async def test_d_completed_session_stays_completed(db):
    source = _source(db)
    event = _event(db, source, "lodgify_4", -10, 3)
    await _sync(db)
    assert _session(db, "lodgify_4").status == "completed"
    event.deleted_at = datetime.now(timezone.utc)
    db.commit()
    await _sync(db)
    assert _session(db, "lodgify_4").status == "completed"


@pytest.mark.asyncio
async def test_e_manual_session_is_untouched(db):
    source = _source(db)
    _event(db, source, "lodgify_5", -1, 3, deleted_at=datetime.now(timezone.utc))
    manual = GuestSession(calendar_event_id=None, lodgify_booking_id=None, guest_name="Walk-in",
                          check_in_date=TODAY - timedelta(days=1), check_out_date=TODAY + timedelta(days=2),
                          status="active")
    db.add(manual)
    db.commit()
    await _sync(db)
    db.expire_all()
    assert db.get(GuestSession, manual.id).status == "active"


@pytest.mark.asyncio
async def test_f_live_confirmed_event_is_unaffected(db):
    source = _source(db)
    _event(db, source, "lodgify_6", 3, 2)
    gone = _event(db, source, "lodgify_7", 8, 2)
    await _sync(db)
    gone.status = "cancelled"
    db.commit()
    await _sync(db)
    assert _session(db, "lodgify_6").status == "upcoming"
    assert _session(db, "lodgify_7").status == "cancelled"


def test_route_runs_the_same_pass(owner_client, db):
    source = _source(db)
    event = _event(db, source, "lodgify_8", 5, 2)
    assert owner_client.post("/api/calendar-sources/sync-guest-sessions").status_code == 200
    assert _session(db, "lodgify_8").status == "upcoming"
    event.deleted_at = datetime.now(timezone.utc)
    db.commit()
    assert owner_client.post("/api/calendar-sources/sync-guest-sessions").status_code == 200
    assert _session(db, "lodgify_8").status == "cancelled"
