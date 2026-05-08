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
from typing import Optional

from fastapi import Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy.orm import Session
import structlog
from shared.config import get_config
from app.database import get_db
from fastapi import Depends

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


def _check_service_key_raw(
    x_service_key: Optional[str],
    configured_key: Optional[str] = None,
) -> bool:
    """Validate an X-Service-Key value without using FastAPI Depends injection.

    Returns True on success.  Raises HTTPException on invalid key.
    Returns False (silently) when no key is provided so the caller can fall
    through to user-auth.

    Args:
        x_service_key: The value from the ``X-Service-Key`` request header.
        configured_key: The expected secret.  When provided (by
            ``verify_service_or_oidc``, which captures ``get_config().service_api_key``
            once), this value is used directly — eliminating the TOCTOU window that
            would exist if this helper made a second independent ``get_config()`` read.
            When ``None`` (standalone callers), falls back to ``get_config()`` at
            call time, preserving backward-compatible behaviour.

    Note: the case where ``x_service_key`` is non-empty AND SERVICE_API_KEY is
    unset is now intercepted by ``verify_service_or_oidc`` before this helper
    is reached (ATHENA-21).  This helper therefore only sees unset-key when
    ``x_service_key`` itself is absent (falsy) *when called via the dispatcher*.

    Note: per Decision 5α, whitespace-only ``SERVICE_API_KEY`` (e.g., ``'   '``)
    is treated as configured (truthy) at the dispatcher level; this helper will
    then run ``hmac.compare_digest`` against the whitespace value and raise 401
    for any non-matching client header.  The whitespace edge case is tracked
    separately for the startup-gate parity fix.

    This is an internal helper used by verify_service_or_oidc; prefer the
    verify_service_api_key dependency for routes that require service-key only.
    """
    if not x_service_key:
        return False
    key = configured_key if configured_key is not None else get_config().service_api_key
    if not key:
        # Defensive branch — should not be reached because verify_service_or_oidc
        # now raises 503 before calling this helper when key is unset.
        logger.warning("service_api_key_not_configured_during_dual_auth")
        return False
    if not hmac.compare_digest(x_service_key, key):
        logger.warning("service_api_key_invalid_during_dual_auth",
                       key_length=len(x_service_key))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service key",
        )
    return True


async def verify_service_or_oidc(
    request: Request,
    db: Session = Depends(get_db),
    x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
) -> bool:
    """Dual-auth dependency: X-Service-Key (CA / internal callers) OR Bearer JWT
    / X-API-Key (admin UI).

    Tries X-Service-Key first (cheaper, no DB hit).  Falls through to the
    user-auth path (Bearer JWT or X-API-Key resolved by get_current_user) if no
    service key is present.  Raises 401 if both paths fail.

    Returns 503 if ``SERVICE_API_KEY`` is unset and the caller presents a
    non-empty ``X-Service-Key`` header (fail-closed misconfiguration signal;
    ATHENA-21).  The 503 fires regardless of DEV_MODE and carries no
    ``WWW-Authenticate`` header — retrying won't help; this is a server-side
    config gap, not a credential error.

    Callers:
      - Control Agent sends ``X-Service-Key``.
      - Admin UI sends ``Authorization: Bearer <jwt>`` (per app.js:2497).
      - ``src/shared/service_registry.py::register_service`` and
        ``unregister_service`` POST to write endpoints with no ``X-Service-Key``
        and no Bearer token; they fall through to the OIDC path and 401 in
        production (unaffected by ATHENA-21).

    (xander CRIT-1 / D9 — ATHENA-1 Phase 2 wires this onto the POST/toggle/
    refresh/delete endpoints in service_registry.py.)
    """
    # 1. X-Service-Key path (preferred for CA and internal callers)
    if x_service_key:
        # Capture once so both the 503 guard and the helper see the same value.
        # A second get_config() call inside _check_service_key_raw would re-open
        # the TOCTOU window if the config mutates between the two reads (e.g. test
        # monkeypatching, live key rotation) — xander Medium / ATHENA-21.
        configured_key = get_config().service_api_key

        # Fail-closed: if the caller signals intent to use service-key auth but
        # SERVICE_API_KEY is not configured, return 503 immediately.  Falling
        # through to OIDC would silently mask a server-side misconfiguration that
        # the caller has no way to diagnose (xander MED-3 / ATHENA-21).
        if not configured_key:
            safe_path = str(request.url.path).replace("\r", "\\r").replace("\n", "\\n")
            logger.error(
                "service_api_key_not_configured_with_header_present",
                path=safe_path,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Service authentication not configured",
            )
        # Pass the already-captured key so the helper does not call get_config()
        # a second time.  Raises 401 on invalid key; returns True on match;
        # returns False only when x_service_key is absent (guarded by the if above).
        if _check_service_key_raw(x_service_key, configured_key=configured_key):
            return True

    # 2. User-auth path (admin UI: Bearer JWT or X-API-Key).
    # Import here to avoid circular dependency at module load time
    # (service_auth → oidc → get_db → models → service_auth would be circular).
    from app.auth.oidc import get_optional_user, optional_security

    credentials: Optional[HTTPAuthorizationCredentials] = await optional_security(request)
    x_api_key: Optional[str] = request.headers.get("X-API-Key")

    user = await get_optional_user(
        credentials=credentials,
        x_api_key=x_api_key,
        db=db,
        request=request,
    )
    if user is not None:
        return True

    logger.warning("dual_auth_failed",
                   path=str(request.url.path),
                   has_service_key=bool(x_service_key))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )
