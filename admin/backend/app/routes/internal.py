"""
Internal Service-to-Service API Routes

These endpoints are designed for internal services to fetch configuration
without user authentication. They should only be accessible from the
internal network (not exposed publicly).

Two databases are used:
- athena: RAG services registry, base_knowledge, hallucination checks, validation
- athena_admin: Conversation settings, clarification, admin UI config

Endpoints:
- /api/internal/config/conversation - Conversation settings (athena_admin)
- /api/internal/config/clarification - Clarification settings (athena_admin)
- /api/internal/config/clarification-types - Clarification types (athena_admin)
- /api/internal/config/sports-teams - Sports team disambiguation (athena_admin)
- /api/internal/config/device-rules - Device disambiguation rules (athena_admin)
- /api/internal/config/multi-intent - Multi-intent config (athena)
- /api/internal/config/intent-chains - Intent chain rules (athena)
- /api/internal/config/hallucination-checks - Hallucination checks (athena)
- /api/internal/config/validation-models - Cross-validation models (athena)
- /api/internal/config/validation-scenarios - Validation test scenarios (athena)
"""
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException
import asyncpg
import os
import structlog

logger = structlog.get_logger()

router = APIRouter(prefix="/api/internal", tags=["internal"], include_in_schema=False)


async def get_athena_db_connection():
    """Get connection to the Athena database (rag_services, validation tables)."""
    password = os.getenv('ATHENA_DB_PASSWORD')
    if not password:
        raise HTTPException(status_code=500, detail="ATHENA_DB_PASSWORD not configured")
    return await asyncpg.connect(
        host=os.getenv('ATHENA_DB_HOST', 'localhost'),
        port=int(os.getenv('ATHENA_DB_PORT', '5432')),
        user=os.getenv('ATHENA_DB_USER', 'psadmin'),
        password=password,
        database=os.getenv('ATHENA_DB_NAME', 'athena')
    )


async def get_admin_db_connection():
    """Get connection to the Admin database (conversation settings, clarification)."""
    password = os.getenv('ATHENA_DB_PASSWORD')
    if not password:
        raise HTTPException(status_code=500, detail="ATHENA_DB_PASSWORD not configured")
    return await asyncpg.connect(
        host=os.getenv('ADMIN_DB_HOST', os.getenv('ATHENA_DB_HOST', 'localhost')),
        port=int(os.getenv('ADMIN_DB_PORT', os.getenv('ATHENA_DB_PORT', '5432'))),
        user=os.getenv('ADMIN_DB_USER', os.getenv('ATHENA_DB_USER', 'psadmin')),
        password=password,
        database=os.getenv('ADMIN_DB_NAME', 'athena_admin')
    )


# =============================================================================
# Conversation Settings (athena_admin database)
# =============================================================================

@router.get("/config/conversation")
async def get_conversation_settings() -> Dict[str, Any]:
    """Get conversation settings for orchestrator."""
    conn = await get_admin_db_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM conversation_settings LIMIT 1")
        if row:
            return dict(row)
        # Return defaults if not found
        return {
            "enabled": True,
            "use_context": True,
            "max_messages": 20,
            "timeout_seconds": 1800,
            "cleanup_interval_seconds": 60,
            "session_ttl_seconds": 3600,
            "max_llm_history_messages": 10,
            "history_mode": "full"
        }
    except Exception as e:
        logger.error("fetch_conversation_settings_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/config/clarification")
async def get_clarification_settings() -> Dict[str, Any]:
    """Get clarification settings for orchestrator."""
    conn = await get_admin_db_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM clarification_settings LIMIT 1")
        if row:
            return dict(row)
        # Return defaults if not found
        return {
            "enabled": True,
            "timeout_seconds": 300
        }
    except Exception as e:
        logger.error("fetch_clarification_settings_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/config/clarification-types")
async def get_clarification_types() -> List[Dict[str, Any]]:
    """Get all clarification types for orchestrator."""
    conn = await get_admin_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT * FROM clarification_types
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("fetch_clarification_types_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# =============================================================================
# Disambiguation Rules (athena_admin database)
# =============================================================================

@router.get("/config/sports-teams")
async def get_sports_teams() -> List[Dict[str, Any]]:
    """Get sports team disambiguation rules."""
    conn = await get_admin_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT * FROM sports_team_disambiguation
            WHERE requires_disambiguation = true
            ORDER BY team_name
        """)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("fetch_sports_teams_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/config/device-rules")
async def get_device_rules() -> List[Dict[str, Any]]:
    """Get device disambiguation rules."""
    conn = await get_admin_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT * FROM device_disambiguation_rules
            WHERE requires_disambiguation = true
            ORDER BY device_type
        """)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("fetch_device_rules_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# =============================================================================
# Multi-Intent Configuration
# =============================================================================

@router.get("/config/multi-intent")
async def get_multi_intent_config() -> Dict[str, Any]:
    """Get multi-intent configuration."""
    conn = await get_athena_db_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM multi_intent_config LIMIT 1")
        if row:
            return dict(row)
        return {}
    except Exception as e:
        logger.error("fetch_multi_intent_config_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/config/intent-chains")
async def get_intent_chain_rules() -> List[Dict[str, Any]]:
    """Get intent chain rules."""
    conn = await get_athena_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT * FROM intent_chain_rules
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("fetch_intent_chain_rules_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# =============================================================================
# Validation Configuration
# =============================================================================

@router.get("/config/hallucination-checks")
async def get_hallucination_checks() -> List[Dict[str, Any]]:
    """Get hallucination check patterns."""
    conn = await get_athena_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT * FROM hallucination_checks
            WHERE enabled = true
            ORDER BY priority DESC, category
        """)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("fetch_hallucination_checks_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/config/validation-models")
async def get_validation_models() -> List[Dict[str, Any]]:
    """Get cross-validation models."""
    conn = await get_athena_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT * FROM cross_validation_models
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("fetch_validation_models_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/config/validation-scenarios")
async def get_validation_scenarios() -> List[Dict[str, Any]]:
    """Get validation test scenarios."""
    conn = await get_athena_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT * FROM validation_test_scenarios
            WHERE enabled = true
            ORDER BY category, name
        """)
        return [dict(row) for row in rows]
    except Exception as e:
        logger.error("fetch_validation_scenarios_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# =============================================================================
# Base Knowledge
# =============================================================================

@router.get("/config/base-knowledge")
async def get_base_knowledge() -> Dict[str, Any]:
    """Get base knowledge configuration (default location, user context)."""
    conn = await get_athena_db_connection()
    try:
        row = await conn.fetchrow("SELECT * FROM base_knowledge LIMIT 1")
        if row:
            return dict(row)
        # Return default values if no config exists
        return {
            "default_location": "Baltimore, MD",
            "user_name": None,
            "preferences": {}
        }
    except Exception as e:
        logger.error("fetch_base_knowledge_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# =============================================================================
# Bundled Config (fetch all at once for efficiency)
# =============================================================================

@router.get("/config/all")
async def get_all_config() -> Dict[str, Any]:
    """
    Get all configuration in a single request.
    More efficient for orchestrator startup than multiple requests.

    Queries from both databases:
    - athena_admin: conversation settings, clarification, disambiguation
    - athena: multi-intent, intent chains, base knowledge
    """
    result = {}
    admin_conn = None
    athena_conn = None

    try:
        # Get connections to both databases
        admin_conn = await get_admin_db_connection()
        athena_conn = await get_athena_db_connection()

        # ===== athena_admin database =====

        # Conversation settings
        row = await admin_conn.fetchrow("SELECT * FROM conversation_settings LIMIT 1")
        result['conversation_settings'] = dict(row) if row else {
            "enabled": True,
            "use_context": True,
            "max_messages": 20,
            "timeout_seconds": 1800
        }

        # Clarification settings
        row = await admin_conn.fetchrow("SELECT * FROM clarification_settings LIMIT 1")
        result['clarification_settings'] = dict(row) if row else {
            "enabled": True,
            "timeout_seconds": 300
        }

        # Clarification types
        rows = await admin_conn.fetch("""
            SELECT * FROM clarification_types
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        result['clarification_types'] = [dict(row) for row in rows]

        # Sports teams
        rows = await admin_conn.fetch("""
            SELECT * FROM sports_team_disambiguation
            WHERE requires_disambiguation = true
            ORDER BY team_name
        """)
        result['sports_teams'] = [dict(row) for row in rows]

        # Device rules
        rows = await admin_conn.fetch("""
            SELECT * FROM device_disambiguation_rules
            WHERE requires_disambiguation = true
            ORDER BY device_type
        """)
        result['device_rules'] = [dict(row) for row in rows]

        # ===== athena database =====

        # Multi-intent config
        row = await athena_conn.fetchrow("SELECT * FROM multi_intent_config LIMIT 1")
        result['multi_intent_config'] = dict(row) if row else {}

        # Intent chains
        rows = await athena_conn.fetch("""
            SELECT * FROM intent_chain_rules
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        result['intent_chains'] = [dict(row) for row in rows]

        # Base knowledge
        row = await athena_conn.fetchrow("SELECT * FROM base_knowledge LIMIT 1")
        result['base_knowledge'] = dict(row) if row else {
            "default_location": "Baltimore, MD",
            "user_name": None,
            "preferences": {}
        }

        return result

    except Exception as e:
        logger.error("fetch_all_config_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if admin_conn:
            await admin_conn.close()
        if athena_conn:
            await athena_conn.close()


@router.get("/config/rag-services")
async def get_rag_service_urls() -> Dict[str, str]:
    """
    Get RAG service URL map for orchestrator startup.

    Returns a dict mapping service name to endpoint URL.
    Only returns enabled services.

    Used by: Orchestrator RAGClient on startup
    """
    conn = await get_athena_db_connection()
    try:
        rows = await conn.fetch("""
            SELECT name, endpoint_url
            FROM rag_services
            WHERE enabled = true
        """)
        return {row['name']: row['endpoint_url'] for row in rows}
    except Exception as e:
        logger.error("fetch_rag_service_urls_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# =============================================================================
# Analytics Logging (for orchestrator to send events)
# =============================================================================

from pydantic import BaseModel
from datetime import datetime
import json

class AnalyticsEventRequest(BaseModel):
    """Request body for logging an analytics event."""
    session_id: str
    event_type: str
    metadata: Optional[Dict[str, Any]] = None


@router.post("/analytics/log")
async def log_analytics_event(event: AnalyticsEventRequest) -> Dict[str, Any]:
    """
    Log an analytics event from the orchestrator.

    This endpoint is used by the orchestrator to log intent classification
    and other analytics events to the database for later analysis.

    No authentication required - internal service-to-service call.
    """
    from app.routes.websocket import broadcast_to_admin_jarvis
    import time

    conn = await get_admin_db_connection()
    try:
        # Insert into conversation_analytics table
        await conn.execute("""
            INSERT INTO conversation_analytics (session_id, event_type, metadata, timestamp)
            VALUES ($1, $2, $3, $4)
        """, event.session_id, event.event_type, json.dumps(event.metadata) if event.metadata else None, datetime.utcnow())

        logger.info(
            "analytics_event_logged",
            session_id=event.session_id,
            event_type=event.event_type
        )

        # Broadcast to Admin Jarvis WebSocket clients
        try:
            await broadcast_to_admin_jarvis({
                "event_type": event.event_type,
                "session_id": event.session_id,
                "data": event.metadata or {},
                "timestamp": time.time()
            })
            logger.debug("analytics_event_broadcast", event_type=event.event_type)
        except Exception as broadcast_error:
            logger.warning("analytics_broadcast_failed", error=str(broadcast_error))

        return {"status": "logged", "event_type": event.event_type}

    except Exception as e:
        logger.error("log_analytics_event_failed", error=str(e), event_type=event.event_type)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/config/validation-all")
async def get_all_validation_config() -> Dict[str, Any]:
    """
    Get all validation configuration in a single request.
    For db_validator.py startup.
    """
    conn = await get_athena_db_connection()
    try:
        result = {}

        # Hallucination checks
        rows = await conn.fetch("""
            SELECT * FROM hallucination_checks
            WHERE enabled = true
            ORDER BY priority DESC, category
        """)
        result['hallucination_checks'] = [dict(row) for row in rows]

        # Validation models
        rows = await conn.fetch("""
            SELECT * FROM cross_validation_models
            WHERE enabled = true
            ORDER BY priority DESC
        """)
        result['validation_models'] = [dict(row) for row in rows]

        # Test scenarios
        rows = await conn.fetch("""
            SELECT * FROM validation_test_scenarios
            WHERE enabled = true
            ORDER BY category, name
        """)
        result['validation_scenarios'] = [dict(row) for row in rows]

        return result

    except Exception as e:
        logger.error("fetch_validation_config_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


# =============================================================================
# Service Usage Tracking (for budget management)
# =============================================================================

@router.get("/service-usage/{service_name}")
async def get_service_usage(service_name: str) -> Dict[str, Any]:
    """
    Get current month's usage for a service.

    Used by RAG services (like Bright Data) to check budget before making requests.
    Returns monthly count and limit (if set).
    """
    conn = await get_admin_db_connection()
    try:
        current_month = datetime.now().strftime("%Y-%m")

        row = await conn.fetchrow("""
            SELECT service_name, month, request_count, monthly_limit
            FROM service_usage
            WHERE service_name = $1 AND month = $2
        """, service_name, current_month)

        if row:
            return {
                "service_name": row['service_name'],
                "month": row['month'],
                "monthly_count": row['request_count'],
                "monthly_limit": row['monthly_limit'],
                "remaining": (row['monthly_limit'] - row['request_count']) if row['monthly_limit'] else None
            }

        # No record for this month yet
        return {
            "service_name": service_name,
            "month": current_month,
            "monthly_count": 0,
            "monthly_limit": None,
            "remaining": None
        }

    except Exception as e:
        logger.error("get_service_usage_failed", service=service_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.post("/service-usage/{service_name}/increment")
async def record_service_usage(service_name: str, count: int = 1) -> Dict[str, Any]:
    """
    Increment usage counter for a service.

    Called by RAG services after each API request to track usage.
    Creates a new record if one doesn't exist for the current month.
    """
    conn = await get_admin_db_connection()
    try:
        current_month = datetime.now().strftime("%Y-%m")

        # Upsert: increment if exists, insert if not
        row = await conn.fetchrow("""
            INSERT INTO service_usage (service_name, month, request_count)
            VALUES ($1, $2, $3)
            ON CONFLICT (service_name, month)
            DO UPDATE SET
                request_count = service_usage.request_count + $3,
                last_updated = CURRENT_TIMESTAMP
            RETURNING request_count, monthly_limit
        """, service_name, current_month, count)

        return {
            "service_name": service_name,
            "month": current_month,
            "monthly_count": row['request_count'],
            "monthly_limit": row['monthly_limit'],
            "remaining": (row['monthly_limit'] - row['request_count']) if row['monthly_limit'] else None
        }

    except Exception as e:
        logger.error("record_service_usage_failed", service=service_name, error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()


@router.get("/service-usage")
async def get_all_service_usage() -> List[Dict[str, Any]]:
    """
    Get usage for all services for the current month.

    Used by Admin UI to display budget status across all tracked services.
    """
    conn = await get_admin_db_connection()
    try:
        current_month = datetime.now().strftime("%Y-%m")

        rows = await conn.fetch("""
            SELECT service_name, month, request_count, monthly_limit, last_updated
            FROM service_usage
            WHERE month = $1
            ORDER BY service_name
        """, current_month)

        return [{
            "service_name": row['service_name'],
            "month": row['month'],
            "monthly_count": row['request_count'],
            "monthly_limit": row['monthly_limit'],
            "remaining": (row['monthly_limit'] - row['request_count']) if row['monthly_limit'] else None,
            "last_updated": row['last_updated'].isoformat() if row['last_updated'] else None
        } for row in rows]

    except Exception as e:
        logger.error("get_all_service_usage_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        await conn.close()
