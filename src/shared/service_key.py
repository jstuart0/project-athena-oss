"""The service key on a call to admin-backend, and what to do when the call
is refused.

``service_key_headers()`` is the per-call ``X-Service-Key`` header. It is
passed on each call rather than set as a client default: a default would
send the key to admin-backend's user-only routes, which refuse any request
that carries one.

``note_admin_refusal()`` is called with the status of every answer from
admin-backend that isn't a success. For 401, 403 and 503 it writes one ERROR,
``admin_backend_refused``, so a service that silently falls back to its
defaults still says that its credential was refused.

Hides: where the key comes from, the omit-when-unset rule, and the
rate-limit state and clock behind the refusal log.

Only the standard library and structlog are imported at module level, and
nothing from ``shared``: jarvis-web copies this single file beside its
``main.py`` (see its Dockerfile) and has no ``shared`` package.
"""
from __future__ import annotations

import time

import structlog

logger = structlog.get_logger()

SERVICE_KEY_HEADER = "X-Service-Key"
REFUSAL_STATUSES = frozenset({401, 403, 503})
REFUSAL_LOG_INTERVAL_SECONDS = 60.0

_clock = time.monotonic
# (route template, status) -> clock reading of the last line written. One
# entry per call site and status, because `route` is a literal template.
_last_logged: dict[tuple[str, int], float] = {}


def service_key_headers() -> dict[str, str]:
    """``{"X-Service-Key": key}``, or ``{}`` when no key is configured, so a
    caller never sends the header with an empty value. A new dict each call."""
    from shared.config import get_config

    key = get_config().service_api_key
    return {SERVICE_KEY_HEADER: key} if key else {}


def note_admin_refusal(status_code: int, route: str) -> bool:
    """True when ``status_code`` means admin-backend refused the credential
    (401, 403, 503); writes at most one ERROR per ``(route, status_code)``
    per minute. False, and no log, for any other status.

    ``route`` must be the route's template as a string literal
    (``"/api/escalation/state/{session_id}/public"``), never the URL that was
    requested: a concrete path can carry an id or a query string into the log.
    A route that answers 503 for its own reasons must not be passed here for
    that status. Never raises.
    """
    if status_code not in REFUSAL_STATUSES:
        return False
    try:
        now = _clock()
        key = (route, status_code)
        last = _last_logged.get(key)
        if last is None or now - last >= REFUSAL_LOG_INTERVAL_SECONDS:
            _last_logged[key] = now
            logger.error("admin_backend_refused", status=status_code, route=route)
    except Exception:
        # Reporting a refusal must never turn into a failure of the call
        # that was refused.
        pass
    return True


def _reset_for_tests() -> None:
    _last_logged.clear()
