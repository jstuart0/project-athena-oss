"""
Emerging Intents API Routes.

Provides endpoints for managing discovered/novel intents:
- Internal endpoints for orchestrator (no auth required)
- Admin endpoints for review/management (auth required)
"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel, Field
import structlog
from datetime import datetime

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, EmergingIntent, IntentMetric

logger = structlog.get_logger()

router = APIRouter(tags=["emerging-intents"])


# =============================================================================
# Pydantic Models
# =============================================================================

class EmergingIntentCreate(BaseModel):
    """Request model for creating an emerging intent."""
    canonical_name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    embedding: Optional[List[float]] = None
    suggested_category: Optional[str] = None
    suggested_api_sources: Optional[List[str]] = None
    sample_queries: Optional[List[str]] = None


class EmergingIntentResponse(BaseModel):
    """Response model for emerging intent."""
    id: int
    canonical_name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    embedding: Optional[List[float]] = None
    occurrence_count: int = 0
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    sample_queries: Optional[List[str]] = None
    suggested_category: Optional[str] = None
    suggested_api_sources: Optional[List[str]] = None
    status: str = "discovered"
    reviewed_at: Optional[str] = None
    reviewed_by: Optional[int] = None
    promoted_to_intent: Optional[str] = None
    rejection_reason: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class IntentMetricCreate(BaseModel):
    """Request model for recording intent metric."""
    intent: str
    confidence: float
    complexity: Optional[str] = "simple"
    is_novel: bool = False
    emerging_intent_id: Optional[int] = None
    raw_query: Optional[str] = None
    query_hash: Optional[str] = None
    session_id: Optional[str] = None
    mode: Optional[str] = None
    room: Optional[str] = None
    request_id: Optional[str] = None
    processing_time_ms: Optional[int] = None


class IncrementRequest(BaseModel):
    """Request model for incrementing intent count."""
    sample_query: Optional[str] = None


class PromoteRequest(BaseModel):
    """Request model for promoting an intent."""
    target_intent: str = Field(..., description="The IntentCategory to promote to")


class RejectRequest(BaseModel):
    """Request model for rejecting an intent."""
    reason: str = Field(..., description="Reason for rejection")


class MergeRequest(BaseModel):
    """Request model for merging intents."""
    source_ids: List[int] = Field(..., description="IDs of intents to merge from")
    target_id: int = Field(..., description="ID of intent to merge into")


# =============================================================================
# Internal API Endpoints (No Auth - for Orchestrator)
# =============================================================================

@router.get("/api/internal/emerging-intents", response_model=List[EmergingIntentResponse])
async def list_emerging_intents_internal(
    status: Optional[str] = Query(None, description="Comma-separated statuses to filter"),
    db: Session = Depends(get_db)
):
    """
    List emerging intents (internal endpoint for orchestrator).

    Used by orchestrator to find similar intents for clustering.
    """
    query = db.query(EmergingIntent)

    if status:
        statuses = [s.strip() for s in status.split(",")]
        query = query.filter(EmergingIntent.status.in_(statuses))

    intents = query.all()
    return [intent.to_dict() for intent in intents]


@router.post("/api/internal/emerging-intents", response_model=EmergingIntentResponse)
async def create_emerging_intent_internal(
    data: EmergingIntentCreate,
    db: Session = Depends(get_db)
):
    """
    Create a new emerging intent (internal endpoint for orchestrator).
    """
    # Check if canonical_name already exists
    existing = db.query(EmergingIntent).filter(
        EmergingIntent.canonical_name == data.canonical_name
    ).first()

    if existing:
        # Update existing instead of creating duplicate
        existing.occurrence_count += 1
        existing.last_seen = func.now()
        if data.sample_queries:
            current_samples = existing.sample_queries or []
            for query in data.sample_queries:
                if query not in current_samples and len(current_samples) < 10:
                    current_samples.append(query)
            existing.sample_queries = current_samples
        db.commit()
        db.refresh(existing)
        logger.info("emerging_intent_updated", id=existing.id, canonical_name=existing.canonical_name)
        return existing.to_dict()

    # Create new
    intent = EmergingIntent(
        canonical_name=data.canonical_name,
        display_name=data.display_name or data.canonical_name.replace("_", " ").title(),
        description=data.description,
        embedding=data.embedding,
        suggested_category=data.suggested_category,
        suggested_api_sources=data.suggested_api_sources,
        sample_queries=data.sample_queries or []
    )

    db.add(intent)
    db.commit()
    db.refresh(intent)

    logger.info("emerging_intent_created", id=intent.id, canonical_name=intent.canonical_name)
    return intent.to_dict()


@router.post("/api/internal/emerging-intents/{intent_id}/increment")
async def increment_intent_count_internal(
    intent_id: int,
    data: IncrementRequest,
    db: Session = Depends(get_db)
):
    """
    Increment occurrence count for an emerging intent (internal).
    """
    intent = db.query(EmergingIntent).filter(EmergingIntent.id == intent_id).first()

    if not intent:
        raise HTTPException(status_code=404, detail="Emerging intent not found")

    intent.occurrence_count += 1
    intent.last_seen = func.now()

    # Add sample query if provided and not duplicate
    if data.sample_query:
        current_samples = intent.sample_queries or []
        if data.sample_query not in current_samples and len(current_samples) < 10:
            current_samples.append(data.sample_query)
            intent.sample_queries = current_samples

    db.commit()

    logger.info("emerging_intent_incremented",
               id=intent.id,
               count=intent.occurrence_count)

    return {"success": True, "occurrence_count": intent.occurrence_count}


@router.post("/api/internal/intent-metrics")
async def record_intent_metric_internal(
    data: IntentMetricCreate,
    db: Session = Depends(get_db)
):
    """
    Record an intent classification metric (internal).
    """
    metric = IntentMetric(
        intent=data.intent,
        confidence=data.confidence,
        complexity=data.complexity,
        is_novel=data.is_novel,
        emerging_intent_id=data.emerging_intent_id,
        raw_query=data.raw_query,
        query_hash=data.query_hash,
        session_id=data.session_id,
        mode=data.mode,
        room=data.room,
        request_id=data.request_id,
        processing_time_ms=data.processing_time_ms
    )

    db.add(metric)
    db.commit()

    return {"success": True, "id": metric.id}


# =============================================================================
# Admin API Endpoints (Auth Required)
# =============================================================================

@router.get("/api/emerging-intents", response_model=List[EmergingIntentResponse])
async def list_emerging_intents(
    status: Optional[str] = Query(None, description="Filter by status"),
    min_count: int = Query(1, description="Minimum occurrence count"),
    category: Optional[str] = Query(None, description="Filter by category"),
    sort_by: str = Query("occurrence_count", description="Sort field"),
    sort_order: str = Query("desc", description="Sort order (asc/desc)"),
    limit: int = Query(50, description="Max results"),
    offset: int = Query(0, description="Offset for pagination"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    List emerging intents for admin review.
    """
    query = db.query(EmergingIntent)

    # Apply filters
    if status:
        query = query.filter(EmergingIntent.status == status)

    query = query.filter(EmergingIntent.occurrence_count >= min_count)

    if category:
        query = query.filter(EmergingIntent.suggested_category == category)

    # Apply sorting
    sort_column = getattr(EmergingIntent, sort_by, EmergingIntent.occurrence_count)
    if sort_order == "desc":
        query = query.order_by(desc(sort_column))
    else:
        query = query.order_by(sort_column)

    # Apply pagination
    query = query.offset(offset).limit(limit)

    intents = query.all()
    return [intent.to_dict() for intent in intents]


# Stats endpoint MUST come before {intent_id} route to avoid being caught by the parameter
@router.get("/api/emerging-intents/stats")
async def get_emerging_intent_stats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get statistics about emerging intents.
    """
    total = db.query(func.count(EmergingIntent.id)).scalar()
    by_status = db.query(
        EmergingIntent.status,
        func.count(EmergingIntent.id)
    ).group_by(EmergingIntent.status).all()

    by_category = db.query(
        EmergingIntent.suggested_category,
        func.count(EmergingIntent.id)
    ).group_by(EmergingIntent.suggested_category).all()

    top_by_count = db.query(EmergingIntent).order_by(
        desc(EmergingIntent.occurrence_count)
    ).limit(10).all()

    return {
        "total": total,
        "by_status": {status: count for status, count in by_status},
        "by_category": {cat or "uncategorized": count for cat, count in by_category},
        "top_by_count": [
            {
                "canonical_name": i.canonical_name,
                "display_name": i.display_name,
                "count": i.occurrence_count,
                "category": i.suggested_category
            }
            for i in top_by_count
        ]
    }


@router.get("/api/emerging-intents/{intent_id}", response_model=EmergingIntentResponse)
async def get_emerging_intent(
    intent_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get details of a specific emerging intent.
    """
    intent = db.query(EmergingIntent).filter(EmergingIntent.id == intent_id).first()

    if not intent:
        raise HTTPException(status_code=404, detail="Emerging intent not found")

    return intent.to_dict()


@router.post("/api/emerging-intents/{intent_id}/promote")
async def promote_intent(
    intent_id: int,
    data: PromoteRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Promote an emerging intent to a known intent category.
    """
    intent = db.query(EmergingIntent).filter(EmergingIntent.id == intent_id).first()

    if not intent:
        raise HTTPException(status_code=404, detail="Emerging intent not found")

    if intent.status == "promoted":
        raise HTTPException(status_code=400, detail="Intent already promoted")

    intent.status = "promoted"
    intent.promoted_to_intent = data.target_intent
    intent.reviewed_at = func.now()
    intent.reviewed_by = current_user.id

    db.commit()

    logger.info("emerging_intent_promoted",
               id=intent.id,
               canonical_name=intent.canonical_name,
               target=data.target_intent,
               by=current_user.username)

    return {"success": True, "message": f"Intent promoted to {data.target_intent}"}


@router.post("/api/emerging-intents/{intent_id}/reject")
async def reject_intent(
    intent_id: int,
    data: RejectRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Reject an emerging intent (won't implement).
    """
    intent = db.query(EmergingIntent).filter(EmergingIntent.id == intent_id).first()

    if not intent:
        raise HTTPException(status_code=404, detail="Emerging intent not found")

    intent.status = "rejected"
    intent.rejection_reason = data.reason
    intent.reviewed_at = func.now()
    intent.reviewed_by = current_user.id

    db.commit()

    logger.info("emerging_intent_rejected",
               id=intent.id,
               canonical_name=intent.canonical_name,
               reason=data.reason,
               by=current_user.username)

    return {"success": True, "message": "Intent rejected"}


@router.post("/api/emerging-intents/{intent_id}/review")
async def mark_reviewed(
    intent_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Mark an emerging intent as reviewed (neither promoted nor rejected).
    """
    intent = db.query(EmergingIntent).filter(EmergingIntent.id == intent_id).first()

    if not intent:
        raise HTTPException(status_code=404, detail="Emerging intent not found")

    intent.status = "reviewed"
    intent.reviewed_at = func.now()
    intent.reviewed_by = current_user.id

    db.commit()

    return {"success": True, "message": "Intent marked as reviewed"}


@router.post("/api/emerging-intents/merge")
async def merge_intents(
    data: MergeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Merge multiple emerging intents into one.

    Combines occurrence counts and sample queries, then deletes source intents.
    """
    # Get target intent
    target = db.query(EmergingIntent).filter(EmergingIntent.id == data.target_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="Target intent not found")

    # Get source intents
    sources = db.query(EmergingIntent).filter(EmergingIntent.id.in_(data.source_ids)).all()
    if len(sources) != len(data.source_ids):
        raise HTTPException(status_code=404, detail="Some source intents not found")

    # Merge data
    total_count = target.occurrence_count
    all_samples = target.sample_queries or []

    for source in sources:
        total_count += source.occurrence_count
        for query in (source.sample_queries or []):
            if query not in all_samples and len(all_samples) < 10:
                all_samples.append(query)

        # Update any metrics pointing to source
        db.query(IntentMetric).filter(
            IntentMetric.emerging_intent_id == source.id
        ).update({"emerging_intent_id": target.id})

        # Delete source
        db.delete(source)

    # Update target
    target.occurrence_count = total_count
    target.sample_queries = all_samples
    target.last_seen = func.now()

    db.commit()

    logger.info("emerging_intents_merged",
               target_id=data.target_id,
               source_ids=data.source_ids,
               by=current_user.username)

    return {
        "success": True,
        "message": f"Merged {len(sources)} intents into {target.canonical_name}",
        "new_count": total_count
    }


# =============================================================================
# Analytics Endpoints
# =============================================================================

@router.get("/api/intent-metrics/stats")
async def get_intent_metric_stats(
    days: int = Query(7, description="Number of days to analyze"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get intent classification statistics.
    """
    from datetime import timedelta

    cutoff = datetime.utcnow() - timedelta(days=days)

    total = db.query(func.count(IntentMetric.id)).filter(
        IntentMetric.created_at >= cutoff
    ).scalar()

    novel_count = db.query(func.count(IntentMetric.id)).filter(
        IntentMetric.created_at >= cutoff,
        IntentMetric.is_novel == True
    ).scalar()

    by_intent = db.query(
        IntentMetric.intent,
        func.count(IntentMetric.id)
    ).filter(
        IntentMetric.created_at >= cutoff
    ).group_by(IntentMetric.intent).all()

    avg_confidence = db.query(func.avg(IntentMetric.confidence)).filter(
        IntentMetric.created_at >= cutoff
    ).scalar()

    return {
        "days": days,
        "total_classifications": total,
        "novel_discoveries": novel_count,
        "novel_percentage": (novel_count / total * 100) if total > 0 else 0,
        "average_confidence": round(avg_confidence or 0, 3),
        "by_intent": {intent: count for intent, count in by_intent}
    }
