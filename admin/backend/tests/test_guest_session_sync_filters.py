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


# ---------------------------------------------------------------------------
# r3.2: a UID migration keeps one session; guest-session failures surface;
# deleting a source cancels its live sessions
# ---------------------------------------------------------------------------

def _derived(source_id, uid):
    import hashlib

    return f"src:{source_id}:" + hashlib.sha256(uid.encode()).hexdigest()[:32]


def _feed(uid, start, end, summary="J*** D**"):
    return (
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\n"
        f"UID:{uid}\nDTSTART;VALUE=DATE:{start:%Y%m%d}\nDTEND;VALUE=DATE:{end:%Y%m%d}\n"
        f"SUMMARY:{summary}\nEND:VEVENT\nEND:VCALENDAR\n"
    )


@pytest.fixture
def _ny(monkeypatch):
    from shared import config as config_module

    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


@pytest.mark.asyncio
async def test_legacy_uid_migration_keeps_exactly_one_session(db, _ny):
    from unittest.mock import AsyncMock, patch
    from app.services import calendar_sync

    source = _source(db)
    event = _event(db, source, "lodgify_101", -1, 3, created_by="ical_sync", title="J*** D**")
    await _sync(db)
    session = _session(db, "lodgify_101")
    assert session is not None and session.calendar_event_id == event.id

    start = TODAY - timedelta(days=1)
    with patch("app.routes.calendar_sources.fetch_ical_data",
               new=AsyncMock(return_value=_feed("lodgify_101", start, start + timedelta(days=3)))):
        outcome = await calendar_sync.run_source_sync(source.id, db, trigger="manual")
    assert outcome.status == "success", outcome

    db.expire_all()
    sessions = db.query(GuestSession).all()
    assert len(sessions) == 1
    assert sessions[0].id == session.id
    assert sessions[0].lodgify_booking_id == _derived(source.id, "lodgify_101")
    assert sessions[0].calendar_event_id == event.id
    assert sessions[0].status == "active"


@pytest.mark.asyncio
async def test_guest_session_failure_surfaces_as_a_warning(db, _ny, monkeypatch):
    from unittest.mock import AsyncMock, patch
    from app.services import calendar_sync

    def boom(_db):
        raise RuntimeError("guest pass exploded")

    monkeypatch.setattr("app.routes.calendar_sources._cancel_sessions_for_gone_events", boom)
    source = _source(db)
    start = TODAY + timedelta(days=5)
    with patch("app.routes.calendar_sources.fetch_ical_data",
               new=AsyncMock(return_value=_feed("gs-fail@x", start, start + timedelta(days=2)))):
        outcome = await calendar_sync.run_source_sync(source.id, db, trigger="manual")

    assert outcome.status == "success"
    assert outcome.warning == "Guest sessions could not be updated (RuntimeError); bookings were saved"
    db.expire_all()
    assert db.query(CalendarEvent).filter(CalendarEvent.source_id == source.id).count() == 1
    db.refresh(source)
    assert source.last_sync_status == "success"
    assert source.last_sync_error == outcome.warning
    assert "exploded" not in (source.last_sync_error or "")


def test_guest_session_failure_shows_in_the_sync_message(owner_client, db, _ny, monkeypatch):
    from unittest.mock import AsyncMock, patch

    monkeypatch.setattr("app.routes.calendar_sources.update_guest_session_statuses",
                        AsyncMock(return_value={"error": "OperationalError"}))
    source = _source(db)
    start = TODAY + timedelta(days=5)
    with patch("app.routes.calendar_sources.fetch_ical_data",
               new=AsyncMock(return_value=_feed("gs-fail2@x", start, start + timedelta(days=2)))):
        resp = owner_client.post(f"/api/calendar-sources/{source.id}/sync")
    body = resp.json()
    assert body["success"] is True
    assert body["message"] == (
        "Synced via ical. Guest sessions could not be updated (OperationalError); bookings were saved"
    )


def test_deleting_a_source_cancels_its_live_sessions(owner_client, db):
    source = _source(db)
    other = _source(db, source_type="lodgify")
    active_ev = _event(db, source, "lodgify_d1", -1, 3)
    upcoming_ev = _event(db, source, "lodgify_d2", 5, 2)
    done_ev = _event(db, source, "lodgify_d3", -10, 2)
    other_ev = _event(db, other, "lodgify_d4", 5, 2)
    import asyncio
    asyncio.run(_sync(db))
    manual = GuestSession(calendar_event_id=None, lodgify_booking_id=None, guest_name="Walk-in",
                          check_in_date=TODAY, check_out_date=TODAY + timedelta(days=1), status="active")
    db.add(manual)
    db.commit()
    assert _session(db, "lodgify_d1").status == "active"

    resp = owner_client.delete(f"/api/calendar-sources/{source.id}")
    assert resp.status_code == 204

    assert _session(db, "lodgify_d1").status == "cancelled"
    assert _session(db, "lodgify_d2").status == "cancelled"
    assert _session(db, "lodgify_d3").status == "completed"
    assert _session(db, "lodgify_d4").status == "upcoming"
    db.expire_all()
    assert db.get(GuestSession, manual.id).status == "active"
    assert {active_ev.id, upcoming_ev.id, done_ev.id, other_ev.id}
