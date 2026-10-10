"""
Conversation transcript review, capture settings and purge endpoints.

Reads conversation data written by the orchestrator (the main database; see
app/database.py). The list, detail and stats routes keep their frozen
``get_current_user`` dependency (tests/route_auth_legacy.py); the text they
return is decided in the handler by ``_text_visible``: only a user holding
``view_conversation_text`` (owner) receives query/answer/error text, and each
such response is audited without the text. Everyone else gets metadata with
``text_visible: false``.

The capture routes use permission-bearing dependencies and are declared before
``GET /{conversation_id}`` (Starlette matches in registration order).

Capture is best-effort: turns in flight at a service restart may not be
recorded.
"""
import json
import re
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.auth.oidc import get_current_user
from app.database import get_analytics_db, get_db
from app.models import User
from app.services import conversation_capture as capture
from app.services import settings_lease
from app.utils.service_auth import require_user_permission
from shared.conversation_capture import DEBUG_DETAIL_KEYS

logger = structlog.get_logger()

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

SESSION_ID_SHOWN_CHARS = 12
MAX_LEGACY_PAGE_WINDOW = 10_000


# ============================================================================
# Permission helpers
# ============================================================================

def _require_read_conversations(user: User) -> None:
    """Enforce read:conversations scope or broad read permission."""
    if user.role in ("owner", "operator"):
        return
    # Check for explicit scopes via API key or user role
    if not user.has_permission("read"):
        raise HTTPException(status_code=403, detail="Insufficient permissions. Requires read:conversations scope.")


def _require_write_evaluations(user: User) -> None:
    """Enforce write:conversation_evaluations scope or broad write permission."""
    if user.role in ("owner", "operator"):
        return
    if not user.has_permission("write"):
        raise HTTPException(status_code=403, detail="Insufficient permissions. Requires write:conversation_evaluations scope.")


def _text_visible(user: User) -> bool:
    """The one decision for every handler that returns conversation text."""
    return user.has_permission(capture.VIEW_TEXT_PERMISSION)


def _pseudonym(session_id: Optional[str]) -> Optional[str]:
    """Session ids are shown truncated for every viewer (SMS ids are an HMAC pseudonym)."""
    return session_id[:SESSION_ID_SHOWN_CHARS] if session_id else session_id


_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


def _debug_detail(raw: Any, visible: bool) -> Optional[Dict[str, Any]]:
    """The debug block. Non-owners get an allowlist: known keys with scalar
    numbers and booleans only. Strings, nested values and unknown keys (a
    legacy row can hold anything) are dropped."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    if visible:
        return raw
    return {
        key: value for key, value in raw.items()
        if key in DEBUG_DETAIL_KEYS and (isinstance(value, (bool, int, float)) or value is None)
    }


def _tool_names(raw: Any, visible: bool) -> Any:
    """``rag_tools_used``: tool identifiers only for non-owners."""
    if visible:
        return raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, list):
        return None
    return [t for t in raw if isinstance(t, str) and _SAFE_LABEL.match(t)]


def _timings(raw: Any, visible: bool) -> Any:
    """``node_timings_ms``: identifier keys with numeric values only for non-owners."""
    if visible:
        return raw
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    return {k: v for k, v in raw.items()
            if isinstance(k, str) and _SAFE_LABEL.match(k) and isinstance(v, (int, float)) and not isinstance(v, bool)}


def _ts(value: Any) -> Optional[str]:
    """ISO text for a timestamp column (the driver returns text on SQLite)."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _error_detail(exc: Exception) -> str:
    return type(exc).__name__


# ============================================================================
# Request / Response models
# ============================================================================

class ConversationSummary(BaseModel):
    id: str
    session_id: str
    session_class: Optional[str] = None
    room: Optional[str]
    user_mode: Optional[str]
    interface_type: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    turn_count: int
    total_tokens: Optional[int]
    first_query: Optional[str] = None   # First 120 chars of turn 1; null unless text is visible

    model_config = ConfigDict(from_attributes=True)


class ConversationTurnOut(BaseModel):
    id: str
    turn_number: int
    request_id: Optional[str]
    query_text: Optional[str]           # null unless text is visible
    response_text: Optional[str]
    intent: Optional[str]
    confidence: Optional[float]
    model_used: Optional[str]
    rag_tools_used: Optional[Any]
    response_time_ms: Optional[int]
    tokens_generated: Optional[int]
    tokens_per_second: Optional[float]
    validation_passed: Optional[bool]
    error_message: Optional[str]
    source: str
    created_at: Optional[datetime]
    complexity: Optional[str] = None
    is_fallback: Optional[bool] = None
    base_knowledge_populated: Optional[bool] = None
    node_timings_ms: Optional[Any] = None
    route: Optional[str] = None
    channel: Optional[str] = None
    audience_class: Optional[str] = None
    outcome: Optional[str] = None
    text_withheld: bool = False
    debug_detail: Optional[Dict[str, Any]] = None
    evaluations: List[Dict[str, Any]] = []

    model_config = ConfigDict(from_attributes=True)


class ConversationDetail(BaseModel):
    id: str
    session_id: str
    session_class: Optional[str] = None
    room: Optional[str]
    user_mode: Optional[str]
    interface_type: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    turn_count: int
    total_tokens: Optional[int]
    text_visible: bool = False
    turns: List[ConversationTurnOut] = []

    model_config = ConfigDict(from_attributes=True)


class ConversationStats(BaseModel):
    total_conversations: int
    total_turns: int
    avg_turns_per_conversation: float
    by_source: Dict[str, int]
    by_room: Dict[str, int]
    by_intent: Dict[str, int]


class EvaluationRequest(BaseModel):
    rating: int = Field(..., ge=1, le=5, description="Rating between 1 and 5")
    notes: Optional[str] = Field(None, max_length=2000)


class CaptureSettingsIn(BaseModel):
    """Both fields are required; unknown fields are refused."""
    model_config = ConfigDict(extra="forbid")

    debug_enabled: bool
    retention_days: int = Field(..., ge=1, le=3650)


class PurgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["expired", "all"]
    confirm: Optional[str] = Field(None, max_length=100)


def get_optional_analytics_db():
    """The conversation database, or ``None`` where it isn't available (DEV_MODE).

    The capture settings routes use this so they keep working, with null turn
    counts, wherever conversation storage can't be read.
    """
    generator = get_analytics_db()
    try:
        session = next(generator)
    except HTTPException:
        yield None
        return
    try:
        yield session
    finally:
        generator.close()


# ============================================================================
# Helper: build pagination params
# ============================================================================

def _paginate(page: int, per_page: int):
    """Return (offset, limit) for a 1-based page."""
    per_page = min(per_page, 200)
    offset = (max(page, 1) - 1) * per_page
    return offset, per_page


def _parse_before(before: str):
    """``<started_at ISO>,<id>`` keyset cursor, or a 422."""
    started, _, row_id = before.rpartition(",")
    try:
        return datetime.fromisoformat(started), row_id
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid 'before' cursor")


# ============================================================================
# Routes
# ============================================================================

@router.get("/stats", response_model=ConversationStats)
async def get_conversation_stats(
    analytics_db: Session = Depends(get_analytics_db),
    current_user: User = Depends(get_current_user),
):
    """
    Aggregate statistics across all recorded conversations (counts only, no text).
    Requires read:conversations scope.
    """
    _require_read_conversations(current_user)

    try:
        total_convs = analytics_db.execute(
            text("SELECT COUNT(*) FROM conversations")
        ).scalar() or 0

        total_turns = analytics_db.execute(
            text("SELECT COUNT(*) FROM conversation_turns")
        ).scalar() or 0

        avg_turns = round(total_turns / total_convs, 2) if total_convs > 0 else 0.0

        # Breakdown by source (the capture level: analytics | debug)
        source_rows = analytics_db.execute(
            text("SELECT source, COUNT(*) FROM conversation_turns GROUP BY source")
        ).fetchall()
        by_source = {r[0]: r[1] for r in source_rows}

        # Breakdown by room (top 20)
        room_rows = analytics_db.execute(
            text("""
                SELECT room, COUNT(*) as cnt FROM conversations
                WHERE room IS NOT NULL
                GROUP BY room ORDER BY cnt DESC LIMIT 20
            """)
        ).fetchall()
        by_room = {r[0]: r[1] for r in room_rows}

        # Breakdown by intent (top 20)
        intent_rows = analytics_db.execute(
            text("""
                SELECT intent, COUNT(*) as cnt FROM conversation_turns
                WHERE intent IS NOT NULL
                GROUP BY intent ORDER BY cnt DESC LIMIT 20
            """)
        ).fetchall()
        by_intent = {r[0]: r[1] for r in intent_rows}

        return ConversationStats(
            total_conversations=total_convs,
            total_turns=total_turns,
            avg_turns_per_conversation=avg_turns,
            by_source=by_source,
            by_room=by_room,
            by_intent=by_intent,
        )

    except Exception as e:
        logger.error("conversation_stats_failed", error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail=f"Failed to load stats: {_error_detail(e)}")


@router.get("/capture", response_model=Dict[str, Any])
async def get_capture_status(
    preview_retention: Optional[int] = Query(None, ge=1, le=3650, description="Count the turns a retention of N days would delete"),
    db: Session = Depends(get_db),
    analytics_db: Optional[Session] = Depends(get_optional_analytics_db),
    current_user: User = Depends(require_user_permission("read")),
):
    """Capture state, retention and counts. Carries no conversation text."""
    status_payload = await run_in_threadpool(capture.capture_status, db, analytics_db)
    if preview_retention is not None:
        status_payload["preview_delete_turns"] = await run_in_threadpool(
            capture.preview_delete_turns, analytics_db, preview_retention)
    return status_payload


@router.put("/capture", response_model=Dict[str, Any])
async def put_capture_settings(
    body: CaptureSettingsIn,
    db: Session = Depends(get_db),
    analytics_db: Optional[Session] = Depends(get_optional_analytics_db),
    current_user: User = Depends(require_user_permission("write")),
):
    """Save the debug-detail flag and the retention period (both required).

    Shortening retention triggers deletion at the next purge, so it needs
    ``delete`` (owner), not just ``write``.
    """
    if capture.retention_is_lowering(db, body.retention_days) and not current_user.has_permission("delete"):
        capture.write_audit(db, current_user, "conversation_capture_settings_refused", "conversation_capture",
                            new={"reason": "lowering_retention_requires_delete", "retention_days": body.retention_days})
        raise HTTPException(status_code=403, detail="Only an owner can shorten retention.")
    change = capture.update_capture_settings(db, debug_enabled=body.debug_enabled, retention_days=body.retention_days)
    capture.write_audit(db, current_user, "conversation_capture_settings_updated", "conversation_capture",
                        old=change["old"], new=change["new"])
    return await run_in_threadpool(capture.capture_status, db, analytics_db)


@router.post("/purge", response_model=Dict[str, Any])
async def purge_conversations(
    body: PurgeRequest,
    db: Session = Depends(get_db),
    session_factory=Depends(capture.get_purge_session_factory),
    current_user: User = Depends(capture.require_audited_purge()),
):
    """Delete expired (or, with the typed confirmation, all) captured conversations."""
    if capture.DEV_MODE:
        raise HTTPException(status_code=503, detail="Conversation storage is not available in DEV_MODE.")

    def refuse(code: int, reason: str, detail: str):
        capture.write_audit(db, current_user, "conversation_purge_refused", "conversation",
                            new={"reason": reason, "scope": body.scope})
        return HTTPException(status_code=code, detail=detail)

    if body.scope == "all" and body.confirm != capture.PURGE_CONFIRM_PHRASE:
        raise refuse(400, "confirmation_missing", f"Type '{capture.PURGE_CONFIRM_PHRASE}' to confirm.")

    capture.write_audit(db, current_user, "conversation_purge_started", "conversation", new={"scope": body.scope})
    progress = capture.PurgeResult()
    final_action: Optional[str] = "conversation_purge_interrupted"
    try:
        await run_in_threadpool(capture.purge, session_factory, body.scope, progress=progress)
        final_action = "conversation_purged"
    except capture.RetentionInvalid:
        final_action = None
        raise refuse(409, "retention_invalid", "The stored retention is invalid; nothing was deleted. Save a retention period first.")
    except settings_lease.LeaseBusy:
        final_action = None
        raise refuse(409, "purge_in_progress", "A purge is already running.")
    except Exception as e:
        final_action = "conversation_purge_failed"
        logger.error("conversation_purge_failed", error_type=type(e).__name__, scope=body.scope)
        raise HTTPException(status_code=500, detail=f"Purge failed: {_error_detail(e)}")
    finally:
        # Always ends with a row: completed, failed, or interrupted (a client
        # disconnect cancels this handler; the counts are what had committed).
        if final_action is not None:
            capture.write_audit(db, current_user, final_action, "conversation",
                                new={"scope": body.scope, **progress.as_dict()})
    return progress.as_dict()


@router.get("", response_model=Dict[str, Any])
async def list_conversations(
    source: Optional[str] = Query(None, description="Filter by capture level: analytics, debug"),
    room: Optional[str] = Query(None, description="Filter by room name"),
    user_mode: Optional[str] = Query(None, description="Filter by user mode"),
    audience_class: Optional[str] = Query(None, description="Filter by conversation class: owner, guest, public, other"),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    intent: Optional[str] = Query(None, description="Filter by intent in any turn"),
    before: Optional[str] = Query(None, description="Keyset cursor '<started_at>,<id>' from a previous page"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    analytics_db: Session = Depends(get_analytics_db),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Paginated list of conversations; the first-query preview is text and is
    null unless the caller may read conversation text.
    Requires read:conversations scope.
    """
    _require_read_conversations(current_user)

    if page * min(per_page, 200) > MAX_LEGACY_PAGE_WINDOW:
        raise HTTPException(status_code=422, detail="Use the 'before' cursor to page past 10,000 conversations.")
    cursor = _parse_before(before) if before else None
    visible = _text_visible(current_user)

    try:
        offset, limit = _paginate(page, per_page)

        where_clauses = []
        params: Dict[str, Any] = {"limit": limit, "offset": offset}

        if source:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM conversation_turns t WHERE t.conversation_id = c.id AND t.source = :source)"
            )
            params["source"] = source

        if room:
            where_clauses.append("c.room ILIKE :room")
            params["room"] = f"%{room}%"

        if user_mode:
            where_clauses.append("c.user_mode = :user_mode")
            params["user_mode"] = user_mode

        if audience_class:
            where_clauses.append("c.session_class = :audience_class")
            params["audience_class"] = audience_class

        if from_date:
            where_clauses.append("c.started_at >= :from_date")
            params["from_date"] = from_date

        if to_date:
            where_clauses.append("c.started_at < :to_date")
            params["to_date"] = to_date

        if intent:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM conversation_turns t WHERE t.conversation_id = c.id AND t.intent ILIKE :intent)"
            )
            params["intent"] = f"%{intent}%"

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        count_params = {k: v for k, v in params.items() if k not in ("limit", "offset")}

        if cursor:
            where_clauses.append("(c.started_at, c.id) < (:before_started, :before_id)")
            params["before_started"], params["before_id"] = cursor
        page_where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        count_sql = f"SELECT COUNT(*) FROM conversations c {where_sql}"
        total = analytics_db.execute(text(count_sql), count_params).scalar() or 0

        list_sql = f"""
            SELECT
                c.id, c.session_id, c.room, c.user_mode, c.interface_type,
                c.started_at, c.ended_at, c.turn_count, c.total_tokens,
                (
                    SELECT SUBSTR(t.query_text, 1, 120)
                    FROM conversation_turns t
                    WHERE t.conversation_id = c.id AND t.turn_number = 1
                    LIMIT 1
                ) AS first_query,
                (
                    SELECT t.source
                    FROM conversation_turns t
                    WHERE t.conversation_id = c.id AND t.turn_number = 1
                    LIMIT 1
                ) AS source,
                c.session_class
            FROM conversations c
            {page_where_sql}
            ORDER BY c.started_at DESC, c.id DESC
            LIMIT :limit OFFSET :offset
        """
        rows = analytics_db.execute(text(list_sql), params).fetchall()

        items = []
        for row in rows:
            items.append({
                "id": str(row[0]),
                "session_id": _pseudonym(row[1]),
                "session_class": row[11],
                "room": row[2],
                "user_mode": row[3],
                "interface_type": row[4],
                "started_at": _ts(row[5]),
                "ended_at": _ts(row[6]),
                "turn_count": row[7],
                "total_tokens": row[8],
                "first_query": row[9] if visible else None,
                "source": row[10],
            })

        if visible:
            capture.audit_text_read(
                db, current_user, "GET /api/conversations",
                page=page, per_page=limit, returned_rows=len(items),
                source=source, room=room, user_mode=user_mode, audience_class=audience_class,
                intent=intent, from_date=str(from_date) if from_date else None,
                to_date=str(to_date) if to_date else None, keyset=bool(before),
            )

        next_before = None
        if rows and len(rows) == limit:
            last = rows[-1]
            next_before = f"{_ts(last[5])},{last[0]}" if last[5] else None

        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 0,
            "next_before": next_before,
            "text_visible": visible,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("conversation_list_failed", error_type=type(e).__name__)
        raise HTTPException(status_code=500, detail=f"Failed to load conversations: {_error_detail(e)}")


@router.get("/{conversation_id}", response_model=Dict[str, Any])
async def get_conversation(
    conversation_id: str,
    analytics_db: Session = Depends(get_analytics_db),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Full conversation thread with all turns and evaluations. Query, answer,
    error and debug-detail text are null unless the caller may read
    conversation text. Requires read:conversations scope.
    """
    _require_read_conversations(current_user)
    visible = _text_visible(current_user)

    try:
        conv_row = analytics_db.execute(
            text("""
                SELECT id, session_id, room, user_mode, interface_type,
                       started_at, ended_at, turn_count, total_tokens, created_at,
                       session_class
                FROM conversations WHERE id = :cid
            """),
            {"cid": conversation_id}
        ).fetchone()

        if not conv_row:
            raise HTTPException(status_code=404, detail="Conversation not found")

        turns_rows = analytics_db.execute(
            text("""
                SELECT id, turn_number, request_id, query_text, response_text,
                       intent, confidence, model_used, rag_tools_used,
                       response_time_ms, tokens_generated, tokens_per_second,
                       validation_passed, error_message, source, created_at,
                       complexity, is_fallback, base_knowledge_populated, node_timings_ms,
                       route, channel, audience_class, outcome, text_withheld, debug_detail
                FROM conversation_turns
                WHERE conversation_id = :cid
                ORDER BY turn_number ASC
            """),
            {"cid": conversation_id}
        ).fetchall()

        evaluations_by_turn: Dict[str, List[Dict[str, Any]]] = {}
        if turns_rows:
            eval_rows = analytics_db.execute(
                text("""
                    SELECT turn_id, id, rating, notes, evaluated_by_user_id, evaluated_by_username, created_at
                    FROM turn_evaluations WHERE turn_id IN :tids
                    ORDER BY created_at ASC
                """).bindparams(bindparam("tids", expanding=True)),
                {"tids": [str(tr[0]) for tr in turns_rows]}
            ).fetchall()
            for er in eval_rows:
                evaluations_by_turn.setdefault(str(er[0]), []).append({
                    "id": str(er[1]),
                    "rating": er[2],
                    "notes": er[3] if visible else None,
                    "evaluated_by_user_id": er[4],
                    "evaluated_by_username": er[5],
                    "created_at": _ts(er[6]),
                })

        turns = []
        for tr in turns_rows:
            turn_id = str(tr[0])
            turns.append({
                "id": turn_id,
                "turn_number": tr[1],
                "request_id": tr[2],
                "query_text": tr[3] if visible else None,
                "response_text": tr[4] if visible else None,
                "intent": tr[5],
                "confidence": float(tr[6]) if tr[6] is not None else None,
                "model_used": tr[7],
                "rag_tools_used": _tool_names(tr[8], visible),
                "response_time_ms": tr[9],
                "tokens_generated": tr[10],
                "tokens_per_second": float(tr[11]) if tr[11] is not None else None,
                "validation_passed": tr[12],
                "error_message": tr[13] if visible else None,
                "source": tr[14],
                "created_at": _ts(tr[15]),
                "complexity": tr[16],
                "is_fallback": tr[17],
                "base_knowledge_populated": tr[18],
                "node_timings_ms": _timings(tr[19], visible),
                "route": tr[20],
                "channel": tr[21],
                "audience_class": tr[22],
                "outcome": tr[23],
                "text_withheld": bool(tr[24]),
                "debug_detail": _debug_detail(tr[25], visible),
                "evaluations": evaluations_by_turn.get(turn_id, []),
            })

        if visible:
            capture.audit_text_read(db, current_user, "GET /api/conversations/{conversation_id}",
                                    conversation_id=str(conv_row[0]), turns=len(turns))

        return {
            "id": str(conv_row[0]),
            "session_id": _pseudonym(conv_row[1]),
            "session_class": conv_row[10],
            "room": conv_row[2],
            "user_mode": conv_row[3],
            "interface_type": conv_row[4],
            "started_at": _ts(conv_row[5]),
            "ended_at": _ts(conv_row[6]),
            "turn_count": conv_row[7],
            "total_tokens": conv_row[8],
            "created_at": _ts(conv_row[9]),
            "text_visible": visible,
            "turns": turns,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("conversation_detail_failed", error_type=type(e).__name__, conversation_id=conversation_id)
        raise HTTPException(status_code=500, detail=f"Failed to load conversation: {_error_detail(e)}")


@router.post("/turns/{turn_id}/evaluate", status_code=200)
async def evaluate_turn(
    turn_id: str,
    evaluation: EvaluationRequest,
    analytics_db: Session = Depends(get_analytics_db),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Submit or update a rating and notes for a conversation turn.
    Upserts — one evaluation per admin user per turn.
    Requires write:conversation_evaluations scope.
    """
    _require_write_evaluations(current_user)

    try:
        # Verify turn exists
        turn_exists = analytics_db.execute(
            text("SELECT id FROM conversation_turns WHERE id = :tid"),
            {"tid": turn_id}
        ).fetchone()

        if not turn_exists:
            raise HTTPException(status_code=404, detail="Turn not found")

        # Upsert: check if this user already evaluated this turn
        existing = analytics_db.execute(
            text("""
                SELECT id FROM turn_evaluations
                WHERE turn_id = :tid AND evaluated_by_user_id = :uid
            """),
            {"tid": turn_id, "uid": current_user.id}
        ).fetchone()

        if existing:
            analytics_db.execute(
                text("""
                    UPDATE turn_evaluations
                    SET rating = :rating, notes = :notes, evaluated_by_username = :username
                    WHERE id = :eid
                """),
                {
                    "rating": evaluation.rating,
                    "notes": evaluation.notes,
                    "username": current_user.username,
                    "eid": str(existing[0]),
                }
            )
            action = "updated"
        else:
            import uuid
            analytics_db.execute(
                text("""
                    INSERT INTO turn_evaluations
                        (id, turn_id, rating, notes, evaluated_by_user_id, evaluated_by_username, created_at)
                    VALUES (:id, :tid, :rating, :notes, :uid, :username, NOW())
                """),
                {
                    "id": str(uuid.uuid4()),
                    "tid": turn_id,
                    "rating": evaluation.rating,
                    "notes": evaluation.notes,
                    "uid": current_user.id,
                    "username": current_user.username,
                }
            )
            action = "created"

        analytics_db.commit()

        logger.info(
            "turn_evaluation_saved",
            turn_id=turn_id,
            user=current_user.username,
            rating=evaluation.rating,
            action=action,
        )

        return {"status": "success", "action": action}

    except HTTPException:
        raise
    except Exception as e:
        analytics_db.rollback()
        logger.error("turn_evaluation_failed", error_type=type(e).__name__, turn_id=turn_id)
        raise HTTPException(status_code=500, detail=f"Failed to save evaluation: {_error_detail(e)}")
