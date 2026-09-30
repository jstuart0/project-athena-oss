"""iCal dedupe: a feed re-fetched with fresh UIDs inserts nothing new, keys
are scoped to their source, owner deletes/cancels stick, and no event is
ever dropped (every ambiguity inserts, which fails toward guest mode)."""
from __future__ import annotations

import hashlib
import os
import time
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import structlog

from app.models import CalendarEvent, CalendarSource, ExternalAPIKey, GuestSession, SystemSetting
from shared import config as config_module
from shared.booking_window import db_value_to_utc, localize_stay, resolve_property_tz
from shared.config import get_config
from tests.fixtures.lodgify_feed_shape import (
    CLOSED_PERIOD,
    LODGIFY_SHAPE_WINDOWS,
    SLICE_0930,
    build_lodgify_shape_feed,
    ical,
)

ICAL = "app.routes.calendar_sources.fetch_ical_data"
API = "app.routes.calendar_sources.fetch_lodgify_reservations"
BOOKINGS_URL = "/api/internal/guest-mode/bookings"
T0 = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def _derived(source_id, uid):
    return f"src:{source_id}:" + hashlib.sha256(uid.encode()).hexdigest()[:32]


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, minutes=30):
        self.now = self.now + timedelta(minutes=minutes)


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


@pytest.fixture
def clock(monkeypatch):
    from app.services import calendar_sync

    c = Clock()
    monkeypatch.setattr(calendar_sync, "_now", c)
    return c


def _source(db, **kw):
    defaults = dict(name="Lodgify", source_type="lodgify", ical_url="https://www.lodgify.com/export/x.ics")
    defaults.update(kw)
    s = CalendarSource(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _event(db, **kw):
    defaults = dict(source="lodgify", status="confirmed", created_by="ical_sync", title="Seeded")
    defaults.update(kw)
    e = CalendarEvent(**defaults)
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def _stay(start, end, hhmm_in="16:00", hhmm_out="11:00", tzname="America/New_York"):
    tz, _ = resolve_property_tz(tzname)
    return localize_stay(start, end, hhmm_in, hhmm_out, tz)


def _snapshot(db, event_id):
    db.expire_all()
    row = db.get(CalendarEvent, event_id)
    return {c.name: getattr(row, c.name) for c in CalendarEvent.__table__.columns}


def _rows(db, source_id=None):
    db.expire_all()
    q = db.query(CalendarEvent)
    if source_id is not None:
        q = q.filter(CalendarEvent.source_id == source_id)
    return q.order_by(CalendarEvent.id).all()


def _pair(row, tzname="America/New_York"):
    from shared.booking_window import day_pair

    tz, _ = resolve_property_tz(tzname)
    return day_pair(row.checkin, row.checkout, tz)


async def _sync(db, source, feed, clock=None):
    from app.services import calendar_sync

    with patch(ICAL, new=AsyncMock(return_value=feed)) as mocked:
        outcome = await calendar_sync.run_source_sync(source.id, db, trigger="manual")
    if clock is not None:
        clock.advance()
    return outcome, mocked


def _bookings(client, start, end):
    key = get_config().service_api_key
    resp = client.get(
        BOOKINGS_URL,
        params={"start": start.isoformat(), "end": end.isoformat()},
        headers={"X-Service-Key": key},
    )
    assert resp.status_code == 200, resp.text
    return {b["id"] for b in resp.json()["bookings"]}


def _one(start, end, summary="Reserved", uid=None):
    return {"uid": uid, "start": start, "end": end, "summary": summary}


# ---------------------------------------------------------------------------
# (a) replay, (b) sticky delete, (c) sticky cancel, (d) same-date block+booking,
# (e) stable UIDs, (f) a served API row on the pair
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_replay_with_fresh_uids_inserts_nothing(db, clock):
    source = _source(db)
    feed1, uids1 = build_lodgify_shape_feed()
    out1, _ = await _sync(db, source, feed1, clock)
    assert out1.status == "success" and out1.added == 9
    keys1 = {r.external_id for r in _rows(db)}
    sessions1 = db.query(GuestSession).count()

    feed2, uids2 = build_lodgify_shape_feed()
    assert set(uids1).isdisjoint(uids2)
    out2, mocked = await _sync(db, source, feed2, clock)
    assert mocked.await_count == 1
    assert out2.added == 0
    assert {r.external_id for r in _rows(db)} == keys1
    assert len(_rows(db)) == 9
    assert db.query(GuestSession).count() == sessions1
    assert {s.lodgify_booking_id for s in db.query(GuestSession).all()} <= keys1

    by_pair = {_pair(r): r for r in _rows(db)}
    assert by_pair[CLOSED_PERIOD].status == "blocked"
    assert by_pair[SLICE_0930].status == "confirmed"


@pytest.mark.asyncio
async def test_b_sticky_delete(db, clock):
    source = _source(db)
    await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    slice_row = next(r for r in _rows(db) if _pair(r) == SLICE_0930)
    slice_row.deleted_at = clock()
    db.commit()

    out, _ = await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    assert out.added == 0
    assert out.matched_deleted == 1
    db.expire_all()
    assert db.get(CalendarEvent, slice_row.id).deleted_at is not None
    assert len(_rows(db)) == 9


@pytest.mark.asyncio
async def test_c_sticky_cancel(db, clock):
    source = _source(db)
    await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    slice_row = next(r for r in _rows(db) if _pair(r) == SLICE_0930)
    slice_row.status = "cancelled"
    db.commit()

    out, _ = await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    assert out.added == 0 and out.matched_deleted == 1
    db.expire_all()
    assert db.get(CalendarEvent, slice_row.id).status == "cancelled"


@pytest.mark.asyncio
async def test_d_same_dates_block_and_booking_is_one_confirmed_row(db, clock):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/d.ics")
    p = (date(2026, 11, 1), date(2026, 11, 3))
    feed = ical([_one(*p, "Blocked", "blk@x"), _one(*p, "Jane Guest", "bk@x")])
    out, _ = await _sync(db, source, feed, clock)
    rows = _rows(db)
    assert len(rows) == 1 and rows[0].status == "confirmed" and out.added == 1


@pytest.mark.asyncio
async def test_e_stable_uids_update_in_place(db, clock):
    source = _source(db)
    feed, uids = build_lodgify_shape_feed()
    await _sync(db, source, feed, clock)
    out, _ = await _sync(db, source, build_lodgify_shape_feed(uids)[0], clock)
    assert out.added == 0 and out.updated == 9
    assert {r.external_id for r in _rows(db)} == set(uids)


@pytest.mark.asyncio
async def test_f_served_api_row_on_the_pair_suppresses_the_insert(db, clock):
    source = _source(db)
    p = (date(2026, 11, 1), date(2026, 11, 3))
    ci, co = _stay(*p)
    api_row = _event(db, external_id="lodgify_900", source_id=source.id, created_by="lodgify_api_sync",
                     checkin=ci, checkout=co, status="confirmed")
    before = _snapshot(db, api_row.id)
    out, _ = await _sync(db, source, ical([_one(*p, "J*** D**", "fresh@x")]), clock)
    assert out.added == 0 and out.matched_non_ical == 1
    assert _snapshot(db, api_row.id) == before
    assert len(_rows(db)) == 1


# ---------------------------------------------------------------------------
# (x) a non-ical row /bookings doesn't serve never suppresses the event
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["deleted", "cancelled", "blocked", "pending"])
async def test_x_unserved_non_ical_row_does_not_suppress(client, db, clock, state):
    source = _source(db)
    p = (date(2026, 11, 1), date(2026, 11, 3))
    ci, co = _stay(*p)
    api_row = _event(
        db, external_id="lodgify_901", source_id=source.id, created_by="lodgify_api_sync", checkin=ci, checkout=co,
        status="confirmed" if state == "deleted" else state,
        deleted_at=T0 if state == "deleted" else None,
    )
    before = _snapshot(db, api_row.id)
    out, _ = await _sync(db, source, ical([_one(*p, "J*** D**", "fresh-x@x")]), clock)
    assert out.added == 1 and out.matched_non_ical == 0
    new = [r for r in _rows(db) if r.created_by == "ical_sync"]
    assert len(new) == 1 and new[0].status == "confirmed" and _pair(new[0]) == p
    assert new[0].id in _bookings(client, ci - timedelta(days=1), co + timedelta(days=1))
    assert _snapshot(db, api_row.id) == before


# ---------------------------------------------------------------------------
# (y) a legacy raw reserved UID on the same source is hit and migrated
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_y_legacy_raw_reserved_uid_is_migrated(db, clock):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/y.ics")
    p1 = (date(2026, 11, 1), date(2026, 11, 3))
    p2 = (date(2026, 11, 10), date(2026, 11, 12))
    ci, co = _stay(*p1)
    legacy = _event(db, external_id="lodgify_101", source_id=source.id, created_by="ical_sync",
                    source="generic_ical", checkin=ci, checkout=co, title="Reserved")

    with structlog.testing.capture_logs() as logs:
        out, _ = await _sync(db, source, ical([_one(*p2, "Reserved", "lodgify_101")]), clock)

    rows = _rows(db)
    assert [r.id for r in rows] == [legacy.id]
    assert rows[0].external_id == _derived(source.id, "lodgify_101")
    assert _pair(rows[0]) == p2 and rows[0].status == "confirmed"
    assert out.added == 0
    migrated = [e for e in logs if e["event"] == "calendar_sync_legacy_reserved_uid_migrated"]
    assert len(migrated) == 1
    assert "lodgify_101" not in repr(logs)


@pytest.mark.asyncio
async def test_y_control_other_sources_raw_row_is_untouched(db, clock):
    g = _source(db, name="G", source_type="generic_ical", ical_url="https://feed.example.com/g.ics")
    s = _source(db, name="S", source_type="generic_ical", ical_url="https://feed.example.com/s.ics")
    p1 = (date(2026, 11, 1), date(2026, 11, 3))
    p2 = (date(2026, 11, 10), date(2026, 11, 12))
    ci, co = _stay(*p1)
    g_row = _event(db, external_id="lodgify_101", source_id=g.id, created_by="ical_sync", checkin=ci, checkout=co)
    before = _snapshot(db, g_row.id)
    await _sync(db, s, ical([_one(*p2, "Reserved", "lodgify_101")]), clock)
    assert _snapshot(db, g_row.id) == before
    mine = _rows(db, s.id)
    assert len(mine) == 1 and mine[0].external_id == _derived(s.id, "lodgify_101") and _pair(mine[0]) == p2


# ---------------------------------------------------------------------------
# (g) stable hit + fresh block on the same dates, both orders
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("block_first", [True, False])
async def test_g_stable_hit_plus_fresh_block(db, clock, block_first):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/g.ics")
    p = (date(2026, 11, 1), date(2026, 11, 3))
    await _sync(db, source, ical([_one(*p, "Jane Guest", "stable@x")]), clock)
    members = [_one(*p, "Jane Guest", "stable@x"), _one(*p, "Blocked", "fresh-block@x")]
    if block_first:
        members.reverse()
    out, _ = await _sync(db, source, ical(members), clock)
    rows = _rows(db)
    assert len(rows) == 1 and rows[0].status == "confirmed" and out.added == 0


# ---------------------------------------------------------------------------
# (h) Auckland, (i) non-UTC process zone, (j) DST days, (k) check-in time
# change, (l) timed event whose UTC date is local + 1
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_h_auckland_replay(db, clock, monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "Pacific/Auckland")
    config_module._clear_cache_for_tests()
    source = _source(db)
    await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    out, _ = await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    assert out.added == 0 and len(_rows(db)) == 9


@pytest.fixture
def la_process_zone():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


@pytest.mark.asyncio
async def test_i_non_utc_process_zone(db, clock, la_process_zone):
    source = _source(db)
    await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    out, _ = await _sync(db, source, build_lodgify_shape_feed()[0], clock)
    assert out.added == 0 and len(_rows(db)) == 9


@pytest.mark.asyncio
async def test_j_dst_days(db, clock):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/j.ics")
    spring = (date(2026, 3, 8), date(2026, 3, 9))
    fall = (date(2026, 11, 1), date(2026, 11, 2))
    await _sync(db, source, ical([_one(*spring), _one(*fall)]), clock)
    assert {_pair(r) for r in _rows(db)} == {spring, fall}
    out, _ = await _sync(db, source, ical([_one(*spring), _one(*fall)]), clock)
    assert out.added == 0 and len(_rows(db)) == 2


@pytest.mark.asyncio
async def test_k_checkin_time_change(db, clock):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/k.ics")
    p = (date(2026, 11, 5), date(2026, 11, 8))
    await _sync(db, source, ical([_one(*p)]), clock)
    source.default_checkin_time = "15:00"
    db.commit()
    out, _ = await _sync(db, source, ical([_one(*p)]), clock)
    rows = _rows(db)
    assert out.added == 0 and len(rows) == 1
    assert db_value_to_utc(rows[0].checkin) == _stay(*p, hhmm_in="15:00")[0]


@pytest.mark.asyncio
async def test_l_timed_event_crossing_utc_midnight(db, clock):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/l.ics")
    ev = {"start": "20261010T030000Z", "end": "20261012T150000Z", "summary": "Reserved"}
    await _sync(db, source, ical([{**ev, "uid": "t1@x"}]), clock)
    row = _rows(db)[0]
    assert _pair(row) == (date(2026, 10, 9), date(2026, 10, 12))
    out, _ = await _sync(db, source, ical([{**ev, "uid": "t2@x"}]), clock)
    assert out.added == 0 and len(_rows(db)) == 1


# ---------------------------------------------------------------------------
# (m) cross-source, three owners
# ---------------------------------------------------------------------------

REKEY_2 = "2 events stored under a source-specific ID because their IDs are used elsewhere"


@pytest.mark.asyncio
async def test_m_cross_source_three_owners(owner_client, db, clock):
    lodgify = _source(db, name="A")
    other = _source(db, name="O", source_type="generic_ical", ical_url="https://feed.example.com/o.ics")
    g = _source(db, name="G", source_type="generic_ical", ical_url="https://feed.example.com/g.ics")
    p_api = (date(2026, 11, 1), date(2026, 11, 3))
    p_other = (date(2026, 11, 10), date(2026, 11, 12))
    p_orphan = (date(2026, 11, 20), date(2026, 11, 22))
    api_row = _event(db, external_id="lodgify_77", source_id=lodgify.id, created_by="lodgify_api_sync",
                     checkin=_stay(*p_api)[0], checkout=_stay(*p_api)[1])
    other_row = _event(db, external_id="shared-uid@example.com", source_id=other.id, source="generic_ical",
                       checkin=_stay(*p_other)[0], checkout=_stay(*p_other)[1])
    orphan_row = _event(db, external_id="orphan-uid@example.com", source_id=None, source="generic_ical",
                        checkin=_stay(*p_orphan)[0], checkout=_stay(*p_orphan)[1])
    owners = {r.id: _snapshot(db, r.id) for r in (api_row, other_row, orphan_row)}

    feed = ical([
        _one(*p_api, "Blocked", "lodgify_77"),
        _one(*p_other, "Reserved", "shared-uid@example.com"),
        _one(*p_orphan, "Reserved", "orphan-uid@example.com"),
    ])
    with structlog.testing.capture_logs() as logs, patch(ICAL, new=AsyncMock(return_value=feed)):
        resp = owner_client.post(f"/api/calendar-sources/{g.id}/sync")
    clock.advance()
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True, body
    assert REKEY_2 in body["message"]
    assert body["events_rekeyed"] == 2

    for rid, snap in owners.items():
        assert _snapshot(db, rid) == snap
    mine = {r.external_id: r for r in _rows(db, g.id)}
    assert set(mine) == {
        _derived(g.id, "lodgify_77"), _derived(g.id, "shared-uid@example.com"), _derived(g.id, "orphan-uid@example.com"),
    }
    assert mine[_derived(g.id, "lodgify_77")].status == "blocked"
    assert mine[_derived(g.id, "shared-uid@example.com")].status == "confirmed"
    db.refresh(g)
    assert g.last_sync_error == REKEY_2
    for raw in ("lodgify_77", "shared-uid@example.com", "orphan-uid@example.com"):
        assert raw not in repr(logs)
    assert sum(e["event"] == "calendar_sync_uid_collision_rekeyed" for e in logs) == 2

    out, _ = await _sync(db, g, ical([_one(date(2026, 12, 1), date(2026, 12, 3), "Reserved", "clean@x")]), clock)
    assert out.status == "success"
    db.refresh(g)
    assert g.last_sync_error is None


# ---------------------------------------------------------------------------
# (n) different guest, (o) continuity, (p) duplicates at roll, (q) no UID,
# (r) RRULE, (s) date-moved booking
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_n_different_guest_after_delete_is_a_new_row(db, clock):
    source = _source(db)
    p = (date(2026, 11, 1), date(2026, 11, 3))
    await _sync(db, source, ical([_one(*p, "A*** B*****", "n1@x")]), clock)
    row = _rows(db)[0]
    row.deleted_at = clock()
    db.commit()
    out, _ = await _sync(db, source, ical([_one(*p, "C*** D*****", "n2@x")]), clock)
    assert out.added == 1 and out.matched_deleted == 0
    new = [r for r in _rows(db) if r.id != row.id]
    assert len(new) == 1 and new[0].status == "confirmed" and new[0].deleted_at is None


def _airbnb(db):
    return _source(db, name="Airbnb", source_type="airbnb", ical_url="https://www.airbnb.example/cal.ics")


@pytest.mark.asyncio
async def test_o_continuity_absent_sync_breaks_the_match(db, clock):
    source = _airbnb(db)
    x = (date(2026, 11, 1), date(2026, 11, 4))
    other = (date(2026, 12, 1), date(2026, 12, 4))
    await _sync(db, source, ical([_one(*x, "Reserved", "x1@x"), _one(*other, "Reserved", "o1@x")]), clock)
    xrow = next(r for r in _rows(db) if _pair(r) == x)
    xrow.status = "cancelled"
    db.commit()
    await _sync(db, source, ical([_one(*other, "Reserved", "o2@x")]), clock)
    out, _ = await _sync(db, source, ical([_one(*x, "Reserved", "x3@x"), _one(*other, "Reserved", "o3@x")]), clock)
    assert out.matched_deleted == 0 and out.added == 1
    live_x = [r for r in _rows(db) if _pair(r) == x and r.status == "confirmed"]
    assert len(live_x) == 1 and live_x[0].id != xrow.id


@pytest.mark.asyncio
async def test_o_control_continuous_listing_matches(db, clock):
    source = _airbnb(db)
    x = (date(2026, 11, 1), date(2026, 11, 4))
    await _sync(db, source, ical([_one(*x, "Reserved", "x1@x")]), clock)
    xrow = _rows(db)[0]
    xrow.status = "cancelled"
    db.commit()
    out, _ = await _sync(db, source, ical([_one(*x, "Reserved", "x2@x")]), clock)
    assert out.matched_deleted == 1 and out.added == 0
    assert len(_rows(db)) == 1


@pytest.mark.asyncio
async def test_p_duplicates_at_roll_update_the_lowest_id(db, clock):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/p.ics")
    p = (date(2026, 11, 1), date(2026, 11, 3))
    ci, co = _stay(*p)
    low = _event(db, external_id="old-1@x", source_id=source.id, checkin=ci, checkout=co)
    high = _event(db, external_id="old-2@x", source_id=source.id, checkin=ci, checkout=co)
    before_high = _snapshot(db, high.id)
    out, _ = await _sync(db, source, ical([_one(*p, "Reserved", "fresh-p@x")]), clock)
    assert out.added == 0 and out.matched_natural_key == 1
    db.expire_all()
    assert db_value_to_utc(db.get(CalendarEvent, low.id).synced_at) == T0
    assert _snapshot(db, high.id) == before_high


@pytest.mark.asyncio
async def test_q_missing_uid(db, clock):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/q.ics")
    p = (date(2026, 11, 1), date(2026, 11, 3))
    await _sync(db, source, ical([_one(*p, "Reserved", None)]), clock)
    await _sync(db, source, ical([_one(*p, "Reserved", None)]), clock)
    rows = _rows(db)
    assert len(rows) == 1
    assert rows[0].external_id.startswith(f"ical-nouid:{source.id}:")


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse_second", [False, True])
async def test_r_rrule_same_uid_two_pairs(db, clock, reverse_second):
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/r.ics")
    p1 = (date(2026, 11, 1), date(2026, 11, 3))
    p2 = (date(2026, 11, 8), date(2026, 11, 10))
    first = [_one(*p1, "Reserved", "rr@x"), _one(*p2, "Reserved", "rr@x")]
    out1, _ = await _sync(db, source, ical(first), clock)
    assert out1.status == "success", out1
    rows1 = {_pair(r): (r.id, r.external_id) for r in _rows(db)}
    assert set(rows1) == {p1, p2}
    assert rows1[p1][1] == "rr@x"

    second = list(reversed(first)) if reverse_second else first
    out2, _ = await _sync(db, source, ical(second), clock)
    assert out2.added == 0
    rows2 = {_pair(r): (r.id, r.external_id) for r in _rows(db)}
    assert rows2 == rows1


@pytest.mark.asyncio
async def test_r_rrule_new_earlier_occurrence_keeps_the_existing_row_on_its_dates(db, clock):
    """A UID seen on several pairs hits only a row already on that pair: a
    new earlier occurrence must not drag the existing row off its dates."""
    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/r2.ics")
    p1 = (date(2026, 11, 1), date(2026, 11, 3))
    p2 = (date(2026, 11, 8), date(2026, 11, 10))
    await _sync(db, source, ical([_one(*p2, "Reserved", "rr2@x")]), clock)
    existing = _rows(db)[0]
    out, _ = await _sync(db, source, ical([_one(*p1, "Reserved", "rr2@x"), _one(*p2, "Reserved", "rr2@x")]), clock)
    assert out.added == 1
    db.expire_all()
    assert _pair(db.get(CalendarEvent, existing.id)) == p2
    assert {_pair(r) for r in _rows(db)} == {p1, p2}


@pytest.mark.asyncio
async def test_s_date_moved_booking(db, clock, monkeypatch):
    from app.services import calendar_sync

    source = _source(db, source_type="generic_ical", ical_url="https://feed.example.com/s.ics")
    p1 = (date(2026, 11, 1), date(2026, 11, 3))
    p2 = (date(2026, 11, 8), date(2026, 11, 10))
    await _sync(db, source, ical([_one(*p1, "Jane Guest", "stable-r@x")]), clock)
    r = _rows(db)[0]

    captured = {}
    real = calendar_sync._upsert_ical_events

    def spy(*args, **kwargs):
        counts = real(*args, **kwargs)
        captured["counts"] = counts
        return counts

    monkeypatch.setattr(calendar_sync, "_upsert_ical_events", spy)
    out, _ = await _sync(db, source, ical([_one(*p2, "Jane Guest", "stable-r@x"), _one(*p1, "Other", "fresh-s@x")]), clock)
    rows = _rows(db)
    assert len(rows) == 2
    moved = db.get(CalendarEvent, r.id)
    assert _pair(moved) == p2
    assert any(_pair(x) == p1 and x.id != r.id for x in rows)
    counts = captured["counts"]
    written = counts.claimed_ids | {row.id for row in counts.inserted}
    assert len(counts.claimed_ids) + len(counts.inserted) == 2
    assert len(written) == 2


# ---------------------------------------------------------------------------
# (t) feed-first reserved key, (u) deleted and re-added source
# ---------------------------------------------------------------------------

def _key(db):
    from app.models import User
    from app.utils.encryption import encrypt_value

    user = User(authentik_id="k", username="k", email="k@example.com", role="owner", active=True)
    db.add(user)
    db.commit()
    db.add(ExternalAPIKey(service_name="lodgify", api_name="Lodgify", api_key_encrypted=encrypt_value("key"),
                          endpoint_url="https://api.lodgify.example", enabled=True, created_by_id=user.id))
    db.commit()


def _api_event(res_id, p):
    ci, co = _stay(*p)
    return {"external_id": f"lodgify_{res_id}", "title": "Lodgify Booking - Guest", "checkin": ci, "checkout": co,
            "guest_name": "Guest", "guest_email": None, "guest_phone": None, "notes": "Source: Lodgify",
            "source": "lodgify", "status": "confirmed", "is_manual_block": False}


@pytest.mark.asyncio
async def test_t_feed_first_reserved_key(client, db, clock):
    from app.services import calendar_sync

    g = _source(db, name="G", source_type="generic_ical", ical_url="https://feed.example.com/g.ics")
    s = _source(db, name="S")
    _key(db)
    p = (date(2026, 11, 1), date(2026, 11, 3))
    await _sync(db, g, ical([_one(*p, "Reserved", "lodgify_101")]), clock)
    assert [r.external_id for r in _rows(db, g.id)] == [_derived(g.id, "lodgify_101")]

    with patch(API, new=AsyncMock(return_value=[_api_event(101, p)])):
        out = await calendar_sync.run_source_sync(s.id, db, trigger="manual")
    assert out.status == "success"
    mine = _rows(db, s.id)
    assert len(mine) == 1 and mine[0].external_id == "lodgify_101" and mine[0].status == "confirmed"
    ci, co = _stay(*p)
    assert mine[0].id in _bookings(client, ci - timedelta(days=1), co + timedelta(days=1))


@pytest.mark.asyncio
async def test_t_variant_legacy_raw_row_on_the_feed_source(client, db, clock):
    from app.services import calendar_sync

    g = _source(db, name="G", source_type="generic_ical", ical_url="https://feed.example.com/g.ics")
    s = _source(db, name="S")
    _key(db)
    p = (date(2026, 11, 1), date(2026, 11, 3))
    ci, co = _stay(*p)
    _event(db, external_id="lodgify_101", source_id=g.id, created_by="ical_sync", source="generic_ical",
           checkin=ci, checkout=co)
    with patch(API, new=AsyncMock(return_value=[_api_event(101, p)])):
        out = await calendar_sync.run_source_sync(s.id, db, trigger="manual")
    assert out.status == "success"
    mine = _rows(db, s.id)
    assert len(mine) == 1 and mine[0].external_id == _derived(s.id, "lodgify_101") and mine[0].status == "confirmed"
    assert mine[0].id in _bookings(client, ci - timedelta(days=1), co + timedelta(days=1))


@pytest.mark.asyncio
async def test_u_deleted_and_readded_source(client, db, clock):
    s1 = _source(db, name="S1", source_type="generic_ical", ical_url="https://feed.example.com/1.ics")
    p1 = (date(2026, 11, 1), date(2026, 11, 3))
    p2 = (date(2026, 11, 10), date(2026, 11, 12))
    await _sync(db, s1, ical([_one(*p1, "Reserved", "stay-u@x")]), clock)
    orphan = _rows(db)[0]
    # calendar_events.source_id is ON DELETE SET NULL; SQLite here doesn't
    # enforce foreign keys, so apply the same effect explicitly.
    orphan.source_id = None
    db.delete(db.get(CalendarSource, s1.id))
    db.commit()
    before = _snapshot(db, orphan.id)

    s2 = _source(db, name="S2", source_type="generic_ical", ical_url="https://feed.example.com/2.ics")
    out, _ = await _sync(db, s2, ical([_one(*p2, "Reserved", "stay-u@x")]), clock)
    assert out.status == "success" and out.rekeyed == 1
    mine = _rows(db, s2.id)
    assert len(mine) == 1 and mine[0].external_id == _derived(s2.id, "stay-u@x")
    assert mine[0].status == "confirmed" and _pair(mine[0]) == p2
    ci, co = _stay(*p2)
    assert mine[0].id in _bookings(client, ci - timedelta(days=1), co + timedelta(days=1))
    assert _snapshot(db, orphan.id) == before


# ---------------------------------------------------------------------------
# (v) continuity boundary, exact mode; (w) legacy mode
# ---------------------------------------------------------------------------

def _stamp_key(source_id):
    return f"calendar_sync.last_stamp.{source_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_us,matches", [(0, True), (-1, False)])
async def test_v_exact_mode_boundary(db, clock, offset_us, matches):
    source = _airbnb(db)
    stamp = datetime(2026, 9, 1, 8, 0, 0, 500000, tzinfo=timezone.utc)
    db.add(SystemSetting(key=_stamp_key(source.id), value=stamp.isoformat(), category="calendar_sync"))
    p = (date(2026, 11, 1), date(2026, 11, 4))
    ci, co = _stay(*p)
    cand = _event(db, external_id="old-v@x", source_id=source.id, source="airbnb", title="Reserved",
                  checkin=ci, checkout=co, deleted_at=T0, synced_at=stamp + timedelta(microseconds=offset_us))
    out, _ = await _sync(db, source, ical([_one(*p, "Reserved", "fresh-v@x")]), clock)
    assert out.matched_deleted == (1 if matches else 0)
    assert out.added == (0 if matches else 1)
    db.expire_all()
    assert db.get(CalendarEvent, cand.id).deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("offset_s,matches", [(-60, True), (-61, False)])
async def test_w_legacy_mode_boundary_then_exact(db, clock, offset_s, matches):
    source = _airbnb(db)
    marker = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
    marker_pair = (date(2026, 12, 20), date(2026, 12, 22))
    mci, mco = _stay(*marker_pair)
    _event(db, external_id="marker@x", source_id=source.id, source="airbnb", title="Reserved",
           checkin=mci, checkout=mco, synced_at=marker)
    p = (date(2026, 11, 1), date(2026, 11, 4))
    ci, co = _stay(*p)
    _event(db, external_id="old-w@x", source_id=source.id, source="airbnb", title="Reserved",
           checkin=ci, checkout=co, deleted_at=T0, synced_at=marker + timedelta(seconds=offset_s))
    later = (date(2026, 12, 1), date(2026, 12, 4))
    lci, lco = _stay(*later)
    at_marker = _event(db, external_id="old-w2@x", source_id=source.id, source="airbnb", title="Reserved",
                       checkin=lci, checkout=lco, deleted_at=T0, synced_at=marker)
    assert db.query(SystemSetting).filter(SystemSetting.key == _stamp_key(source.id)).first() is None

    run_stamp = clock()
    out, _ = await _sync(db, source, ical([_one(*p, "Reserved", "fresh-w@x")]), clock)
    assert out.matched_deleted == (1 if matches else 0)
    assert out.added == (0 if matches else 1)
    db.expire_all()
    setting = db.query(SystemSetting).filter(SystemSetting.key == _stamp_key(source.id)).one()
    assert datetime.fromisoformat(setting.value) == run_stamp
    assert setting.category == "calendar_sync"

    # Second run is exact mode: the candidate at the old marker (which legacy
    # mode would have matched) no longer matches.
    out2, _ = await _sync(db, source, ical([_one(*later, "Reserved", "fresh-w2@x")]), clock)
    assert out2.matched_deleted == 0 and out2.added == 1
    db.expire_all()
    assert db.get(CalendarEvent, at_marker.id).deleted_at is not None
