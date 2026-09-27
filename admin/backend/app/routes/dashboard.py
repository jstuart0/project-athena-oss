"""
Dashboard API - Consolidated endpoint for Mission Control.

Aggregates data from multiple sources in a single request to reduce
browser polling overhead and provide time-series for sparklines.
"""
import os
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
import structlog

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, PipelineEvent, Alert, ExternalAPIKey, Feature, ConversationAnalytics, RagService
from app.utils.rag_urls import check_ssrf_safe
import httpx

logger = structlog.get_logger()

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

# Service URLs — configurable via env vars.
# GATEWAY_URL and ORCHESTRATOR_URL are set by k8s deployments to cluster services.
_GATEWAY_BASE = os.getenv("GATEWAY_URL", "http://localhost:8000").rstrip("/")
_ORCHESTRATOR_BASE = os.getenv("ORCHESTRATOR_URL", "http://localhost:8001").rstrip("/")

# Core (non-RAG) services shown on the voice-health card, keyed by their
# service-registry row name.
_CORE_SERVICE_NAMES = {
    "gateway": "Gateway",
    "orchestrator": "Orchestrator",
}


def _registry_row_status(svc: RagService) -> str:
    """Map a registry row to the voice-health card's status vocabulary.

    A disabled row is reported 'disabled' regardless of its last cached
    health_status (which goes stale the moment the row is disabled and the
    poller stops touching it) -- same convention as ATHENA-112/113c.
    'unconfigured' (the poller's own "reachable but not configured" state)
    counts as needing attention -- it appears in critical_services and is
    excluded from healthy_count -- but keeps its own literal status string
    rather than being relabeled 'unhealthy'; the two are different claims
    (this service isn't set up vs. this service is failing).
    """
    if not svc.enabled:
        return "disabled"
    if svc.health_status is None:
        return "pending"
    return svc.health_status  # healthy | unhealthy | unconfigured


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
    if not current_user.has_permission('read:dashboard'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    now = datetime.utcnow()

    # 1. Voice Health - Check actual service health
    #
    # Registry-driven (this used to hard-code exactly Gateway, Orchestrator,
    # and 3 named RAGs -- arbitrary, from the initial OSS commit, and stale
    # the moment an operator added or removed a RAG service). Core services
    # (gateway/orchestrator) read their registry row if one exists, falling
    # back to a gated live probe of GATEWAY_URL/ORCHESTRATOR_URL only when no
    # row is registered. Every ENABLED registry row with service_type='rag'
    # is included -- disabled rows are excluded entirely (not counted, not
    # shown), matching ATHENA-112's enabled-only convention.
    core_rows = {
        r.name: r for r in db.query(RagService).filter(
            RagService.name.in_(_CORE_SERVICE_NAMES.keys())
        ).all()
    }
    rag_rows = db.query(RagService).filter(
        RagService.service_type == "rag", RagService.enabled == True
    ).order_by(RagService.name).all()

    service_entries = []  # [{"name", "status", "error"}]

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            for service_key, display_name in _CORE_SERVICE_NAMES.items():
                row = core_rows.get(service_key)
                if row is not None:
                    service_entries.append({
                        "name": display_name,
                        "status": _registry_row_status(row),
                        "error": row.last_error,
                    })
                    continue

                # No registry row -- fall back to a gated live probe.
                base_url = _GATEWAY_BASE if service_key == "gateway" else _ORCHESTRATOR_BASE
                url = f"{base_url}/health"
                # codex BLOCK: GATEWAY_URL/ORCHESTRATOR_URL are operator-set,
                # but DNS can change after write-time validation -- probe
                # through the same SSRF/runtime-DNS allowlist the health
                # poller uses, not an unvalidated direct request.
                allowed, reason = await check_ssrf_safe(url)
                if not allowed:
                    logger.warning("dashboard_voice_health_ssrf_blocked", service=display_name, reason=reason)
                    service_entries.append({"name": display_name, "status": "ssrf_blocked", "error": reason})
                    continue
                try:
                    response = await client.get(url)
                    status_str = "healthy" if response.status_code == 200 else "unhealthy"
                    service_entries.append({"name": display_name, "status": status_str, "error": None})
                except Exception:
                    service_entries.append({"name": display_name, "status": "unreachable", "error": None})

        for svc in rag_rows:
            service_entries.append({
                "name": svc.display_name or svc.name,
                "status": _registry_row_status(svc),
                "error": svc.last_error,
            })

        healthy_count = sum(1 for e in service_entries if e["status"] == "healthy")
        total_count = len(service_entries)
        critical_services = [
            {"name": e["name"], "status": e["status"], "last_error": e["error"]}
            for e in service_entries if e["status"] != "healthy"
        ]

        health_pct = round((healthy_count / total_count * 100) if total_count > 0 else 0)
        health_history = [health_pct] * 20  # Would need time-series tracking for real history
    except Exception as e:
        logger.warning("dashboard_voice_health_error", error=str(e))
        healthy_count = 0
        total_count = len(_CORE_SERVICE_NAMES) + len(rag_rows)
        health_pct = 0
        health_history = [0] * 20
        critical_services = [
            {"name": name, "status": "unknown", "last_error": None}
            for name in _CORE_SERVICE_NAMES.values()
        ]
        critical_services += [
            {"name": svc.display_name or svc.name, "status": "unknown", "last_error": None}
            for svc in rag_rows
        ]
        # Section 5 (service status grid) reads service_entries -- rebuild it
        # fully here too so that grid stays consistent with critical_services
        # when this fallback path is hit.
        service_entries = [{"name": e["name"], "status": e["status"], "error": None} for e in critical_services]

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

    # 5. Service status grid (from actual health checks) -- same set as the
    # voice-health card above (core rows/probes + every enabled RAG row).
    service_status = [
        {"name": e["name"], "status": e["status"], "latency_ms": None}
        for e in service_entries
    ]

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
    if not current_user.has_permission('read:dashboard'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
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
    if not current_user.has_permission('read:dashboard'):
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    now = datetime.utcnow()

    # Service health - quick check of core services
    core_services = [
        f"{_GATEWAY_BASE}/health",      # Gateway
        f"{_ORCHESTRATOR_BASE}/health", # Orchestrator
    ]
    healthy, total = 0, len(core_services)
    try:
        async with httpx.AsyncClient(timeout=1.0) as client:
            for url in core_services:
                try:
                    # codex r2 delta: GATEWAY_URL/ORCHESTRATOR_URL are
                    # operator-set, but that's a write-time trust decision
                    # only -- gate the probe through the same SSRF/runtime-
                    # DNS allowlist the voice-health card and the health
                    # poller use, same as get_dashboard_data above.
                    allowed, reason = await check_ssrf_safe(url)
                    if not allowed:
                        logger.warning("dashboard_quick_stats_ssrf_blocked", url_status="ssrf_blocked", reason=reason)
                        continue
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
