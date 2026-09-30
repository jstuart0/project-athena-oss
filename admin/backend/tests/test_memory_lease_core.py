"""The two backward-compatible additions the memory vector rebuild needs
from the shared system_settings lease core: LeaseBusy says when the holder's
lease expires (for a 409's retry_after), and a lease can be read by key."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import SystemSetting
from app.services import settings_lease
from app.services.settings_lease import LeaseBusy
from tests.conftest import TestingSessionLocal

KEY = "memory_vectors.reindex.lock"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def _acquire(ttl=120, now=lambda: NOW):
    return settings_lease.acquire(TestingSessionLocal, KEY, category="memory_vectors", ttl=ttl,
                                  busy_message="rebuild busy", fields={"action": "missing"}, now=now)


def test_busy_carries_the_holders_expiry(db):
    _acquire(ttl=120)
    with pytest.raises(LeaseBusy, match="rebuild busy") as busy:
        _acquire(ttl=120)
    assert busy.value.expires_at == NOW + timedelta(seconds=120)


def test_busy_without_expiry_stays_constructible():
    assert LeaseBusy("plain").expires_at is None
    assert str(LeaseBusy("plain")) == "plain"


def test_read_by_key(db):
    assert settings_lease.read(db, KEY) is None
    _acquire()
    info = settings_lease.read(db, KEY)
    assert info["action"] == "missing" and info["expires_at"] == (NOW + timedelta(seconds=120)).isoformat()


def test_read_unparsable_is_none(db):
    db.add(SystemSetting(key=KEY, value="not json", category="memory_vectors"))
    db.commit()
    assert settings_lease.read(db, KEY) is None


def test_expired_holder_is_taken_over_not_busy(db):
    _acquire(ttl=1, now=lambda: NOW - timedelta(hours=1))
    lease = _acquire(ttl=60)
    assert json.loads(lease.value)["action"] == "missing"
