"""One writer per calendar source: every trigger takes the
`calendar_sync.lock.<S>` lease before loading the source, and a lease lost
mid-sync writes nothing (the compare-and-swap renew immediately before
commit is the fence)."""
from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import CalendarEvent, CalendarSource, GuestSession, SystemSetting
from app.services import settings_lease
from shared import config as config_module
from tests.conftest import TestingSessionLocal
from tests.fixtures.lodgify_feed_shape import build_lodgify_shape_feed

ICAL = "app.routes.calendar_sources.fetch_ical_data"
BUSY_MESSAGE = "A sync for this source is already running"


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


def _source(db, **kw):
    defaults = dict(name="Feed", source_type="generic_ical", ical_url="https://feed.example.com/a.ics")
    defaults.update(kw)
    s = CalendarSource(**defaults)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _lock_key(source_id):
    return f"calendar_sync.lock.{source_id}"


def _hold(source_id, factory=TestingSessionLocal, **kw):
    kw.setdefault("ttl", 600)
    return settings_lease.acquire(factory, _lock_key(source_id), category="calendar_sync",
                                  busy_message="held by test", **kw)


def _lease_row(session_factory, source_id):
    s = session_factory()
    try:
        return s.query(SystemSetting).filter(SystemSetting.key == _lock_key(source_id)).first()
    finally:
        s.close()


def _event_count(db):
    db.expire_all()
    return db.query(CalendarEvent).count()


async def _run(source_id, db, trigger="manual"):
    from app.services import calendar_sync

    return await calendar_sync.run_source_sync(source_id, db, trigger=trigger)


# ---------------------------------------------------------------------------
# (a) two concurrent manual syncs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_concurrent_manual_syncs_one_writes_one_is_busy(db):
    source = _source(db)
    feed = build_lodgify_shape_feed()[0]
    arrived = asyncio.Event()

    async def slow_fetch(url):
        arrived.set()
        for _ in range(3):
            await asyncio.sleep(0)
        return feed

    db_a, db_b = TestingSessionLocal(), TestingSessionLocal()
    try:
        with patch(ICAL, new=AsyncMock(side_effect=slow_fetch)) as fetch:
            outcomes = await asyncio.wait_for(
                asyncio.gather(_run(source.id, db_a), _run(source.id, db_b)), 10,
            )
    finally:
        db_a.close()
        db_b.close()
    assert sorted(o.status for o in outcomes) == ["busy", "success"]
    assert fetch.await_count == 1
    assert _event_count(db) == 9


# ---------------------------------------------------------------------------
# (b) held, (c) expired, (j) another source's lease
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_b_held_lease_is_busy_with_no_fetch_and_no_writes(db):
    source = _source(db)
    _hold(source.id)
    with patch(ICAL, new=AsyncMock(return_value=build_lodgify_shape_feed()[0])) as fetch:
        outcome = await _run(source.id, db)
    assert outcome.status == "busy"
    assert fetch.await_count == 0
    assert _event_count(db) == 0
    db.refresh(source)
    assert source.last_sync_at is None


@pytest.mark.asyncio
async def test_c_expired_lease_is_taken_over(db):
    source = _source(db)
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    _hold(source.id, ttl=1, now=lambda: past)
    with patch(ICAL, new=AsyncMock(return_value=build_lodgify_shape_feed()[0])):
        outcome = await _run(source.id, db)
    assert outcome.status == "success"
    assert _event_count(db) == 9
    assert _lease_row(TestingSessionLocal, source.id) is None


@pytest.mark.asyncio
async def test_j_another_sources_lease_does_not_block(db):
    a = _source(db, name="A")
    b = _source(db, name="B", ical_url="https://feed.example.com/b.ics")
    _hold(a.id)
    with patch(ICAL, new=AsyncMock(return_value=build_lodgify_shape_feed()[0])):
        outcome = await _run(b.id, db)
    assert outcome.status == "success"


# ---------------------------------------------------------------------------
# (d) lease lost mid-sync, on separate connections
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d_lost_lease_writes_nothing(db, tmp_path, monkeypatch):
    from app.services import calendar_sync

    lease_engine = create_engine(f"sqlite:///{tmp_path / 'lease.db'}", connect_args={"timeout": 5})
    Base.metadata.create_all(bind=lease_engine)
    LeaseSession = sessionmaker(bind=lease_engine, autoflush=False)
    monkeypatch.setattr(calendar_sync, "LEASE_SESSION_FACTORY", LeaseSession)

    source = _source(db, source_type="lodgify", ical_url="https://www.lodgify.com/export/d.ics")
    feed = build_lodgify_shape_feed()[0]
    thief = {}

    async def fetch_then_steal(url):
        s = LeaseSession()
        row = s.query(SystemSetting).filter(SystemSetting.key == _lock_key(source.id)).one()
        value = json.loads(row.value)
        value["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        row.value = json.dumps(value, sort_keys=True)
        s.commit()
        s.close()
        thief["lease"] = _hold(source.id, factory=LeaseSession)
        return feed

    try:
        with patch(ICAL, new=AsyncMock(side_effect=fetch_then_steal)):
            outcome = await _run(source.id, db)

        assert outcome.status == "busy"
        assert _event_count(db) == 0
        db.expire_all()
        db.refresh(source)
        assert source.last_sync_at is None
        assert source.last_sync_status == "pending"
        assert db.query(SystemSetting).filter(
            SystemSetting.key == f"calendar_sync.last_stamp.{source.id}"
        ).first() is None
        assert db.query(GuestSession).count() == 0
        row = _lease_row(LeaseSession, source.id)
        assert row is not None and row.value == thief["lease"].value
    finally:
        lease_engine.dispose()


# ---------------------------------------------------------------------------
# (e) scheduled second holder, (f) route busy
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_e_scheduled_second_holder_is_not_due(db):
    source = _source(db)
    with patch(ICAL, new=AsyncMock(return_value=build_lodgify_shape_feed()[0])) as fetch:
        first = await _run(source.id, db, trigger="scheduled")
        second = await _run(source.id, db, trigger="scheduled")
    assert first.status == "success"
    assert second.status == "not_due"
    assert fetch.await_count == 1


def test_f_route_busy_message(owner_client, db):
    source = _source(db)
    _hold(source.id)
    with patch(ICAL, new=AsyncMock(return_value=build_lodgify_shape_feed()[0])) as fetch:
        resp = owner_client.post(f"/api/calendar-sources/{source.id}/sync")
    assert resp.status_code == 200
    assert resp.json()["success"] is False
    assert resp.json()["message"] == BUSY_MESSAGE
    assert fetch.await_count == 0


# ---------------------------------------------------------------------------
# (g) timeout, (h) error, (i) cancellation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_g_fetch_timeout_fails_and_releases(db, monkeypatch):
    from app.services import calendar_sync

    monkeypatch.setattr(calendar_sync, "SYNC_FETCH_TIMEOUT_SECONDS", 0.05)
    source = _source(db)

    async def hang(url):
        await asyncio.sleep(5)

    with patch(ICAL, new=AsyncMock(side_effect=hang)):
        outcome = await _run(source.id, db)
    assert outcome.status == "failed"
    assert _lease_row(TestingSessionLocal, source.id) is None
    assert _event_count(db) == 0


@pytest.mark.asyncio
async def test_h_runtime_error_fails_and_releases(db):
    source = _source(db)
    with patch(ICAL, new=AsyncMock(side_effect=RuntimeError("boom"))):
        outcome = await _run(source.id, db)
    assert outcome.status == "failed"
    assert _lease_row(TestingSessionLocal, source.id) is None


@pytest.mark.asyncio
async def test_i_cancellation_propagates_and_releases(db):
    source = _source(db)
    started = asyncio.Event()

    async def wait_forever(url):
        started.set()
        await asyncio.Event().wait()

    with patch(ICAL, new=AsyncMock(side_effect=wait_forever)):
        task = asyncio.create_task(_run(source.id, db))
        await asyncio.wait_for(started.wait(), 5)
        assert _lease_row(TestingSessionLocal, source.id) is not None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert _lease_row(TestingSessionLocal, source.id) is None


# ---------------------------------------------------------------------------
# (k) two engines, two threads, both through the scheduler
# ---------------------------------------------------------------------------

def test_k_two_engines_two_threads_one_writer(tmp_path, monkeypatch):
    from app.services import calendar_sync

    path = tmp_path / "shared.db"
    setup_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(bind=setup_engine)
    Setup = sessionmaker(bind=setup_engine)
    s = Setup()
    s.add(CalendarSource(name="Feed", source_type="generic_ical", ical_url="https://feed.example.com/k.ics",
                         enabled=True, last_sync_at=None))
    s.commit()
    s.close()

    local = threading.local()

    def factory():
        return local.Session()

    monkeypatch.setattr(calendar_sync, "SessionLocal", factory)
    monkeypatch.setattr(calendar_sync, "LEASE_SESSION_FACTORY", factory)

    barrier = threading.Barrier(2, timeout=5)
    real_acquire = settings_lease.acquire

    def acquire_after_barrier(*args, **kwargs):
        barrier.wait()
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(settings_lease, "acquire", acquire_after_barrier)

    outcomes = []
    lock = threading.Lock()
    real_run = calendar_sync.run_source_sync

    async def recording_run(*args, **kwargs):
        outcome = await real_run(*args, **kwargs)
        with lock:
            outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(calendar_sync, "run_source_sync", recording_run)

    fetches = []
    feed = build_lodgify_shape_feed()[0]

    async def fetch(url):
        with lock:
            fetches.append(url)
        await asyncio.sleep(0.05)
        return feed

    errors = []

    def worker():
        engine = create_engine(f"sqlite:///{path}", connect_args={"timeout": 5})
        local.Session = sessionmaker(bind=engine, autoflush=False)
        try:
            asyncio.run(calendar_sync.check_and_sync_sources())
        except BaseException as exc:  # surfaced to the main thread below
            errors.append(exc)
        finally:
            engine.dispose()

    with patch(ICAL, new=AsyncMock(side_effect=fetch)):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)

    assert not errors, errors
    check = Setup()
    try:
        assert check.query(CalendarEvent).count() == 9
    finally:
        check.close()
        setup_engine.dispose()
    assert len(fetches) == 1
    assert sorted(o.status for o in outcomes) == ["busy", "success"]
