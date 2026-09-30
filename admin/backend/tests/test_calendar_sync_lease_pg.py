"""One writer per source on real Postgres: two OS threads, each with its own
engine and event loop, both run the scheduler against one scratch database.

Local-only, like test_migration_057_rename.py: no CI workflow exports
POSTGRES_TEST_URL. To run:
    POSTGRES_TEST_URL=postgresql://postgres:postgres@localhost:5432/postgres \\
        pytest admin/backend/tests/test_calendar_sync_lease_pg.py -m postgres

(b) is the control that proves the harness can see a double write: with the
lease stubbed out and both threads held just before commit, it must find
exactly 18 rows.
"""
from __future__ import annotations

import asyncio
import os
import threading
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import CalendarEvent, CalendarSource, SystemSetting
from app.services import settings_lease
from shared import config as config_module
from tests.fixtures.lodgify_feed_shape import build_lodgify_shape_feed

pytestmark = pytest.mark.postgres

ICAL = "app.routes.calendar_sources.fetch_ical_data"


@pytest.fixture
def scratch_url(monkeypatch):
    base = os.getenv("POSTGRES_TEST_URL")
    if not base:
        pytest.skip("POSTGRES_TEST_URL not set; skipping Postgres run")
    monkeypatch.setenv("DEFAULT_TIMEZONE", "America/New_York")
    config_module._clear_cache_for_tests()
    name = f"t144_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(base).set(database=name)
    engine = create_engine(url)
    Base.metadata.create_all(bind=engine)
    Setup = sessionmaker(bind=engine)
    s = Setup()
    source = CalendarSource(name="Feed", source_type="generic_ical", ical_url="https://feed.example.com/pg.ics",
                            enabled=True, last_sync_at=None)
    s.add(source)
    s.flush()
    # A source that has synced before (the steady state). Without this, a
    # first-ever sync INSERTs calendar_sync.last_stamp.<S>, and that unique
    # key alone would stop the control's second commit -- hiding the very
    # double write (b) must be able to see.
    s.add(SystemSetting(key=f"calendar_sync.last_stamp.{source.id}", value="2026-01-01T00:00:00+00:00",
                        category="calendar_sync"))
    s.commit()
    s.close()
    engine.dispose()
    try:
        yield url
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
        config_module._clear_cache_for_tests()


def _count_rows(url):
    engine = create_engine(url)
    try:
        s = sessionmaker(bind=engine)()
        try:
            return s.query(CalendarEvent).count()
        finally:
            s.close()
    finally:
        engine.dispose()


def _run_two_schedulers(url, monkeypatch):
    """Both threads run check_and_sync_sources; returns (outcomes, uid sets
    returned per fetch). Barrier 1 sits before the lease acquire, so both
    threads have passed the scheduler's own due check first."""
    from app.services import calendar_sync

    local = threading.local()

    def factory():
        return local.Session()

    monkeypatch.setattr(calendar_sync, "SessionLocal", factory)
    monkeypatch.setattr(calendar_sync, "LEASE_SESSION_FACTORY", factory)

    barrier1 = threading.Barrier(2, timeout=10)
    real_acquire = settings_lease.acquire

    def acquire_after_barrier(*args, **kwargs):
        barrier1.wait()
        return real_acquire(*args, **kwargs)

    monkeypatch.setattr(settings_lease, "acquire", acquire_after_barrier)

    outcomes, fetched_uids = [], []
    lock = threading.Lock()
    real_run = calendar_sync.run_source_sync

    async def recording_run(*args, **kwargs):
        outcome = await real_run(*args, **kwargs)
        with lock:
            outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(calendar_sync, "run_source_sync", recording_run)

    async def fetch(_url):
        feed, uids = build_lodgify_shape_feed()
        with lock:
            fetched_uids.append(set(uids))
        await asyncio.sleep(0.05)
        return feed

    errors = []

    def worker():
        engine = create_engine(url)
        local.Session = sessionmaker(bind=engine, autoflush=False)
        try:
            asyncio.run(calendar_sync.check_and_sync_sources())
        except BaseException as exc:
            errors.append(exc)
        finally:
            engine.dispose()

    with patch(ICAL, new=AsyncMock(side_effect=fetch)):
        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    assert not errors, errors
    if len(fetched_uids) == 2:
        assert fetched_uids[0].isdisjoint(fetched_uids[1])
    return outcomes, fetched_uids


def test_a_real_lease_one_writer(scratch_url, monkeypatch):
    outcomes, fetched = _run_two_schedulers(scratch_url, monkeypatch)
    assert _count_rows(scratch_url) == 9
    assert len(fetched) == 1
    statuses = sorted(o.status for o in outcomes)
    assert statuses in (["busy", "success"], ["not_due", "success"]), statuses


def test_b_control_without_a_lease_double_writes(scratch_url, monkeypatch):
    """The lease is stubbed out and both threads are held at the pre-commit
    renew until both have staged their inserts: the harness must then see
    both writes."""
    from app.services import calendar_sync

    barrier2 = threading.Barrier(2, timeout=10)

    def dummy_acquire(_factory, key, **_kwargs):
        return settings_lease.Lease(key=key, holder=f"dummy/{uuid.uuid4()}", value="{}")

    def renew_after_barrier(*_args, **_kwargs):
        barrier2.wait()
        return True

    monkeypatch.setattr(settings_lease, "renew", renew_after_barrier)
    monkeypatch.setattr(settings_lease, "release", lambda *_a, **_k: None)

    # Barrier 1 wraps whatever `acquire` is installed when the harness runs,
    # so install the dummy first.
    monkeypatch.setattr(settings_lease, "acquire", dummy_acquire)
    outcomes, fetched = _run_two_schedulers(scratch_url, monkeypatch)
    assert len(fetched) == 2
    assert _count_rows(scratch_url) == 18
    assert sorted(o.status for o in outcomes) == ["success", "success"]
    assert calendar_sync.LEASE_SESSION_FACTORY is not None
