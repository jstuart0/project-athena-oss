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

from fastapi import Header, HTTPException, status
import structlog
from shared.config import get_config

logger = structlog.get_logger()


def verify_service_api_key(x_service_key: str = Header(..., alias="X-Service-Key")) -> bool:
    """
    FastAPI dependency that authenticates service-to-service requests.

    Requires an X-Service-Key header matching the SERVICE_API_KEY env var.
    Uses constant-time comparison to prevent timing attacks.

    The key is read via get_config() at call time (not at module import) so that:
      - monkeypatch-based tests work without module reloads
      - runtime key rotation takes effect without a process restart (xander:40)

    Raises:
        HTTPException 503: If SERVICE_API_KEY is not configured (fail-closed).
        HTTPException 401: If the key is missing or does not match.
    """
    key = get_config().service_api_key
    # Fail-closed: never compare against an empty secret.
    # hmac.compare_digest("", "") returns True, which would allow any caller
    # sending an empty X-Service-Key header to bypass auth entirely.
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service authentication not configured",
        )
    if not hmac.compare_digest(x_service_key, key):
        logger.warning("service_api_key_invalid", key_length=len(x_service_key) if x_service_key else 0)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service key",
        )
    return True
