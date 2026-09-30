"""A cross-replica lease stored as one `system_settings` row.

`system_settings.key` is unique, so an INSERT is an atomic test-and-set: a
second INSERT for the same key raises `IntegrityError`. An expired lease is
taken over by a conditional UPDATE that matches the exact value observed,
and renewal is the same compare-and-swap, so two holders can never both
believe they own a key.

Invariants:
- `acquire` returns a committed lease or raises `LeaseBusy`.
- `renew` and `release` act only when the stored value exactly matches the
  holder's own `Lease.value`; a lease another holder took over after expiry
  is never extended or deleted.
- Every call uses its own session from `session_factory`, so the lease is
  visible to other replicas immediately and the caller's session is never
  touched.
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

SessionFactory = Callable[[], Session]
Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class LeaseBusy(Exception):
    """The lease is held (unexpired) by another holder, or a takeover race
    was lost."""


@dataclass
class Lease:
    key: str
    holder: str
    value: str  # the exact JSON string written; renew/release match on it


def acquire(
    session_factory: SessionFactory,
    key: str,
    *,
    category: str,
    ttl: int,
    busy_message: str,
    fields: Optional[dict] = None,
    now: Clock = _utcnow,
) -> Lease:
    """INSERT-based acquire; conditional-UPDATE takeover when the existing
    lease has expired. `fields` are stored alongside `holder` and
    `expires_at` in the lease's JSON value."""
    holder = f"{os.getenv('HOSTNAME', 'local')}/{uuid.uuid4()}"
    expires_at = now() + timedelta(seconds=ttl)
    value = json.dumps(
        {**(fields or {}), "holder": holder, "expires_at": expires_at.isoformat()},
        sort_keys=True,
    )

    session = session_factory()
    try:
        session.add(SystemSetting(key=key, value=value, category=category))
        try:
            session.commit()
            return Lease(key=key, holder=holder, value=value)
        except (IntegrityError, OperationalError):
            # SQLite serializes writers: a genuine unique-key collision
            # raises IntegrityError, but two connections racing the same
            # INSERT can instead surface as OperationalError ("database is
            # locked"). Both mean someone else is contending right now.
            session.rollback()

        existing = session.query(SystemSetting).filter(SystemSetting.key == key).first()
        if existing is None:
            # Raced with a release between the failed INSERT and this read:
            # retry the INSERT once. Losing again is a lost race, not a crash.
            try:
                session.add(SystemSetting(key=key, value=value, category=category))
                session.commit()
                return Lease(key=key, holder=holder, value=value)
            except (IntegrityError, OperationalError):
                session.rollback()
                raise LeaseBusy(busy_message)

        observed_value = existing.value
        if not is_expired(observed_value, now):
            raise LeaseBusy(busy_message)

        rowcount = (
            session.query(SystemSetting)
            .filter(SystemSetting.key == key, SystemSetting.value == observed_value)
            .update({"value": value}, synchronize_session=False)
        )
        session.commit()
        if rowcount != 1:
            raise LeaseBusy(busy_message)
        return Lease(key=key, holder=holder, value=value)
    finally:
        session.close()


def is_expired(value: str, now: Clock = _utcnow) -> bool:
    """An unparsable value, or one without `expires_at`, counts as expired."""
    try:
        expires_at_str = json.loads(value).get("expires_at")
        if not expires_at_str:
            return True
        return datetime.fromisoformat(expires_at_str) <= now()
    except (TypeError, ValueError, AttributeError):
        return True


def release(session_factory: SessionFactory, lease: Lease) -> None:
    """Owner-only release: deletes the row only if it still holds this
    holder's exact value."""
    session = session_factory()
    try:
        session.query(SystemSetting).filter(
            SystemSetting.key == lease.key, SystemSetting.value == lease.value
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def renew(
    session_factory: SessionFactory,
    lease: Lease,
    *,
    ttl: int,
    now: Clock = _utcnow,
) -> bool:
    """Atomic compare-and-swap renewal: pushes `expires_at` forward only if
    the stored value still exactly matches `lease.value`. The check and the
    extension are one UPDATE...WHERE, so there's no window in which another
    holder could take over between them. Returns True (and updates
    `lease.value` so a later release matches) iff the row was still ours."""
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
            .filter(SystemSetting.key == lease.key, SystemSetting.value == old_value)
            .update({"value": new_value}, synchronize_session=False)
        )
        session.commit()
        if rowcount == 1:
            lease.value = new_value
        return rowcount == 1
    finally:
        session.close()
