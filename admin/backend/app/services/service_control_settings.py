"""Replica memory and the cross-replica action lease (ATHENA-118, D11).

Both live in `system_settings` (category `service_control`) rather than a
new table -- migrations in this repo are run manually, and `system_settings`
already exists with a unique `key` column, which is exactly the atomicity
primitive the lease needs (`INSERT` on a unique key is an atomic
test-and-set; a second INSERT for the same key raises `IntegrityError`).

The lease (`service_control.lock.<deployment>`) is held for the WHOLE
duration of a k8s start/stop/restart action, including restart's bounded
wait, so a second admin-backend replica attempting an action on the same
Deployment gets 409 `action_in_progress` instead of racing the first
replica's in-flight scale operation (bob r2 M1 -- the case a per-process
lock alone cannot close is restart-vs-stop across two replicas: the second
replica's `stop` would see replicas==0 mid-restart and no-op "already
stopped", then the first replica's scale-back would silently resurrect the
service).

Replica memory (`service_control.replicas.<deployment>`) is a plain
key-value read/write on the caller's own request session -- it doesn't need
lease-grade atomicity, just "don't ever remember a 0".
"""
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models import SystemSetting

_CATEGORY = "service_control"
LEASE_TTL_SECONDS = 90
_REPLICA_CLAMP_MIN = 1
_REPLICA_CLAMP_MAX = 10


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _replicas_key(deployment: str) -> str:
    return f"service_control.replicas.{deployment}"


def _lease_key(deployment: str) -> str:
    return f"service_control.lock.{deployment}"


def remember_replicas(db: Session, deployment: str, n: int) -> None:
    """Store the pre-stop replica count. A no-op when n <= 0 -- a stored
    0 is impossible by construction (D11)."""
    if n <= 0:
        return
    key = _replicas_key(deployment)
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is not None:
        row.value = str(n)
    else:
        db.add(SystemSetting(key=key, value=str(n), category=_CATEGORY))
    db.commit()


def recall_replicas(db: Session, deployment: str) -> int:
    """clamp(int(value), 1, 10); default 1 when the key is absent or unparsable."""
    key = _replicas_key(deployment)
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        return _REPLICA_CLAMP_MIN
    try:
        n = int(row.value)
    except (TypeError, ValueError):
        return _REPLICA_CLAMP_MIN
    return max(_REPLICA_CLAMP_MIN, min(_REPLICA_CLAMP_MAX, n))


class LeaseBusy(Exception):
    """Raised when a lease is held (unexpired) by another holder, or a
    takeover race is lost. The caller (service_control.py) maps this to
    409 `action_in_progress`. ``expires_at`` is the holder's expiry when it
    was read (None when unknown, e.g. a lost race)."""

    def __init__(self, msg: str = "", expires_at: Optional[datetime] = None):
        super().__init__(msg)
        self.expires_at = expires_at


@dataclass
class Lease:
    key: str
    holder: str
    value: str  # the exact JSON string written -- release matches on this


def acquire_lease(
    session_factory: Callable[[], Session],
    deployment: str,
    action: str,
    target_replicas: int,
    ttl: int = LEASE_TTL_SECONDS,
    now: Callable[[], datetime] = _utcnow,
    *,
    key: Optional[str] = None,
) -> Lease:
    """INSERT-based acquire; conditional-UPDATE takeover when the existing
    lease has expired. Uses its OWN session (via `session_factory`), so the
    write is visible to another replica immediately and independent of the
    caller's own request transaction (D11). ``key`` overrides the
    service-control key for other lease users (the memory vector reindex)."""
    key = key or _lease_key(deployment)
    holder = f"{os.getenv('HOSTNAME', 'local')}/{uuid.uuid4()}"
    expires_at = now() + timedelta(seconds=ttl)
    value = json.dumps(
        {"holder": holder, "action": action, "target_replicas": target_replicas, "expires_at": expires_at.isoformat()},
        sort_keys=True,
    )

    session = session_factory()
    try:
        session.add(SystemSetting(key=key, value=value, category=_CATEGORY))
        try:
            session.commit()
            return Lease(key=key, holder=holder, value=value)
        except (IntegrityError, OperationalError):
            # SQLite serializes writers: a genuine unique-key collision
            # raises IntegrityError, but two connections racing the same
            # INSERT at the same instant can instead surface as
            # OperationalError ("database is locked") -- both mean
            # "someone else is contending for this lease right now"
            # (xander P2 Low #3), not a crash.
            session.rollback()

        existing = session.query(SystemSetting).filter(SystemSetting.key == key).first()
        if existing is None:
            # Raced with a concurrent release between the failed INSERT and
            # this read -- retry the INSERT once. A second writer landing
            # in this exact window is itself a losing race, not a crash.
            try:
                session.add(SystemSetting(key=key, value=value, category=_CATEGORY))
                session.commit()
                return Lease(key=key, holder=holder, value=value)
            except (IntegrityError, OperationalError):
                session.rollback()
                raise LeaseBusy(f"a service-control action is already in progress for '{deployment}'")

        observed_value = existing.value
        expired = True
        existing_expiry = None
        try:
            observed = json.loads(observed_value)
            expires_at_str = observed.get("expires_at")
            if expires_at_str:
                existing_expiry = datetime.fromisoformat(expires_at_str)
                expired = existing_expiry <= now()
        except (TypeError, ValueError):
            expired = True

        if not expired:
            raise LeaseBusy(
                f"a service-control action is already in progress for '{deployment}'",
                expires_at=existing_expiry,
            )

        rowcount = (
            session.query(SystemSetting)
            .filter(SystemSetting.key == key, SystemSetting.value == observed_value)
            .update({"value": value}, synchronize_session=False)
        )
        session.commit()
        if rowcount != 1:
            raise LeaseBusy(f"a service-control action is already in progress for '{deployment}'")
        return Lease(key=key, holder=holder, value=value)
    finally:
        session.close()


def release_lease(session_factory: Callable[[], Session], lease: Lease) -> None:
    """Owner-only release: matches on the exact value this holder wrote, so
    a lease another holder took over after this one expired is never
    deleted out from under them."""
    session = session_factory()
    try:
        session.query(SystemSetting).filter(
            SystemSetting.key == lease.key, SystemSetting.value == lease.value
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def read_lease(db: Session, deployment: str, *, key: Optional[str] = None) -> Optional[dict]:
    """Used by the envelope builder to detect `restart_interrupted` (an
    expired restart lease whose Deployment is stuck at 0 replicas)."""
    key = key or _lease_key(deployment)
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        return None
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return None


def lease_is_expired(lease_info: dict, now: Callable[[], datetime] = _utcnow) -> bool:
    expires_at_str = lease_info.get("expires_at")
    if not expires_at_str:
        return True
    try:
        return datetime.fromisoformat(expires_at_str) <= now()
    except (TypeError, ValueError):
        return True


def renew_lease(
    session_factory: Callable[[], Session],
    lease: Lease,
    ttl: int = LEASE_TTL_SECONDS,
    now: Callable[[], datetime] = _utcnow,
) -> bool:
    """Atomic compare-and-swap renewal (codex diff review r2 High #2,
    replacing the read-then-act `still_holds_lease`): conditionally UPDATEs
    the lease row to a fresh `expires_at` ONLY if its value still exactly
    matches `lease.value` -- the same primitive acquire_lease's own
    takeover uses, so a second replica racing this renewal can't also
    succeed. Returns True (and mutates `lease.value` to the renewed value,
    so a subsequent release_lease matches it) iff the row was still ours;
    False means another replica has already taken over.

    A plain read (does the stored value still equal mine?) has a TOCTOU
    gap: replica A could observe itself as owner right at the TTL edge,
    replica B could take over and act on the Deployment in the window
    between that read and A's own subsequent PATCH, and A would then act
    on stale authority regardless. The UPDATE...WHERE here closes that
    window -- it is the check and the extension in one atomic statement."""
    key = lease.key
    old_value = lease.value
    try:
        observed = json.loads(old_value)
    except (TypeError, ValueError):
        observed = {}
    expires_at = now() + timedelta(seconds=ttl)
    new_value = json.dumps({**observed, "expires_at": expires_at.isoformat()}, sort_keys=True)

    session = session_factory()
    try:
        rowcount = (
            session.query(SystemSetting)
            .filter(SystemSetting.key == key, SystemSetting.value == old_value)
            .update({"value": new_value}, synchronize_session=False)
        )
        session.commit()
        if rowcount == 1:
            lease.value = new_value
        return rowcount == 1
    finally:
        session.close()


def _interrupted_key(deployment: str) -> str:
    return f"service_control.interrupted.{deployment}"


def mark_interrupted(session_factory: Callable[[], Session], deployment: str) -> None:
    """Persist an explicit 'this restart didn't complete its scale-back'
    marker (codex diff review r1 Critical #4) -- set when the scale-back
    PATCH itself fails, or when the lease was lost before the scale-back
    could run. `restart_interrupted` must not depend SOLELY on an expired
    lease: if the scale-back PATCH fails, release_lease still runs in the
    dispatcher's own `finally`, so an expired-lease check alone would never
    catch this case."""
    key = _interrupted_key(deployment)
    session = session_factory()
    try:
        existing = session.query(SystemSetting).filter(SystemSetting.key == key).first()
        if existing is None:
            session.add(SystemSetting(key=key, value="1", category=_CATEGORY))
        else:
            existing.value = "1"
        session.commit()
    finally:
        session.close()


def clear_interrupted(session_factory: Callable[[], Session], deployment: str) -> None:
    """A successful `start` clears the marker -- the operator's recovery
    action succeeded, so the row should stop reading as interrupted."""
    key = _interrupted_key(deployment)
    session = session_factory()
    try:
        session.query(SystemSetting).filter(SystemSetting.key == key).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def read_interrupted(db: Session, deployment: str) -> bool:
    key = _interrupted_key(deployment)
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    return row is not None and row.value == "1"
