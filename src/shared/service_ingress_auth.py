"""service_ingress_auth.py — factory for per-service X-Service-Key ingress
auth dependencies (ATHENA-69 D15).

`orchestrator/ingress_auth.py::require_service_caller` implements the six-step
decision table for the orchestrator specifically (its own config attribute and
its own event names). The mode service needs the identical table against a
different config attribute (`mode_service_ingress_auth` instead of
`orchestrator_ingress_auth`) and its own event-name prefix, so future services
that need the same contract don't have to hand-copy and silently drift from
either. `make_require_service_caller` returns a FastAPI dependency closure
implementing that same table; a parity test
(`tests/unit/test_mode_service_ingress_auth.py::test_shared_dependency_parity_with_orchestrator`)
drives both dependencies through the same case table and asserts identical
outcomes.

Decision order (six steps, mirrors `admin/backend/app/utils/service_auth.py`'s
`_check_service_key_raw` and `orchestrator/ingress_auth.py`):

1. A present, non-empty, WRONG `X-Service-Key` → 401 in every mode, including
   `DEV_MODE` and `warn`. Logs WARNING `f"{event_prefix}_bad_service_key"`.
2. A header that matches a non-empty configured key → allow.
3. `DEV_MODE=true` (and no header, or step 1 already handled a bad one) →
   allow.
4. The configured key is empty (and not dev) → 401 in both `enforce` and
   `warn` (never let `hmac.compare_digest("", "")`'s True fail a caller open).
5. Mode `warn` → allow, and log WARNING
   `f"{event_prefix}_unauthenticated_request"` with `path`, `client_host`, and
   `user_agent`.
6. Otherwise (mode `enforce`, or any invalid mode value, which behaves as
   `enforce` and logs one ERROR `f"{event_prefix}_ingress_auth_invalid_mode"`)
   → 401.
"""
from __future__ import annotations

import hmac
from typing import Callable, Coroutine

from fastapi import HTTPException, Request

import structlog

from shared.config import get_config

_VALID_MODES = frozenset({"enforce", "warn"})


def make_require_service_caller(
    mode_attr: str, event_prefix: str
) -> Callable[[Request], Coroutine[None, None, None]]:
    """Build a FastAPI dependency gating a route behind X-Service-Key.

    Args:
        mode_attr: the `AthenaConfig` attribute name holding this service's
            ingress-auth mode (e.g. ``"mode_service_ingress_auth"``).
        event_prefix: prefix for the three log event names this dependency
            emits (e.g. ``"mode_service"`` → ``mode_service_bad_service_key``).
    """
    # Deliberately `structlog.get_logger(...)`, not
    # `shared.logging_config.configure_logging(...)`: the latter calls
    # `structlog.configure()` and `bind_contextvars(service=...)`, both
    # process-wide. The owning service (mode_service/main.py) already calls
    # `configure_logging()` once at its own import; calling it again here
    # would rebind the global "service" context to this dependency's own
    # name for every subsequent log line the *whole process* emits,
    # including ones that have nothing to do with ingress auth.
    logger = structlog.get_logger(f"{event_prefix}.ingress_auth")
    invalid_mode_warned = False

    async def require_service_caller(request: Request) -> None:
        nonlocal invalid_mode_warned
        cfg = get_config()
        mode = getattr(cfg, mode_attr)
        configured_key = cfg.service_api_key
        header_value = request.headers.get("X-Service-Key")

        if mode not in _VALID_MODES:
            if not invalid_mode_warned:
                logger.error(f"{event_prefix}_ingress_auth_invalid_mode", mode=mode)
                invalid_mode_warned = True
            mode = "enforce"

        # Step 1: a present, non-empty, WRONG key is never tolerated, in any mode.
        if header_value and configured_key and not hmac.compare_digest(header_value, configured_key):
            logger.warning(
                f"{event_prefix}_bad_service_key",
                path=request.url.path,
                client_host=request.client.host if request.client else None,
            )
            raise HTTPException(status_code=401, detail="Invalid service key")

        # Step 2: a header that matches a non-empty configured key.
        if header_value and configured_key and hmac.compare_digest(header_value, configured_key):
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
                f"{event_prefix}_unauthenticated_request",
                path=request.url.path,
                client_host=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            return

        # Step 6: enforce (or an invalid mode, already normalized to enforce).
        raise HTTPException(status_code=401, detail="Not authenticated")

    return require_service_caller
