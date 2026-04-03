"""
Service-to-service authentication utility.

Provides a shared FastAPI dependency for authenticating internal service calls
(orchestrator, gateway, RAG services → admin backend).

Uses a shared secret passed via the X-Service-Key header — distinct from the
X-API-Key header used for user API keys in get_current_user.

Configuration:
    SERVICE_API_KEY env var — must be set to a strong random secret in production.
    Defaults to an insecure placeholder that triggers a startup failure when
    DEV_MODE is not active (see main.py startup_event).
"""
import hmac
import os

from fastapi import Header, HTTPException, status
import structlog

logger = structlog.get_logger()

SERVICE_API_KEY = os.getenv("SERVICE_API_KEY", "dev-service-key-change-in-production")


def verify_service_api_key(x_service_key: str = Header(..., alias="X-Service-Key")) -> bool:
    """
    FastAPI dependency that authenticates service-to-service requests.

    Requires an X-Service-Key header matching the SERVICE_API_KEY env var.
    Uses constant-time comparison to prevent timing attacks.

    Raises:
        HTTPException 401: If the key is missing or does not match.
    """
    if not hmac.compare_digest(x_service_key, SERVICE_API_KEY):
        logger.warning("service_api_key_invalid", key_prefix=x_service_key[:8] if x_service_key else "empty")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service key",
        )
    return True
