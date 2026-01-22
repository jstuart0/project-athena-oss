"""
Cloud LLM Usage Tracking API Routes.

Provides endpoints for logging and querying cloud LLM usage data
for cost analytics, monitoring, and budgeting.

Open Source Compatible - Uses standard FastAPI patterns.
"""
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel, Field
from datetime import datetime, timedelta, timezone, date
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user, get_optional_user
from app.models import User, CloudLLMUsage, CloudLLMProvider, CloudLLMModelPricing

logger = structlog.get_logger()

router = APIRouter(prefix="/api/cloud-llm-usage", tags=["cloud-llm-usage"])


# =============================================================================
# Pydantic Models
# =============================================================================

class UsageLogCreate(BaseModel):
    """Request model for logging cloud LLM usage."""
    provider: str = Field(..., description="Provider ID (openai, anthropic, google)")
    model: str = Field(..., description="Model ID used")
    input_tokens: int = Field(default=0, description="Input token count")
    output_tokens: int = Field(default=0, description="Output token count")
    cost_usd: float = Field(default=0.0, description="Cost in USD")
    latency_ms: Optional[int] = Field(None, description="Request latency in ms")
    ttft_ms: Optional[int] = Field(None, description="Time to first token in ms")
    streaming: bool = Field(default=False, description="Whether streaming was used")
    request_id: Optional[str] = Field(None, description="Request ID for tracking")
    session_id: Optional[str] = Field(None, description="Session ID")
    user_id: Optional[str] = Field(None, description="User ID")
    zone: Optional[str] = Field(None, description="Zone/room")
    intent: Optional[str] = Field(None, description="Classified intent")
    was_fallback: bool = Field(default=False, description="Whether this was a fallback")
    fallback_reason: Optional[str] = Field(None, description="Reason for fallback")


class UsageLogResponse(BaseModel):
    """Response model for usage log."""
    id: int
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: Optional[int]
    ttft_ms: Optional[int]
    streaming: bool
    request_id: Optional[str]
    session_id: Optional[str]
    user_id: Optional[str]
    zone: Optional[str]
    intent: Optional[str]
    was_fallback: bool
    fallback_reason: Optional[str]
    timestamp: str

    class Config:
        from_attributes = True


class UsageSummary(BaseModel):
    """Summary of usage for a time period."""
    period: str  # 'day', 'week', 'month'
    start_date: str
    end_date: str
    total_requests: int
    total_input_tokens: int
    total_output_tokens: int
    total_cost_usd: float
    avg_latency_ms: Optional[float]
    by_provider: dict
    by_model: dict
    by_zone: dict


class CostAlert(BaseModel):
    """Cost alert when threshold exceeded."""
    level: str  # 'warning', 'critical'
    message: str
    current_spend: float
    threshold: float
    period: str


# =============================================================================
# Usage Logging Routes
# =============================================================================

@router.post("", status_code=201)
async def log_usage(
    data: UsageLogCreate,
    db: Session = Depends(get_db)
):
    """
    Log cloud LLM usage.

    Called by LLMRouter after each cloud request. No auth required
    to allow internal services to log usage.
    """
    usage = CloudLLMUsage(
        provider=data.provider,
        model=data.model,
        input_tokens=data.input_tokens,
        output_tokens=data.output_tokens,
        cost_usd=data.cost_usd,
        latency_ms=data.latency_ms,
        ttft_ms=data.ttft_ms,
        streaming=data.streaming,
        request_id=data.request_id,
        session_id=data.session_id,
        user_id=data.user_id,
        zone=data.zone,
        intent=data.intent,
        was_fallback=data.was_fallback,
        fallback_reason=data.fallback_reason
    )

    db.add(usage)
    db.commit()
    db.refresh(usage)

    logger.debug(
        "cloud_usage_logged",
        provider=data.provider,
        model=data.model,
        cost_usd=data.cost_usd
    )

    return {"id": usage.id, "logged": True}


# =============================================================================
# Usage Query Routes
# =============================================================================

@router.get("/recent", response_model=List[UsageLogResponse])
async def get_recent_usage(
    minutes: int = Query(60, ge=1, le=1440, description="Minutes of history"),
    provider: Optional[str] = Query(None, description="Filter by provider"),
    limit: int = Query(100, ge=1, le=1000, description="Max results"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get recent cloud LLM usage logs."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)

    query = db.query(CloudLLMUsage).filter(
        CloudLLMUsage.timestamp >= cutoff
    )

    if provider:
        query = query.filter(CloudLLMUsage.provider == provider)

    usage = query.order_by(desc(CloudLLMUsage.timestamp)).limit(limit).all()

    return [u.to_dict() for u in usage]


@router.get("/summary/today")
async def get_today_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get usage summary for today."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    return await _get_usage_summary(db, today_start, datetime.now(timezone.utc), "day")


@router.get("/summary/week")
async def get_week_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get usage summary for the current week."""
    now = datetime.now(timezone.utc)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)

    return await _get_usage_summary(db, week_start, now, "week")


@router.get("/summary/month")
async def get_month_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get usage summary for the current month."""
    now = datetime.now(timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    return await _get_usage_summary(db, month_start, now, "month")


@router.get("/summary/range")
async def get_range_summary(
    start_date: date = Query(..., description="Start date"),
    end_date: date = Query(..., description="End date"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get usage summary for a custom date range."""
    start = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

    return await _get_usage_summary(db, start, end, "custom")


async def _get_usage_summary(
    db: Session,
    start: datetime,
    end: datetime,
    period: str
) -> dict:
    """Build usage summary for a time range."""
    base_query = db.query(CloudLLMUsage).filter(
        CloudLLMUsage.timestamp >= start,
        CloudLLMUsage.timestamp <= end
    )

    # Aggregate totals
    totals = base_query.with_entities(
        func.count(CloudLLMUsage.id).label("count"),
        func.sum(CloudLLMUsage.input_tokens).label("input_tokens"),
        func.sum(CloudLLMUsage.output_tokens).label("output_tokens"),
        func.sum(CloudLLMUsage.cost_usd).label("cost"),
        func.avg(CloudLLMUsage.latency_ms).label("avg_latency")
    ).first()

    # By provider
    by_provider = {}
    provider_stats = base_query.with_entities(
        CloudLLMUsage.provider,
        func.count(CloudLLMUsage.id).label("count"),
        func.sum(CloudLLMUsage.input_tokens).label("input_tokens"),
        func.sum(CloudLLMUsage.output_tokens).label("output_tokens"),
        func.sum(CloudLLMUsage.cost_usd).label("cost")
    ).group_by(CloudLLMUsage.provider).all()

    for stat in provider_stats:
        by_provider[stat.provider] = {
            "requests": stat.count,
            "input_tokens": stat.input_tokens or 0,
            "output_tokens": stat.output_tokens or 0,
            "cost_usd": float(stat.cost) if stat.cost else 0.0
        }

    # By model
    by_model = {}
    model_stats = base_query.with_entities(
        CloudLLMUsage.model,
        func.count(CloudLLMUsage.id).label("count"),
        func.sum(CloudLLMUsage.input_tokens).label("input_tokens"),
        func.sum(CloudLLMUsage.output_tokens).label("output_tokens"),
        func.sum(CloudLLMUsage.cost_usd).label("cost")
    ).group_by(CloudLLMUsage.model).all()

    for stat in model_stats:
        by_model[stat.model] = {
            "requests": stat.count,
            "input_tokens": stat.input_tokens or 0,
            "output_tokens": stat.output_tokens or 0,
            "cost_usd": float(stat.cost) if stat.cost else 0.0
        }

    # By zone
    by_zone = {}
    zone_stats = base_query.with_entities(
        CloudLLMUsage.zone,
        func.count(CloudLLMUsage.id).label("count"),
        func.sum(CloudLLMUsage.cost_usd).label("cost")
    ).filter(CloudLLMUsage.zone.isnot(None)).group_by(CloudLLMUsage.zone).all()

    for stat in zone_stats:
        by_zone[stat.zone] = {
            "requests": stat.count,
            "cost_usd": float(stat.cost) if stat.cost else 0.0
        }

    return {
        "period": period,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "total_requests": totals.count or 0,
        "total_input_tokens": totals.input_tokens or 0,
        "total_output_tokens": totals.output_tokens or 0,
        "total_cost_usd": float(totals.cost) if totals.cost else 0.0,
        "avg_latency_ms": float(totals.avg_latency) if totals.avg_latency else None,
        "by_provider": by_provider,
        "by_model": by_model,
        "by_zone": by_zone
    }


# =============================================================================
# Cost Alerting Routes
# =============================================================================

@router.get("/alerts")
async def get_cost_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """
    Get current cost alerts based on configured thresholds.

    Default thresholds:
    - Daily: $10 warning, $20 critical
    - Monthly: $100 warning, $200 critical
    """
    # TODO: Make thresholds configurable via system settings
    daily_warning = 10.0
    daily_critical = 20.0
    monthly_warning = 100.0
    monthly_critical = 200.0

    alerts = []

    # Check daily spend
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_cost = db.query(func.sum(CloudLLMUsage.cost_usd)).filter(
        CloudLLMUsage.timestamp >= today_start
    ).scalar() or 0.0

    if daily_cost >= daily_critical:
        alerts.append({
            "level": "critical",
            "message": f"Daily cloud LLM spend (${daily_cost:.2f}) exceeds critical threshold (${daily_critical:.2f})",
            "current_spend": float(daily_cost),
            "threshold": daily_critical,
            "period": "daily"
        })
    elif daily_cost >= daily_warning:
        alerts.append({
            "level": "warning",
            "message": f"Daily cloud LLM spend (${daily_cost:.2f}) exceeds warning threshold (${daily_warning:.2f})",
            "current_spend": float(daily_cost),
            "threshold": daily_warning,
            "period": "daily"
        })

    # Check monthly spend
    month_start = datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    monthly_cost = db.query(func.sum(CloudLLMUsage.cost_usd)).filter(
        CloudLLMUsage.timestamp >= month_start
    ).scalar() or 0.0

    if monthly_cost >= monthly_critical:
        alerts.append({
            "level": "critical",
            "message": f"Monthly cloud LLM spend (${monthly_cost:.2f}) exceeds critical threshold (${monthly_critical:.2f})",
            "current_spend": float(monthly_cost),
            "threshold": monthly_critical,
            "period": "monthly"
        })
    elif monthly_cost >= monthly_warning:
        alerts.append({
            "level": "warning",
            "message": f"Monthly cloud LLM spend (${monthly_cost:.2f}) exceeds warning threshold (${monthly_warning:.2f})",
            "current_spend": float(monthly_cost),
            "threshold": monthly_warning,
            "period": "monthly"
        })

    return {
        "alerts": alerts,
        "daily": {
            "spend": float(daily_cost),
            "threshold": daily_warning,
            "percent": (daily_cost / daily_warning * 100) if daily_warning > 0 else 0
        },
        "monthly": {
            "spend": float(monthly_cost),
            "threshold": monthly_warning,
            "percent": (monthly_cost / monthly_warning * 100) if monthly_warning > 0 else 0
        }
    }


# =============================================================================
# Analytics Routes
# =============================================================================

@router.get("/analytics/daily")
async def get_daily_analytics(
    days: int = Query(7, ge=1, le=90, description="Number of days"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get daily usage analytics for charting."""
    start = datetime.now(timezone.utc) - timedelta(days=days)

    # Group by date
    daily_stats = db.query(
        func.date(CloudLLMUsage.timestamp).label("date"),
        func.count(CloudLLMUsage.id).label("requests"),
        func.sum(CloudLLMUsage.input_tokens).label("input_tokens"),
        func.sum(CloudLLMUsage.output_tokens).label("output_tokens"),
        func.sum(CloudLLMUsage.cost_usd).label("cost")
    ).filter(
        CloudLLMUsage.timestamp >= start
    ).group_by(
        func.date(CloudLLMUsage.timestamp)
    ).order_by(
        func.date(CloudLLMUsage.timestamp)
    ).all()

    return [
        {
            "date": stat.date.isoformat() if stat.date else None,
            "requests": stat.requests,
            "input_tokens": stat.input_tokens or 0,
            "output_tokens": stat.output_tokens or 0,
            "cost_usd": float(stat.cost) if stat.cost else 0.0
        }
        for stat in daily_stats
    ]


@router.get("/analytics/hourly")
async def get_hourly_analytics(
    hours: int = Query(24, ge=1, le=168, description="Number of hours"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get hourly usage analytics."""
    start = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Group by hour
    hourly_stats = db.query(
        func.date_trunc('hour', CloudLLMUsage.timestamp).label("hour"),
        func.count(CloudLLMUsage.id).label("requests"),
        func.sum(CloudLLMUsage.cost_usd).label("cost"),
        func.avg(CloudLLMUsage.latency_ms).label("avg_latency")
    ).filter(
        CloudLLMUsage.timestamp >= start
    ).group_by(
        func.date_trunc('hour', CloudLLMUsage.timestamp)
    ).order_by(
        func.date_trunc('hour', CloudLLMUsage.timestamp)
    ).all()

    return [
        {
            "hour": stat.hour.isoformat() if stat.hour else None,
            "requests": stat.requests,
            "cost_usd": float(stat.cost) if stat.cost else 0.0,
            "avg_latency_ms": float(stat.avg_latency) if stat.avg_latency else None
        }
        for stat in hourly_stats
    ]


@router.get("/analytics/by-intent")
async def get_intent_analytics(
    days: int = Query(7, ge=1, le=90, description="Number of days"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_optional_user)
):
    """Get usage breakdown by intent."""
    start = datetime.now(timezone.utc) - timedelta(days=days)

    intent_stats = db.query(
        CloudLLMUsage.intent,
        func.count(CloudLLMUsage.id).label("requests"),
        func.sum(CloudLLMUsage.cost_usd).label("cost"),
        func.avg(CloudLLMUsage.latency_ms).label("avg_latency")
    ).filter(
        CloudLLMUsage.timestamp >= start,
        CloudLLMUsage.intent.isnot(None)
    ).group_by(
        CloudLLMUsage.intent
    ).order_by(
        desc(func.count(CloudLLMUsage.id))
    ).limit(20).all()

    return [
        {
            "intent": stat.intent,
            "requests": stat.requests,
            "cost_usd": float(stat.cost) if stat.cost else 0.0,
            "avg_latency_ms": float(stat.avg_latency) if stat.avg_latency else None
        }
        for stat in intent_stats
    ]


# =============================================================================
# Admin Routes
# =============================================================================

@router.delete("/purge")
async def purge_old_usage(
    days: int = Query(90, ge=30, le=365, description="Keep records newer than this"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Purge usage records older than specified days.

    Requires admin permission. Used for database maintenance.
    """
    if not current_user.has_permission('delete'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    deleted = db.query(CloudLLMUsage).filter(
        CloudLLMUsage.timestamp < cutoff
    ).delete(synchronize_session=False)

    db.commit()

    logger.info(
        "cloud_usage_purged",
        deleted_count=deleted,
        cutoff=cutoff.isoformat(),
        user=current_user.username
    )

    return {"deleted": deleted, "cutoff": cutoff.isoformat()}


# =============================================================================
# Cost Alerting Routes
# =============================================================================

@router.get("/cost-alerts")
async def get_cost_alerts(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get current cost threshold status and any active alerts.

    Returns daily and monthly spending with threshold comparisons.
    Generates alerts when thresholds are exceeded or approaching.
    """
    from app.utils.cost_alerting import check_cost_thresholds, get_cost_projection

    alerts = check_cost_thresholds(db)
    projection = get_cost_projection(db)

    return {
        **alerts,
        "projection": projection
    }


@router.get("/cost-breakdown")
async def get_cost_breakdown(
    period: str = Query("today", regex="^(today|week|month)$"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get cost breakdown by provider for the specified period.

    Args:
        period: 'today', 'week', or 'month'

    Returns per-provider spending data.
    """
    from app.utils.cost_alerting import get_provider_breakdown

    breakdown = get_provider_breakdown(db, period)
    return {
        "period": period,
        "providers": breakdown,
        "total_cost": sum(p["total_cost"] for p in breakdown),
        "total_requests": sum(p["request_count"] for p in breakdown)
    }
