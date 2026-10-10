"""ingress_auth.py — orchestrator ingress authentication (ATHENA-89 / D10).

A FastAPI dependency gating the orchestrator's query and session routes so
an unauthenticated caller can no longer create, drive, read, export or
delete session state. Config (`orchestrator_ingress_auth`, `service_api_key`) is read via
`get_config()` on every call — never captured once at import time as a
module-level constant — so it never goes stale within a running process
the way a constant snapshotted at import would. `get_config()` itself is
`functools.lru_cache`d, so a mode change or key rotation made by editing
the ConfigMap/env still needs a process restart (or a call to
`shared.config._clear_cache_for_tests()` in tests) to actually take
effect; this dependency does not add its own additional staleness on top
of that.

Decision order (D10, six steps — mirrors the parity established at
`admin/backend/app/utils/service_auth.py:142-168`'s `_check_service_key_raw`,
which 401s an explicitly wrong key before any other fall-through runs):

1. A present, non-empty, WRONG `X-Service-Key` → 401 in every mode,
   including `DEV_MODE` and `warn`. An explicitly wrong key is never
   tolerated. Logs WARNING `orchestrator_bad_service_key`.
2. A header that matches a non-empty configured key → allow.
3. `DEV_MODE=true` (and no header, or step 1 already handled a bad one) →
   allow.
4. The configured key is empty (and not dev) → 401 in both `enforce` and
   `warn`. `hmac.compare_digest("", "")` is True, so without this step an
   empty key would fail open for any entry point that skips `lifespan`
   (`lifespan` itself raises `SystemExit` on an empty/placeholder key
   outside `DEV_MODE`, so this is a backstop for code paths that skip it —
   tests, future entry points).
5. Mode `warn` → allow, and log WARNING `orchestrator_unauthenticated_request`
   with `path`, `client_host` and `user_agent`.
6. Otherwise (mode `enforce`, or any invalid mode value, which behaves as
   `enforce` and logs one ERROR) → 401.

No 503 branch: unlike admin-backend's dual-auth dependency, the orchestrator
cannot legitimately run with an empty key in production (`lifespan` exits
first), so a plain 401 is sufficient here.
"""
from __future__ import annotations

import hmac

from fastapi import HTTPException, Request

from shared.config import get_config
from shared.logging_config import configure_logging

logger = configure_logging("orchestrator.ingress_auth")

_VALID_MODES = frozenset({"enforce", "warn"})

# DC14 item v4 (valerie): the invalid-mode ERROR fires on every request that
# reaches this dependency while misconfigured -- at request volume, that's
# log spam for a condition that doesn't change request-to-request (the mode
# comes from get_config(), which is lru_cache'd; it can't flip mid-process
# without a restart per this module's own docstring above). Logged once per
# process instead, module-flag-gated like the other "unset/misconfigured at
# startup" warnings elsewhere in this codebase (e.g. gateway's
# _warn_if_trusted_proxy_unset).
_invalid_mode_warned = False


def _keys_equal(presented: str, configured: str) -> bool:
    """Constant-time comparison on bytes. hmac.compare_digest raises TypeError
    on a non-ASCII str, which would turn a hostile header into a 500."""
    return hmac.compare_digest(
        presented.encode("utf-8", "surrogatepass"), configured.encode("utf-8", "surrogatepass")
    )


async def require_service_caller(request: Request) -> None:
    """FastAPI dependency: gate a route behind X-Service-Key (D10)."""
    global _invalid_mode_warned
    cfg = get_config()
    mode = cfg.orchestrator_ingress_auth
    configured_key = cfg.service_api_key
    header_value = request.headers.get("X-Service-Key")

    if mode not in _VALID_MODES:
        if not _invalid_mode_warned:
            logger.error("orchestrator_ingress_auth_invalid_mode", mode=mode)
            _invalid_mode_warned = True
        mode = "enforce"

    # Step 1: a present, non-empty, WRONG key is never tolerated, in any mode.
    if header_value and configured_key and not _keys_equal(header_value, configured_key):
        logger.warning(
            "orchestrator_bad_service_key",
            path=request.url.path,
            client_host=request.client.host if request.client else None,
        )
        raise HTTPException(status_code=401, detail="Invalid service key")

    # Step 2: a header that matches a non-empty configured key.
    if header_value and configured_key and _keys_equal(header_value, configured_key):
        return

    # Step 3: DEV_MODE bypass (no header, or a header that matched above).
    if cfg.dev_mode:
        return

    # Step 4: an empty configured key is a backstop 401 in every mode.
    if not configured_key:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Step 5: warn mode allows through and logs.
    if mode == "warn":
        logger.warning(
            "orchestrator_unauthenticated_request",
            path=request.url.path,
            client_host=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        return

    # Step 6: enforce (or an invalid mode, already normalized to enforce).
    raise HTTPException(status_code=401, detail="Not authenticated")


def service_key_matches(request: Request) -> bool:
    """True only when the request carries an X-Service-Key equal to a
    non-empty configured SERVICE_API_KEY (same comparison as step 2 above).

    This is the proof that a request crossed an authenticated service hop.
    It is False for DEV_MODE and warn-mode passthrough (no header), and for an
    empty configured key, so a caller-supplied body field alone can never be
    taken as coming from trusted server code.
    """
    configured_key = get_config().service_api_key
    header_value = request.headers.get("X-Service-Key")
    return bool(header_value and configured_key and _keys_equal(header_value, configured_key))


async def service_authenticated(request: Request) -> bool:
    """FastAPI dependency: ``service_key_matches`` for the handler to consume.

    Independent of ``require_service_caller`` and of dependency ordering.
    Handlers must test the result with ``is True``, so a direct call that
    passes nothing (leaving the ``Depends`` default object) fails closed.
    """
    return service_key_matches(request)
