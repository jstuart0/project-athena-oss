"""
Cost Alerting for Cloud LLM Usage

Monitors cloud LLM spending and generates alerts when thresholds are exceeded.
Supports configurable daily and monthly limits via feature flags.

Open Source Compatible - Uses standard SQLAlchemy and PostgreSQL.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func, text
import structlog

logger = structlog.get_logger(__name__)

# Alert thresholds (configurable via feature flags)
DEFAULT_DAILY_THRESHOLD = Decimal("10.00")    # $10/day
DEFAULT_MONTHLY_THRESHOLD = Decimal("100.00")  # $100/month
DEFAULT_HOURLY_RATE_THRESHOLD = Decimal("5.00")  # $5/hour sustained


def check_cost_thresholds(db: Session) -> Dict[str, Any]:
    """
    Check if cost thresholds have been exceeded.

    Calculates current spending against configured thresholds
    and generates appropriate alerts.

    Args:
        db: Database session

    Returns:
        Dict with spending data and any active alerts
    """
    from ..models import CloudLLMUsage

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hour_ago = now.replace(minute=0, second=0, microsecond=0)

    # Get configured thresholds from feature flags
    daily_threshold = Decimal(_get_feature_value(db, "cloud_llm_daily_cost_limit", str(DEFAULT_DAILY_THRESHOLD)))
    monthly_threshold = Decimal(_get_feature_value(db, "cloud_llm_monthly_cost_limit", str(DEFAULT_MONTHLY_THRESHOLD)))
    hourly_rate_threshold = Decimal(_get_feature_value(db, "cloud_llm_hourly_rate_limit", str(DEFAULT_HOURLY_RATE_THRESHOLD)))

    # Calculate today's spend
    daily_spend = db.query(func.coalesce(func.sum(CloudLLMUsage.cost_usd), 0)).filter(
        CloudLLMUsage.timestamp >= today_start
    ).scalar() or Decimal("0")

    # Calculate month's spend
    monthly_spend = db.query(func.coalesce(func.sum(CloudLLMUsage.cost_usd), 0)).filter(
        CloudLLMUsage.timestamp >= month_start
    ).scalar() or Decimal("0")

    # Calculate last hour's spend (for rate alerting)
    hourly_spend = db.query(func.coalesce(func.sum(CloudLLMUsage.cost_usd), 0)).filter(
        CloudLLMUsage.timestamp >= hour_ago
    ).scalar() or Decimal("0")

    # Ensure we have Decimals
    daily_spend = Decimal(str(daily_spend))
    monthly_spend = Decimal(str(monthly_spend))
    hourly_spend = Decimal(str(hourly_spend))

    # Check thresholds
    daily_exceeded = daily_spend >= daily_threshold
    monthly_exceeded = monthly_spend >= monthly_threshold
    hourly_rate_exceeded = hourly_spend >= hourly_rate_threshold

    # Calculate percentages
    daily_percent = float((daily_spend / daily_threshold * 100) if daily_threshold > 0 else 0)
    monthly_percent = float((monthly_spend / monthly_threshold * 100) if monthly_threshold > 0 else 0)

    result = {
        "daily": {
            "spend": float(daily_spend),
            "threshold": float(daily_threshold),
            "percent": daily_percent,
            "exceeded": daily_exceeded,
        },
        "monthly": {
            "spend": float(monthly_spend),
            "threshold": float(monthly_threshold),
            "percent": monthly_percent,
            "exceeded": monthly_exceeded,
        },
        "hourly_rate": {
            "spend": float(hourly_spend),
            "threshold": float(hourly_rate_threshold),
            "exceeded": hourly_rate_exceeded,
        },
        "alerts": [],
        "checked_at": now.isoformat(),
    }

    # Generate alerts
    if daily_exceeded:
        result["alerts"].append({
            "level": "critical",
            "type": "daily_limit_exceeded",
            "message": f"Daily cloud LLM spend (${daily_spend:.2f}) exceeded threshold (${daily_threshold:.2f})",
        })
        logger.warning(
            "cost_alert_daily_exceeded",
            spend=float(daily_spend),
            threshold=float(daily_threshold)
        )
    elif daily_percent >= 80:
        result["alerts"].append({
            "level": "warning",
            "type": "daily_limit_warning",
            "message": f"Daily cloud LLM spend at {daily_percent:.0f}% of threshold (${daily_spend:.2f}/${daily_threshold:.2f})",
        })

    if monthly_exceeded:
        result["alerts"].append({
            "level": "critical",
            "type": "monthly_limit_exceeded",
            "message": f"Monthly cloud LLM spend (${monthly_spend:.2f}) exceeded threshold (${monthly_threshold:.2f})",
        })
        logger.warning(
            "cost_alert_monthly_exceeded",
            spend=float(monthly_spend),
            threshold=float(monthly_threshold)
        )
    elif monthly_percent >= 80:
        result["alerts"].append({
            "level": "warning",
            "type": "monthly_limit_warning",
            "message": f"Monthly cloud LLM spend at {monthly_percent:.0f}% of threshold (${monthly_spend:.2f}/${monthly_threshold:.2f})",
        })

    if hourly_rate_exceeded:
        result["alerts"].append({
            "level": "warning",
            "type": "high_hourly_rate",
            "message": f"High hourly spend rate: ${hourly_spend:.2f}/hour (threshold: ${hourly_rate_threshold:.2f}/hour)",
        })
        logger.warning(
            "cost_alert_high_hourly_rate",
            spend=float(hourly_spend),
            threshold=float(hourly_rate_threshold)
        )

    return result


def get_cost_projection(db: Session) -> Dict[str, Any]:
    """
    Project costs for the rest of the day/month based on current usage rate.

    Args:
        db: Database session

    Returns:
        Dict with projected costs
    """
    from ..models import CloudLLMUsage

    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Hours elapsed today
    hours_today = (now - today_start).total_seconds() / 3600

    # Days elapsed this month
    days_in_month = (now.replace(month=now.month % 12 + 1, day=1) - now.replace(day=1)).days
    days_elapsed = now.day + (hours_today / 24)

    # Get current spend
    daily_spend = db.query(func.coalesce(func.sum(CloudLLMUsage.cost_usd), 0)).filter(
        CloudLLMUsage.timestamp >= today_start
    ).scalar() or Decimal("0")

    monthly_spend = db.query(func.coalesce(func.sum(CloudLLMUsage.cost_usd), 0)).filter(
        CloudLLMUsage.timestamp >= month_start
    ).scalar() or Decimal("0")

    daily_spend = Decimal(str(daily_spend))
    monthly_spend = Decimal(str(monthly_spend))

    # Calculate projected amounts
    if hours_today > 0:
        hourly_rate = float(daily_spend) / hours_today
        projected_daily = hourly_rate * 24
    else:
        projected_daily = 0

    if days_elapsed > 0:
        daily_rate = float(monthly_spend) / days_elapsed
        projected_monthly = daily_rate * days_in_month
    else:
        projected_monthly = 0

    return {
        "current_daily": float(daily_spend),
        "projected_daily": projected_daily,
        "current_monthly": float(monthly_spend),
        "projected_monthly": projected_monthly,
        "hours_elapsed_today": hours_today,
        "days_elapsed_month": days_elapsed,
        "days_in_month": days_in_month,
        "projected_at": now.isoformat(),
    }


def get_provider_breakdown(db: Session, period: str = "today") -> List[Dict[str, Any]]:
    """
    Get cost breakdown by provider for the specified period.

    Args:
        db: Database session
        period: "today", "week", or "month"

    Returns:
        List of dicts with provider spending data
    """
    from ..models import CloudLLMUsage

    now = datetime.now(timezone.utc)

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.replace(day=start.day - start.weekday())
    else:  # month
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    results = db.query(
        CloudLLMUsage.provider,
        func.count(CloudLLMUsage.id).label('request_count'),
        func.sum(CloudLLMUsage.input_tokens).label('total_input_tokens'),
        func.sum(CloudLLMUsage.output_tokens).label('total_output_tokens'),
        func.sum(CloudLLMUsage.cost_usd).label('total_cost'),
    ).filter(
        CloudLLMUsage.timestamp >= start
    ).group_by(
        CloudLLMUsage.provider
    ).all()

    return [
        {
            "provider": r.provider,
            "request_count": r.request_count or 0,
            "total_input_tokens": r.total_input_tokens or 0,
            "total_output_tokens": r.total_output_tokens or 0,
            "total_cost": float(r.total_cost or 0),
        }
        for r in results
    ]


def _get_feature_value(db: Session, flag_name: str, default: str) -> str:
    """
    Get feature flag value or default.

    Args:
        db: Database session
        flag_name: Feature flag name
        default: Default value if flag not found

    Returns:
        Feature flag value or default
    """
    from ..models import Feature

    try:
        flag = db.query(Feature).filter(Feature.name == flag_name).first()
        if flag and flag.enabled:
            # Check if there's a custom value stored
            if hasattr(flag, 'value') and flag.value:
                return flag.value
            return default
    except Exception as e:
        logger.warning(f"Failed to get feature flag {flag_name}: {e}")

    return default
