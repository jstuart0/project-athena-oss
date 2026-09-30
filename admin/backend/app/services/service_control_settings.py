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
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy.orm import Session

from app.models import SystemSetting
from app.services import settings_lease
from app.services.settings_lease import Lease, LeaseBusy  # noqa: F401  (re-exported for service_control.py)

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


def _busy_message(deployment: str) -> str:
    return f"a service-control action is already in progress for '{deployment}'"


def acquire_lease(
    session_factory: Callable[[], Session],
    deployment: str,
    action: str,
    target_replicas: int,
    ttl: int = LEASE_TTL_SECONDS,
    now: Callable[[], datetime] = _utcnow,
) -> Lease:
    """Acquire `service_control.lock.<deployment>` on its OWN session (via
    `session_factory`), so the write is visible to another replica
    immediately and independent of the caller's request transaction (D11).
    Raises LeaseBusy, which service_control.py maps to 409
    `action_in_progress`."""
    return settings_lease.acquire(
        session_factory,
        _lease_key(deployment),
        category=_CATEGORY,
        ttl=ttl,
        busy_message=_busy_message(deployment),
        fields={"action": action, "target_replicas": target_replicas},
        now=now,
    )


def release_lease(session_factory: Callable[[], Session], lease: Lease) -> None:
    """Owner-only release: a lease another holder took over after this one
    expired is never deleted out from under them."""
    settings_lease.release(session_factory, lease)


def read_lease(db: Session, deployment: str) -> Optional[dict]:
    """Used by the envelope builder to detect `restart_interrupted` (an
    expired restart lease whose Deployment is stuck at 0 replicas)."""
    key = _lease_key(deployment)
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
    replacing the read-then-act `still_holds_lease`). True iff the lease was
    still ours; False means another replica has already taken over. The
    UPDATE...WHERE in settings_lease.renew is the check and the extension in
    one statement, so there's no TOCTOU window between them."""
    return settings_lease.renew(session_factory, lease, ttl=ttl, now=now)


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
