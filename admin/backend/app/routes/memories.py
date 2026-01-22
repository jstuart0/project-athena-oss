"""
Hierarchical Memory Management API Routes.

Provides endpoints for managing scoped memories with Qdrant vector integration.
Supports three memory scopes: global, owner, and guest (session-scoped).

IMPORTANT: Route Ordering
    FastAPI matches routes in definition order. Static routes (like /config,
    /guest-sessions) MUST be defined BEFORE dynamic routes (like /{memory_id})
    to prevent the dynamic route from catching everything.
"""
import os
import re
import uuid
import json
import asyncio
from typing import List, Optional, Dict, Any
from datetime import datetime, date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func as sql_func, or_
from pydantic import BaseModel, Field
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, Memory, GuestSession, MemoryConfig, Feature

logger = structlog.get_logger()

router = APIRouter(prefix="/api/memories", tags=["memories"])

# Configuration
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = os.getenv("QDRANT_PORT", "6333")
QDRANT_URL = os.getenv("QDRANT_URL", f"http://{QDRANT_HOST}:{QDRANT_PORT}")
COLLECTION_NAME = "athena_memories"

# Lazy-loaded clients
_qdrant_client = None
_embedder = None


def get_qdrant():
    """Get or create Qdrant client."""
    global _qdrant_client
    if _qdrant_client is None:
        try:
            from qdrant_client import QdrantClient
            _qdrant_client = QdrantClient(url=QDRANT_URL, timeout=10)
            logger.info("qdrant_client_initialized", url=QDRANT_URL)
        except Exception as e:
            logger.error("qdrant_client_init_failed", error=str(e))
            return None
    return _qdrant_client


def get_embedder():
    """Get or create FastEmbed embedder (lightweight alternative to sentence-transformers)."""
    global _embedder
    if _embedder is None:
        try:
            from fastembed import TextEmbedding
            # all-MiniLM-L6-v2 produces 384-dimensional embeddings
            _embedder = TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")
            logger.info("embedder_initialized", model="all-MiniLM-L6-v2")
        except Exception as e:
            logger.error("embedder_init_failed", error=str(e))
            return None
    return _embedder


def embed_text(text: str) -> List[float]:
    """Generate embedding for text using FastEmbed.

    FastEmbed returns a generator, so we need to convert to list.
    This wrapper provides a consistent interface.
    """
    embedder = get_embedder()
    if embedder is None:
        return []
    # FastEmbed's embed() returns a generator, take first result
    embeddings = list(embedder.embed([text]))
    if embeddings:
        return embeddings[0].tolist()
    return []


async def check_qdrant_available() -> bool:
    """Check if Qdrant is reachable."""
    client = get_qdrant()
    if client is None:
        return False
    try:
        client.get_collections()
        return True
    except Exception as e:
        logger.warning("qdrant_health_check_failed", error=str(e))
        return False


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


async def keyword_search_memories(
    db: Session,
    keywords: List[str],
    mode: str = "owner",
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
        # Build scope filter
        if mode == "guest" and guest_session_id:
            scope_filter = or_(
                Memory.scope == "global",
                (Memory.scope == "guest") & (Memory.guest_session_id == guest_session_id)
            )
        else:
            # Owner mode
            scope_filter = or_(
                Memory.scope == "global",
                Memory.scope == "owner"
            )

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

class MemoryCreate(BaseModel):
    """Schema for creating a new memory."""
    content: str
    summary: Optional[str] = None
    scope: str  # 'global', 'owner', 'guest'
    guest_session_id: Optional[int] = None
    category: Optional[str] = None
    importance: float = Field(default=0.5, ge=0, le=1)
    source_type: str = "manual"
    source_query: Optional[str] = None


class MemoryUpdate(BaseModel):
    """Schema for updating a memory."""
    content: Optional[str] = None
    summary: Optional[str] = None
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

    # Generate embedding and store in Qdrant
    vector_id = str(uuid.uuid4())
    qdrant = get_qdrant()

    if qdrant:
        try:
            from qdrant_client.models import PointStruct

            vector = embed_text(memory.content)
            if not vector:
                logger.warning("embedding_failed_for_memory", content=memory.content[:50])
                vector_id = None
            else:
                qdrant.upsert(
                    collection_name=COLLECTION_NAME,
                    points=[
                        PointStruct(
                            id=vector_id,
                            vector=vector,
                            payload={
                                "content": memory.content,
                                "summary": memory.summary or memory.content[:100],
                                "scope": memory.scope,
                                "guest_session_id": memory.guest_session_id,
                                "category": memory.category,
                                "importance": memory.importance,
                                "source_type": memory.source_type,
                                "created_at": datetime.utcnow().isoformat(),
                                "expires_at": expires_at.isoformat() if expires_at else None
                            }
                        )
                    ]
                )
                logger.info("memory_stored_in_qdrant", vector_id=vector_id)
        except Exception as e:
            logger.error("qdrant_store_failed", error=str(e))
            # Continue anyway - PostgreSQL is the source of truth

    # Create memory in PostgreSQL
    new_memory = Memory(
        content=memory.content,
        summary=memory.summary,
        scope=memory.scope,
        guest_session_id=memory.guest_session_id,
        vector_id=vector_id,
        category=memory.category,
        importance=memory.importance,
        source_type=memory.source_type,
        source_query=memory.source_query,
        expires_at=expires_at
    )

    db.add(new_memory)
    db.commit()
    db.refresh(new_memory)

    logger.info("memory_created",
               user=current_user.username,
               memory_id=new_memory.id,
               scope=memory.scope)

    return new_memory.to_dict()


@router.get("")
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

    Public endpoint (no auth) to allow internal service calls.
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

@router.post("/search")
async def search_memories(
    request: MemorySearchRequest,
    db: Session = Depends(get_db)
):
    """
    Scoped semantic search over memories.

    - Guest mode: Returns global + current guest session memories
    - Owner mode: Returns global + all owner memories

    Public endpoint (no auth) to allow orchestrator calls.
    """
    if not await check_qdrant_available():
        return {"results": [], "qdrant_available": False}

    # Build filter based on mode
    from qdrant_client.models import Filter, FieldCondition, MatchValue

    if request.mode == "guest":
        if not request.guest_session_id:
            raise HTTPException(status_code=400, detail="Guest mode requires guest_session_id")

        # Guest sees: global OR their specific session
        filter_condition = Filter(
            should=[
                FieldCondition(key="scope", match=MatchValue(value="global")),
                Filter(
                    must=[
                        FieldCondition(key="scope", match=MatchValue(value="guest")),
                        FieldCondition(key="guest_session_id", match=MatchValue(value=request.guest_session_id))
                    ]
                )
            ]
        )
    else:
        # Owner sees: global OR owner scope
        filter_condition = Filter(
            should=[
                FieldCondition(key="scope", match=MatchValue(value="global")),
                FieldCondition(key="scope", match=MatchValue(value="owner"))
            ]
        )

    # Generate query embedding
    query_vector = embed_text(request.query)
    if not query_vector:
        return {"results": [], "qdrant_available": False, "error": "Embedder not available"}

    # Search Qdrant using query_points API (qdrant-client 1.7+)
    qdrant = get_qdrant()
    try:
        from qdrant_client.models import QueryRequest

        # Use query_points with the new API
        search_result = qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=filter_condition,
            limit=request.limit,
            score_threshold=request.min_score,
            with_payload=True
        )
        # query_points returns a QueryResponse with .points attribute
        results = search_result.points if hasattr(search_result, 'points') else []
    except Exception as e:
        logger.error("qdrant_search_failed", error=str(e))
        return {"results": [], "qdrant_available": True, "error": str(e)}

    # Update access counts and build results
    result_list = []
    for hit in results:
        # Try to find in PostgreSQL to update access count
        memory = db.query(Memory).filter(Memory.vector_id == str(hit.id)).first()
        if memory:
            memory.access_count += 1
            memory.last_accessed_at = datetime.utcnow()

        # Handle both ScoredPoint and QueryPoint response types
        payload = hit.payload if hasattr(hit, 'payload') else {}
        score = hit.score if hasattr(hit, 'score') else 0.0

        result_list.append({
            "id": memory.id if memory else 0,
            "content": payload.get("content", ""),
            "summary": payload.get("summary", ""),
            "scope": payload.get("scope", ""),
            "score": score,
            "category": payload.get("category")
        })

    db.commit()

    return {
        "results": result_list,
        "query": request.query,
        "mode": request.mode,
        "qdrant_available": True
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


@router.get("/guest-sessions/active")
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

@router.get("/internal/search")
async def internal_memory_search(
    query: str,
    mode: str = "owner",
    guest_session_id: Optional[int] = None,
    limit: int = Query(default=3, le=10),
    db: Session = Depends(get_db)
):
    """
    Internal endpoint for orchestrator memory retrieval.
    Returns empty results gracefully if Qdrant unavailable.

    When hybrid_memory_search feature flag is enabled, combines:
    - Semantic vector search (via Qdrant)
    - Keyword-based search (via PostgreSQL)

    This improves recall for queries with specific keywords that may not
    match semantically (e.g., "miles driven" vs "my car is a Tesla").
    """
    qdrant_available = await check_qdrant_available()

    try:
        # Check if hybrid search is enabled
        hybrid_enabled = is_hybrid_search_enabled(db)
        threshold = await get_config_value(db, "similarity_threshold", 0.35)

        if hybrid_enabled:
            # Hybrid search: run keyword and semantic search in parallel
            config = get_hybrid_search_config(db)
            keywords = extract_keywords(query)

            logger.info(
                "hybrid_search_starting",
                query_preview=query[:50],
                keywords=keywords,
                mode=mode
            )

            # Run searches in parallel for minimal latency
            semantic_task = None
            keyword_task = keyword_search_memories(db, keywords, mode, guest_session_id, limit)

            if qdrant_available:
                async def run_semantic():
                    result = await search_memories(MemorySearchRequest(
                        query=query,
                        mode=mode,
                        guest_session_id=guest_session_id,
                        limit=limit,
                        min_score=float(threshold)
                    ), db)
                    return [
                        {"content": r["content"], "scope": r["scope"], "score": r["score"]}
                        for r in result.get("results", [])
                    ]
                semantic_task = run_semantic()

            # Wait for both searches
            if semantic_task:
                semantic_results, keyword_results = await asyncio.gather(
                    semantic_task,
                    keyword_task
                )
            else:
                semantic_results = []
                keyword_results = await keyword_task

            # Merge results with configurable weights
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
                "qdrant_available": qdrant_available,
                "search_type": "hybrid"
            }

        else:
            # Standard semantic-only search
            if not qdrant_available:
                return {"results": [], "qdrant_available": False}

            result = await search_memories(MemorySearchRequest(
                query=query,
                mode=mode,
                guest_session_id=guest_session_id,
                limit=limit,
                min_score=float(threshold)
            ), db)

            return {
                "results": [
                    {
                        "content": r["content"],
                        "scope": r["scope"],
                        "score": r["score"]
                    }
                    for r in result.get("results", [])
                ],
                "qdrant_available": True,
                "search_type": "semantic"
            }

    except Exception as e:
        logger.error("internal_search_failed", error=str(e))
        return {"results": [], "qdrant_available": False}


@router.post("/internal/create")
async def internal_create_memory(
    content: str,
    mode: str = "owner",
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

    # Determine scope
    if mode == "guest" and guest_session_id:
        scope = "guest"
    else:
        scope = "owner"

    try:
        # Generate embedding and store
        vector_id = str(uuid.uuid4())
        qdrant = get_qdrant()

        if qdrant:
            from qdrant_client.models import PointStruct
            vector = embed_text(content)
            if vector:
                qdrant.upsert(
                    collection_name=COLLECTION_NAME,
                    points=[
                        PointStruct(
                            id=vector_id,
                            vector=vector,
                            payload={
                                "content": content,
                                "summary": content[:100],
                                "scope": scope,
                                "guest_session_id": guest_session_id,
                                "category": category,
                                "importance": importance,
                                "source_type": "conversation",
                                "created_at": datetime.utcnow().isoformat()
                            }
                        )
                    ]
                )
            else:
                vector_id = None

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

        # Create in PostgreSQL
        memory = Memory(
            content=content,
            scope=scope,
            guest_session_id=guest_session_id if scope == "guest" else None,
            vector_id=vector_id,
            category=category,
            importance=importance,
            source_type="conversation",
            source_query=source_query,
            expires_at=expires_at
        )

        db.add(memory)
        db.commit()
        db.refresh(memory)

        return {"created": True, "memory_id": memory.id}
    except Exception as e:
        logger.error("internal_create_failed", error=str(e))
        return {"created": False, "reason": str(e)}


@router.post("/internal/forget")
async def internal_forget_memory(
    search_query: str,
    mode: str = "owner",
    min_score: float = 0.4,
    db: Session = Depends(get_db)
):
    """
    Internal endpoint for orchestrator to delete memories by content search.
    Searches for matching memories and deletes them.
    """
    if not await check_qdrant_available():
        return {"deleted": 0, "error": "Qdrant unavailable"}

    try:
        from qdrant_client.models import Filter, FieldCondition, MatchValue

        # Build filter based on mode (same as search)
        if mode == "owner":
            filter_condition = Filter(
                should=[
                    FieldCondition(key="scope", match=MatchValue(value="global")),
                    FieldCondition(key="scope", match=MatchValue(value="owner"))
                ]
            )
        else:
            # Guest mode - would need guest_session_id
            filter_condition = Filter(
                should=[
                    FieldCondition(key="scope", match=MatchValue(value="global"))
                ]
            )

        # Generate query embedding
        query_vector = embed_text(search_query)
        if not query_vector:
            return {"deleted": 0, "error": "Embedder not available"}

        # Search for matching memories
        qdrant = get_qdrant()
        search_result = qdrant.query_points(
            collection_name=COLLECTION_NAME,
            query=query_vector,
            query_filter=filter_condition,
            limit=5,  # Only delete top matches
            score_threshold=min_score,
            with_payload=True
        )
        results = search_result.points if hasattr(search_result, 'points') else []

        if not results:
            return {"deleted": 0, "message": "No matching memories found"}

        deleted_memories = []
        for hit in results:
            # Find in PostgreSQL
            memory = db.query(Memory).filter(Memory.vector_id == str(hit.id)).first()
            if memory and not memory.is_deleted:
                # Soft delete in PostgreSQL
                memory.is_deleted = True
                memory.deleted_at = datetime.utcnow()

                # Delete from Qdrant
                try:
                    from qdrant_client.models import PointIdsList
                    qdrant.delete(
                        collection_name=COLLECTION_NAME,
                        points_selector=PointIdsList(points=[str(hit.id)])
                    )
                except Exception as e:
                    logger.warning("qdrant_delete_failed", error=str(e), vector_id=str(hit.id))

                deleted_memories.append({
                    "id": memory.id,
                    "content": memory.content[:100],
                    "score": hit.score if hasattr(hit, 'score') else 0.0
                })

        db.commit()

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

@router.get("/qdrant/health")
async def qdrant_health():
    """Check Qdrant connection and collection status."""
    available = await check_qdrant_available()

    if not available:
        return {
            "status": "unavailable",
            "url": QDRANT_URL,
            "collection": COLLECTION_NAME
        }

    qdrant = get_qdrant()
    try:
        info = qdrant.get_collection(COLLECTION_NAME)
        # Handle different qdrant-client versions - attribute names vary
        vectors_count = getattr(info, 'vectors_count', None)
        if vectors_count is None:
            vectors_count = getattr(info, 'indexed_vectors_count', 0)
        points_count = getattr(info, 'points_count', 0)

        return {
            "status": "healthy",
            "url": QDRANT_URL,
            "collection": COLLECTION_NAME,
            "vectors_count": vectors_count,
            "points_count": points_count
        }
    except Exception as e:
        return {
            "status": "error",
            "url": QDRANT_URL,
            "collection": COLLECTION_NAME,
            "error": str(e)
        }


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

    # Update fields if provided
    if update_data.content is not None:
        memory.content = update_data.content
        # Re-embed if content changed
        qdrant = get_qdrant()
        if qdrant and memory.vector_id:
            try:
                from qdrant_client.models import PointStruct
                vector = embed_text(update_data.content)
                if vector:
                    qdrant.upsert(
                        collection_name=COLLECTION_NAME,
                        points=[
                            PointStruct(
                                id=memory.vector_id,
                                vector=vector,
                                payload={
                                    "content": update_data.content,
                                    "summary": update_data.summary or memory.summary or update_data.content[:100],
                                    "scope": memory.scope,
                                    "guest_session_id": memory.guest_session_id,
                                    "category": update_data.category or memory.category,
                                    "importance": update_data.importance if update_data.importance is not None else memory.importance,
                                    "source_type": memory.source_type,
                                    "created_at": memory.created_at.isoformat() if memory.created_at else None,
                                    "expires_at": memory.expires_at.isoformat() if memory.expires_at else None
                                }
                            )
                        ]
                    )
            except Exception as e:
                logger.error("qdrant_update_failed", error=str(e))

    if update_data.summary is not None:
        memory.summary = update_data.summary
    if update_data.category is not None:
        memory.category = update_data.category
    if update_data.importance is not None:
        memory.importance = update_data.importance

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

    # Delete from Qdrant
    qdrant = get_qdrant()
    if qdrant:
        try:
            from qdrant_client.models import PointIdsList
            qdrant.delete(
                collection_name=COLLECTION_NAME,
                points_selector=PointIdsList(points=[memory.vector_id])
            )
        except Exception as e:
            logger.error("qdrant_delete_failed", error=str(e))

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

    # Create new memory in target scope
    new_vector_id = str(uuid.uuid4())

    # Copy to Qdrant with new scope
    qdrant = get_qdrant()
    if qdrant:
        try:
            original = qdrant.retrieve(
                collection_name=COLLECTION_NAME,
                ids=[memory.vector_id]
            )

            if original:
                from qdrant_client.models import PointStruct
                qdrant.upsert(
                    collection_name=COLLECTION_NAME,
                    points=[
                        PointStruct(
                            id=new_vector_id,
                            vector=original[0].vector,
                            payload={
                                **original[0].payload,
                                "scope": target_scope,
                                "guest_session_id": None,
                                "expires_at": None,
                                "promoted_from_id": memory_id
                            }
                        )
                    ]
                )
        except Exception as e:
            logger.error("qdrant_promotion_failed", error=str(e))

    # Create new PostgreSQL record
    new_memory = Memory(
        content=memory.content,
        summary=memory.summary,
        scope=target_scope,
        guest_session_id=None,
        vector_id=new_vector_id,
        category=memory.category,
        importance=memory.importance,
        source_type='promotion',
        promoted_from_id=memory_id
    )

    db.add(new_memory)
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
