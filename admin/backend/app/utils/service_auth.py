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


def control_agent_headers() -> dict:
    """Outbound `X-Service-Key` header for admin-backend's Control Agent
    client (ATHENA-110). Every admin-backend call into a *mutating* Control
    Agent route (`/process/start|stop|restart`, `/docker/start|stop|restart`,
    `/ollama/start|stop|restart`, `/huggingface/download`,
    `/huggingface/import-to-ollama`, `/huggingface/downloaded` DELETE) must
    send this, or the Control Agent's `require_service_caller` dependency
    401s/503s it. Harmless to attach on read-only Control Agent calls too
    (e.g. `/docker/list`, `/ollama/health`) — those routes aren't gated and
    ignore the extra header.

    Returns `{}` when `SERVICE_API_KEY` is unset so callers don't send a
    literal `X-Service-Key: ` header with an empty value; the Control Agent
    treats a missing header and an empty one identically (401, or 503 if
    its own key is also unset).
    """
    key = get_config().service_api_key
    return {"X-Service-Key": key} if key else {}


def service_keys_match(presented: str, configured: str) -> bool:
    """Constant-time comparison of a presented service key with the
    configured one, as UTF-8 bytes: ``hmac.compare_digest`` raises on a
    ``str`` holding a non-ASCII character, and a header can carry one."""
    return hmac.compare_digest(presented.encode("utf-8"), configured.encode("utf-8"))


def verify_service_api_key(
    request: Request,
    x_service_key: str = Header(..., alias="X-Service-Key"),
) -> bool:
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
    if not service_keys_match(x_service_key, key):
        logger.warning("service_api_key_invalid", key_length=len(x_service_key) if x_service_key else 0)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service key",
        )
    request.state.auth_kind = "service"
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
    if not service_keys_match(x_service_key, key):
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
        ``unregister_service`` both send ``X-Service-Key`` (from
        ``get_config().service_api_key`` / env fallback; ATHENA-108 /
        xander diff-review Medium 2026-09-28). A caller with the key unset
        still POSTs with an empty header and 401s in production, same as
        any other misconfigured service caller.

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
            request.state.auth_kind = "service"
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
        # Which branch authenticated, for dependencies that authorize by
        # caller kind (memories' require_memory_maintainer). Additive:
        # nothing else reads these.
        request.state.auth_kind = "user"
        request.state.auth_user = user
        return True

    logger.warning("dual_auth_failed",
                   path=str(request.url.path),
                   has_service_key=bool(x_service_key))
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_user_permission(permission: str):
    """Dependency factory for routes that only a signed-in user may call.

    The caller must authenticate as a user (Bearer JWT or ``X-API-Key``, via
    ``get_current_user``) and hold ``permission``. Any ``X-Service-Key``
    header is refused with 401 -- correct, wrong, or with
    ``SERVICE_API_KEY`` unset alike -- so a leaked or shared service key can
    never reach these routes, and a caller can't mix the two credentials.
    Returns the authenticated ``User`` and records the accepted caller in
    ``request.state.auth_kind``.
    """
    from app.auth.oidc import get_current_user

    async def _dependency(
        request: Request,
        user=Depends(get_current_user),
        x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
    ):
        if x_service_key is not None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="service key not accepted on this route",
            )
        if not user.has_permission(permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        request.state.auth_kind = "user"
        return user

    _dependency.required_permission = permission
    _dependency.caller_kinds = ("user",)
    return _dependency


def require_service_or_user_permission(permission: str):
    """Dependency factory for routes that a service (``X-Service-Key``) or a
    signed-in user holding ``permission`` may call.

    - A key is present: the request is decided on the key alone, exactly as
      ``verify_service_or_oidc`` decides it (wrong key 401, key sent while
      ``SERVICE_API_KEY`` is unset 503). A Bearer token sent alongside is
      ignored.
    - No key: the user is resolved with ``get_current_user``, so an anonymous
      caller gets 401 and a scoped role gets its own 403, then the permission
      is checked (403).

    Returns ``"service"`` or ``"user"``. The user is on
    ``request.state.auth_user`` (``None`` on the service branch), and
    ``request.state.auth_kind`` names the branch.

    ``get_current_user`` is called here, not declared with ``Depends``, so
    ``app.dependency_overrides[get_current_user]`` doesn't reach it: tests
    authenticate with real tokens.
    """

    async def _dependency(
        request: Request,
        db: Session = Depends(get_db),
        x_service_key: Optional[str] = Header(default=None, alias="X-Service-Key"),
    ) -> str:
        if x_service_key:
            await verify_service_or_oidc(request, db, x_service_key)
            request.state.auth_kind = "service"
            request.state.auth_user = None
            return "service"

        # Imported here for the same circular-import reason as in
        # verify_service_or_oidc.
        from app.auth.oidc import get_current_user, optional_security

        user = await get_current_user(
            credentials=await optional_security(request),
            x_api_key=request.headers.get("X-API-Key"),
            db=db,
            request=request,
        )
        if not user.has_permission(permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        request.state.auth_kind = "user"
        request.state.auth_user = user
        return "user"

    _dependency.required_permission = permission
    _dependency.caller_kinds = ("service", "user")
    return _dependency
