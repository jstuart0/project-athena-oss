"""ATHENA-118 Phase 2: replica memory + cross-replica lease
(app/services/service_control_settings.py), covering the bob r2 M1 fix
folded into T8 -- restart(replica A) vs stop(replica B) losing across two
admin-backend replicas.

Mocking strategy: real SQLite via the existing db/client fixtures (and a
second sessionmaker bound to the SAME StaticPool engine, standing in for a
second admin-backend replica) -- the lease's whole point is a real
unique-key INSERT race, which a mock would defeat.
"""
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
if os.path.join(_REPO_ROOT, 'src') not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, 'src'))

os.environ.setdefault("DEV_MODE", "true")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SERVICE_API_KEY", "test-svc-key-athena-118")

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.models import SystemSetting
from app.services.service_control_settings import (
    LeaseBusy,
    acquire_lease,
    read_lease,
    release_lease,
    renew_lease,
)
from tests.conftest import TestingSessionLocal


def _fixed_clock(when):
    def _clock():
        return when
    return _clock


def test_second_replica_gets_lease_busy_while_first_holds_it(db):
    lease_a = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "restart", target_replicas=0)
    with pytest.raises(LeaseBusy):
        acquire_lease(TestingSessionLocal, "athena-rag-tesla", "stop", target_replicas=0)

    release_lease(TestingSessionLocal, lease_a)
    # After release, a fresh acquire succeeds.
    lease_b = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "stop", target_replicas=0)
    release_lease(TestingSessionLocal, lease_b)


def test_expired_lease_is_taken_over(db):
    past = datetime.now(timezone.utc) - timedelta(seconds=200)
    lease_a = acquire_lease(
        TestingSessionLocal, "athena-rag-tesla", "restart", target_replicas=0,
        ttl=1, now=_fixed_clock(past),
    )
    # lease_a's expires_at is now well in the past -- a second acquire at
    # "real now" must take it over rather than raising LeaseBusy.
    lease_b = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "stop", target_replicas=0)
    assert lease_b.holder != lease_a.holder


def test_owner_only_release_does_not_delete_a_takeover(db):
    past = datetime.now(timezone.utc) - timedelta(seconds=200)
    lease_a = acquire_lease(
        TestingSessionLocal, "athena-rag-tesla", "restart", target_replicas=0,
        ttl=1, now=_fixed_clock(past),
    )
    lease_b = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "stop", target_replicas=0)

    # A's (stale) release must not delete B's lease.
    release_lease(TestingSessionLocal, lease_a)
    still_there = read_lease(db, "athena-rag-tesla")
    assert still_there is not None
    assert still_there["holder"] == lease_b.holder

    release_lease(TestingSessionLocal, lease_b)
    assert read_lease(db, "athena-rag-tesla") is None


def test_lease_row_uses_service_control_category(db):
    lease = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "stop", target_replicas=0)
    row = db.query(SystemSetting).filter(SystemSetting.key == "service_control.lock.athena-rag-tesla").first()
    assert row is not None
    assert row.category == "service_control"
    release_lease(TestingSessionLocal, lease)


# ---------------------------------------------------------------------------
# xander P2 review, Low #3: SQLite lock contention (two genuinely separate
# connections, not the StaticPool-shared single connection the rest of this
# file uses) can surface a losing race as OperationalError ("database is
# locked") instead of IntegrityError. acquire_lease must treat it the same
# way -- LeaseBusy, not a crash.
# ---------------------------------------------------------------------------

def test_acquire_lease_treats_operational_error_as_losing_race(tmp_path):
    import os as _os
    from sqlalchemy import create_engine, text
    from app.database import Base

    db_path = tmp_path / "lease_two_engines.db"
    engine_a = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 0.2})
    engine_b = create_engine(f"sqlite:///{db_path}", connect_args={"timeout": 0.2})
    Base.metadata.create_all(bind=engine_a)

    from sqlalchemy.orm import sessionmaker
    SessionA = sessionmaker(bind=engine_a)
    SessionB = sessionmaker(bind=engine_b)

    session_a = SessionA()
    session_a.execute(text("BEGIN IMMEDIATE"))
    session_a.add(SystemSetting(key="service_control.lock.holder-a", value="{}", category="service_control"))
    session_a.flush()

    try:
        with pytest.raises(LeaseBusy):
            acquire_lease(SessionB, "athena-rag-tesla", "stop", target_replicas=0)
    finally:
        session_a.rollback()
        session_a.close()
        engine_a.dispose()
        engine_b.dispose()


# ---------------------------------------------------------------------------
# codex diff review r2 High #2: renew_lease replaces the read-then-act
# still_holds_lease check with an atomic compare-and-swap.
# ---------------------------------------------------------------------------

def test_renew_lease_succeeds_and_extends_expiry_when_still_owner(db):
    lease = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "restart", target_replicas=0)
    original_value = lease.value

    ok = renew_lease(TestingSessionLocal, lease)

    assert ok is True
    assert lease.value != original_value  # mutated to the renewed value
    stored = read_lease(db, "athena-rag-tesla")
    assert stored == json.loads(lease.value)

    # release_lease must still work against the RENEWED value.
    release_lease(TestingSessionLocal, lease)
    assert read_lease(db, "athena-rag-tesla") is None


def test_renew_lease_fails_and_leaves_the_takeover_alone_when_superseded(db):
    """Simulates A's renewal failing because B took over mid-wait -- the
    real scenario codex diff review r2 High #2 exists to close: no PATCH
    may follow a failed renewal, and B's own lease must be untouched."""
    lease_a = acquire_lease(
        TestingSessionLocal, "athena-rag-tesla", "restart", target_replicas=0,
        ttl=1, now=lambda: datetime.now(timezone.utc) - timedelta(seconds=200),
    )  # already expired relative to real now()

    lease_b = acquire_lease(TestingSessionLocal, "athena-rag-tesla", "stop", target_replicas=0)

    ok = renew_lease(TestingSessionLocal, lease_a)

    assert ok is False
    current = read_lease(db, "athena-rag-tesla")
    assert current["holder"] == lease_b.holder
    assert current["action"] == "stop"
