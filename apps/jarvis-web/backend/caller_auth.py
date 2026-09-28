"""ATHENA-69 D18/D19/D28: jarvis-web caller resolution and the owner-only
route gate.

Pre-ATHENA-69, every jarvis-web caller (including anonymous internet
traffic on jarvis.your-domain) was served in the household's mode and could
reach every direct device/mode/LiveKit route. This module reverses that:
unauthenticated callers are guests and refused on writes; only a caller
holding a Bearer token that the admin backend's GET /api/auth/me confirms
has role "owner" or "operator" gets the household's actual mode
(get_current_mode()'s existing auto-detect/override behavior).

Hides: Bearer token validation against the admin backend, the
authenticated-result cache (positive and negative, keyed by SHA-256 of the
token so the raw token is never used as a dict key or logged), the
JARVIS_PUBLIC_MODE escape hatch for LAN-only deployments, and the hourly
posture reminder for that escape hatch. Callers of this module never see
the token; it is not logged and not forwarded to the orchestrator.

Dependency category: remote-but-owned (admin backend). Two adapters: httpx
in production, an injectable async callable for tests.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional, Tuple

import httpx
import structlog
from fastapi import HTTPException, Request, WebSocket

from admin_url import get_admin_url

logger = structlog.get_logger()

_AUTH_CACHE_TTL_SECONDS = 60
_AUTH_ME_TIMEOUT_SECONDS = 3.0
_PERMITTED_ROLES = frozenset({"owner", "operator"})
_POSTURE_REMINDER_INTERVAL_SECONDS = 3600.0


def _parse_public_mode() -> str:
    raw = os.getenv("JARVIS_PUBLIC_MODE", "guest").strip().lower()
    if raw in ("guest", "household"):
        return raw
    if raw:
        logger.error("jarvis_public_mode_invalid", value=raw)
    return "guest"


JARVIS_PUBLIC_MODE = _parse_public_mode()

HouseholdModeResolver = Callable[[], Awaitable[str]]
AuthMeCallable = Callable[[str], Awaitable[httpx.Response]]


@dataclass(frozen=True)
class Caller:
    authenticated: bool
    role: Optional[str]
    mode: str  # "owner" or "guest"
    trust: str  # "web_authenticated" or "web_public"
    reason: str


@dataclass(frozen=True)
class _AuthDecision:
    """Cached portion of caller resolution -- everything except mode, which
    is re-derived fresh on every call (a guest booking or the mode_override
    can change between two requests carrying the same still-cached token)."""
    authenticated: bool
    role: Optional[str]
    reason: str


_auth_cache: Dict[str, Tuple[_AuthDecision, float]] = {}
_auth_me_override: Optional[AuthMeCallable] = None


def _reset_for_tests() -> None:
    """PRIVATE -- test isolation only. Production code never calls this."""
    _auth_cache.clear()
    global _auth_me_override
    _auth_me_override = None


def _set_auth_me_callable_for_tests(fn: Optional[AuthMeCallable]) -> None:
    """PRIVATE -- injects a fake GET /api/auth/me for tests."""
    global _auth_me_override
    _auth_me_override = fn


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _call_auth_me(token: str) -> httpx.Response:
    if _auth_me_override is not None:
        return await _auth_me_override(token)
    admin_url = get_admin_url()
    async with httpx.AsyncClient(timeout=_AUTH_ME_TIMEOUT_SECONDS) as client:
        return await client.get(
            f"{admin_url}/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )


async def _resolve_mode(authenticated: bool, household_mode_resolver: Optional[HouseholdModeResolver]) -> str:
    """Authenticated owner/operator callers, and any caller at all when
    JARVIS_PUBLIC_MODE=="household" (legacy, D19's gate bypass), get the
    household's actual mode. Everyone else is guest."""
    if authenticated or JARVIS_PUBLIC_MODE == "household":
        if household_mode_resolver is None:
            logger.error("jarvis_caller_mode_resolver_missing")
            return "guest"
        return await household_mode_resolver()
    return "guest"


async def _resolve_auth_decision(token: Optional[str]) -> _AuthDecision:
    if not token:
        return _AuthDecision(authenticated=False, role=None, reason="no_bearer_token")

    cache_key = _cache_key(token)
    now = time.monotonic()
    cached = _auth_cache.get(cache_key)
    if cached is not None and (now - cached[1]) < _AUTH_CACHE_TTL_SECONDS:
        return cached[0]

    admin_url = get_admin_url()
    if not admin_url:
        decision = _AuthDecision(authenticated=False, role=None, reason="admin_url_unset")
        _auth_cache[cache_key] = (decision, now)
        return decision

    try:
        response = await _call_auth_me(token)
    except Exception as exc:  # ConnectError, TimeoutException, etc. -- never a 500
        logger.warning("jarvis_auth_me_unreachable", error=str(exc))
        decision = _AuthDecision(authenticated=False, role=None, reason="admin_unreachable")
        _auth_cache[cache_key] = (decision, now)
        return decision

    if response.status_code != 200:
        decision = _AuthDecision(authenticated=False, role=None, reason="token_rejected")
        _auth_cache[cache_key] = (decision, now)
        return decision

    try:
        body = response.json()
    except Exception:
        decision = _AuthDecision(authenticated=False, role=None, reason="token_rejected")
        _auth_cache[cache_key] = (decision, now)
        return decision

    role = body.get("role") if isinstance(body, dict) else None
    if role not in _PERMITTED_ROLES:
        decision = _AuthDecision(authenticated=False, role=role, reason="role_not_permitted")
        _auth_cache[cache_key] = (decision, now)
        return decision

    decision = _AuthDecision(authenticated=True, role=role, reason="authenticated")
    _auth_cache[cache_key] = (decision, now)
    return decision


def _extract_bearer_token(headers) -> Optional[str]:
    auth_header = headers.get("authorization")
    if not auth_header or not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header[len("bearer "):].strip()
    return token or None


async def _resolve_caller_from_headers(
    headers,
    household_mode_resolver: Optional[HouseholdModeResolver],
) -> Caller:
    token = _extract_bearer_token(headers)
    decision = await _resolve_auth_decision(token)
    mode = await _resolve_mode(decision.authenticated, household_mode_resolver)
    trust = "web_authenticated" if decision.authenticated else "web_public"
    return Caller(
        authenticated=decision.authenticated,
        role=decision.role,
        mode=mode,
        trust=trust,
        reason=decision.reason,
    )


async def resolve_caller(
    request: Request,
    household_mode_resolver: Optional[HouseholdModeResolver] = None,
) -> Caller:
    """Resolve the calling identity for an HTTP request.

    household_mode_resolver is the caller's get_current_mode -- passed in
    rather than imported, so this module never imports main.py (which
    imports this module)."""
    caller = await _resolve_caller_from_headers(request.headers, household_mode_resolver)
    logger.info(
        "jarvis_caller_resolved",
        authenticated=caller.authenticated,
        role=caller.role,
        mode=caller.mode,
        reason=caller.reason,
    )
    return caller


async def resolve_caller_ws(
    websocket: WebSocket,
    household_mode_resolver: Optional[HouseholdModeResolver] = None,
) -> Caller:
    """Resolve the calling identity for a WebSocket upgrade request. Callers
    must close(code=1008) before accept() when the result isn't permitted --
    this function only resolves identity, it never touches the socket."""
    caller = await _resolve_caller_from_headers(websocket.headers, household_mode_resolver)
    logger.info(
        "jarvis_caller_resolved",
        authenticated=caller.authenticated,
        role=caller.role,
        mode=caller.mode,
        reason=caller.reason,
        transport="websocket",
    )
    return caller


def is_owner_permitted(caller: Caller) -> bool:
    """D19's gate: a signed-in owner/operator, or any caller at all when
    JARVIS_PUBLIC_MODE=="household" (legacy bypass)."""
    return caller.authenticated or JARVIS_PUBLIC_MODE == "household"


async def require_owner_caller(
    request: Request,
    household_mode_resolver: Optional[HouseholdModeResolver] = None,
) -> Caller:
    """FastAPI dependency for the owner_only HTTP routes. main.py binds
    household_mode_resolver=get_current_mode via functools.partial so an
    authenticated caller's returned Caller.mode is the real household mode
    rather than the safe-fallback "guest" _resolve_mode uses when no
    resolver is given -- the gate itself only reads .authenticated, but a
    correct .mode avoids a misleading resolver-missing log on every
    request."""
    caller = await resolve_caller(request, household_mode_resolver)
    if not is_owner_permitted(caller):
        logger.warning("jarvis_owner_only_route_refused", path=request.url.path, reason=caller.reason)
        raise HTTPException(status_code=403, detail="sign_in_required")
    return caller


async def _posture_reminder_loop(interval: float = _POSTURE_REMINDER_INTERVAL_SECONDS) -> None:
    """D28: warns at task start and every `interval` seconds thereafter
    while JARVIS_PUBLIC_MODE=="household" -- the internet-facing gate is
    bypassed for every caller. Re-reads the setting each iteration; a tiny
    interval makes this testable."""
    while True:
        if JARVIS_PUBLIC_MODE == "household":
            logger.warning("jarvis_public_mode_household_active", interval_seconds=interval)
        await asyncio.sleep(interval)
