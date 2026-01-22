"""
Analytics API routes.

Provides endpoints for intent analytics, query statistics, and RAG service usage tracking.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, and_
from pydantic import BaseModel, Field
from datetime import datetime, timedelta
import structlog
import json

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, ConversationAnalytics

logger = structlog.get_logger()

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ============================================================================
# Pydantic Models
# ============================================================================

class IntentStatistic(BaseModel):
    """Statistics for a single intent."""
    intent: str
    count: int
    has_rag_service: bool
    system_mapping: str
    percentage: float


class IntentAnalyticsResponse(BaseModel):
    """Response model for intent analytics."""
    total_queries: int
    date_range: str
    intents: List[IntentStatistic]


class QueryLogEntry(BaseModel):
    """Individual query log entry."""
    intent: str
    query: str
    has_rag_service: bool
    system_mapping: str
    user_id: Optional[str]
    timestamp: datetime


class QueryLogsResponse(BaseModel):
    """Response model for query logs."""
    total: int
    logs: List[QueryLogEntry]


# ============================================================================
# Routes
# ============================================================================

@router.get("/intents", response_model=IntentAnalyticsResponse)
async def get_intent_analytics(
    days: int = Query(default=7, ge=1, le=90, description="Number of days to analyze"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get intent classification analytics.

    Returns statistics on query intents, including:
    - Which intents are most common
    - Which intents have dedicated RAG services
    - Which intents fall back to general handling

    Args:
        days: Number of days to analyze (1-90)
        db: Database session
        current_user: Authenticated user

    Returns:
        Intent analytics with counts and percentages
    """
    try:
        # Calculate date range
        cutoff_date = datetime.utcnow() - timedelta(days=days)

        # Query for all query_intent events within date range
        query_logs = (
            db.query(ConversationAnalytics)
            .filter(
                and_(
                    ConversationAnalytics.event_type == "query_intent",
                    ConversationAnalytics.timestamp >= cutoff_date
                )
            )
            .all()
        )

        # Count total queries
        total_queries = len(query_logs)

        if total_queries == 0:
            return IntentAnalyticsResponse(
                total_queries=0,
                date_range=f"Last {days} days",
                intents=[]
            )

        # Aggregate intent statistics
        intent_counts = {}
        for log in query_logs:
            metadata = json.loads(log.event_metadata) if isinstance(log.event_metadata, str) else log.event_metadata
            intent = metadata.get("intent", "unknown")
            has_rag = metadata.get("has_rag_service", False)
            mapping = metadata.get("system_mapping", "general")

            if intent not in intent_counts:
                intent_counts[intent] = {
                    "count": 0,
                    "has_rag_service": has_rag,
                    "system_mapping": mapping
                }
            intent_counts[intent]["count"] += 1

        # Convert to response format with percentages
        intents = [
            IntentStatistic(
                intent=intent,
                count=data["count"],
                has_rag_service=data["has_rag_service"],
                system_mapping=data["system_mapping"],
                percentage=round((data["count"] / total_queries) * 100, 2)
            )
            for intent, data in intent_counts.items()
        ]

        # Sort by count (most common first)
        intents.sort(key=lambda x: x.count, reverse=True)

        logger.info(
            "intent_analytics_retrieved",
            user_id=current_user.id,
            total_queries=total_queries,
            unique_intents=len(intents),
            days=days
        )

        return IntentAnalyticsResponse(
            total_queries=total_queries,
            date_range=f"Last {days} days",
            intents=intents
        )

    except Exception as e:
        logger.error("intent_analytics_error", error=str(e), user_id=current_user.id)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve intent analytics: {str(e)}")


@router.get("/query-logs", response_model=QueryLogsResponse)
async def get_query_logs(
    days: int = Query(default=7, ge=1, le=90, description="Number of days to retrieve"),
    intent_filter: Optional[str] = Query(default=None, description="Filter by specific intent"),
    has_rag: Optional[bool] = Query(default=None, description="Filter by has_rag_service"),
    limit: int = Query(default=100, ge=1, le=1000, description="Maximum number of logs to return"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get detailed query logs with intent information.

    Args:
        days: Number of days to retrieve (1-90)
        intent_filter: Optional intent to filter by
        has_rag: Optional filter for queries with/without RAG services
        limit: Maximum number of logs to return (1-1000)
        db: Database session
        current_user: Authenticated user

    Returns:
        List of query log entries
    """
    try:
        # Calculate date range
        cutoff_date = datetime.utcnow() - timedelta(days=days)

        # Query for logs
        query = (
            db.query(ConversationAnalytics)
            .filter(
                and_(
                    ConversationAnalytics.event_type == "query_intent",
                    ConversationAnalytics.timestamp >= cutoff_date
                )
            )
            .order_by(desc(ConversationAnalytics.timestamp))
        )

        logs = query.limit(limit).all()

        # Parse and filter logs
        parsed_logs = []
        for log in logs:
            metadata = json.loads(log.event_metadata) if isinstance(log.event_metadata, str) else log.event_metadata
            intent = metadata.get("intent", "unknown")
            has_rag_service = metadata.get("has_rag_service", False)

            # Apply filters
            if intent_filter and intent != intent_filter:
                continue
            if has_rag is not None and has_rag_service != has_rag:
                continue

            parsed_logs.append(QueryLogEntry(
                intent=intent,
                query=metadata.get("query", ""),
                has_rag_service=has_rag_service,
                system_mapping=metadata.get("system_mapping", "general"),
                user_id=metadata.get("user_id"),
                timestamp=log.timestamp
            ))

        logger.info(
            "query_logs_retrieved",
            user_id=current_user.id,
            total_logs=len(parsed_logs),
            days=days,
            filters={"intent": intent_filter, "has_rag": has_rag}
        )

        return QueryLogsResponse(
            total=len(parsed_logs),
            logs=parsed_logs
        )

    except Exception as e:
        logger.error("query_logs_error", error=str(e), user_id=current_user.id)
        raise HTTPException(status_code=500, detail=f"Failed to retrieve query logs: {str(e)}")


@router.get("/latency-distribution")
async def get_latency_distribution(
    period: str = Query(default="1h", description="Time period (1h, 6h, 24h, 7d)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get latency distribution for voice pipeline components.

    Returns histogram-style buckets and counts for the Voice Pipelines chart.
    """
    # Parse period to get appropriate time range
    period_map = {
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7)
    }
    time_range = period_map.get(period, timedelta(hours=1))
    cutoff = datetime.utcnow() - time_range

    # Histogram buckets (in ms)
    buckets = ['<100ms', '100-200ms', '200-500ms', '500ms-1s', '>1s']
    bucket_ranges = [(0, 100), (100, 200), (200, 500), (500, 1000), (1000, float('inf'))]

    try:
        # Query for latency events from conversation analytics
        logs = (
            db.query(ConversationAnalytics)
            .filter(
                and_(
                    ConversationAnalytics.event_type.in_(["stt_complete", "llm_complete", "tts_complete", "query_complete", "voice_response"]),
                    ConversationAnalytics.timestamp >= cutoff
                )
            )
            .order_by(ConversationAnalytics.timestamp)
            .limit(1000)
            .all()
        )

        # Count latencies into buckets
        counts = [0, 0, 0, 0, 0]

        for log in logs:
            metadata = json.loads(log.event_metadata) if isinstance(log.event_metadata, str) else (log.event_metadata or {})
            latency = metadata.get("latency_ms") or metadata.get("duration_ms") or metadata.get("total_latency_ms")

            if latency:
                for i, (low, high) in enumerate(bucket_ranges):
                    if low <= latency < high:
                        counts[i] += 1
                        break

        return {
            "period": period,
            "buckets": buckets,
            "counts": counts
        }

    except Exception as e:
        logger.error("latency_distribution_error", error=str(e))
        # Return empty histogram on error
        return {
            "period": period,
            "buckets": buckets,
            "counts": [0, 0, 0, 0, 0]
        }


@router.get("/pipeline-stats")
async def get_pipeline_stats(
    period: str = Query(default="1h", description="Time period (1h, 6h, 24h, 7d)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get voice pipeline statistics for the dashboard.

    Returns aggregate stats like request count, avg latency, error rate.
    """
    period_map = {
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
        "7d": timedelta(days=7)
    }
    time_range = period_map.get(period, timedelta(hours=1))
    cutoff = datetime.utcnow() - time_range

    try:
        # Query for pipeline completion events
        logs = (
            db.query(ConversationAnalytics)
            .filter(
                and_(
                    ConversationAnalytics.event_type.in_(["query_complete", "voice_response", "conversation_turn"]),
                    ConversationAnalytics.timestamp >= cutoff
                )
            )
            .all()
        )

        total_requests = len(logs)
        total_latency = 0
        error_count = 0

        for log in logs:
            metadata = json.loads(log.event_metadata) if isinstance(log.event_metadata, str) else (log.event_metadata or {})
            latency = metadata.get("total_latency_ms") or metadata.get("latency_ms") or metadata.get("duration_ms")
            if latency:
                total_latency += latency
            if metadata.get("error") or metadata.get("status") == "error":
                error_count += 1

        avg_latency = round(total_latency / total_requests, 1) if total_requests > 0 else 0
        error_rate = round((error_count / total_requests) * 100, 1) if total_requests > 0 else 0
        success_rate = round(100 - error_rate, 1)

        return {
            "period": period,
            "total_requests": total_requests,
            "avg_latency_ms": avg_latency,
            "success_rate": success_rate,
            "error_rate": error_rate,
            "error_count": error_count
        }

    except Exception as e:
        logger.error("pipeline_stats_error", error=str(e))
        return {
            "period": period,
            "total_requests": 0,
            "avg_latency_ms": 0,
            "success_rate": 0,
            "error_rate": 0,
            "error_count": 0
        }
