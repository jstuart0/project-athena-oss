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

Hides: where the key comes from, the omit-when-unset and omit-when-unusable
rules, and the rate-limit state, its bound and its clock behind the refusal
log.

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
# Routes are literal templates, so the table holds one entry per call site
# and status (well under a hundred). The cap is for a caller that breaks
# that rule.
MAX_TRACKED_REFUSALS = 256
MAX_ROUTE_LENGTH = 200
INVALID_ROUTE = "<invalid-route>"
SERVICE_KEY_VARIABLE = "SERVICE_API_KEY"

_clock = time.monotonic
# (route template, status) -> clock reading of the last line written, oldest
# first.
_last_logged: dict[tuple[str, int], float] = {}
_unusable_key_reported = False


def is_header_safe(key: str) -> bool:
    """True when ``key`` can be sent as a header value: visible ASCII only.
    Anything else (a trailing newline from a Secret, a space, a non-ASCII
    character) is refused by the HTTP client with an exception whose text
    carries the whole value, and callers log that text.

    For a sender that holds its own key instead of calling
    ``service_key_headers()``: check before building the header, and send
    none when this is False."""
    return all("\x21" <= character <= "\x7e" for character in key)


def service_key_headers() -> dict[str, str]:
    """``{"X-Service-Key": key}``, or ``{}`` when no key is configured, so a
    caller never sends the header with an empty value. A new dict each call.

    Also ``{}`` when the configured key can't be a header value (see
    ``is_header_safe``); the variable's name is logged once, never its
    value. The call then goes out without a credential and is refused.
    """
    global _unusable_key_reported
    from shared.config import get_config

    key = get_config().service_api_key
    if not key:
        return {}
    if not is_header_safe(key):
        if not _unusable_key_reported:
            _unusable_key_reported = True
            try:
                logger.error("service_api_key_unusable", variable=SERVICE_KEY_VARIABLE)
            except Exception:
                pass  # a failing logger must not fail the call
        return {}
    return {SERVICE_KEY_HEADER: key}


def note_admin_refusal(status_code: int, route: str) -> bool:
    """True when ``status_code`` means admin-backend refused the credential
    (401, 403, 503); writes at most one ERROR per ``(route, status_code)``
    per minute. False, and no log, for any other status.

    ``route`` must be the route's template as a string literal
    (``"/api/escalation/state/{session_id}/public"``), never the URL that was
    requested: a concrete path can carry an id or a query string into the log.
    A value with a ``?`` in it, longer than ``MAX_ROUTE_LENGTH``, or not a
    string is logged as ``INVALID_ROUTE`` instead. A route that answers 503
    for its own reasons must not be passed here for that status. Never raises.
    """
    try:
        if status_code not in REFUSAL_STATUSES:
            return False
    except TypeError:
        return False  # an unhashable status is not one of the three
    try:
        if not isinstance(route, str) or "?" in route or len(route) > MAX_ROUTE_LENGTH:
            route = INVALID_ROUTE
        now = _clock()
        key = (route, status_code)
        last = _last_logged.get(key)
        if last is None or now - last >= REFUSAL_LOG_INTERVAL_SECONDS:
            # Re-inserted, so the table stays ordered oldest line first and
            # the eviction below drops the key least recently logged.
            _last_logged.pop(key, None)
            while len(_last_logged) >= MAX_TRACKED_REFUSALS:
                del _last_logged[next(iter(_last_logged))]
            _last_logged[key] = now
            logger.error("admin_backend_refused", status=status_code, route=route)
    except Exception:
        # Reporting a refusal must never turn into a failure of the call
        # that was refused.
        pass
    return True


def _reset_for_tests() -> None:
    global _unusable_key_reported
    _last_logged.clear()
    _unusable_key_reported = False
