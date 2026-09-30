"""The generic system_settings lease core (app/services/settings_lease.py),
shared by service control and the calendar sync. Service control's own
behavior is proven by its existing, unedited test files."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import SystemSetting
from app.services import settings_lease
from app.services.settings_lease import LeaseBusy
from app.services.service_control_settings import acquire_lease, release_lease
from tests.conftest import TestingSessionLocal

KEY = "calendar_sync.lock.1"


def _acquire(key=KEY, **kw):
    kw.setdefault("category", "calendar_sync")
    kw.setdefault("ttl", 600)
    kw.setdefault("busy_message", "busy!")
    return settings_lease.acquire(TestingSessionLocal, key, **kw)


def _past():
    when = datetime.now(timezone.utc) - timedelta(seconds=3600)
    return lambda: when


def _row(db, key=KEY):
    db.expire_all()
    return db.query(SystemSetting).filter(SystemSetting.key == key).first()


def test_second_acquire_is_busy_with_the_callers_message(db):
    lease = _acquire()
    with pytest.raises(LeaseBusy, match="busy!"):
        _acquire(busy_message="busy!")
    row = _row(db)
    assert row.value == lease.value
    assert row.category == "calendar_sync"
    assert json.loads(row.value)["holder"] == lease.holder


def test_expired_lease_is_taken_over(db):
    stale = _acquire(ttl=1, now=_past())
    fresh = _acquire()
    assert fresh.holder != stale.holder
    assert _row(db).value == fresh.value


def test_renew_after_takeover_returns_false(db):
    stale = _acquire(ttl=1, now=_past())
    fresh = _acquire()
    assert settings_lease.renew(TestingSessionLocal, stale, ttl=600) is False
    assert _row(db).value == fresh.value
    assert settings_lease.renew(TestingSessionLocal, fresh, ttl=600) is True
    assert _row(db).value == fresh.value


def test_non_holder_release_is_a_noop(db):
    stale = _acquire(ttl=1, now=_past())
    fresh = _acquire()
    settings_lease.release(TestingSessionLocal, stale)
    assert _row(db).value == fresh.value
    settings_lease.release(TestingSessionLocal, fresh)
    assert _row(db) is None


def test_calendar_lease_does_not_block_service_control(db):
    cal = _acquire()
    sc = acquire_lease(TestingSessionLocal, "x", "stop", target_replicas=0)
    assert sc.key == "service_control.lock.x"
    assert _row(db).value == cal.value
    release_lease(TestingSessionLocal, sc)
    settings_lease.release(TestingSessionLocal, cal)
