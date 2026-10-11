"""Admin side of conversation capture: settings, status, purge and the retention loop.

The orchestrator writes turns (``shared.conversation_capture``); this module
reads the flags and the writer's per-replica status rows, validates and saves
the two operator settings, and deletes expired rows.

Rules:

* SQL here is SQLAlchemy ``text()`` with named parameters, constants only.
  Intervals are ``make_interval(days => :days)`` against the database's own
  ``NOW()``, never a Python clock (``created_at`` is a naive ``DEFAULT NOW()``).
* The stored retention is read strictly (``parse_stored_retention``). A bad
  value deletes nothing: there is no fallback, because a fallback lower than
  the operator's value would delete data.
* Purge runs under the ``settings_lease`` key ``conversation_capture.purge.lock``
  (one holder across replicas, renewed between batches). Losing the lease
  aborts at once and commits nothing further.
* Logs and audit rows carry ids, counts, scopes and ``error_type`` only,
  never conversation text.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import structlog
from fastapi import Depends, Header, HTTPException, Request, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import DEV_MODE, SessionLocal, get_db
from app.models import AuditLog, SystemSetting, User
from app.services import settings_lease
from app.utils import rate_limit
from shared import conversation_capture as contract

logger = structlog.get_logger()

VIEW_TEXT_PERMISSION = "view_conversation_text"
PURGE_CONFIRM_PHRASE = "purge all conversations"

PURGE_BATCH_SIZE = 5000
PURGE_LEASE_TTL_S = 300
LOOP_TICK_S = 6 * 60 * 60
LOOP_FIRST_RUN_DELAY_S = 5 * 60
WRITER_STALE_AFTER_S = 180
WRITER_IGNORED_AFTER_S = 24 * 60 * 60

STATUS_OFF = "off"
STATUS_NO_WRITER = "on_no_writer"
STATUS_STALE = "on_stale"
STATUS_FAILING = "on_failing"
STATUS_OK = "on_ok"
RETENTION_OK = "ok"
RETENTION_INVALID = "retention_invalid"

ABORTED_LEASE_LOST = "lease_lost"

REFUSAL_AUDIT_INTERVAL_S = 60.0
_refusal_audited_at: Dict[tuple, float] = {}
_REFUSAL_TRACKED_MAX = 512

_AUDIT_VALUE_MAX_CHARS = 100

SessionFactory = Callable[[], Session]

# ---------------------------------------------------------------------------
# SQL (named parameters; constants only)
# ---------------------------------------------------------------------------

TURN_COUNTS_SQL = """
SELECT COUNT(*) AS turns, MIN(created_at) AS oldest, MAX(created_at) AS newest
FROM conversation_turns
"""

PREVIEW_EXPIRED_TURNS_SQL = """
SELECT COUNT(*) FROM conversation_turns
WHERE created_at < NOW() - make_interval(days => :days)
"""

DELETE_EXPIRED_TURNS_SQL = """
DELETE FROM conversation_turns
WHERE id IN (
    SELECT id FROM conversation_turns
    WHERE created_at < NOW() - make_interval(days => :days)
    ORDER BY created_at
    LIMIT :batch
)
"""

DELETE_ALL_TURNS_SQL = """
DELETE FROM conversation_turns
WHERE id IN (SELECT id FROM conversation_turns LIMIT :batch)
"""

# The age test is repeated in the outer WHERE on purpose. A writer that re-used
# the conversation bumped its ended_at; under READ COMMITTED the DELETE
# re-checks the outer predicate on the new row version (it does not re-run the
# sub-select), so a conversation a concurrent writer just extended survives
# instead of being deleted with its new turn cascading away.
DELETE_EMPTY_EXPIRED_CONVERSATIONS_SQL = """
DELETE FROM conversations
WHERE id IN (
    SELECT c.id FROM conversations c
    WHERE NOT EXISTS (SELECT 1 FROM conversation_turns t WHERE t.conversation_id = c.id)
      AND COALESCE(c.ended_at, c.started_at) < NOW() - make_interval(days => :days)
    LIMIT :batch
)
AND COALESCE(ended_at, started_at) < NOW() - make_interval(days => :days)
"""

DELETE_EMPTY_CONVERSATIONS_SQL = """
DELETE FROM conversations
WHERE id IN (
    SELECT c.id FROM conversations c
    WHERE NOT EXISTS (SELECT 1 FROM conversation_turns t WHERE t.conversation_id = c.id)
    LIMIT :batch
)
"""

EMERGING_INTENTS_EXIST_SQL = "SELECT to_regclass('emerging_intents') IS NOT NULL"

# The Emerging Intents page keeps up to 10 example queries per intent. They are what people
# said, so they expire with the conversations: cleared on rows not seen within the window
# (every row for a purge of everything). The intent itself and its counters stay.
CLEAR_EXPIRED_SAMPLES_SQL = """
UPDATE emerging_intents SET sample_queries = '[]'::json
WHERE last_seen < NOW() - make_interval(days => :days)
  AND sample_queries::text NOT IN ('[]', 'null')
"""

CLEAR_ALL_SAMPLES_SQL = """
UPDATE emerging_intents SET sample_queries = '[]'::json
WHERE sample_queries::text NOT IN ('[]', 'null')
"""

DELETE_STALE_WRITER_STATUS_SQL = """
DELETE FROM system_settings
WHERE key LIKE :prefix
  AND updated_at < NOW() - make_interval(hours => 24)
"""


# ---------------------------------------------------------------------------
# Settings and status
# ---------------------------------------------------------------------------

def _setting(db: Session, key: str) -> Optional[SystemSetting]:
    return db.query(SystemSetting).filter(SystemSetting.key == key).first()


def _flag(db: Session, key: str) -> bool:
    row = _setting(db, key)
    return bool(row) and (row.value or "").strip().lower() == "true"


def stored_retention(db: Session) -> Optional[int]:
    """The strictly parsed stored retention, or ``None`` (missing or invalid)."""
    row = _setting(db, contract.RETENTION_KEY)
    return contract.parse_stored_retention(row.value) if row else None


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _iso(moment: Optional[datetime]) -> Optional[str]:
    return moment.isoformat() if moment else None


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        return _aware(datetime.fromisoformat(value)) if value else None
    except (TypeError, ValueError):
        return None


def _as_datetime(value: Any) -> Optional[datetime]:
    """Aggregates come back as datetimes from PostgreSQL and as strings from SQLite."""
    if isinstance(value, str):
        return _parse_time(value)
    return value


def _writer_state(db: Session, now: datetime) -> Dict[str, Any]:
    """Aggregate the per-replica status rows.

    Rows older than 24 h are ignored. With rows left: none fresh (within
    180 s) is stale; otherwise any fresh failing replica makes it failing
    (worst wins); otherwise ok. ``dropped`` sums every counted row.
    """
    prefix = contract.WRITER_STATUS_KEY + "."
    rows = db.query(SystemSetting).filter(SystemSetting.key.like(prefix + "%")).all()
    live, dropped, fresh_states = 0, 0, []
    for row in rows:
        updated = _aware(row.updated_at) or _aware(row.created_at)
        if updated is None or (now - updated).total_seconds() > WRITER_IGNORED_AFTER_S:
            continue
        try:
            report = json.loads(row.value)
        except (TypeError, ValueError):
            continue
        if not isinstance(report, dict):
            continue
        live += 1
        dropped += report.get("dropped") if isinstance(report.get("dropped"), int) else 0
        if (now - updated).total_seconds() <= WRITER_STALE_AFTER_S:
            last_ok, last_err = _parse_time(report.get("last_ok_at")), _parse_time(report.get("last_error_at"))
            failing = last_err is not None and (last_ok is None or last_err > last_ok)
            fresh_states.append(STATUS_FAILING if failing else STATUS_OK)
    if live == 0:
        state = STATUS_NO_WRITER
    elif not fresh_states:
        state = STATUS_STALE
    elif STATUS_FAILING in fresh_states:
        state = STATUS_FAILING
    else:
        state = STATUS_OK
    return {"state": state, "replicas": live, "dropped": dropped}


def _turn_counts(analytics_db: Optional[Session]) -> Dict[str, Any]:
    empty = {"turn_count": None, "oldest_turn_at": None, "last_turn_at": None}
    if analytics_db is None:
        return empty
    try:
        row = analytics_db.execute(text(TURN_COUNTS_SQL)).fetchone()
    except Exception as exc:
        analytics_db.rollback()
        logger.warning("conversation_capture_counts_failed", error_type=type(exc).__name__)
        return empty
    return {"turn_count": int(row[0]), "oldest_turn_at": _iso(_as_datetime(row[1])), "last_turn_at": _iso(_as_datetime(row[2]))}


def capture_status(db: Session, analytics_db: Optional[Session] = None, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """The D13 status. Carries counts and times only, never text.

    ``analytics_db`` is optional: without it the turn counts are ``None`` so
    the settings stay readable where conversation storage isn't available.
    """
    now = now or datetime.now(timezone.utc)
    analytics_on = _flag(db, contract.ANALYTICS_KEY)
    debug_on = _flag(db, contract.DEBUG_KEY)
    writer = _writer_state(db, now)
    retention = stored_retention(db)
    counts = _turn_counts(analytics_db)

    debug_row = _setting(db, contract.DEBUG_KEY)
    debug_since = _iso(_aware(debug_row.updated_at)) if debug_on and debug_row else None
    next_expiry = None
    oldest = _parse_time(counts["oldest_turn_at"])
    if oldest is not None and retention is not None:
        next_expiry = _iso(oldest + timedelta(days=retention))

    return {
        "state": STATUS_OFF if not analytics_on else writer["state"],
        "analytics_enabled": analytics_on,
        "debug_enabled": debug_on,
        "debug_enabled_since": debug_since,
        "retention_days": retention,
        "retention_state": RETENTION_OK if retention is not None else RETENTION_INVALID,
        "next_expiry_at": next_expiry,
        "writer_replicas": writer["replicas"],
        "dropped": writer["dropped"],
        **counts,
    }


def preview_delete_turns(analytics_db: Optional[Session], days: int) -> Optional[int]:
    """How many turns a retention of ``days`` would delete now (counts only)."""
    if analytics_db is None:
        return None
    try:
        return int(analytics_db.execute(text(PREVIEW_EXPIRED_TURNS_SQL), {"days": days}).scalar() or 0)
    except Exception as exc:
        analytics_db.rollback()
        logger.warning("conversation_capture_preview_failed", error_type=type(exc).__name__)
        return None


def retention_is_lowering(db: Session, new_days: int) -> bool:
    """True when ``new_days`` is below the stored value, or the stored value is unreadable.

    An unreadable stored value counts as lowering: replacing it is the one
    case where the operator can't see what is being shortened.
    """
    current = stored_retention(db)
    return current is None or new_days < current


def _upsert_setting(db: Session, key: str, value: str, description: str) -> None:
    row = _setting(db, key)
    if row is None:
        db.add(SystemSetting(key=key, value=value, description=description, category="privacy"))
    else:
        row.value = value


def update_capture_settings(db: Session, *, debug_enabled: bool, retention_days: int) -> Dict[str, Any]:
    """Save both settings; returns ``{old, new}`` (booleans and ints only)."""
    old = {"debug_enabled": _flag(db, contract.DEBUG_KEY), "retention_days": stored_retention(db)}
    _upsert_setting(db, contract.DEBUG_KEY, "true" if debug_enabled else "false",
                    "Store tuning detail with captured conversations (needs Analytics Mode).")
    _upsert_setting(db, contract.RETENTION_KEY, str(retention_days),
                    "Days captured conversations are kept before they are deleted (1-3650).")
    db.commit()
    return {"old": old, "new": {"debug_enabled": debug_enabled, "retention_days": retention_days}}


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _audit_values(values: Dict[str, Any]) -> Dict[str, Any]:
    """Booleans, numbers and short labels only: never conversation text."""
    clean: Dict[str, Any] = {}
    for key, value in values.items():
        if isinstance(value, (bool, int, float)) or value is None:
            clean[key] = value
        elif isinstance(value, str):
            clean[key] = value[:_AUDIT_VALUE_MAX_CHARS]
    return clean


def write_audit(db: Session, actor: User, action: str, resource_type: str, *,
                old: Optional[Dict[str, Any]] = None, new: Optional[Dict[str, Any]] = None) -> None:
    db.add(AuditLog(
        user_id=actor.id,
        action=action,
        resource_type=resource_type,
        old_value=_audit_values(old) if old is not None else None,
        new_value=_audit_values(new) if new is not None else None,
        success=True,
    ))
    db.commit()


def audit_text_read(db: Session, actor: User, endpoint: str, **counts_and_filters: Any) -> None:
    """One row per response that carries conversation text. Never the text."""
    write_audit(db, actor, "conversation_text_viewed", "conversation",
                new={"endpoint": endpoint, **counts_and_filters})


# ---------------------------------------------------------------------------
# Purge
# ---------------------------------------------------------------------------

class RetentionInvalid(Exception):
    """The stored retention can't be read; nothing is deleted."""


@dataclass
class PurgeResult:
    turns: int = 0
    conversations: int = 0
    aborted: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


def get_purge_session_factory() -> SessionFactory:
    """Dependency seam: tests substitute their own session factory."""
    return SessionLocal


def _read_retention(session_factory: SessionFactory) -> Optional[int]:
    with session_factory() as session:
        return stored_retention(session)


def purge(session_factory: SessionFactory, scope: str, *, batch_size: int = PURGE_BATCH_SIZE,
          renew: Callable[..., bool] = settings_lease.renew,
          progress: Optional["PurgeResult"] = None) -> PurgeResult:
    """Delete expired (``scope='expired'``) or all captured conversations.

    Raises ``RetentionInvalid`` (before taking the lease) for ``expired`` with an
    unreadable stored retention, and ``settings_lease.LeaseBusy`` when another
    purge holds the lease. The lease is renewed before every delete batch; a
    failed renewal stops at once, reporting ``aborted='lease_lost'``, before
    anything more is deleted. ``progress`` is the result object updated as
    batches commit, so a caller can report partial counts if this raises or is
    interrupted.
    """
    if scope not in ("expired", "all"):
        raise ValueError("unknown purge scope")
    days = None
    if scope == "expired":
        days = _read_retention(session_factory)
        if days is None:
            raise RetentionInvalid()

    lease = settings_lease.acquire(
        session_factory, contract.PURGE_LEASE_KEY,
        category=contract.WRITER_STATUS_CATEGORY, ttl=PURGE_LEASE_TTL_S,
        busy_message="A purge is already running.",
    )
    result = progress if progress is not None else PurgeResult()
    try:
        steps = (
            ("turns", DELETE_EXPIRED_TURNS_SQL if scope == "expired" else DELETE_ALL_TURNS_SQL),
            ("conversations", DELETE_EMPTY_EXPIRED_CONVERSATIONS_SQL if scope == "expired" else DELETE_EMPTY_CONVERSATIONS_SQL),
        )
        params: Dict[str, Any] = {"batch": batch_size}
        if days is not None:
            params["days"] = days
        for counter, sql in steps:
            while True:
                if not renew(session_factory, lease, ttl=PURGE_LEASE_TTL_S):
                    result.aborted = ABORTED_LEASE_LOST
                    return result
                with session_factory() as session:
                    deleted = session.execute(text(sql), params).rowcount or 0
                    session.commit()
                setattr(result, counter, getattr(result, counter) + deleted)
                if deleted < batch_size:
                    break
        if not renew(session_factory, lease, ttl=PURGE_LEASE_TTL_S):
            result.aborted = ABORTED_LEASE_LOST
            return result
        with session_factory() as session:
            if session.execute(text(EMERGING_INTENTS_EXIST_SQL)).scalar():
                cleared = session.execute(
                    text(CLEAR_EXPIRED_SAMPLES_SQL if scope == "expired" else CLEAR_ALL_SAMPLES_SQL),
                    {"days": days} if scope == "expired" else {},
                ).rowcount or 0
                if cleared:
                    logger.info("emerging_intent_samples_cleared", scope=scope, intents=cleared)
            session.commit()
        with session_factory() as session:
            session.execute(text(DELETE_STALE_WRITER_STATUS_SQL), {"prefix": contract.WRITER_STATUS_KEY + ".%"})
            session.commit()
        return result
    finally:
        settings_lease.release(session_factory, lease)


# ---------------------------------------------------------------------------
# Retention loop (shape of memory_vectors._maintenance_loop)
# ---------------------------------------------------------------------------

_task: Optional["asyncio.Task[None]"] = None


def run_scheduled_purge(session_factory: Optional[SessionFactory] = None) -> Optional[PurgeResult]:
    """One scheduled ``expired`` purge. An invalid retention or a held lease skips the run."""
    factory = session_factory or SessionLocal
    try:
        result = purge(factory, "expired")
    except RetentionInvalid:
        logger.error("conversation_retention_invalid")
        return None
    except settings_lease.LeaseBusy:
        logger.debug("conversation_purge_skipped", reason="lease_busy")
        return None
    if result.turns or result.conversations or result.aborted:
        logger.info("conversation_purge_finished", scope="expired", turns=result.turns,
                    conversations=result.conversations, aborted=result.aborted)
    return result


async def _retention_loop() -> None:
    await asyncio.sleep(LOOP_FIRST_RUN_DELAY_S)
    while True:
        try:
            await asyncio.to_thread(run_scheduled_purge)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("conversation_purge_tick_failed", error_type=type(exc).__name__)
        await asyncio.sleep(LOOP_TICK_S)


def start_retention_loop() -> None:
    """Start the daily purge. Skipped in DEV_MODE; runs whether or not Analytics Mode is on."""
    global _task
    if DEV_MODE:
        return
    if _task is not None and not _task.done():
        logger.warning("conversation_retention_loop_already_running")
        return
    _task = asyncio.create_task(_retention_loop())
    logger.info("conversation_retention_loop_started")


async def stop_retention_loop() -> None:
    global _task
    task, _task = _task, None
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    logger.info("conversation_retention_loop_stopped")


# ---------------------------------------------------------------------------
# Audited guard for POST /api/conversations/purge
# ---------------------------------------------------------------------------

def _audit_refusal(db: Session, user: User, reason: str, scope: Optional[str]) -> None:
    """At most one refusal row per user and reason per minute (per process)."""
    now = time.monotonic()
    key = (user.id, reason)
    last = _refusal_audited_at.get(key)
    if last is not None and now - last < REFUSAL_AUDIT_INTERVAL_S:
        return
    if len(_refusal_audited_at) >= _REFUSAL_TRACKED_MAX:
        _refusal_audited_at.clear()
    _refusal_audited_at[key] = now
    write_audit(db, user, "conversation_purge_refused", "conversation", new={"reason": reason, "scope": scope})


async def _requested_scope(request: Request) -> Optional[str]:
    try:
        scope = (await request.json()).get("scope")
    except Exception:
        return None
    return scope if scope in ("expired", "all") else None


def require_audited_purge():
    """Like ``require_user_permission``, but a refusal is audited first.

    Needs a signed-in user holding both ``view_conversation_text`` and
    ``delete``; any ``X-Service-Key`` is refused with 401. A 403 or 429 writes a
    ``conversation_purge_refused`` row (actor, reason, requested scope; no
    text) and commits it before raising. A 401 has no actor and isn't audited
    here (``AuthRejectionMiddleware`` logs it).
    """
    from app.auth.oidc import get_current_user

    async def _dependency(
        request: Request,
        response: Response,
        user=Depends(get_current_user),
        db: Session = Depends(get_db),
        x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
    ):
        if x_service_key is not None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="service key not accepted on this route")
        scope = await _requested_scope(request)
        if not (user.has_permission(VIEW_TEXT_PERMISSION) and user.has_permission("delete")):
            _audit_refusal(db, user, "insufficient_permission", scope)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        request.state.auth_kind = "user"
        try:
            await rate_limit.conversation_purge_rate_limit(request, response, str(user.id))
        except HTTPException as exc:
            if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                _audit_refusal(db, user, "rate_limited", scope)
            raise
        return user

    _dependency.required_permission = VIEW_TEXT_PERMISSION
    _dependency.caller_kinds = ("user",)
    return _dependency
