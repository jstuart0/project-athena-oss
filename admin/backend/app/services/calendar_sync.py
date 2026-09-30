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
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone, timedelta
from typing import Callable, Literal, Optional
import structlog

from app.database import SessionLocal, DEV_MODE
from app.models import CalendarEvent, CalendarSource, SystemSetting
from shared.booking_window import (
    DEFAULT_CHECKIN_TIME,
    DEFAULT_CHECKOUT_TIME,
    day_pair,
    db_value_to_utc,
    normalize_summary,
    resolve_property_tz,
)
from shared.config import get_config

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

SETTINGS_CATEGORY = 'calendar_sync'
# Before a source's first successful sync under this code there is no
# last_stamp; the previous sync is then recognised by per-row synced_at
# stamps, which the old code wrote one by one within a single run.
LEGACY_CONTINUITY_TOLERANCE = timedelta(seconds=60)

_RESERVED_UID = re.compile(r"lodgify_\d+")

DayPair = tuple[date, date]


def _now() -> datetime:
    """The sync clock. One value per run (`run_stamp`); patched in tests."""
    return datetime.now(timezone.utc)


def derived_key(source_id: int, uid: str) -> str:
    """The source-scoped storage key for a UID another row already uses."""
    return f"src:{source_id}:" + hashlib.sha256(uid.encode()).hexdigest()[:32]


def uid_sha10(uid: str) -> str:
    """A log-safe fingerprint of a UID (UIDs can embed guest emails)."""
    return hashlib.sha256(uid.encode()).hexdigest()[:10]


def is_reserved_uid(uid: str) -> bool:
    """UIDs in a key space this code mints itself: Lodgify API keys and the
    two derived forms. A feed UID of this shape is always stored derived."""
    return bool(_RESERVED_UID.fullmatch(uid)) or uid.startswith(("ical-nouid:", "src:"))


def _nouid_key(source_id: int) -> str:
    return f"ical-nouid:{source_id}:{uuid.uuid4().hex}"


def last_stamp_key(source_id: int) -> str:
    return f"calendar_sync.last_stamp.{source_id}"


def _read_last_stamp(db, source_id: int) -> Optional[datetime]:
    row = db.query(SystemSetting).filter(SystemSetting.key == last_stamp_key(source_id)).first()
    if row is None:
        return None
    try:
        return db_value_to_utc(datetime.fromisoformat(row.value))
    except (TypeError, ValueError):
        return None


def _stage_last_stamp(db, source_id: int, run_stamp: datetime) -> None:
    """Stage `calendar_sync.last_stamp.<S>` in the caller's transaction, so
    it commits exactly when the run's rows do."""
    key = last_stamp_key(source_id)
    row = db.query(SystemSetting).filter(SystemSetting.key == key).first()
    if row is None:
        db.add(SystemSetting(key=key, value=run_stamp.isoformat(), category=SETTINGS_CATEGORY))
    else:
        row.value = run_stamp.isoformat()


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
    inserted: list = field(default_factory=list)


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


def _apply_event_fields(row: CalendarEvent, event: dict, run_stamp: datetime, *,
                        status: Optional[str] = None, update_status: bool = True) -> None:
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
    if update_status and row.status in ('confirmed', 'blocked') and new_status:
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


@dataclass
class _Group:
    pair: DayPair
    members: list  # [(uid, event)] in feed order
    status: str
    rep: dict


def _group_events(events: list, tz) -> list:
    """Group feed events by local day pair, in pair order. A group is
    confirmed if any member is; its representative is the first confirmed
    member in feed order, else the first member."""
    by_pair: dict = {}
    for event in events:
        uid = (event.get('external_id') or '').strip()
        pair = day_pair(event['checkin'], event['checkout'], tz)
        by_pair.setdefault(pair, []).append((uid, event))
    groups = []
    for pair in sorted(by_pair):
        members = by_pair[pair]
        confirmed = [e for _, e in members if e.get('status', 'confirmed') == 'confirmed']
        groups.append(_Group(
            pair=pair,
            members=members,
            status='confirmed' if confirmed else 'blocked',
            rep=confirmed[0] if confirmed else members[0][1],
        ))
    return groups


def _continuity_check(db, source_id: int, ical_rows: list) -> Callable[[CalendarEvent], bool]:
    """Was this row listed in the source's previous successful sync?

    Exact mode (the normal case): its synced_at equals the stored
    last_stamp. Legacy mode (no last_stamp yet): its synced_at is within
    60 s of the newest synced_at among the source's iCal rows."""
    last_stamp = _read_last_stamp(db, source_id)
    if last_stamp is not None:
        return lambda row: row.synced_at is not None and db_value_to_utc(row.synced_at) == last_stamp
    stamps = [db_value_to_utc(r.synced_at) for r in ical_rows if r.synced_at is not None]
    if not stamps:
        return lambda row: False
    floor = max(stamps) - LEGACY_CONTINUITY_TOLERANCE
    return lambda row: row.synced_at is not None and db_value_to_utc(row.synced_at) >= floor


def _upsert_ical_events(db, source: CalendarSource, events: list, tz, run_stamp: datetime) -> _Counts:
    """iCal branch. Deterministic and independent of feed order.

    Feeds (Lodgify's export in particular) can mint a fresh UID on every
    fetch, so a UID miss falls back to the natural key: this source's own
    `ical_sync` row on the same local (check-in, check-out) day pair. Every
    ambiguity inserts rather than skips -- an extra row adds guest time for
    dates the feed really lists; a skipped one could leave a guest in owner
    mode.

    1. Group events by day pair (sorted).
    2. Pass A: a UID hit on this source's own `ical_sync` row (stored raw
       or derived). A UID that occurs in several groups (RRULE overrides)
       hits only a row already on that group's pair.
    3. Pass B, for groups without a hit: a live row on the pair; else a
       deleted/cancelled/pending row with the same title that was listed in
       the previous successful sync (left as the owner set it); else a
       row of this source from another writer that /bookings serves (no-op);
       else insert.
    No row is claimed twice in a run.
    """
    counts = _Counts()
    sid = source.id
    groups = _group_events(events, tz)

    uid_pairs: dict = {}
    for g in groups:
        for uid, _ in g.members:
            if uid:
                uid_pairs.setdefault(uid, set()).add(g.pair)
    multi_group = {uid for uid, pairs in uid_pairs.items() if len(pairs) > 1}
    first_pair = {uid: min(pairs) for uid, pairs in uid_pairs.items()}

    own_rows = db.query(CalendarEvent).filter(CalendarEvent.source_id == sid).order_by(CalendarEvent.id).all()
    ical_rows = [r for r in own_rows if r.created_by == ICAL_CREATED_BY]
    other_rows = [r for r in own_rows if r.created_by != ICAL_CREATED_BY]
    by_key = {r.external_id: r for r in ical_rows}
    pair_of = {r.id: day_pair(r.checkin, r.checkout, tz) for r in own_rows}
    ical_by_pair: dict = {}
    for r in ical_rows:
        ical_by_pair.setdefault(pair_of[r.id], []).append(r)
    other_by_pair: dict = {}
    for r in other_rows:
        other_by_pair.setdefault(pair_of[r.id], []).append(r)
    listed_last_sync = _continuity_check(db, sid, ical_rows)
    inserted_keys: dict = {}

    def claim(row: CalendarEvent) -> None:
        counts.claimed_ids.add(row.id)

    # Pass A: UID hits.
    hit_pairs = set()
    for g in groups:
        candidates = []
        for uid, _ in g.members:
            if not uid:
                continue
            for key in (uid, derived_key(sid, uid)):
                row = by_key.get(key)
                if row is None or row.id in counts.claimed_ids:
                    continue
                if uid in multi_group and pair_of[row.id] != g.pair:
                    continue
                candidates.append((row, uid))
        if not candidates:
            continue
        row, uid = min(candidates, key=lambda c: c[0].id)
        claim(row)
        if row.external_id == uid and is_reserved_uid(uid):
            migrated = derived_key(sid, uid)
            if migrated not in by_key:
                row.external_id = migrated
                by_key[migrated] = row
                logger.info("calendar_sync_legacy_reserved_uid_migrated",
                            source_id=sid, event_id=row.id, uid_sha10=uid_sha10(uid))
        _apply_event_fields(row, g.rep, run_stamp, status=g.status)
        counts.updated += 1
        hit_pairs.add(g.pair)

    def taken(key: str) -> Optional[CalendarEvent]:
        """The row using `key`, if any (a staged insert counts as taken)."""
        if key in inserted_keys:
            return inserted_keys[key]
        return db.query(CalendarEvent).filter(CalendarEvent.external_id == key).first()

    def insert_key(uid: str, pair: DayPair) -> str:
        if not uid:
            return _nouid_key(sid)
        if uid in multi_group and first_pair[uid] != pair:
            counts.rekeyed += 1
            _log_rekeyed("ical", source, uid, None)
            return _nouid_key(sid)
        derived = derived_key(sid, uid)
        if is_reserved_uid(uid):
            return derived if taken(derived) is None else _nouid_key(sid)
        owner = taken(uid)
        if owner is None:
            return uid
        counts.rekeyed += 1
        _log_rekeyed("ical", source, uid, owner)
        return derived if taken(derived) is None else _nouid_key(sid)

    # Pass B: natural key, then insert.
    for g in groups:
        if g.pair in hit_pairs:
            continue
        candidates = [r for r in ical_by_pair.get(g.pair, []) if r.id not in counts.claimed_ids]
        live = [r for r in candidates if r.deleted_at is None and r.status in ('confirmed', 'blocked')]
        if live:
            row = live[0]
            claim(row)
            _apply_event_fields(row, g.rep, run_stamp, status=g.status)
            counts.updated += 1
            counts.matched_natural_key += 1
            continue

        title = normalize_summary(g.rep.get('title'))
        resolved = [
            r for r in candidates
            if (r.deleted_at is not None or r.status in ('cancelled', 'pending'))
            and normalize_summary(r.title) == title
            and listed_last_sync(r)
        ]
        if resolved:
            row = resolved[0]
            claim(row)
            _apply_event_fields(row, g.rep, run_stamp, update_status=False)
            counts.matched_deleted += 1
            continue

        served = [r for r in other_by_pair.get(g.pair, []) if r.deleted_at is None and r.status == 'confirmed']
        if served:
            counts.matched_non_ical += 1
            continue

        rep_uid = next((uid for uid, e in g.members if e is g.rep), '')
        key = insert_key(rep_uid, g.pair)
        row = _new_event(source, g.rep, key, ICAL_CREATED_BY, run_stamp, status=g.status)
        db.add(row)
        inserted_keys[key] = row
        counts.inserted.append(row)
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
            tz, _ = resolve_property_tz(get_config().default_timezone)
            counts = _upsert_ical_events(db, source, events, tz, run_stamp)

        _stage_last_stamp(db, source.id, run_stamp)
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
