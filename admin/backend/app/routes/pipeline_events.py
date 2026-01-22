"""
Pipeline Events API endpoints.

Provides REST API access to pipeline events for:
- Historical event querying
- Polling fallback when WebSocket unavailable
- Analytics and debugging
"""

from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Query, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import desc, func
import structlog

from app.database import get_db
from app.models import PipelineEvent

logger = structlog.get_logger()

router = APIRouter(prefix="/api/pipeline-events", tags=["pipeline-events"])


class PipelineEventResponse(BaseModel):
    """Pipeline event response model."""
    id: int
    event_type: str
    session_id: str
    interface: Optional[str] = None
    data: dict
    timestamp: float
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


@router.get("", response_model=List[PipelineEventResponse])
async def get_pipeline_events(
    since: Optional[float] = Query(None, description="Unix timestamp - only return events after this time"),
    until: Optional[float] = Query(None, description="Unix timestamp - only return events before this time"),
    session_id: Optional[str] = Query(None, description="Filter by session ID"),
    event_type: Optional[str] = Query(None, description="Filter by event type"),
    interface: Optional[str] = Query(None, description="Filter by interface (voice, chat, etc.)"),
    limit: int = Query(50, ge=1, le=500, description="Maximum events to return"),
    offset: int = Query(0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db)
):
    """
    Get pipeline events with optional filters.

    Used by:
    - Admin Jarvis UI polling fallback
    - Analytics dashboards
    - Debugging and troubleshooting
    """
    try:
        query = db.query(PipelineEvent)

        # Apply filters
        if since:
            since_dt = datetime.fromtimestamp(since)
            query = query.filter(PipelineEvent.timestamp > since_dt)

        if until:
            until_dt = datetime.fromtimestamp(until)
            query = query.filter(PipelineEvent.timestamp < until_dt)

        if session_id:
            query = query.filter(PipelineEvent.session_id == session_id)

        if event_type:
            query = query.filter(PipelineEvent.event_type == event_type)

        if interface:
            query = query.filter(PipelineEvent.interface == interface)

        # Order by timestamp descending, apply pagination
        events = query.order_by(desc(PipelineEvent.timestamp)).offset(offset).limit(limit).all()

        result = []
        for event in events:
            result.append(PipelineEventResponse(
                id=event.id,
                event_type=event.event_type,
                session_id=event.session_id,
                interface=event.interface,
                data=event.event_data or {},
                timestamp=event.timestamp.timestamp() if event.timestamp else 0,
                created_at=event.timestamp
            ))

        logger.debug("pipeline_events_fetched", count=len(result), filters={
            "since": since, "session_id": session_id, "event_type": event_type
        })

        return result

    except Exception as e:
        logger.error("pipeline_events_fetch_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to fetch events: {str(e)}")


@router.get("/sessions")
async def get_active_sessions(
    since_minutes: int = Query(5, ge=1, le=60, description="Look back N minutes for active sessions"),
    db: Session = Depends(get_db)
):
    """
    Get currently active pipeline sessions.

    A session is considered "active" if it has a session_start event
    but no session_end event within the lookback window.
    """
    try:
        since_dt = datetime.utcnow() - timedelta(minutes=since_minutes)

        # Get sessions that started in the window
        from sqlalchemy import and_, not_, exists

        # Subquery to find sessions that have ended
        ended_sessions = db.query(PipelineEvent.session_id).filter(
            PipelineEvent.event_type == 'session_end',
            PipelineEvent.timestamp > since_dt
        ).subquery()

        # Find active sessions (started but not ended)
        active = db.query(
            PipelineEvent.session_id,
            func.min(PipelineEvent.timestamp).label('start_time'),
            func.max(PipelineEvent.timestamp).label('last_event_time'),
            PipelineEvent.interface
        ).filter(
            PipelineEvent.timestamp > since_dt,
            PipelineEvent.event_type == 'session_start',
            ~PipelineEvent.session_id.in_(db.query(ended_sessions))
        ).group_by(
            PipelineEvent.session_id,
            PipelineEvent.interface
        ).order_by(desc('start_time')).limit(50).all()

        sessions = []
        for row in active:
            sessions.append({
                "session_id": row.session_id,
                "start_time": row.start_time.timestamp() if row.start_time else None,
                "last_event_time": row.last_event_time.timestamp() if row.last_event_time else None,
                "interface": row.interface,
                "status": "active"
            })

        return {"active_sessions": sessions, "count": len(sessions)}

    except Exception as e:
        logger.error("active_sessions_fetch_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to fetch sessions: {str(e)}")


@router.get("/stats")
async def get_event_stats(
    hours: int = Query(24, ge=1, le=168, description="Hours to look back for stats"),
    db: Session = Depends(get_db)
):
    """
    Get pipeline event statistics.

    Returns aggregated stats like:
    - Events by type
    - Events by interface
    - Average session duration
    """
    try:
        since_dt = datetime.utcnow() - timedelta(hours=hours)

        # Events by type
        type_counts = db.query(
            PipelineEvent.event_type,
            func.count(PipelineEvent.id)
        ).filter(
            PipelineEvent.timestamp > since_dt
        ).group_by(PipelineEvent.event_type).all()

        events_by_type = {row[0]: row[1] for row in type_counts}

        # Events by interface
        interface_counts = db.query(
            func.coalesce(PipelineEvent.interface, 'unknown'),
            func.count(PipelineEvent.id)
        ).filter(
            PipelineEvent.timestamp > since_dt
        ).group_by(PipelineEvent.interface).all()

        events_by_interface = {row[0]: row[1] for row in interface_counts}

        # Total events
        total_events = db.query(func.count(PipelineEvent.id)).filter(
            PipelineEvent.timestamp > since_dt
        ).scalar() or 0

        # Session count
        session_count = db.query(func.count(func.distinct(PipelineEvent.session_id))).filter(
            PipelineEvent.timestamp > since_dt,
            PipelineEvent.event_type == 'session_start'
        ).scalar() or 0

        return {
            "hours": hours,
            "total_events": total_events,
            "session_count": session_count,
            "events_by_type": events_by_type,
            "events_by_interface": events_by_interface
        }

    except Exception as e:
        logger.error("event_stats_fetch_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to fetch stats: {str(e)}")


@router.get("/{session_id}")
async def get_session_events(
    session_id: str,
    db: Session = Depends(get_db)
):
    """
    Get all events for a specific session.

    Returns events in chronological order with timing information.
    """
    try:
        events = db.query(PipelineEvent).filter(
            PipelineEvent.session_id == session_id
        ).order_by(PipelineEvent.timestamp).all()

        if not events:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        result = []
        start_time = None

        for event in events:
            if event.event_type == 'session_start':
                start_time = event.timestamp

            offset_ms = 0
            if start_time and event.timestamp:
                offset_ms = int((event.timestamp - start_time).total_seconds() * 1000)

            result.append({
                "id": event.id,
                "event_type": event.event_type,
                "interface": event.interface,
                "data": event.event_data or {},
                "timestamp": event.timestamp.timestamp() if event.timestamp else 0,
                "created_at": event.timestamp.isoformat() if event.timestamp else None,
                "offset_ms": offset_ms
            })

        # Calculate total duration if session ended
        duration_ms = None
        if result and result[-1]["event_type"] == "session_end":
            duration_ms = result[-1]["offset_ms"]

        return {
            "session_id": session_id,
            "event_count": len(result),
            "duration_ms": duration_ms,
            "events": result
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("session_events_fetch_error", error=str(e), session_id=session_id)
        raise HTTPException(status_code=500, detail=f"Failed to fetch session events: {str(e)}")


@router.post("/emit")
async def emit_pipeline_event(
    event_type: str,
    session_id: str,
    data: dict = None,
    interface: str = None,
    db: Session = Depends(get_db)
):
    """
    Emit a pipeline event (for testing or external integrations).

    This endpoint allows services to emit pipeline events that will be:
    - Stored in the database
    - Broadcast to WebSocket clients
    """
    try:
        from app.routes.websocket import broadcast_to_admin_jarvis

        event = PipelineEvent(
            event_type=event_type,
            session_id=session_id,
            event_data=data or {},
            interface=interface,
            timestamp=datetime.utcnow()
        )

        db.add(event)
        db.commit()
        db.refresh(event)

        # Broadcast to WebSocket clients
        await broadcast_to_admin_jarvis({
            "event_type": event_type,
            "session_id": session_id,
            "data": data or {},
            "interface": interface,
            "timestamp": event.timestamp.timestamp()
        })

        logger.info("pipeline_event_emitted", event_type=event_type, session_id=session_id)

        return {"status": "ok", "event_id": event.id}

    except Exception as e:
        logger.error("pipeline_event_emit_error", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to emit event: {str(e)}")
