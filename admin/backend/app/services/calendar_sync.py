"""
Calendar sync: the one writer of feed-derived calendar events.

`run_source_sync` is the only code path that fetches a calendar source and
writes `calendar_events` for it. The background loop, sync-all and the
manual sync route all go through it.

Rules it enforces:
- A source whose bookings come from Lodgify (`source_type == 'lodgify'` or a
  lodgify.com feed host) and has a configured Lodgify API key writes only
  from the API. An API failure or an unreadable key writes nothing and never
  falls back to the iCal export.
- Reservation keys are scoped to their source: a key another source already
  uses is stored under `derived_key(source_id, key)` instead, so no source
  can move, block or hide another source's row, and no event is dropped.
- Nothing is written unless the whole sync succeeds.
- Status strings and logs carry only the exception class and HTTP status,
  never exception text (httpx messages embed feed URLs; SQLAlchemy errors
  embed bound parameters).
"""
import asyncio
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Literal, Optional
import structlog

from app.database import SessionLocal, DEV_MODE
from app.models import CalendarEvent, CalendarSource
from shared.booking_window import DEFAULT_CHECKIN_TIME, DEFAULT_CHECKOUT_TIME

logger = structlog.get_logger()

# Global reference to the background task (for graceful shutdown)
_sync_task: Optional[asyncio.Task] = None

# Minimum interval between sync cycles (to avoid hammering the database)
MIN_CHECK_INTERVAL_SECONDS = 60

# The scheduler never syncs a source more often than this, whatever its
# stored interval (create/update already reject smaller values).
MIN_SYNC_INTERVAL_MINUTES = 5

SyncStatus = Literal["success", "failed", "busy", "not_found", "not_due"]
Trigger = Literal["scheduled", "manual"]

API_CREATED_BY = 'lodgify_api_sync'
ICAL_CREATED_BY = 'ical_sync'


def _now() -> datetime:
    """The sync clock. One value per run (`run_stamp`); patched in tests."""
    return datetime.now(timezone.utc)


def derived_key(source_id: int, uid: str) -> str:
    """The source-scoped storage key for a UID another row already uses."""
    return f"src:{source_id}:" + hashlib.sha256(uid.encode()).hexdigest()[:32]


def uid_sha10(uid: str) -> str:
    """A log-safe fingerprint of a UID (UIDs can embed guest emails)."""
    return hashlib.sha256(uid.encode()).hexdigest()[:10]


@dataclass(frozen=True)
class SyncOutcome:
    status: SyncStatus
    method: Optional[str] = None
    events_total: int = 0
    added: int = 0
    updated: int = 0
    matched_natural_key: int = 0
    matched_deleted: int = 0
    matched_non_ical: int = 0
    rekeyed: int = 0
    adopted: int = 0
    error: Optional[str] = None
    warning: Optional[str] = None


@dataclass
class _Counts:
    added: int = 0
    updated: int = 0
    matched_natural_key: int = 0
    matched_deleted: int = 0
    matched_non_ical: int = 0
    rekeyed: int = 0
    adopted: int = 0
    claimed_ids: set = field(default_factory=set)


class _SyncFailed(Exception):
    """A handled failure whose `message` is already safe to store and show."""

    def __init__(self, method: Optional[str], message: str):
        super().__init__(message)
        self.method = method
        self.message = message


def _warning(counts: _Counts) -> Optional[str]:
    parts = []
    if counts.rekeyed:
        parts.append(
            f"{counts.rekeyed} events stored under a source-specific ID because their IDs are used elsewhere"
        )
    if counts.matched_deleted:
        parts.append(
            f"{counts.matched_deleted} events matched entries you deleted or cancelled and were left as you set them"
        )
    return "; ".join(parts) or None


def _apply_event_fields(row: CalendarEvent, event: dict, run_stamp: datetime, *, status: Optional[str] = None) -> None:
    """Copy feed fields onto a matched row. Reclassifies confirmed<->blocked,
    but never overwrites an owner-set cancelled/pending status; never
    touches `deleted_at`, `external_id` or `source_id`."""
    row.title = event['title']
    row.checkin = event['checkin']
    row.checkout = event['checkout']
    row.guest_name = event['guest_name']
    row.guest_phone = event.get('guest_phone')
    row.guest_email = event.get('guest_email')
    row.notes = event['notes']
    row.source = event['source']
    new_status = status or event.get('status')
    if row.status in ('confirmed', 'blocked') and new_status:
        row.status = new_status
    row.synced_at = run_stamp


def _new_event(source: CalendarSource, event: dict, key: str, created_by: str,
               run_stamp: datetime, *, status: Optional[str] = None) -> CalendarEvent:
    return CalendarEvent(
        external_id=key,
        title=event['title'],
        checkin=event['checkin'],
        checkout=event['checkout'],
        guest_name=event['guest_name'],
        guest_phone=event.get('guest_phone'),
        guest_email=event.get('guest_email'),
        notes=event['notes'],
        source=event['source'],
        source_id=source.id,
        status=status or event.get('status', 'confirmed'),
        created_by=created_by,
        synced_at=run_stamp,
    )


def _log_rekeyed(branch: str, source: CalendarSource, uid: str, owner: Optional[CalendarEvent]) -> None:
    logger.warning(
        "calendar_sync_uid_collision_rekeyed",
        branch=branch,
        source_id=source.id,
        other_source_id=owner.source_id if owner is not None else None,
        other_created_by=owner.created_by if owner is not None else None,
        uid_sha10=uid_sha10(uid),
    )


def _upsert_api_events(db, source: CalendarSource, events: list, run_stamp: datetime) -> _Counts:
    """API branch of the key rules. A reservation key `k` hits only this
    source's own row (stored as `k` or `derived_key(S, k)`); an orphaned
    API row (`source_id IS NULL`, written by the API sync) is adopted;
    otherwise it's inserted under `k` when free anywhere, else under the
    derived key. Another source's row is never read for writing."""
    counts = _Counts()
    inserted: dict[str, CalendarEvent] = {}
    for event in events:
        k = event['external_id']
        derived = derived_key(source.id, k)
        if k in inserted:
            _apply_event_fields(inserted[k], event, run_stamp)
            continue

        hit = (
            db.query(CalendarEvent)
            .filter(CalendarEvent.source_id == source.id, CalendarEvent.external_id.in_([k, derived]))
            .order_by(CalendarEvent.id)
            .first()
        )
        if hit is not None:
            _apply_event_fields(hit, event, run_stamp)
            counts.updated += 1
            continue

        orphan = (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.external_id == k,
                CalendarEvent.source_id.is_(None),
                CalendarEvent.created_by == API_CREATED_BY,
            )
            .first()
        )
        if orphan is not None:
            orphan.source_id = source.id
            _apply_event_fields(orphan, event, run_stamp)
            counts.adopted += 1
            logger.info("calendar_sync_orphan_adopted", source_id=source.id, event_id=orphan.id)
            continue

        owner = db.query(CalendarEvent).filter(CalendarEvent.external_id == k).first()
        if owner is None:
            key = k
        else:
            key = derived
            counts.rekeyed += 1
            _log_rekeyed("api", source, k, owner)
        row = _new_event(source, event, key, API_CREATED_BY, run_stamp)
        db.add(row)
        inserted[k] = row
        counts.added += 1
    return counts


def _upsert_ical_events(db, source: CalendarSource, events: list, run_stamp: datetime) -> _Counts:
    """iCal branch: update by UID, else insert."""
    counts = _Counts()
    for event in events:
        existing = db.query(CalendarEvent).filter(
            CalendarEvent.external_id == event['external_id']
        ).first()
        if existing is not None:
            _apply_event_fields(existing, event, run_stamp)
            counts.updated += 1
        else:
            db.add(_new_event(source, event, event['external_id'], ICAL_CREATED_BY, run_stamp))
            counts.added += 1
    return counts


async def _fetch_events(db, source: CalendarSource, cs) -> tuple[str, list]:
    """Returns (method, events). Raises _SyncFailed with a safe message."""
    checkin_time = source.default_checkin_time or DEFAULT_CHECKIN_TIME
    checkout_time = source.default_checkout_time or DEFAULT_CHECKOUT_TIME

    if source.source_type == 'lodgify' or cs.is_lodgify_host(source.ical_url):
        key_status, api_key = cs.resolve_lodgify_api_key(db)
        if key_status == "unreadable":
            logger.error("calendar_sync_lodgify_api_failed", source_id=source.id, reason="api_key_unreadable")
            raise _SyncFailed(
                'lodgify_api',
                "Lodgify API key could not be read; no changes written, existing bookings kept",
            )
        if key_status == "ok":
            try:
                events = await cs.fetch_lodgify_reservations(
                    api_key, checkin_time=checkin_time, checkout_time=checkout_time
                )
            except Exception as exc:
                logger.error("calendar_sync_lodgify_api_failed", source_id=source.id, **cs.safe_error(exc))
                raise _SyncFailed(
                    'lodgify_api',
                    f"Lodgify API sync failed ({cs.describe_error(exc)}); no changes written, existing bookings kept",
                )
            return 'lodgify_api', events

    try:
        ical_data = await cs.fetch_ical_data(source.ical_url)
    except Exception as exc:
        logger.warning("calendar_sync_ical_fetch_failed", source_id=source.id, **cs.safe_error(exc))
        raise _SyncFailed('ical', f"iCal fetch failed ({cs.describe_error(exc)}); no changes written")
    try:
        events = cs.parse_ical_events(
            ical_data, source.source_type, checkin_time=checkin_time, checkout_time=checkout_time
        )
    except Exception as exc:
        logger.warning("calendar_sync_ical_parse_failed", source_id=source.id, **cs.safe_error(exc))
        raise _SyncFailed('ical', f"iCal parse failed ({cs.describe_error(exc)}); no changes written")
    return 'ical', events


def _record_failure(db, source_id: int, message: str) -> None:
    """Roll back anything staged, then stamp the failure on the source."""
    from app.routes.calendar_sources import safe_error

    try:
        db.rollback()
        source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()
        if source is not None:
            source.last_sync_at = _now()
            source.last_sync_status = 'failed'
            source.last_sync_error = message
            db.commit()
    except Exception as exc:
        logger.error("calendar_sync_status_write_failed", source_id=source_id, **safe_error(exc))
        db.rollback()


async def run_source_sync(source_id: int, db, *, trigger: Trigger) -> SyncOutcome:
    """Sync one source. Never raises except `asyncio.CancelledError`.

    `trigger="scheduled"` is the background loop; everything else (the
    route, sync-all, `sync_single_source`) is `"manual"`.
    """
    from app.routes import calendar_sources as cs

    run_stamp = _now()
    method: Optional[str] = None
    try:
        source = db.query(CalendarSource).filter(CalendarSource.id == source_id).first()
        if source is None:
            logger.warning("calendar_source_not_found", source_id=source_id)
            return SyncOutcome(status="not_found")

        method, events = await _fetch_events(db, source, cs)
        if method == 'lodgify_api':
            counts = _upsert_api_events(db, source, events, run_stamp)
        else:
            counts = _upsert_ical_events(db, source, events, run_stamp)

        warning = _warning(counts)
        source.last_sync_at = run_stamp
        source.last_sync_status = 'success'
        source.last_sync_error = warning
        source.last_event_count = len(events)
        db.commit()

        logger.info(
            "background_calendar_sync_complete" if trigger == "scheduled" else "calendar_source_sync_complete",
            source_id=source_id,
            sync_method=method,
            trigger=trigger,
            events_total=len(events),
            added=counts.added,
            updated=counts.updated,
            matched_natural_key=counts.matched_natural_key,
            matched_deleted=counts.matched_deleted,
            matched_non_ical=counts.matched_non_ical,
            rekeyed=counts.rekeyed,
            adopted=counts.adopted,
        )

        if source.source_type == 'lodgify':
            await cs.sync_lodgify_to_guest_sessions(db)
            await cs.update_guest_session_statuses(db)

        return SyncOutcome(
            status="success",
            method=method,
            events_total=len(events),
            added=counts.added,
            updated=counts.updated,
            matched_natural_key=counts.matched_natural_key,
            matched_deleted=counts.matched_deleted,
            matched_non_ical=counts.matched_non_ical,
            rekeyed=counts.rekeyed,
            adopted=counts.adopted,
            warning=warning,
        )

    except _SyncFailed as failure:
        _record_failure(db, source_id, failure.message)
        return SyncOutcome(status="failed", method=failure.method, error=failure.message)
    except Exception as exc:
        logger.error("calendar_sync_failed", source_id=source_id, **cs.safe_error(exc))
        message = f"Calendar sync failed ({cs.describe_error(exc)}); no changes written"
        _record_failure(db, source_id, message)
        return SyncOutcome(status="failed", method=method, error=message)


async def sync_single_source(source_id: int, db_session) -> bool:
    """Sync one source as a manual trigger. True iff it succeeded."""
    outcome = await run_source_sync(source_id, db_session, trigger="manual")
    return outcome.status == "success"


async def sync_source_in_new_session(source_id: int) -> bool:
    """Sync one source in a fresh SessionLocal(), for use as a
    BackgroundTasks callback (ATHENA-127 bob H3d) -- the request session is
    gone by the time a background task runs."""
    db = SessionLocal()
    try:
        return await sync_single_source(source_id, db)
    finally:
        db.close()


def _effective_interval(source: CalendarSource) -> timedelta:
    return timedelta(minutes=max(MIN_SYNC_INTERVAL_MINUTES, source.sync_interval_minutes or 0))


def _is_due(source: CalendarSource, now: datetime) -> bool:
    if source.last_sync_at is None:
        return True
    last_sync = source.last_sync_at
    if last_sync.tzinfo is None:
        last_sync = last_sync.replace(tzinfo=timezone.utc)
    return now >= last_sync + _effective_interval(source)


async def check_and_sync_sources():
    """Sync every enabled source that is due: never synced, or at least
    `max(5, sync_interval_minutes)` minutes since its last sync."""
    from app.routes.calendar_sources import safe_error

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        sources = db.query(CalendarSource).filter(
            CalendarSource.enabled == True  # noqa: E712
        ).all()

        if not sources:
            logger.debug("no_enabled_calendar_sources")
            return

        synced_count = 0
        for source in sources:
            if not _is_due(source, now):
                continue
            logger.info("source_sync_due", source_id=source.id, source_name=source.name)

            # A fresh session per sync to avoid transaction issues
            sync_db = SessionLocal()
            try:
                outcome = await run_source_sync(source.id, sync_db, trigger="scheduled")
                if outcome.status == "success":
                    synced_count += 1
            finally:
                sync_db.close()

        if synced_count > 0:
            logger.info("background_sync_cycle_complete",
                       sources_synced=synced_count,
                       total_sources=len(sources))

    except Exception as exc:
        logger.error("background_sync_check_failed", **safe_error(exc))
    finally:
        db.close()


async def calendar_sync_loop():
    """
    Main background loop that periodically checks and syncs calendar sources.

    Runs continuously until the application shuts down.
    """
    from app.routes.calendar_sources import safe_error

    logger.info("calendar_sync_background_task_started")

    # Initial delay to let the app fully start
    await asyncio.sleep(10)

    while True:
        try:
            await check_and_sync_sources()
        except asyncio.CancelledError:
            logger.info("calendar_sync_background_task_cancelled")
            break
        except Exception as exc:
            logger.error("calendar_sync_loop_error", **safe_error(exc))

        # Wait before next check cycle
        await asyncio.sleep(MIN_CHECK_INTERVAL_SECONDS)


def start_background_sync():
    """
    Start the background calendar sync task.

    Called from main.py during application startup.
    """
    global _sync_task

    if DEV_MODE:
        logger.info("calendar_sync_skipped_dev_mode")
        return

    if _sync_task is not None and not _sync_task.done():
        logger.warning("calendar_sync_task_already_running")
        return

    _sync_task = asyncio.create_task(calendar_sync_loop())
    logger.info("calendar_sync_background_task_created")


async def stop_background_sync():
    """
    Stop the background calendar sync task gracefully.

    Called from main.py during application shutdown.
    """
    global _sync_task

    if _sync_task is None or _sync_task.done():
        return

    logger.info("calendar_sync_background_task_stopping")
    _sync_task.cancel()

    try:
        await _sync_task
    except asyncio.CancelledError:
        pass

    logger.info("calendar_sync_background_task_stopped")
