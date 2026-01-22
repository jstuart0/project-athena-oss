"""
Integration status routes.

Provides status endpoints for external service integrations:
- Voice: LiveKit
- Communication: SMS/Twilio
- Scheduling: Calendar APIs
- Location: Google Maps/Directions
- RAG Services: Weather, Sports, Dining, News, Stocks, Flights
"""
from typing import Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
import structlog
import httpx
import asyncpg
import os

from app.database import get_db
from app.auth.oidc import get_current_user
from app.models import User, ExternalAPIKey

logger = structlog.get_logger()

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


# =============================================================================
# Response Models
# =============================================================================

class IntegrationStatus(BaseModel):
    """Status response for an integration."""
    id: str
    name: str
    status: str  # connected, not_configured, error, unknown
    message: Optional[str] = None
    details: Optional[Dict[str, Any]] = None


# =============================================================================
# Integration Configuration
# =============================================================================

# Map integration IDs to their configuration
INTEGRATIONS = {
    # Voice
    "livekit": {
        "name": "LiveKit",
        "type": "api_key",
        "service_name": "livekit",  # Key in external_api_keys table
        "health_check": None,  # No direct health check available
    },

    # Communication
    "sms": {
        "name": "SMS (Twilio)",
        "type": "api_key",
        "service_name": "twilio",
        "health_check": None,
    },

    # Scheduling
    "calendar": {
        "name": "Google Calendar",
        "type": "api_key",
        "service_name": "google-calendar",
        "health_check": None,
    },

    # Location
    "directions": {
        "name": "Google Maps",
        "type": "api_key",
        "service_name": "google-maps",
        "health_check": None,
    },

    # RAG Services
    "weather": {
        "name": "Weather",
        "type": "rag_service",
        "rag_service_name": "weather",
        "port": 8010,
    },
    "sports": {
        "name": "Sports",
        "type": "rag_service",
        "rag_service_name": "sports",
        "port": 8017,
    },
    "dining": {
        "name": "Dining",
        "type": "rag_service",
        "rag_service_name": "dining",
        "port": 8019,
    },
    "news": {
        "name": "News",
        "type": "rag_service",
        "rag_service_name": "news",
        "port": 8015,
    },
    "stocks": {
        "name": "Stocks",
        "type": "rag_service",
        "rag_service_name": "stocks",
        "port": 8014,
    },
    "flights": {
        "name": "Flights",
        "type": "rag_service",
        "rag_service_name": "flights",
        "port": 8011,
    },
}

# RAG service host - configurable via environment
RAG_SERVICE_HOST = os.getenv("RAG_SERVICE_HOST", "localhost")


# =============================================================================
# Helper Functions
# =============================================================================

async def check_api_key_status(service_name: str, db: Session) -> Dict[str, Any]:
    """Check if an API key is configured for a service."""
    key = db.query(ExternalAPIKey).filter(
        ExternalAPIKey.service_name == service_name
    ).first()

    if not key:
        return {"configured": False, "enabled": False}

    return {
        "configured": True,
        "enabled": key.enabled,
        "has_key": bool(key.api_key_encrypted),
        "endpoint_url": key.endpoint_url,
    }


async def check_rag_service_health(service_name: str, port: int) -> Dict[str, Any]:
    """Check health of a RAG service."""
    health_url = f"http://{RAG_SERVICE_HOST}:{port}/health"

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(health_url)
            if response.status_code == 200:
                return {
                    "healthy": True,
                    "status_code": response.status_code,
                    "endpoint": f"http://{MAC_STUDIO_IP}:{port}",
                }
            else:
                return {
                    "healthy": False,
                    "status_code": response.status_code,
                    "error": f"Service returned {response.status_code}",
                }
    except httpx.ConnectError:
        return {
            "healthy": False,
            "error": "Service not running",
        }
    except httpx.TimeoutException:
        return {
            "healthy": False,
            "error": "Service timeout",
        }
    except Exception as e:
        return {
            "healthy": False,
            "error": str(e),
        }


async def get_rag_service_from_registry(service_name: str) -> Optional[Dict[str, Any]]:
    """Get RAG service info from the service registry database."""
    password = os.getenv('ATHENA_DB_PASSWORD')
    if not password:
        return None

    try:
        conn = await asyncpg.connect(
            host=os.getenv('ATHENA_DB_HOST', 'localhost'),
            port=int(os.getenv('ATHENA_DB_PORT', '5432')),
            user=os.getenv('ATHENA_DB_USER', 'psadmin'),
            password=password,
            database=os.getenv('ATHENA_DB_NAME', 'athena')
        )

        try:
            row = await conn.fetchrow("""
                SELECT name, display_name, endpoint_url, enabled
                FROM rag_services
                WHERE name = $1
            """, service_name)

            if row:
                return dict(row)
            return None
        finally:
            await conn.close()
    except Exception as e:
        logger.error("rag_registry_lookup_failed", service=service_name, error=str(e))
        return None


# =============================================================================
# Status Endpoints
# =============================================================================

@router.get("/{integration_id}/status", response_model=IntegrationStatus)
async def get_integration_status(
    integration_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get status for a specific integration.

    Returns:
        - connected: Integration is configured and working
        - not_configured: Integration needs to be configured
        - error: Integration is configured but not working
        - unknown: Unable to determine status
    """
    if integration_id not in INTEGRATIONS:
        raise HTTPException(status_code=404, detail=f"Unknown integration: {integration_id}")

    config = INTEGRATIONS[integration_id]

    try:
        if config["type"] == "api_key":
            # Check external_api_keys table
            key_status = await check_api_key_status(config["service_name"], db)

            if not key_status["configured"]:
                return IntegrationStatus(
                    id=integration_id,
                    name=config["name"],
                    status="not_configured",
                    message="API key not configured",
                    details=key_status
                )

            if not key_status["enabled"]:
                return IntegrationStatus(
                    id=integration_id,
                    name=config["name"],
                    status="not_configured",
                    message="Integration disabled",
                    details=key_status
                )

            return IntegrationStatus(
                id=integration_id,
                name=config["name"],
                status="connected",
                message="API key configured",
                details=key_status
            )

        elif config["type"] == "rag_service":
            # Check RAG service health
            health = await check_rag_service_health(
                config["rag_service_name"],
                config["port"]
            )

            # Also check service registry
            registry_info = await get_rag_service_from_registry(config["rag_service_name"])

            if health["healthy"]:
                return IntegrationStatus(
                    id=integration_id,
                    name=config["name"],
                    status="connected",
                    message="Service running",
                    details={
                        "health": health,
                        "registry": registry_info
                    }
                )
            else:
                return IntegrationStatus(
                    id=integration_id,
                    name=config["name"],
                    status="error",
                    message=health.get("error", "Service unavailable"),
                    details={
                        "health": health,
                        "registry": registry_info
                    }
                )

        else:
            return IntegrationStatus(
                id=integration_id,
                name=config["name"],
                status="unknown",
                message="Unknown integration type"
            )

    except Exception as e:
        logger.error("integration_status_check_failed",
                    integration=integration_id, error=str(e))
        return IntegrationStatus(
            id=integration_id,
            name=config["name"],
            status="unknown",
            message=f"Status check failed: {str(e)}"
        )


@router.get("/", response_model=list[IntegrationStatus])
async def get_all_integration_statuses(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """Get status for all integrations."""
    statuses = []

    for integration_id in INTEGRATIONS:
        try:
            status = await get_integration_status(integration_id, db, current_user)
            statuses.append(status)
        except Exception as e:
            logger.error("integration_status_failed",
                        integration=integration_id, error=str(e))
            statuses.append(IntegrationStatus(
                id=integration_id,
                name=INTEGRATIONS[integration_id]["name"],
                status="unknown",
                message=str(e)
            ))

    return statuses
