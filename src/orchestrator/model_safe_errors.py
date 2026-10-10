"""Error text that is safe to hand to the model, the speaker or an API client.

Backend errors carry operator detail: environment-variable names, internal
URLs and addresses, file paths, class names. Nothing of that belongs in a
prompt, a spoken sentence or a response body. The policy here is an allowlist:

- every error string bound for the model, the speaker or a client becomes one
  of a few fixed phrases, chosen by category (`not_configured`, `bad_request`,
  `unavailable`, `timeout`, `failed`);
- the one exception is text carried by `UserSafeText`, a marker only
  `make_user_safe` creates, only for a 4xx response's `detail` that is a single
  line, at most 200 characters, made of letters, digits, spaces and `.,'-`, and
  free of IPv4 literals and `...Error`/`...Exception` class names. That is how a
  tool's guidance ("No trains found for that date") still reaches the model.

The raw text stays where operators look for it: in logs and in
`tool_usage_metrics`. Log lines carry `log_safe(exc)` (class and HTTP status),
mirroring `calendar_sources.safe_error`.

R2-C3: a leaf module; imports nothing from the orchestrator.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict, Optional

import httpx

MAX_USER_SAFE_LENGTH = 200
_USER_SAFE_CHARS = re.compile(r"^[A-Za-z0-9 .,'-]+$")
_IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_CLASS_NAME = re.compile(r"\b[A-Za-z0-9]*(?:Error|Exception)\b")
_HOST_LIKE = re.compile(r"\b[\w-]+(?:\.[\w-]+){2,}\b")          # api.internal.corp, 10.0.0.5, 1.2.3
MAX_USER_SAFE_WORDS = 30

PHRASES: Dict[str, str] = {
    "not_configured": "That service isn't set up yet.",
    "bad_request": (
        "That request needs more detail. Ask the user for the missing information, "
        "such as a place or a date."
    ),
    "unavailable": "That service is unavailable right now.",
    "timeout": "That service took too long to respond.",
    "failed": "That didn't work.",
}
TOOL_ERROR_FALLBACK = "The tool returned an error."
_PHRASE_VALUES = frozenset(PHRASES.values())

# Keys whose values describe a failure. Inside an error object each is replaced.
SCRUBBED_KEYS = frozenset({"error", "detail", "message", "msg", "reason", "exception", "errors"})
_NESTED_ERROR_KEYS = ("error", "errors")

_NOT_CONFIGURED_HINTS = ("not configured", "not set", "unconfigured", "api key", "credential", "unauthor", "forbidden")
_TIMEOUT_HINTS = ("timed out", "timeout")
_UNAVAILABLE_HINTS = ("connection", "unavailable", "unreachable", "refused")
_MAX_DEPTH = 40


_SEAL = object()


class UserSafeText(str):
    """Text vetted by `make_user_safe`. Only that function can construct it: the
    constructor needs a module-private sentinel, so a stray `UserSafeText(text)`
    raises instead of vouching for the text."""

    __slots__ = ()

    def __new__(cls, value: str, _seal: object = None):
        if _seal is not _SEAL:
            raise TypeError("UserSafeText is built by make_user_safe only")
        return super().__new__(cls, value)


def make_user_safe(detail: Any, status_code: Optional[int]) -> Optional[UserSafeText]:
    """`detail` as a `UserSafeText` when it is a 4xx body detail that passes every
    rule above, else None. Only `rag_client` calls this, for a 4xx response."""
    if not isinstance(detail, str) or not isinstance(status_code, int) or not 400 <= status_code < 500:
        return None
    if not detail or len(detail) > MAX_USER_SAFE_LENGTH or not _USER_SAFE_CHARS.match(detail):
        return None
    if _IPV4.search(detail) or _HOST_LIKE.search(detail) or _CLASS_NAME.search(detail):
        return None
    if len(detail.split()) > MAX_USER_SAFE_WORDS:
        return None
    return UserSafeText(detail, _SEAL)


class RAGToolError(Exception):
    """A failed RAG response raised as an exception.

    `str(error)` is the raw text, for logs and `tool_usage_metrics`. What the
    model may see is `model_safe_error(error)`: the carried `UserSafeText` when
    the service gave a vetted 4xx detail, else a phrase for the status.
    """

    def __init__(self, response: Any, default: str = ""):
        self.status_code: Optional[int] = getattr(response, "status_code", None)
        self.user_detail: Optional[UserSafeText] = getattr(response, "user_detail", None)
        self.raw = getattr(response, "error", None) or default
        super().__init__(self.raw)


def _status_of(value: Any) -> Optional[int]:
    if isinstance(value, RAGToolError):
        return value.status_code
    if isinstance(value, httpx.HTTPStatusError):
        return value.response.status_code
    status = getattr(value, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def _category(value: Any, status_code: Optional[int]) -> str:
    status = status_code if status_code is not None else _status_of(value)
    text = str(value).lower()
    if isinstance(value, (asyncio.TimeoutError, httpx.TimeoutException)) or any(h in text for h in _TIMEOUT_HINTS):
        return "timeout"
    # A client error status decides on its own: the service understood the request and wants something
    # different from the caller ("not configured" inside a 400 is the caller's missing input, not a setup problem).
    if isinstance(status, int) and 400 <= status < 500 and status != 429:
        if status in (401, 403):
            return "not_configured"
        return "bad_request" if status in (400, 404, 409, 422) else "failed"
    if any(h in text for h in _NOT_CONFIGURED_HINTS):
        return "not_configured"
    if isinstance(status, int) and (status == 429 or status >= 500):
        return "unavailable"
    if isinstance(value, (httpx.ConnectError, ConnectionError)) or any(h in text for h in _UNAVAILABLE_HINTS):
        return "unavailable"
    return "failed"


def model_safe_error(exc_or_text: Any, *, status_code: Optional[int] = None) -> str:
    """The text a model, a speaker or a client may see for a failure.

    A `UserSafeText` (given directly, or carried by a `RAGToolError`) comes back
    as is; anything else becomes the fixed phrase for its category. Never raises.
    """
    try:
        if isinstance(exc_or_text, UserSafeText):
            return exc_or_text
        if isinstance(exc_or_text, str) and exc_or_text in _PHRASE_VALUES:
            return exc_or_text          # idempotent: a phrase already chosen is never re-categorized
        if isinstance(exc_or_text, RAGToolError) and exc_or_text.user_detail:
            return exc_or_text.user_detail
        return PHRASES[_category(exc_or_text, status_code)]
    except Exception:
        return PHRASES["failed"]


def tool_error_result(exc_or_text: Any, *, status_code: Optional[int] = None) -> Dict[str, str]:
    """The one constructor of a failed tool's result: `{"error": <safe text>}`."""
    return {"error": model_safe_error(exc_or_text, status_code=status_code)}


def is_error_object(value: Any) -> bool:
    """A dict that reports a failure: a truthy top-level `error`, or `success: False`
    (which includes everything `tool_error_result` builds). A dict that merely has
    a `message`, `reason` or `detail` is a successful payload."""
    return isinstance(value, dict) and (bool(value.get("error")) or value.get("success") is False)


def _scrub_leaf_or_container(value: Any, depth: int) -> Any:
    """Everything under an error key: containers keep their shape, every leaf
    (string, number, None, exception, anything) becomes a fixed phrase unless
    it is a `UserSafeText`."""
    if depth > _MAX_DEPTH:
        raise ValueError("too deep")
    if isinstance(value, UserSafeText):
        return value
    if isinstance(value, dict):
        return {k: _scrub_leaf_or_container(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_leaf_or_container(v, depth + 1) for v in value]
    return model_safe_error(value)


def _scrub(value: Any, depth: int, seen: set) -> Any:
    if depth > _MAX_DEPTH:
        raise ValueError("too deep")
    if isinstance(value, (dict, list, tuple)):
        if id(value) in seen:
            raise ValueError("cycle")
        seen = seen | {id(value)}
    if isinstance(value, dict):
        error_object = is_error_object(value)
        out = {}
        for key, item in value.items():
            if error_object and key in SCRUBBED_KEYS:
                out[key] = _scrub_leaf_or_container(item, depth + 1)
            else:
                out[key] = _scrub(item, depth + 1, seen)
        return out
    if isinstance(value, (list, tuple)):
        return [_scrub(item, depth + 1, seen) for item in value]
    return value


def scrub_tool_result(result: Any) -> Any:
    """A tool result with every failure description made safe.

    Only error objects are touched: for each dict that `is_error_object` (at any
    depth), the values under its own `SCRUBBED_KEYS` are replaced; everything
    else, including every successful dict and its `message` or `reason`, comes
    back equal. If the result can't be processed (a cycle, a value JSON can't
    carry, any exception) the fixed string `TOOL_ERROR_FALLBACK` comes back.
    Never raises.
    """
    try:
        scrubbed = _scrub(result, 0, frozenset())
        json.dumps(scrubbed)
        return scrubbed
    except Exception:
        return TOOL_ERROR_FALLBACK


def log_safe(exc: BaseException) -> Dict[str, Any]:
    """Log fields for an error: its class and HTTP status, never its text."""
    return {"error_class": type(exc).__name__, "http_status": _status_of(exc)}
