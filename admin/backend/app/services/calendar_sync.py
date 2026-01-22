"""
Background calendar sync service.

This service runs in the background and periodically syncs all enabled
calendar sources based on their configured sync_interval_minutes.

The sync logic is imported from the calendar_sources routes module.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional
import structlog

from app.database import SessionLocal, DEV_MODE
from app.models import CalendarSource

logger = structlog.get_logger()

# Global reference to the background task (for graceful shutdown)
_sync_task: Optional[asyncio.Task] = None

# Minimum interval between sync cycles (to avoid hammering the database)
MIN_CHECK_INTERVAL_SECONDS = 60


async def sync_single_source(source_id: int, db_session) -> bool:
    """
    Sync a single calendar source.

    Returns True if sync was successful, False otherwise.
    """
    from app.routes.calendar_sources import (
        fetch_ical_data, parse_ical_events, fetch_lodgify_reservations,
        get_lodgify_api_key, sync_lodgify_to_guest_sessions,
        update_guest_session_statuses
    )
    from app.models import CalendarEvent

    try:
        source = db_session.query(CalendarSource).filter(
            CalendarSource.id == source_id
        ).first()

        if not source:
            logger.warning("calendar_source_not_found", source_id=source_id)
            return False

        events = []
        sync_method = 'ical'

        # For Lodgify sources, try API first for full guest details
        if source.source_type == 'lodgify':
            lodgify_api_key = get_lodgify_api_key(db_session)
            if lodgify_api_key:
                try:
                    checkin_time = source.default_checkin_time or '16:00'
                    checkout_time = source.default_checkout_time or '11:00'
                    events = await fetch_lodgify_reservations(
                        lodgify_api_key,
                        checkin_time=checkin_time,
                        checkout_time=checkout_time
                    )
                    sync_method = 'lodgify_api'
                    logger.info("background_lodgify_api_sync",
                               source_id=source_id,
                               event_count=len(events))
                except Exception as api_error:
                    logger.warning("background_lodgify_api_failed_fallback_to_ical",
                                  source_id=source_id,
                                  error=str(api_error))
                    ical_data = await fetch_ical_data(source.ical_url)
                    events = parse_ical_events(ical_data, source.source_type)
            else:
                ical_data = await fetch_ical_data(source.ical_url)
                events = parse_ical_events(ical_data, source.source_type)
        else:
            ical_data = await fetch_ical_data(source.ical_url)
            events = parse_ical_events(ical_data, source.source_type)

        added = 0
        updated = 0

        # Process each event
        for event_data in events:
            existing = db_session.query(CalendarEvent).filter(
                CalendarEvent.external_id == event_data['external_id']
            ).first()

            if existing:
                existing.title = event_data['title']
                existing.checkin = event_data['checkin']
                existing.checkout = event_data['checkout']
                existing.guest_name = event_data['guest_name']
                existing.guest_phone = event_data.get('guest_phone')
                existing.guest_email = event_data.get('guest_email')
                existing.notes = event_data['notes']
                existing.source = event_data['source']
                existing.source_id = source.id
                existing.synced_at = datetime.now(timezone.utc)
                updated += 1
            else:
                new_event = CalendarEvent(
                    external_id=event_data['external_id'],
                    title=event_data['title'],
                    checkin=event_data['checkin'],
                    checkout=event_data['checkout'],
                    guest_name=event_data['guest_name'],
                    guest_phone=event_data.get('guest_phone'),
                    guest_email=event_data.get('guest_email'),
                    notes=event_data['notes'],
                    source=event_data['source'],
                    source_id=source.id,
                    status=event_data.get('status', 'confirmed'),
                    created_by=f'{sync_method}_sync',
                    synced_at=datetime.now(timezone.utc)
                )
                db_session.add(new_event)
                added += 1

        # Update source status
        source.last_sync_at = datetime.now(timezone.utc)
        source.last_sync_status = 'success'
        source.last_sync_error = None
        source.last_event_count = len(events)

        db_session.commit()

        logger.info("background_calendar_sync_complete",
                   source_id=source_id,
                   source_name=source.name,
                   sync_method=sync_method,
                   events_total=len(events),
                   added=added,
                   updated=updated)

        # Auto-sync to guest sessions for Lodgify sources
        if source.source_type == 'lodgify':
            await sync_lodgify_to_guest_sessions(db_session)
            await update_guest_session_statuses(db_session)

        return True

    except Exception as e:
        logger.error("background_calendar_sync_failed",
                    source_id=source_id,
                    error=str(e))

        # Update source with error
        try:
            source = db_session.query(CalendarSource).filter(
                CalendarSource.id == source_id
            ).first()
            if source:
                source.last_sync_at = datetime.now(timezone.utc)
                source.last_sync_status = 'failed'
                source.last_sync_error = str(e)
                db_session.commit()
        except Exception as update_error:
            logger.error("failed_to_update_source_error_status",
                        source_id=source_id,
                        error=str(update_error))
            db_session.rollback()

        return False


async def check_and_sync_sources():
    """
    Check all enabled calendar sources and sync those that are due.

    A source is due for sync if:
    - It has never been synced (last_sync_at is None), OR
    - Time since last_sync_at >= sync_interval_minutes
    """
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)

        # Get all enabled sources
        sources = db.query(CalendarSource).filter(
            CalendarSource.enabled == True
        ).all()

        if not sources:
            logger.debug("no_enabled_calendar_sources")
            return

        synced_count = 0
        for source in sources:
            # Calculate if sync is due
            if source.last_sync_at is None:
                # Never synced - sync now
                needs_sync = True
                logger.info("source_never_synced",
                           source_id=source.id,
                           source_name=source.name)
            else:
                # Check if interval has passed
                # Handle timezone-naive datetime from database
                last_sync = source.last_sync_at
                if last_sync.tzinfo is None:
                    last_sync = last_sync.replace(tzinfo=timezone.utc)

                interval = timedelta(minutes=source.sync_interval_minutes)
                next_sync_due = last_sync + interval
                needs_sync = now >= next_sync_due

                if needs_sync:
                    logger.info("source_sync_due",
                               source_id=source.id,
                               source_name=source.name,
                               last_sync=last_sync.isoformat(),
                               interval_minutes=source.sync_interval_minutes)

            if needs_sync:
                # Use a fresh session for each sync to avoid transaction issues
                sync_db = SessionLocal()
                try:
                    success = await sync_single_source(source.id, sync_db)
                    if success:
                        synced_count += 1
                finally:
                    sync_db.close()

        if synced_count > 0:
            logger.info("background_sync_cycle_complete",
                       sources_synced=synced_count,
                       total_sources=len(sources))

    except Exception as e:
        logger.error("background_sync_check_failed", error=str(e))
    finally:
        db.close()


async def calendar_sync_loop():
    """
    Main background loop that periodically checks and syncs calendar sources.

    Runs continuously until the application shuts down.
    """
    logger.info("calendar_sync_background_task_started")

    # Initial delay to let the app fully start
    await asyncio.sleep(10)

    while True:
        try:
            await check_and_sync_sources()
        except asyncio.CancelledError:
            logger.info("calendar_sync_background_task_cancelled")
            break
        except Exception as e:
            logger.error("calendar_sync_loop_error", error=str(e))

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
