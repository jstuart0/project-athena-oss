"""
Hierarchical Memory Management API Routes.

Provides endpoints for managing scoped memories with Qdrant vector integration.
Supports three memory scopes: global, owner, and guest (session-scoped).

IMPORTANT: Route Ordering
    FastAPI matches routes in definition order. Static routes (like /config,
    /guest-sessions) MUST be defined BEFORE dynamic routes (like /{memory_id})
    to prevent the dynamic route from catching everything.
"""
import re
import uuid
import json
import asyncio
import functools
from dataclasses import dataclass
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import false, func as sql_func, or_
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Memory, GuestSession, MemoryConfig, Feature
from app.routes.internal import require_service_key_401
from app.services import memory_vectors
from app.services.memory_vectors import EmbeddingUnavailable, VectorStoreNotReady
from app.utils.service_auth import verify_service_or_oidc

logger = structlog.get_logger()

router = APIRouter(prefix="/api/memories", tags=["memories"])

async def get_config_value(db: Session, key: str, default=None):
    """Get a configuration value from memory_config table."""
    config = db.query(MemoryConfig).filter(MemoryConfig.key == key).first()
    if config and config.value is not None:
        # Handle JSON-encoded values
        val = config.value
        if isinstance(val, str):
            try:
                return json.loads(val)
            except:
                return val
        return val
    return default


# =============================================================================
# Hybrid Search Helpers
# =============================================================================

def is_hybrid_search_enabled(db: Session) -> bool:
    """Check if hybrid_memory_search feature flag is enabled."""
    try:
        feature = db.query(Feature).filter(Feature.name == 'hybrid_memory_search').first()
        return feature.enabled if feature else False
    except Exception as e:
        logger.warning("hybrid_search_flag_check_failed", error=str(e))
        return False


def get_hybrid_search_config(db: Session) -> Dict[str, Any]:
    """Get hybrid search configuration from feature flag."""
    try:
        feature = db.query(Feature).filter(Feature.name == 'hybrid_memory_search').first()
        if feature and feature.config:
            return feature.config
    except Exception:
        pass
    # Defaults
    return {
        "keyword_weight": 0.3,
        "semantic_weight": 0.7,
        "min_keyword_score": 0.5
    }


def extract_keywords(query: str) -> List[str]:
    """
    Extract meaningful keywords from a query for keyword-based search.
    Removes common stop words and short words.
    """
    stop_words = {
        'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'must', 'can', 'this', 'that', 'these',
        'those', 'i', 'you', 'he', 'she', 'it', 'we', 'they', 'what', 'which',
        'who', 'whom', 'where', 'when', 'why', 'how', 'all', 'each', 'every',
        'both', 'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor',
        'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very', 'just',
        'and', 'but', 'if', 'or', 'because', 'as', 'until', 'while', 'of',
        'at', 'by', 'for', 'with', 'about', 'against', 'between', 'into',
        'through', 'during', 'before', 'after', 'above', 'below', 'to', 'from',
        'up', 'down', 'in', 'out', 'on', 'off', 'over', 'under', 'again',
        'further', 'then', 'once', 'here', 'there', 'any', 'many', 'my', 'me',
        'last', 'week', 'month', 'year', 'today', 'yesterday', 'tomorrow'
    }

    # Lowercase and extract words
    words = re.findall(r'\b[a-zA-Z]+\b', query.lower())

    # Filter: remove stop words and short words (< 3 chars)
    keywords = [w for w in words if w not in stop_words and len(w) >= 3]

    # Simple stemming: truncate longer words to 4 chars for prefix matching
    # This helps "drive", "driving", "drove" all become "driv"
    stems = []
    for kw in keywords:
        if len(kw) > 4:
            stem = kw[:4]
        else:
            stem = kw
        if stem not in stems:  # Avoid duplicates
            stems.append(stem)

    return stems


# =============================================================================
# Scope rules (one place decides what a caller may read, delete and create)
# =============================================================================

@dataclass(frozen=True)
class MemoryScopes:
    """What one caller may touch. ``"guest"`` in a tuple means the guest
    memories of ``guest_session_id`` only, never other guests'."""
    readable: Tuple[str, ...]
    deletable: Tuple[str, ...]
    create_scope: Optional[str]
    guest_session_id: Optional[int]


def _memory_scopes(mode: Optional[str], guest_session_id: Optional[int]) -> MemoryScopes:
    """Owner: read and delete global + owner, create owner. Any other mode
    is a guest (fail closed): with a session, read global + that session's
    guest memories, delete only those, create guest; without one, read
    global only, delete nothing, create nothing."""
    if mode == "owner":
        return MemoryScopes(("global", "owner"), ("global", "owner"), "owner", None)
    if guest_session_id:
        return MemoryScopes(("global", "guest"), ("guest",), "guest", guest_session_id)
    return MemoryScopes(("global",), (), None, None)


def _scope_sql_filter(scope_names: Tuple[str, ...], guest_session_id: Optional[int]):
    """SQL rendering of a scope set; an empty set matches nothing."""
    clauses = []
    for name in scope_names:
        if name == "guest":
            clauses.append((Memory.scope == "guest") & (Memory.guest_session_id == guest_session_id))
        else:
            clauses.append(Memory.scope == name)
    return or_(*clauses) if clauses else false()


MEMORY_READER_ROLES = frozenset({"owner", "operator"})


async def require_memory_reader(
    request: Request,
    db: Session = Depends(get_db),
    x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
) -> None:
    """Service key, or a signed-in user allowed to read household memories.

    verify_service_or_oidc authenticates (401 on neither). On the user
    branch the user also needs the 'read' permission and an owner/operator
    role: memories of every scope are household data.
    """
    await verify_service_or_oidc(request, db, x_service_key)
    _memory_caller_kind(
        request, lambda user: user.has_permission("read") and user.role in MEMORY_READER_ROLES,
    )


async def require_memory_maintainer(
    request: Request,
    db: Session = Depends(get_db),
    x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
) -> str:
    """Who may rebuild vectors: the service key (returns "service"; the
    route limits it to mode=missing without prune) or a signed-in user with
    manage_infrastructure (returns "user"). Any other user gets 403."""
    await verify_service_or_oidc(request, db, x_service_key)
    return _memory_caller_kind(request, lambda user: user.has_permission("manage_infrastructure"))


def _memory_caller_kind(request: Request, allow) -> str:
    """After verify_service_or_oidc: "service" for the service-key branch,
    "user" for a user ``allow`` accepts, else 403. Uses the user that
    verify_service_or_oidc authenticated (never a second resolution)."""
    if getattr(request.state, "auth_kind", None) == "service":
        return "service"
    user = getattr(request.state, "auth_user", None)
    if user is None or not allow(user):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return "user"


def _row_in_scopes(memory: Memory, scope_names: Tuple[str, ...], guest_session_id: Optional[int]) -> bool:
    if memory.scope == "guest":
        return "guest" in scope_names and memory.guest_session_id == guest_session_id
    return memory.scope in scope_names


# States in which a semantic query may be attempted (an embedder failure is
# retried: a later successful embed clears it).
_SEMANTIC_USABLE = (memory_vectors.READY, memory_vectors.EMBEDDER_UNAVAILABLE)


def _servable_rows_by_vector_id(db: Session, vector_ids: List[str]) -> Dict[str, Memory]:
    """Rows a semantic hit may be served from: live and with a stored vector."""
    if not vector_ids:
        return {}
    rows = db.query(Memory).filter(
        Memory.vector_id.in_(vector_ids),
        Memory.is_deleted == False,
        Memory.vector_status == "stored",
    ).all()
    return {row.vector_id: row for row in rows}


async def _semantic_search(
    db: Session,
    query: str,
    scope_names: Tuple[str, ...],
    guest_session_id: Optional[int],
    limit: int,
    min_score: float,
) -> Tuple[Optional[List[Tuple[Memory, float]]], str]:
    """Nearest live, stored rows in ``scope_names``, best first, at most
    ``limit``. Results are built from Postgres rows; the vector store only
    supplies ids and scores. Returns ``(None, reason)`` when semantic search
    can't run."""
    state = await run_in_threadpool(memory_vectors.get_state)
    if state.status not in _SEMANTIC_USABLE:
        return None, state.detail or state.status
    try:
        vector = (await run_in_threadpool(memory_vectors.embed, [query]))[0]
    except EmbeddingUnavailable as exc:
        return None, str(exc) or memory_vectors.EMBEDDER_UNAVAILABLE
    fetch = min(limit * memory_vectors.SEARCH_OVERFETCH, memory_vectors.SEARCH_FETCH_CAP)
    try:
        hits = await run_in_threadpool(functools.partial(
            memory_vectors.query, vector, readable_scopes=scope_names,
            guest_session_id=guest_session_id, limit=fetch, score_threshold=min_score,
        ))
    except VectorStoreNotReady as exc:
        return None, exc.state.detail or exc.state.status
    rows = _servable_rows_by_vector_id(db, [point_id for point_id, _ in hits])
    kept: List[Tuple[Memory, float]] = []
    for point_id, score in hits:
        row = rows.get(point_id)
        if row is None or not _row_in_scopes(row, scope_names, guest_session_id):
            continue
        kept.append((row, score))
        if len(kept) >= limit:
            break
    return kept, ""


def _record_access(db: Session, rows: List[Memory]) -> None:
    now = datetime.utcnow()
    for row in rows:
        row.access_count += 1
        row.last_accessed_at = now
    db.commit()


async def keyword_search_memories(
    db: Session,
    keywords: List[str],
    mode: str = "guest",
    guest_session_id: Optional[int] = None,
    limit: int = 5
) -> List[Dict[str, Any]]:
    """
    Perform keyword-based search on memory content using PostgreSQL ILIKE.
    Returns memories that contain any of the keywords.
    """
    if not keywords:
        return []

    try:
        scopes = _memory_scopes(mode, guest_session_id)
        scope_filter = _scope_sql_filter(scopes.readable, scopes.guest_session_id)

        # Build keyword filter (any keyword matches)
        keyword_filters = [Memory.content.ilike(f"%{kw}%") for kw in keywords]
        keyword_filter = or_(*keyword_filters) if keyword_filters else True

        # Query with scoring based on number of keyword matches
        memories = db.query(Memory).filter(
            scope_filter,
            keyword_filter,
            Memory.is_deleted == False
        ).limit(limit * 2).all()  # Fetch more, then score and filter

        # Score each memory based on keyword matches
        results = []
        for mem in memories:
            content_lower = mem.content.lower()
            matches = sum(1 for kw in keywords if kw in content_lower)
            score = matches / len(keywords) if keywords else 0

            results.append({
                "content": mem.content,
                "scope": mem.scope,
                "score": score,
                "id": mem.id,
                "source": "keyword"
            })

        # Sort by score descending and limit
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    except Exception as e:
        logger.warning("keyword_search_failed", error=str(e))
        return []


def merge_search_results(
    semantic_results: List[Dict[str, Any]],
    keyword_results: List[Dict[str, Any]],
    semantic_weight: float = 0.7,
    keyword_weight: float = 0.3,
    min_keyword_score: float = 0.5
) -> List[Dict[str, Any]]:
    """
    Merge semantic and keyword search results with weighted scoring.
    Deduplicates by content and combines scores.
    """
    # Index keyword results by content for lookup
    keyword_by_content = {r["content"]: r for r in keyword_results if r["score"] >= min_keyword_score}

    merged = {}

    # Add semantic results with weighting
    for r in semantic_results:
        content = r["content"]
        semantic_score = r["score"] * semantic_weight

        # Check if also found via keyword search
        keyword_score = 0
        if content in keyword_by_content:
            keyword_score = keyword_by_content[content]["score"] * keyword_weight
            del keyword_by_content[content]  # Mark as processed

        merged[content] = {
            "content": content,
            "scope": r["scope"],
            "score": semantic_score + keyword_score,
            "sources": ["semantic"] + (["keyword"] if keyword_score > 0 else [])
        }

    # Add remaining keyword-only results
    for content, r in keyword_by_content.items():
        if content not in merged:
            merged[content] = {
                "content": content,
                "scope": r["scope"],
                "score": r["score"] * keyword_weight,
                "sources": ["keyword"]
            }

    # Sort by combined score
    result_list = list(merged.values())
    result_list.sort(key=lambda x: x["score"], reverse=True)

    return result_list


# =============================================================================
# Pydantic Models
# =============================================================================

# Memory text is capped so one request can't hand the embedder an unbounded
# input (the embedder truncates further, to EMBED_MAX_CHARS); the summary cap
# is the column width.
MEMORY_CONTENT_MAX_CHARS = 8192
MEMORY_SUMMARY_MAX_CHARS = 255


class MemoryCreate(BaseModel):
    """Schema for creating a new memory."""
    content: str = Field(..., max_length=MEMORY_CONTENT_MAX_CHARS)
    summary: Optional[str] = Field(None, max_length=MEMORY_SUMMARY_MAX_CHARS)
    scope: str  # 'global', 'owner', 'guest'
    guest_session_id: Optional[int] = None
    category: Optional[str] = None
    importance: float = Field(default=0.5, ge=0, le=1)
    source_type: str = "manual"
    source_query: Optional[str] = None


class MemoryUpdate(BaseModel):
    """Schema for updating a memory."""
    content: Optional[str] = Field(None, max_length=MEMORY_CONTENT_MAX_CHARS)
    summary: Optional[str] = Field(None, max_length=MEMORY_SUMMARY_MAX_CHARS)
    category: Optional[str] = None
    importance: Optional[float] = Field(default=None, ge=0, le=1)


class MemoryResponse(BaseModel):
    """Schema for memory response."""
    id: int
    content: str
    summary: Optional[str]
    scope: str
    guest_session_id: Optional[int]
    category: Optional[str]
    importance: float
    access_count: int
    created_at: Optional[str]
    expires_at: Optional[str]

    class Config:
        from_attributes = True


class MemorySearchRequest(BaseModel):
    """Schema for memory search."""
    query: str
    mode: str  # 'guest' or 'owner'
    guest_session_id: Optional[int] = None
    limit: int = Field(default=5, le=20)
    min_score: float = Field(default=0.6, ge=0, le=1)


class PromoteRequest(BaseModel):
    """Schema for promoting a memory to a higher scope."""
    target_scope: str  # 'owner' or 'global'


class GuestSessionCreate(BaseModel):
    """Schema for creating a guest session."""
    calendar_event_id: Optional[int] = None
    lodgify_booking_id: Optional[str] = None
    guest_name: Optional[str] = None
    guest_email: Optional[str] = None
    check_in_date: date
    check_out_date: date


class GuestSessionResponse(BaseModel):
    """Schema for guest session response."""
    id: int
    lodgify_booking_id: Optional[str]
    guest_name: Optional[str]
    check_in_date: str
    check_out_date: str
    status: str
    memory_count: int = 0

    class Config:
        from_attributes = True


class ConfigUpdate(BaseModel):
    """Schema for updating a config value."""
    value: str | int | float | bool


# =============================================================================
# Memory Collection Endpoints (no path parameters)
# =============================================================================

@router.post("", status_code=201)
async def create_memory(
    memory: MemoryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Create a new memory with automatic embedding generation.

    - Guest memories require guest_session_id
    - Guest memories auto-expire based on checkout + retention period
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Validate scope
    if memory.scope not in ('global', 'owner', 'guest'):
        raise HTTPException(status_code=400, detail="Invalid scope. Must be 'global', 'owner', or 'guest'")

    # Validate guest scope requirements
    if memory.scope == "guest" and not memory.guest_session_id:
        raise HTTPException(status_code=400, detail="Guest memories require guest_session_id")

    if memory.scope != "guest" and memory.guest_session_id:
        raise HTTPException(status_code=400, detail="Only guest scope can have guest_session_id")

    # Calculate expiration for guest memories
    expires_at = None
    if memory.scope == "guest":
        session = db.query(GuestSession).filter(GuestSession.id == memory.guest_session_id).first()
        if not session:
            raise HTTPException(status_code=404, detail="Guest session not found")

        retention_days = await get_config_value(db, "guest_retention_days", 7)
        expires_at = datetime.combine(
            session.check_out_date,
            datetime.min.time()
        ) + timedelta(days=int(retention_days))

    # Check memory limits
    max_key = f"{memory.scope}_max_memories"
    max_memories = await get_config_value(db, max_key, 10000)

    if memory.scope == "guest" and memory.guest_session_id:
        count = db.query(Memory).filter(
            Memory.scope == 'guest',
            Memory.guest_session_id == memory.guest_session_id,
            Memory.is_deleted == False
        ).count()
    else:
        count = db.query(Memory).filter(
            Memory.scope == memory.scope,
            Memory.is_deleted == False
        ).count()

    if count >= int(max_memories):
        raise HTTPException(
            status_code=400,
            detail=f"Memory limit reached for {memory.scope} scope ({max_memories})"
        )

    # Postgres first: the row commits pending, then its vector is written and
    # the row marked stored. A vector-store failure leaves it pending.
    new_memory = Memory(
        content=memory.content,
        summary=memory.summary,
        scope=memory.scope,
        guest_session_id=memory.guest_session_id,
        vector_id=str(uuid.uuid4()),
        vector_status="pending",
        category=memory.category,
        importance=memory.importance,
        source_type=memory.source_type,
        source_query=memory.source_query,
        expires_at=expires_at
    )

    db.add(new_memory)
    db.commit()
    db.refresh(new_memory)
    await run_in_threadpool(memory_vectors.store_vector, new_memory)
    db.commit()
    db.refresh(new_memory)

    logger.info("memory_created",
               user=current_user.username,
               memory_id=new_memory.id,
               scope=memory.scope)

    return new_memory.to_dict()


@router.get("", dependencies=[Depends(require_memory_reader)])
async def list_memories(
    scope: Optional[str] = Query(None, description="Filter by scope (global/owner/guest)"),
    guest_session_id: Optional[int] = Query(None, description="Filter by guest session"),
    category: Optional[str] = Query(None, description="Filter by category"),
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """
    List memories with optional filtering.

    Requires X-Service-Key or a signed-in user.
    """
    query = db.query(Memory).filter(Memory.is_deleted == False)

    if scope:
        query = query.filter(Memory.scope == scope)
    if guest_session_id:
        query = query.filter(Memory.guest_session_id == guest_session_id)
    if category:
        query = query.filter(Memory.category == category)

    # Order by importance and created_at
    query = query.order_by(Memory.importance.desc(), Memory.created_at.desc())

    total = query.count()
    memories = query.offset(offset).limit(limit).all()

    # Get counts by scope
    counts = {}
    for s in ['global', 'owner', 'guest']:
        counts[s] = db.query(Memory).filter(
            Memory.scope == s,
            Memory.is_deleted == False
        ).count()

    return {
        "memories": [m.to_dict() for m in memories],
        "total": total,
        "counts": counts
    }


# =============================================================================
# Static Routes (MUST be defined BEFORE dynamic /{memory_id} routes)
# =============================================================================

@router.post("/search", dependencies=[Depends(require_memory_reader)])
async def search_memories(
    request: MemorySearchRequest,
    db: Session = Depends(get_db)
):
    """
    Scoped semantic search over memories.

    - Owner: global + owner memories
    - Guest with a session: global + that session's guest memories
    - Anything else (guest without a session, unknown mode): global only

    Results are the live rows behind the nearest vectors. When semantic
    search can't run, the response says so (semantic_available false).

    Requires X-Service-Key or a signed-in user.
    """
    scopes = _memory_scopes(request.mode, request.guest_session_id)
    kept, reason = await _semantic_search(
        db, request.query, scopes.readable, scopes.guest_session_id, request.limit, request.min_score,
    )
    if kept is None:
        return {"results": [], "qdrant_available": False, "semantic_available": False, "error": reason}

    _record_access(db, [row for row, _ in kept])
    return {
        "results": [
            {
                "id": row.id,
                "content": row.content,
                "summary": row.summary or "",
                "scope": row.scope,
                "score": score,
                "category": row.category,
            }
            for row, score in kept
        ],
        "query": request.query,
        "mode": request.mode,
        "qdrant_available": True,
        "semantic_available": True,
    }


# =============================================================================
# Guest Session Static Routes (before /{memory_id})
# =============================================================================

@router.get("/guest-sessions")
async def list_guest_sessions(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(default=20, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """List guest sessions with memory counts. Requires authentication."""
    query = db.query(GuestSession)

    if status:
        query = query.filter(GuestSession.status == status)

    query = query.order_by(GuestSession.check_in_date.desc())
    sessions = query.limit(limit).all()

    # Get memory counts for each session
    result = []
    for session in sessions:
        memory_count = db.query(Memory).filter(
            Memory.guest_session_id == session.id,
            Memory.is_deleted == False
        ).count()

        session_dict = session.to_dict()
        session_dict['memory_count'] = memory_count
        result.append(session_dict)

    return {"sessions": result}


@router.post("/guest-sessions", status_code=201)
async def create_guest_session(
    session: GuestSessionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Create a new guest session (usually from Lodgify sync)."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Determine initial status
    today = date.today()
    if session.check_in_date <= today <= session.check_out_date:
        status = "active"
    elif session.check_in_date > today:
        status = "upcoming"
    else:
        status = "completed"

    new_session = GuestSession(
        calendar_event_id=session.calendar_event_id,
        lodgify_booking_id=session.lodgify_booking_id,
        guest_name=session.guest_name,
        guest_email=session.guest_email,
        check_in_date=session.check_in_date,
        check_out_date=session.check_out_date,
        status=status
    )

    db.add(new_session)
    db.commit()
    db.refresh(new_session)

    logger.info("guest_session_created",
               user=current_user.username,
               session_id=new_session.id)

    return new_session.to_dict()


@router.get("/guest-sessions/active", dependencies=[Depends(require_memory_reader)])
async def get_active_guest_session(db: Session = Depends(get_db)):
    """Get the currently active guest session (if any)."""
    session = db.query(GuestSession).filter(GuestSession.status == 'active').first()

    if session:
        memory_count = db.query(Memory).filter(
            Memory.guest_session_id == session.id,
            Memory.is_deleted == False
        ).count()

        result = session.to_dict()
        result['memory_count'] = memory_count
        return result

    return None


# =============================================================================
# Configuration Static Routes (before /{memory_id})
# =============================================================================

@router.get("/config")
async def get_memory_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get all memory configuration settings. Requires authentication."""
    configs = db.query(MemoryConfig).order_by(MemoryConfig.key).all()

    return {
        "config": {c.key: c.value for c in configs}
    }


@router.post("/config/seed-defaults")
async def seed_default_config(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Seed default configuration values."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    defaults = [
        ("guest_retention_days", 7, "Days to retain guest memories after checkout"),
        ("owner_max_memories", 10000, "Maximum memories in owner scope"),
        ("guest_max_memories", 500, "Maximum memories per guest session"),
        ("global_max_memories", 1000, "Maximum memories in global scope"),
        ("auto_create_memories", True, "Automatically create memories from conversations"),
        ("memory_importance_threshold", 0.6, "Minimum importance to auto-create memory"),
        ("search_result_limit", 5, "Default number of memories to retrieve"),
        ("similarity_threshold", 0.35, "Minimum similarity score for retrieval"),
    ]

    created = 0
    for key, value, description in defaults:
        existing = db.query(MemoryConfig).filter(MemoryConfig.key == key).first()
        if not existing:
            config = MemoryConfig(key=key, value=value, description=description)
            db.add(config)
            created += 1

    db.commit()

    logger.info("memory_config_seeded", created=created)

    return {"success": True, "created": created}


# =============================================================================
# Internal API Static Routes (before /{memory_id})
# =============================================================================

@router.get("/internal/search", dependencies=[Depends(require_service_key_401)])
async def internal_memory_search(
    query: str,
    mode: str = "guest",
    guest_session_id: Optional[int] = None,
    limit: int = Query(default=3, le=10),
    db: Session = Depends(get_db)
):
    """
    Internal endpoint for orchestrator memory retrieval.

    Results stay usable through a vector-store outage: ``qdrant_available``
    means "these results are usable" and ``semantic_available`` says whether
    semantic search contributed.

    - hybrid_memory_search on: keyword (PostgreSQL) plus semantic results,
      merged; semantic is skipped when it can't run.
    - off: semantic results, or keyword results (search_type
      "keyword_fallback") when semantic search can't run.
    """
    try:
        hybrid_enabled = is_hybrid_search_enabled(db)
        threshold = float(await get_config_value(db, "similarity_threshold", 0.35))
        scopes = _memory_scopes(mode, guest_session_id)

        if hybrid_enabled:
            config = get_hybrid_search_config(db)
            keywords = extract_keywords(query)

            logger.info(
                "hybrid_search_starting",
                query_preview=query[:50],
                keywords=keywords,
                mode=mode
            )

            (kept, _), keyword_results = await asyncio.gather(
                _semantic_search(db, query, scopes.readable, scopes.guest_session_id, limit, threshold),
                keyword_search_memories(db, keywords, mode, guest_session_id, limit),
            )
            semantic_results = [
                {"content": row.content, "scope": row.scope, "score": score} for row, score in kept or []
            ]
            if kept:
                _record_access(db, [row for row, _ in kept])

            merged = merge_search_results(
                semantic_results,
                keyword_results,
                semantic_weight=config.get("semantic_weight", 0.7),
                keyword_weight=config.get("keyword_weight", 0.3),
                min_keyword_score=config.get("min_keyword_score", 0.5)
            )

            logger.info(
                "hybrid_search_completed",
                semantic_count=len(semantic_results),
                keyword_count=len(keyword_results),
                merged_count=len(merged)
            )

            return {
                "results": merged[:limit],
                "qdrant_available": True,
                "semantic_available": kept is not None,
                "search_type": "hybrid"
            }

        kept, reason = await _semantic_search(db, query, scopes.readable, scopes.guest_session_id, limit, threshold)
        if kept is not None:
            _record_access(db, [row for row, _ in kept])
            return {
                "results": [{"content": row.content, "scope": row.scope, "score": score} for row, score in kept],
                "qdrant_available": True,
                "semantic_available": True,
                "search_type": "semantic"
            }

        logger.info("memory_search_keyword_fallback", reason=reason)
        keyword_results = await keyword_search_memories(db, extract_keywords(query), mode, guest_session_id, limit)
        return {
            "results": [{"content": r["content"], "scope": r["scope"], "score": r["score"]} for r in keyword_results],
            "qdrant_available": True,
            "semantic_available": False,
            "search_type": "keyword_fallback"
        }

    except Exception as e:
        logger.error("internal_search_failed", error=str(e))
        return {"results": [], "qdrant_available": False, "semantic_available": False}


@router.post("/internal/create", dependencies=[Depends(require_service_key_401)])
async def internal_create_memory(
    content: str = Query(..., max_length=MEMORY_CONTENT_MAX_CHARS),
    mode: str = "guest",
    guest_session_id: Optional[int] = None,
    category: str = "conversation",
    importance: float = 0.5,
    source_query: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Internal endpoint for orchestrator to create memories.
    Auto-determines scope based on mode.
    """
    # Check if auto-create is enabled
    auto_create = await get_config_value(db, "auto_create_memories", True)
    if not auto_create:
        return {"created": False, "reason": "auto_create_disabled"}

    # Check importance threshold
    threshold = await get_config_value(db, "memory_importance_threshold", 0.6)
    if importance < float(threshold):
        return {"created": False, "reason": "below_importance_threshold"}

    scopes = _memory_scopes(mode, guest_session_id)
    scope = scopes.create_scope
    if scope is None:
        return {"created": False, "reason": "guest_without_session"}

    try:
        # Calculate expiration for guest memories
        expires_at = None
        if scope == "guest" and guest_session_id:
            session = db.query(GuestSession).filter(GuestSession.id == guest_session_id).first()
            if session:
                retention_days = await get_config_value(db, "guest_retention_days", 7)
                expires_at = datetime.combine(
                    session.check_out_date,
                    datetime.min.time()
                ) + timedelta(days=int(retention_days))

        # Postgres first: the memory exists even if its vector can't be written.
        memory = Memory(
            content=content,
            scope=scope,
            guest_session_id=guest_session_id if scope == "guest" else None,
            vector_id=str(uuid.uuid4()),
            vector_status="pending",
            category=category,
            importance=importance,
            source_type="conversation",
            source_query=source_query,
            expires_at=expires_at
        )

        db.add(memory)
        db.commit()
        db.refresh(memory)
    except Exception as e:
        db.rollback()
        logger.error("internal_create_failed", error=str(e))
        return {"created": False, "reason": str(e)}

    vector_stored = await run_in_threadpool(memory_vectors.store_vector, memory)
    try:
        db.commit()
    except Exception as e:
        db.rollback()
        vector_stored = False
        logger.error("memory_vector_status_commit_failed", memory_id=memory.id, error=str(e))

    return {"created": True, "memory_id": memory.id, "vector_stored": vector_stored}


_FORGET_LIMIT = 5


@router.post("/internal/forget", dependencies=[Depends(require_service_key_401)])
async def internal_forget_memory(
    search_query: str,
    mode: str = "guest",
    min_score: float = 0.4,
    guest_session_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """
    Internal endpoint for orchestrator to delete memories by content search.
    Searches for matching memories the caller may delete and deletes them:
    an owner deletes global + owner memories; a guest only their own
    session's guest memories; a guest without a session nothing.
    """
    scopes = _memory_scopes(mode, guest_session_id)
    if not scopes.deletable:
        return {"deleted": 0, "message": "Nothing this caller may forget"}

    try:
        kept, reason = await _semantic_search(
            db, search_query, scopes.deletable, scopes.guest_session_id, _FORGET_LIMIT, min_score,
        )
        if kept is None:
            return {"deleted": 0, "error": "Semantic search unavailable"}
        if not kept:
            return {"deleted": 0, "message": "No matching memories found"}

        # Commit the soft deletes first, then remove the points.
        now = datetime.utcnow()
        deleted_memories = []
        for memory, score in kept:
            memory.is_deleted = True
            memory.deleted_at = now
            deleted_memories.append({"id": memory.id, "content": memory.content[:100], "score": score})
        db.commit()
        await run_in_threadpool(memory_vectors.delete_points, [memory.vector_id for memory, _ in kept])

        logger.info(
            "memories_forgotten",
            count=len(deleted_memories),
            search_query=search_query[:50]
        )

        return {
            "deleted": len(deleted_memories),
            "memories": deleted_memories,
            "search_query": search_query
        }

    except Exception as e:
        logger.error("internal_forget_failed", error=str(e))
        return {"deleted": 0, "error": str(e)}


# =============================================================================
# Qdrant Health Static Route (before /{memory_id})
# =============================================================================

@router.get("/qdrant/health", dependencies=[Depends(require_memory_reader)])
async def qdrant_health(db: Session = Depends(get_db)):
    """Vector store status against Postgres, the source of truth.

    status: healthy (ready and every live memory has its vector, nothing
    pending), degraded (ready but out of sync), unavailable (store or
    embedder down), error (shape or model mismatch, or unreadable). The
    Postgres counts are always present."""
    report = await run_in_threadpool(memory_vectors.describe)
    live = db.query(Memory).filter(Memory.is_deleted == False)
    pg_live_count = live.count()
    pending_count = live.filter(Memory.vector_status == "pending").count()
    state = report["state"]
    in_sync = (
        state == memory_vectors.READY
        and report.get("error") is None
        and report.get("points_count") == pg_live_count
        and pending_count == 0
    )
    if state in (memory_vectors.UNAVAILABLE, memory_vectors.EMBEDDER_UNAVAILABLE):
        status = "unavailable"
    elif state != memory_vectors.READY or report.get("error") is not None:
        status = "error"
    else:
        status = "healthy" if in_sync else "degraded"
    return {**report, "status": status, "pg_live_count": pg_live_count, "pending_count": pending_count,
            "in_sync": in_sync}


# =============================================================================
# Vector store maintenance (before /{memory_id})
# =============================================================================

@router.post("/vector-store/reindex")
async def reindex_memory_vectors(
    request: Request,
    mode: str = Query("missing", pattern="^(missing|all)$"),
    dry_run: bool = False,
    caller_kind: str = Depends(require_memory_maintainer),
    db: Session = Depends(get_db),
):
    """Rebuild memory vectors from Postgres.

    An owner (manage_infrastructure) may run either mode and prunes orphan
    points. The service key may run mode=missing only and never prunes.
    One rebuild at a time across replicas, with a cooldown after a real
    run; 409 reindex_busy carries retry_after_seconds.
    """
    if caller_kind == "service" and mode != "missing":
        raise HTTPException(status_code=403, detail="service_key_limited_to_missing")
    if caller_kind == "service":
        logger.info("memory_vector_reindex_requested", caller="service", mode=mode, dry_run=dry_run)

    outcome: Dict[str, Any]
    status_code = 200
    try:
        report = await run_in_threadpool(functools.partial(
            memory_vectors.reindex, mode, dry_run=dry_run, prune=caller_kind == "user", caller=caller_kind,
        ))
    except memory_vectors.ReindexBusy as busy:
        status_code = 409
        outcome = {"error": "reindex_busy", "retry_after_seconds": busy.retry_after_seconds}
    else:
        outcome = report.to_dict()
        if report.refused:
            status_code, outcome = 409, {"error": "reindex_refused", **outcome}
        elif report.aborted:
            status_code, outcome = 409, {"error": "reindex_aborted", **outcome}

    if caller_kind == "user":
        from app.routes.services import create_audit_log

        create_audit_log(
            db, request.state.auth_user, "memory_vector_reindex",
            new_value={"mode": mode, "dry_run": dry_run, **outcome},
            request=request, success=status_code == 200,
            error_message=outcome.get("error"),
        )
    if status_code != 200:
        return JSONResponse(status_code=status_code, content=outcome)
    return outcome


# =============================================================================
# Dynamic Routes with Path Parameters (MUST be AFTER static routes)
# =============================================================================

@router.get("/{memory_id}")
async def get_memory(
    memory_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific memory by ID."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    memory = db.query(Memory).filter(
        Memory.id == memory_id,
        Memory.is_deleted == False
    ).first()

    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Include guest session info if available
    result = memory.to_dict()
    if memory.guest_session_id and memory.guest_session:
        result['guest_session'] = {
            'guest_name': memory.guest_session.guest_name,
            'check_in_date': memory.guest_session.check_in_date.isoformat() if memory.guest_session.check_in_date else None,
            'check_out_date': memory.guest_session.check_out_date.isoformat() if memory.guest_session.check_out_date else None,
        }

    return result


@router.put("/{memory_id}")
async def update_memory(
    memory_id: int,
    update_data: MemoryUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update an existing memory."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    memory = db.query(Memory).filter(
        Memory.id == memory_id,
        Memory.is_deleted == False
    ).first()

    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Any change to what the vector or its payload carries makes the row
    # pending in the same commit; store_vector alone marks it stored again.
    changed = False
    for field in ("content", "summary", "category", "importance"):
        value = getattr(update_data, field)
        if value is not None and value != getattr(memory, field):
            setattr(memory, field, value)
            changed = True
    if changed:
        memory.vector_status = "pending"

    db.commit()
    db.refresh(memory)
    if changed:
        await run_in_threadpool(memory_vectors.store_vector, memory)
        db.commit()
        db.refresh(memory)

    logger.info("memory_updated",
               user=current_user.username,
               memory_id=memory_id)

    return memory.to_dict()


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Soft delete a memory."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    memory = db.query(Memory).filter(Memory.id == memory_id).first()

    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Soft delete in PostgreSQL
    memory.is_deleted = True
    memory.deleted_at = datetime.utcnow()
    db.commit()

    await run_in_threadpool(memory_vectors.delete_points, [memory.vector_id])

    logger.info("memory_deleted",
               user=current_user.username,
               memory_id=memory_id)

    return {"success": True, "deleted_id": memory_id}


@router.post("/{memory_id}/promote")
async def promote_memory(
    memory_id: int,
    request: PromoteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Promote a memory to a higher scope.

    - Guest -> Owner or Global
    - Owner -> Global
    """
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    # Get existing memory
    memory = db.query(Memory).filter(
        Memory.id == memory_id,
        Memory.is_deleted == False
    ).first()

    if not memory:
        raise HTTPException(status_code=404, detail="Memory not found")

    # Validate promotion path
    current_scope = memory.scope
    target_scope = request.target_scope

    valid_promotions = {
        "guest": ["owner", "global"],
        "owner": ["global"]
    }

    if target_scope not in valid_promotions.get(current_scope, []):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot promote from {current_scope} to {target_scope}"
        )

    # A new row in the target scope, re-embedded from its content (never a
    # copied vector: the copy could belong to different text).
    new_memory = Memory(
        content=memory.content,
        summary=memory.summary,
        scope=target_scope,
        guest_session_id=None,
        vector_id=str(uuid.uuid4()),
        vector_status="pending",
        category=memory.category,
        importance=memory.importance,
        source_type='promotion',
        promoted_from_id=memory_id
    )

    db.add(new_memory)
    db.commit()
    db.refresh(new_memory)
    await run_in_threadpool(memory_vectors.store_vector, new_memory)
    db.commit()
    db.refresh(new_memory)

    logger.info("memory_promoted",
               user=current_user.username,
               original_id=memory_id,
               new_id=new_memory.id,
               from_scope=current_scope,
               to_scope=target_scope)

    return {
        "success": True,
        "original_id": memory_id,
        "new_id": new_memory.id,
        "original_scope": current_scope,
        "new_scope": target_scope
    }


@router.get("/guest-sessions/{session_id}")
async def get_guest_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get a specific guest session with its memories."""
    if not current_user.has_permission('read'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    session = db.query(GuestSession).filter(GuestSession.id == session_id).first()

    if not session:
        raise HTTPException(status_code=404, detail="Guest session not found")

    memories = db.query(Memory).filter(
        Memory.guest_session_id == session_id,
        Memory.is_deleted == False
    ).order_by(Memory.importance.desc()).all()

    result = session.to_dict()
    result['memories'] = [m.to_dict() for m in memories]
    result['memory_count'] = len(memories)

    return result


@router.put("/config/{key}")
async def update_memory_config(
    key: str,
    update: ConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Update a configuration setting."""
    if not current_user.has_permission('write'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    config = db.query(MemoryConfig).filter(MemoryConfig.key == key).first()

    if config:
        config.value = update.value
    else:
        config = MemoryConfig(key=key, value=update.value)
        db.add(config)

    db.commit()

    logger.info("memory_config_updated",
               user=current_user.username,
               key=key)

    return {"success": True, "key": key, "value": update.value}
