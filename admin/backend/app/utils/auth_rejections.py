"""One log line for a request admin-backend refuses for its credential.

``AuthRejectionMiddleware`` watches every HTTP response and emits a WARNING
``admin_auth_rejected`` when no guard accepted the request
(``request.state.auth_kind`` unset) and the answer is a credential refusal:

- 401 or 403;
- 503 while an ``X-Service-Key`` header was sent (the key isn't configured);
- 422 without an ``X-Service-Key`` on a route behind the service-only guard
  (FastAPI refuses the missing required header before any guard code runs).

Failed password logins (``POST /api/auth/local-login``) are not reported:
that route has its own lockout and failure handling.

The line says "this route template is being refused, with this kind of
credential presented". It is not a per-caller record: ``credential_presented``
is what the request carried, and anyone can send a junk ``X-Service-Key``.
It carries the route template, never the concrete path, a query, a header
value or a client address.

One line per (route, reason, credential) per minute; refusals inside the
window are counted and reported as ``suppressed`` on the next line for that
key, when another refusal finds the window expired, or at shutdown.

The middleware never changes a response and never raises into the request.
"""
from __future__ import annotations

import time
from typing import Any

import structlog

from shared.route_walk import dependency_calls, iter_api_routes

logger = structlog.get_logger()

EVENT = "admin_auth_rejected"
UNMATCHED_ROUTE = "<unmatched>"
WINDOW_SECONDS = 60.0

_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
_OTHER_METHOD = "OTHER"
_UNREPORTED = frozenset({("POST", "/api/auth/local-login")})
_SERVICE_ONLY_GUARD = ("app.utils.service_auth", "verify_service_api_key")
_SERVICE_KEY_HEADER = b"x-service-key"
_USER_CREDENTIAL_HEADERS = frozenset({b"authorization", b"x-api-key"})

_clock = time.monotonic

# (route, reason, credential_presented) -> [window start, suppressed, method, status]
_windows: dict[tuple[str, str, str], list] = {}
# id(app) -> templates of the routes behind the service-only guard
_service_only_templates: dict[int, frozenset] = {}


def _reset_for_tests() -> None:
    _windows.clear()
    _service_only_templates.clear()


def tracked_key_count() -> int:
    return len(_windows)


def flush_suppressed() -> None:
    """Report every count still held (the shutdown hook). Never raises."""
    try:
        for key, window in list(_windows.items()):
            if window[1]:
                suppressed, window[1] = window[1], 0
                _emit(key, window[2], window[3], suppressed)
    except Exception:
        pass


def _emit(key: tuple[str, str, str], method: str, status: int, suppressed: int) -> None:
    route, reason, credential = key
    logger.warning(EVENT, route=route, method=method, status=status, reason=reason,
                   credential_presented=credential, suppressed=suppressed)


def _record(key: tuple[str, str, str], method: str, status: int) -> None:
    now = _clock()
    for other, window in list(_windows.items()):
        if other != key and now - window[0] >= WINDOW_SECONDS:
            del _windows[other]
            if window[1]:
                _emit(other, window[2], window[3], window[1])
    window = _windows.get(key)
    if window is not None and now - window[0] < WINDOW_SECONDS:
        window[1] += 1
        return
    suppressed = window[1] if window is not None else 0
    _windows[key] = [now, 0, method, status]
    _emit(key, method, status, suppressed)


def _service_only(app: Any, template: str) -> bool:
    templates = _service_only_templates.get(id(app))
    if templates is None:
        templates = frozenset(
            walked.path for walked in iter_api_routes(app)
            if any((getattr(call, "__module__", None), getattr(call, "__qualname__", None)) == _SERVICE_ONLY_GUARD
                   for call in dependency_calls(walked))
        )
        _service_only_templates[id(app)] = templates
    return template in templates


def _reason(status: int, credential: str) -> str:
    if status == 503:
        return "service_key_unconfigured"
    if credential == "service_key":
        return "service_key_refused"
    if credential == "user":
        return "insufficient_permission" if status == 403 else "user_credential_refused"
    return "no_credential"


def _observe(scope: dict, status: int) -> None:
    if scope["state"].get("auth_kind") is not None:
        return
    names = {name.lower() for name, _value in scope.get("headers") or ()}
    has_key = _SERVICE_KEY_HEADER in names
    template = getattr(scope.get("route"), "path", None)
    if status in (401, 403):
        pass
    elif status == 503:
        if not has_key:
            return
    elif status == 422:
        if has_key or template is None or not _service_only(scope.get("app"), template):
            return
    else:
        return
    method = str(scope.get("method", "")).upper()
    if method not in _METHODS:
        method = _OTHER_METHOD
    if (method, template) in _UNREPORTED:
        return
    credential = "service_key" if has_key else "user" if names & _USER_CREDENTIAL_HEADERS else "none"
    _record((template or UNMATCHED_ROUTE, _reason(status, credential), credential), method, status)


class AuthRejectionMiddleware:
    """Pure ASGI. Registered innermost, so it sees the response as the router
    produced it and the scope the router matched the route on."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Seeded here so the state a guard writes through any inner copy of
        # the scope is the object read back below.
        scope.setdefault("state", {})

        async def observing_send(message):
            if message["type"] == "http.response.start":
                try:
                    _observe(scope, message["status"])
                except Exception:
                    pass
            await send(message)

        await self.app(scope, receive, observing_send)
