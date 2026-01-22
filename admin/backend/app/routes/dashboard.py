import os
"""
Dashboard API - Consolidated endpoint for Mission Control.

Aggregates data from multiple sources in a single request to reduce
browser polling overhead and provide time-series for sparklines.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, PipelineEvent, Alert, AthenaService, ExternalAPIKey, Feature, ConversationAnalytics
import httpx

logger = structlog.get_logger()

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("")
async def get_dashboard_data(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Consolidated dashboard data for Mission Control.

    Returns all data needed to render the dashboard in a single request:
    - Voice health with sparkline history
    - Traffic metrics with time-series
    - Pending actions count
    - Alert summary
    - Service status grid
    """
    now = datetime.utcnow()

    # 1. Voice Health - Check actual service health
    service_host = os.getenv("SERVICE_HOST", "localhost")
    core_services = [
        {"name": "Gateway", "url": f"http://{service_host}:8000/health", "critical": True},
        {"name": "Orchestrator", "url": f"http://{service_host}:8001/health", "critical": True},
        {"name": "Weather RAG", "url": f"http://{service_host}:8010/health", "critical": False},
        {"name": "Sports RAG", "url": f"http://{service_host}:8017/health", "critical": False},
        {"name": "Dining RAG", "url": f"http://{service_host}:8019/health", "critical": False},
    ]

    healthy_count = 0
    total_count = len(core_services)
    critical_services = []

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            for svc in core_services:
                try:
                    response = await client.get(svc["url"])
                    if response.status_code == 200:
                        healthy_count += 1
                    else:
                        critical_services.append({"name": svc["name"], "status": "unhealthy"})
                except Exception:
                    critical_services.append({"name": svc["name"], "status": "unreachable"})

        health_pct = round((healthy_count / total_count * 100) if total_count > 0 else 0)
        health_history = [health_pct] * 20  # Would need time-series tracking for real history
    except Exception as e:
        logger.warning("dashboard_voice_health_error", error=str(e))
        healthy_count = 0
        health_pct = 0
        health_history = [0] * 20
        critical_services = [{"name": s["name"], "status": "unknown"} for s in core_services]

    # 2. Traffic Metrics - From conversation_analytics table
    try:
        two_hours_ago = now - timedelta(hours=2)

        # Get events per 5-minute bucket for sparkline (query_intent events = queries)
        traffic_query = db.query(
            func.date_trunc('hour', ConversationAnalytics.timestamp).label('bucket'),
            func.count(ConversationAnalytics.id).label('count')
        ).filter(
            ConversationAnalytics.timestamp > two_hours_ago,
            ConversationAnalytics.event_type == 'query_intent'
        ).group_by('bucket').order_by('bucket').all()

        traffic_history = [row.count for row in traffic_query][-20:] if traffic_query else [0] * 20
        # Pad to 20 points if needed
        while len(traffic_history) < 20:
            traffic_history.insert(0, 0)

        # Calculate requests per minute (last 5 minutes)
        five_min_ago = now - timedelta(minutes=5)
        recent_events = db.query(func.count(ConversationAnalytics.id)).filter(
            ConversationAnalytics.timestamp > five_min_ago,
            ConversationAnalytics.event_type == 'query_intent'
        ).scalar() or 0
        requests_per_minute = round(recent_events / 5, 1)

        # Total last 24h
        day_ago = now - timedelta(hours=24)
        total_24h = db.query(func.count(ConversationAnalytics.id)).filter(
            ConversationAnalytics.timestamp > day_ago,
            ConversationAnalytics.event_type == 'query_intent'
        ).scalar() or 0
    except Exception as e:
        logger.warning("dashboard_traffic_error", error=str(e))
        traffic_history = [0] * 20
        requests_per_minute = 0
        total_24h = 0

    # 3. Pending Actions - Things that need operator attention
    pending_actions = []

    # Check for unhealthy services
    if critical_services:
        pending_actions.append({
            "type": "unhealthy_services",
            "message": f"{len(critical_services)} services need attention",
            "count": len(critical_services),
            "action": "service-control",
            "severity": "critical" if len(critical_services) >= 3 else "warning"
        })

    # Check for disabled high-priority features
    try:
        disabled_features = db.query(func.count(Feature.id)).filter(
            Feature.enabled == False,
            Feature.priority >= 80
        ).scalar() or 0
        if disabled_features > 0:
            pending_actions.append({
                "type": "disabled_features",
                "message": f"{disabled_features} high-priority features disabled",
                "count": disabled_features,
                "action": "features",
                "severity": "info"
            })
    except Exception:
        pass  # Features table might not exist

    # Check for active alerts
    try:
        active_alert_count = db.query(func.count(Alert.id)).filter(
            Alert.status == 'active'
        ).scalar() or 0
        if active_alert_count > 0:
            pending_actions.append({
                "type": "active_alerts",
                "message": f"{active_alert_count} active alerts",
                "count": active_alert_count,
                "action": "alerts",
                "severity": "warning"
            })
    except Exception:
        pass

    # 4. Alert Summary
    try:
        alert_query = db.query(Alert).filter(Alert.status == 'active').all()
        alert_summary = {
            "total": len(alert_query),
            "critical": sum(1 for a in alert_query if a.severity == 'critical'),
            "warning": sum(1 for a in alert_query if a.severity == 'warning'),
            "info": sum(1 for a in alert_query if a.severity == 'info'),
            "recent": [
                {
                    "id": a.id,
                    "title": a.title if hasattr(a, 'title') else str(a.alert_type),
                    "severity": a.severity,
                    "created_at": a.created_at.isoformat() if a.created_at else None
                }
                for a in sorted(alert_query, key=lambda x: x.created_at or datetime.min, reverse=True)[:3]
            ]
        }
    except Exception as e:
        logger.warning("dashboard_alerts_error", error=str(e))
        alert_summary = {"total": 0, "critical": 0, "warning": 0, "info": 0, "recent": []}

    # 5. Service status grid (from actual health checks)
    service_status = []
    for svc in core_services:
        is_healthy = svc["name"] not in [c["name"] for c in critical_services]
        service_status.append({
            "name": svc["name"],
            "status": "healthy" if is_healthy else "unhealthy",
            "latency_ms": None
        })

    logger.info("dashboard_data_fetched", user=current_user.username,
                healthy=healthy_count, total=total_count,
                pending_actions=len(pending_actions))

    return {
        "timestamp": now.isoformat(),
        "voice_health": {
            "healthy": healthy_count,
            "total": total_count,
            "percentage": health_pct,
            "history": health_history,
            "critical_services": critical_services
        },
        "traffic": {
            "requests_per_minute": requests_per_minute,
            "total_24h": total_24h,
            "history": traffic_history
        },
        "pending_actions": pending_actions,
        "alerts": alert_summary,
        "services": service_status
    }


@router.get("/integrations")
async def get_integration_statuses(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get status of all configured integrations.

    Checks: LiveKit, SMS (Twilio), Calendar, Weather API, etc.
    """
    integrations = []

    # Define integration providers to check
    providers = [
        {"id": "livekit", "name": "LiveKit", "service_name": "livekit", "category": "Voice"},
        {"id": "twilio", "name": "SMS (Twilio)", "service_name": "twilio", "category": "Communication"},
        {"id": "google_calendar", "name": "Google Calendar", "service_name": "google-calendar", "category": "Scheduling"},
        {"id": "openweathermap", "name": "Weather", "service_name": "openweathermap", "category": "RAG"},
        {"id": "google_places", "name": "Google Places", "service_name": "google-places", "category": "RAG"},
        {"id": "sports_api", "name": "Sports API", "service_name": "sports-api", "category": "RAG"},
    ]

    for provider in providers:
        try:
            # Check if API key exists and is enabled
            api_key = db.query(ExternalAPIKey).filter(
                ExternalAPIKey.service_name == provider["service_name"],
                ExternalAPIKey.enabled == True
            ).first()

            status = "connected" if api_key else "not_configured"
            last_used = api_key.last_used.isoformat() if api_key and api_key.last_used else None

            integrations.append({
                "id": provider["id"],
                "name": provider["name"],
                "category": provider["category"],
                "status": status,
                "last_sync": last_used,
                "quota": None  # Would need per-service quota tracking
            })
        except Exception as e:
            logger.warning("integration_status_error", provider=provider["id"], error=str(e))
            integrations.append({
                "id": provider["id"],
                "name": provider["name"],
                "category": provider["category"],
                "status": "error",
                "error": str(e)
            })

    return {"integrations": integrations}


@router.get("/quick-stats")
async def get_quick_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Lightweight stats endpoint for quick dashboard refresh.
    Returns only numerical values, no history arrays.
    """
    now = datetime.utcnow()

    # Service health - quick check of core services
    service_host = os.getenv("SERVICE_HOST", "localhost")
    core_services = [
        f"http://{service_host}:8000/health",  # Gateway
        f"http://{service_host}:8001/health",  # Orchestrator
    ]
    healthy, total = 0, len(core_services)
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            for url in core_services:
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        healthy += 1
                except Exception:
                    pass
    except Exception:
        pass

    # Recent traffic from conversation_analytics
    try:
        five_min_ago = now - timedelta(minutes=5)
        recent = db.query(func.count(ConversationAnalytics.id)).filter(
            ConversationAnalytics.timestamp > five_min_ago,
            ConversationAnalytics.event_type == 'query_intent'
        ).scalar() or 0
        rpm = round(recent / 5, 1)
    except Exception:
        rpm = 0

    # Active alerts
    try:
        alerts = db.query(func.count(Alert.id)).filter(Alert.status == 'active').scalar() or 0
    except Exception:
        alerts = 0

    return {
        "timestamp": now.isoformat(),
        "healthy_services": healthy,
        "total_services": total,
        "requests_per_minute": rpm,
        "active_alerts": alerts
    }
