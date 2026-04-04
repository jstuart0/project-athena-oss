"""
Conversation transcript review and evaluation endpoints.

Reads conversation data from the 'athena' database (written by the orchestrator).
Requires read:conversations scope for GET endpoints and
write:conversation_evaluations scope for the evaluation POST endpoint.

Analytics capture is best-effort — turns in-flight at service restart may not
be recorded. See the analytics mode plan for full privacy policy.
"""
from typing import Optional, List, Dict, Any
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, text
import structlog

from app.database import get_db, get_analytics_db
from app.auth.oidc import get_current_user
from app.models import User, SystemSetting

logger = structlog.get_logger()

router = APIRouter(prefix="/api/conversations", tags=["conversations"])


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


# ============================================================================
# Request / Response models
# ============================================================================

class ConversationSummary(BaseModel):
    id: str
    session_id: str
    room: Optional[str]
    user_mode: Optional[str]
    interface_type: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    turn_count: int
    total_tokens: Optional[int]
    first_query: Optional[str] = None   # First 120 chars of turn 1

    class Config:
        from_attributes = True


class ConversationTurnOut(BaseModel):
    id: str
    turn_number: int
    request_id: Optional[str]
    query_text: str
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
    evaluations: List[Dict[str, Any]] = []

    class Config:
        from_attributes = True


class ConversationDetail(BaseModel):
    id: str
    session_id: str
    room: Optional[str]
    user_mode: Optional[str]
    interface_type: Optional[str]
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    turn_count: int
    total_tokens: Optional[int]
    turns: List[ConversationTurnOut] = []

    class Config:
        from_attributes = True


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


# ============================================================================
# Helper: build pagination params
# ============================================================================

def _paginate(page: int, per_page: int):
    """Return (offset, limit) for a 1-based page."""
    per_page = min(per_page, 200)
    offset = (max(page, 1) - 1) * per_page
    return offset, per_page


# ============================================================================
# Routes
# ============================================================================

@router.get("/stats", response_model=ConversationStats)
async def get_conversation_stats(
    analytics_db: Session = Depends(get_analytics_db),
    current_user: User = Depends(get_current_user),
):
    """
    Aggregate statistics across all recorded conversations.
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

        # Breakdown by source
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
        logger.error("conversation_stats_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to load stats: {str(e)}")


@router.get("", response_model=Dict[str, Any])
async def list_conversations(
    source: Optional[str] = Query(None, description="Filter by source: analytics, debug"),
    room: Optional[str] = Query(None, description="Filter by room name"),
    user_mode: Optional[str] = Query(None, description="Filter by user mode"),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
    intent: Optional[str] = Query(None, description="Filter by intent in any turn"),
    page: int = Query(1, ge=1),
    per_page: int = Query(25, ge=1, le=200),
    analytics_db: Session = Depends(get_analytics_db),
    current_user: User = Depends(get_current_user),
):
    """
    Paginated list of conversations with preview of first query.
    Requires read:conversations scope.
    """
    _require_read_conversations(current_user)

    try:
        offset, limit = _paginate(page, per_page)

        # Build base query
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

        # Count query
        count_sql = f"SELECT COUNT(*) FROM conversations c {where_sql}"
        total = analytics_db.execute(text(count_sql), params).scalar() or 0

        # List query with first query preview
        list_sql = f"""
            SELECT
                c.id, c.session_id, c.room, c.user_mode, c.interface_type,
                c.started_at, c.ended_at, c.turn_count, c.total_tokens,
                (
                    SELECT LEFT(t.query_text, 120)
                    FROM conversation_turns t
                    WHERE t.conversation_id = c.id AND t.turn_number = 1
                    LIMIT 1
                ) AS first_query
            FROM conversations c
            {where_sql}
            ORDER BY c.started_at DESC
            LIMIT :limit OFFSET :offset
        """
        rows = analytics_db.execute(text(list_sql), params).fetchall()

        items = []
        for row in rows:
            items.append({
                "id": str(row[0]),
                "session_id": row[1],
                "room": row[2],
                "user_mode": row[3],
                "interface_type": row[4],
                "started_at": row[5].isoformat() if row[5] else None,
                "ended_at": row[6].isoformat() if row[6] else None,
                "turn_count": row[7],
                "total_tokens": row[8],
                "first_query": row[9],
            })

        return {
            "items": items,
            "total": total,
            "page": page,
            "per_page": limit,
            "pages": (total + limit - 1) // limit if limit > 0 else 0,
        }

    except Exception as e:
        logger.error("conversation_list_failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Failed to load conversations: {str(e)}")


@router.get("/{conversation_id}", response_model=Dict[str, Any])
async def get_conversation(
    conversation_id: str,
    analytics_db: Session = Depends(get_analytics_db),
    current_user: User = Depends(get_current_user),
):
    """
    Full conversation thread with all turns and evaluations.
    Requires read:conversations scope.
    """
    _require_read_conversations(current_user)

    try:
        conv_row = analytics_db.execute(
            text("""
                SELECT id, session_id, room, user_mode, interface_type,
                       started_at, ended_at, turn_count, total_tokens, created_at
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
                       validation_passed, error_message, source, created_at
                FROM conversation_turns
                WHERE conversation_id = :cid
                ORDER BY turn_number ASC
            """),
            {"cid": conversation_id}
        ).fetchall()

        turns = []
        for tr in turns_rows:
            turn_id = str(tr[0])
            eval_rows = analytics_db.execute(
                text("""
                    SELECT id, rating, notes, evaluated_by_user_id, evaluated_by_username, created_at
                    FROM turn_evaluations WHERE turn_id = :tid
                    ORDER BY created_at ASC
                """),
                {"tid": turn_id}
            ).fetchall()

            evaluations = [
                {
                    "id": str(er[0]),
                    "rating": er[1],
                    "notes": er[2],
                    "evaluated_by_user_id": er[3],
                    "evaluated_by_username": er[4],
                    "created_at": er[5].isoformat() if er[5] else None,
                }
                for er in eval_rows
            ]

            turns.append({
                "id": turn_id,
                "turn_number": tr[1],
                "request_id": tr[2],
                "query_text": tr[3],
                "response_text": tr[4],
                "intent": tr[5],
                "confidence": float(tr[6]) if tr[6] is not None else None,
                "model_used": tr[7],
                "rag_tools_used": tr[8],
                "response_time_ms": tr[9],
                "tokens_generated": tr[10],
                "tokens_per_second": float(tr[11]) if tr[11] is not None else None,
                "validation_passed": tr[12],
                "error_message": tr[13],
                "source": tr[14],
                "created_at": tr[15].isoformat() if tr[15] else None,
                "evaluations": evaluations,
            })

        return {
            "id": str(conv_row[0]),
            "session_id": conv_row[1],
            "room": conv_row[2],
            "user_mode": conv_row[3],
            "interface_type": conv_row[4],
            "started_at": conv_row[5].isoformat() if conv_row[5] else None,
            "ended_at": conv_row[6].isoformat() if conv_row[6] else None,
            "turn_count": conv_row[7],
            "total_tokens": conv_row[8],
            "created_at": conv_row[9].isoformat() if conv_row[9] else None,
            "turns": turns,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("conversation_detail_failed", error=str(e), conversation_id=conversation_id)
        raise HTTPException(status_code=500, detail=f"Failed to load conversation: {str(e)}")


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
            # Update existing evaluation
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
            # Insert new evaluation
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
        logger.error("turn_evaluation_failed", error=str(e), turn_id=turn_id)
        raise HTTPException(status_code=500, detail=f"Failed to save evaluation: {str(e)}")
