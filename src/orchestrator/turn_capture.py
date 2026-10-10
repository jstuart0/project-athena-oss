"""Conversation capture for the orchestrator's answer routes.

A route is decorated with ``@captures_turn("<route>")``. The handler binds the
turn right after the owner-PIN check (``bind``, or ``bind_withheld`` on the PIN
branch), optionally ``observe``s the finished graph state, and the decorator
submits exactly one record to the process-wide ``CaptureSink`` when the response
is done: at return for a plain response, when the stream ends (or is closed
mid-way) for a streaming one. Nothing here awaits or does I/O on the response
path; ``submit`` is a ``queue.put_nowait`` behind a flag check.

Binding rules:

* An unbound capture is never submitted. ``bind`` is called only after the PIN
  check returned, so a PIN utterance can't reach a record as text.
* ``bind_withheld`` takes no text parameter. The PIN branch can only produce a
  content-free marker row (the anchor for the writer's sticky withholding).
* ``bind`` withholds the whole row for PIN-shaped queries and digit-redacts the
  rest; the writer sanitises again (``shared.conversation_capture``).

Import rule (R2-C3): this module never imports ``orchestrator.main``.
"""
from __future__ import annotations

import contextvars
import functools
import logging
import os
import re
import socket
import time
from typing import Any, Callable, Dict, Optional

from orchestrator.metrics import conversation_capture_total
from orchestrator.mode_permission import detect_owner_mode_command, is_public_caller
from orchestrator.session_keys import (
    CALLER_CLASS_GUEST,
    CALLER_CLASS_OTHER,
    CALLER_CLASS_OWNER,
    CALLER_CLASS_PUBLIC,
    id_class,
)
from shared import conversation_capture as contract
from shared.output_channel import channel_for_interface_type

logger = logging.getLogger(__name__)

ROUTE_MARKER = "__athena_capture_route__"

OUTCOME_ANSWERED = "answered"
OUTCOME_NO_ANSWER = "no_answer"
OUTCOME_ERROR = "error"
OUTCOME_INCOMPLETE = "incomplete"

_ERROR_STATE_MAX_CHARS = contract.ERROR_MAX_CHARS

_current: contextvars.ContextVar[Optional["TurnCapture"]] = contextvars.ContextVar("turn_capture", default=None)


_CALLER_TRUST_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def audience_class_for(audience: Any, caller_trust: Optional[str] = None) -> str:
    """The audience class of a turn, from server-derived inputs only.

    Mirrors main.py's ``_session_caller_class`` / ``_audience_session_class``
    (a drift test compares them): a public caller is ``public``; otherwise
    ``owner`` only for a proven owner, ``guest`` only for the server's own
    verified guest mode, else the household.
    """
    if is_public_caller(caller_trust) or getattr(audience, "public", False):
        return CALLER_CLASS_PUBLIC
    if getattr(audience, "owner_proven", False):
        return CALLER_CLASS_OWNER
    if getattr(audience, "mode", None) == "guest" and getattr(audience, "guest_verified", False):
        return CALLER_CLASS_GUEST
    return CALLER_CLASS_OTHER


def _valid_session_id(session_id: Any) -> bool:
    return isinstance(session_id, str) and 0 < len(session_id) <= contract.FIELD_WIDTHS["session_id"]


def _field(state: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from an ``OrchestratorState`` or a graph dict."""
    if isinstance(state, dict):
        return state.get(name, default)
    return getattr(state, name, default)


def _label(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _number(value: Any) -> Optional[float]:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


class TurnCapture:
    """The capture of one turn on one route.

    All methods are synchronous, cheap and never raise.
    """

    def __init__(self, route: str, sink: Optional[contract.CaptureSink]):
        self.route = route
        self._sink = sink
        self._bound = False
        self._withheld_marker = False
        self._submitted = False
        self._started = time.monotonic()
        self._labels: Dict[str, Any] = {}
        self._query = ""
        self._withheld = False
        self._answer: Optional[str] = None
        self._answered = False
        self._outcome: Optional[str] = None
        self._error_class: Optional[str] = None
        self._cache_hit = False
        self._observed: Dict[str, Any] = {}
        self._detail: Dict[str, Any] = {}

    # -- binding -----------------------------------------------------------

    def bind(
        self,
        *,
        session_id: str,
        query: str,
        interface_type: Optional[str],
        room: Optional[str],
        user_mode: Optional[str],
        audience: Any,
        caller_trust: Optional[str] = None,
        stay_read_only: bool = False,
    ) -> None:
        """Start recording this turn. Call it only after the PIN check returned.

        Every argument is server-derived: ``user_mode`` and ``audience`` from
        ``resolve_request_authorization``, ``session_id`` from the server-built
        session, ``caller_trust`` set by server code. The audience class is
        derived here from ``audience`` and ``caller_trust``, never passed in. A
        session id that isn't a non-empty string within its column width leaves
        the capture unbound. A second ``bind`` on the same capture (a nested call
        that reuses it) is ignored.
        """
        try:
            if self._bound:
                return
            if not _valid_session_id(session_id):
                logger.warning("turn_capture_bind_refused", extra={"reason": "invalid_session_id"})
                return
            self._bound = True
            self._started = time.monotonic()
            self._labels = {
                "session_id": session_id,
                "channel": channel_for_interface_type(interface_type).value,
                "audience_class": audience_class_for(audience, caller_trust),
                "session_class": id_class(session_id),
                "room": room,
                "user_mode": user_mode,
                "interface_type": interface_type,
            }
            if contract.withhold_reason(query, detect_owner_mode_command) is not None:
                self._withheld = True
                self._query = ""
            else:
                self._query = contract.redact_digits(query[: contract.QUERY_MAX_CHARS]) or ""
            self._detail = {"stay_read_only": bool(stay_read_only)}
            if isinstance(caller_trust, str) and _CALLER_TRUST_RE.match(caller_trust):
                self._detail["caller_trust_tier"] = caller_trust
            if audience is not None:
                for name in ("owner_proven", "guest_verified", "public", "degraded"):
                    value = getattr(audience, name, None)
                    if isinstance(value, bool):
                        self._detail[name] = value
        except Exception as exc:
            self._bound = False
            logger.warning("turn_capture_bind_failed", extra={"error_type": type(exc).__name__})

    def bind_withheld(
        self,
        *,
        session_id: str,
        audience: Any,
        interface_type: Optional[str],
        room: Optional[str],
        caller_trust: Optional[str] = None,
    ) -> None:
        """The owner-PIN branch: a content-free marker row, no text at all.

        There is deliberately no query, answer or message parameter. The marker
        takes the session class of the same server-built id the next turns use,
        so the writer's sticky rule finds it.
        """
        try:
            if self._bound:
                return
            if not _valid_session_id(session_id):
                logger.warning("turn_capture_bind_refused", extra={"reason": "invalid_session_id"})
                return
            self._bound = True
            self._withheld_marker = True
            self._withheld = True
            self._labels = {
                "session_id": session_id,
                "channel": channel_for_interface_type(interface_type).value,
                "audience_class": audience_class_for(audience, caller_trust),
                "session_class": id_class(session_id),
                "room": room,
                "user_mode": None,
                "interface_type": interface_type,
            }
            self._outcome = contract.OUTCOME_WITHHELD
        except Exception as exc:
            self._bound = False
            logger.warning("turn_capture_bind_failed", extra={"error_type": type(exc).__name__})

    # -- observation -------------------------------------------------------

    def observe(self, state: Any) -> None:
        """Copy the allowlisted fields of a finished graph state."""
        if self._withheld_marker:
            return
        try:
            tools = [k for k in (_field(state, "retrieved_data") or {}) if isinstance(k, str)]
            data_source = _field(state, "data_source")
            if isinstance(data_source, str) and data_source not in tools:
                tools.append(data_source)
            error = _field(state, "error")
            self._observed = {
                "request_id": _field(state, "request_id"),
                "intent": _label(_field(state, "intent")),
                "confidence": _number(_field(state, "confidence")),
                "is_fallback": bool(_field(state, "is_fallback", False)),
                "model_used": _field(state, "model_used"),
                "rag_tools_used": tools or None,
                "tokens_generated": _field(state, "llm_tokens") or None,
                "tokens_per_second": _number(_field(state, "llm_tokens_per_second")) or None,
                "validation_passed": _field(state, "validation_passed"),
                "complexity": _label(_field(state, "complexity")),
                "base_knowledge_populated": bool(_field(state, "base_knowledge_populated", False)),
                "node_timings_ms": dict(_field(state, "node_timings") or {}) or None,
                "error": str(error)[:_ERROR_STATE_MAX_CHARS] if error else None,
            }
            for key in ("validation_reason", "was_truncated", "is_multi_intent", "is_novel_intent", "prompt_tokens"):
                value = _field(state, key)
                if value is not None:
                    self._detail[key] = value
            if not self._answered:
                answer = _field(state, "answer")
                if isinstance(answer, str):
                    self._answer = answer
        except Exception as exc:
            logger.warning("turn_capture_observe_failed", extra={"error_type": type(exc).__name__})

    def answered(self, text: Optional[str], *, outcome: str = OUTCOME_ANSWERED, cache_hit: bool = False) -> None:
        """Record the final raw (unrendered) answer. The first call wins."""
        if self._withheld_marker or self._answered:
            return
        self._answered = True
        self._answer = text if isinstance(text, str) else None
        self._outcome = outcome
        self._cache_hit = bool(cache_hit)

    def failed(self, exc: BaseException) -> None:
        """The turn raised: the outcome is ``error`` and only the class name is kept."""
        if self._withheld_marker or self._outcome == OUTCOME_ERROR:
            return
        self._outcome = OUTCOME_ERROR
        self._error_class = type(exc).__name__

    # -- submission --------------------------------------------------------

    def submit(self) -> None:
        """Hand the record to the sink. Idempotent; a no-op if unbound; never raises."""
        if self._submitted or not self._bound or self._sink is None:
            return
        self._submitted = True
        try:
            self._sink.enqueue(self._record())
        except Exception as exc:
            logger.warning("turn_capture_submit_failed", extra={"error_type": type(exc).__name__})

    def _record(self) -> contract.CaptureRecord:
        marker = self._withheld_marker
        outcome = self._outcome or (OUTCOME_ANSWERED if self._answer else OUTCOME_INCOMPLETE)
        if self._outcome == OUTCOME_ANSWERED and not self._answer:
            outcome = OUTCOME_NO_ANSWER
        observed = {} if marker else self._observed
        error = None if self._withheld else (self._error_class or observed.get("error"))
        detail = None if marker else dict(self._detail, cache_hit=self._cache_hit)
        if detail is not None and outcome in (OUTCOME_INCOMPLETE, OUTCOME_ANSWERED, OUTCOME_NO_ANSWER):
            detail["stream_completed"] = outcome != OUTCOME_INCOMPLETE
        elapsed_ms = int((time.monotonic() - self._started) * 1000)
        return contract.CaptureRecord(
            session_id=self._labels["session_id"],
            query_text="" if marker else self._query,
            response_text=None if (marker or self._withheld) else self._answer,
            route=self.route,
            channel=self._labels["channel"],
            audience_class=self._labels["audience_class"],
            session_class=self._labels["session_class"],
            outcome=outcome,
            text_withheld=self._withheld,
            request_id=observed.get("request_id"),
            room=self._labels["room"],
            user_mode=self._labels["user_mode"],
            interface_type=self._labels["interface_type"],
            intent=observed.get("intent"),
            confidence=observed.get("confidence"),
            is_fallback=observed.get("is_fallback"),
            response_time_ms=None if marker else elapsed_ms,
            model_used=observed.get("model_used"),
            rag_tools_used=observed.get("rag_tools_used"),
            tokens_generated=observed.get("tokens_generated"),
            tokens_per_second=observed.get("tokens_per_second"),
            validation_passed=observed.get("validation_passed"),
            error_message=error,
            complexity=observed.get("complexity"),
            base_knowledge_populated=observed.get("base_knowledge_populated"),
            node_timings_ms=observed.get("node_timings_ms"),
            debug_detail=detail,
        )


def current_capture() -> TurnCapture:
    """The capture of the request being handled; an inert one outside a decorated route."""
    return _current.get() or TurnCapture("none", None)


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------

def _answer_from_response(capture: TurnCapture, response: Any) -> None:
    """Take the answer from a returned ``QueryResponse``-shaped result, if not answered yet."""
    answer = getattr(response, "answer", None)
    if capture._answered or not isinstance(answer, str):
        return
    metadata = getattr(response, "metadata", None)
    cache_hit = bool(metadata.get("cache_hit")) if isinstance(metadata, dict) else False
    capture.answered(answer, cache_hit=cache_hit)


def _wrap_stream(capture: TurnCapture, response: Any) -> None:
    """Wrap ``response.body_iterator`` so the record is submitted when the stream ends.

    The wrapper yields each chunk unchanged as it arrives (no buffering) and does
    its only work in ``finally``, which also runs when the client disconnects or
    the stream is closed early.
    """
    inner = response.body_iterator

    async def stream():
        # The capture stays current while the body runs, so a generator can use
        # current_capture() as well as hold the reference it took at handler top.
        token = _current.set(capture)
        try:
            async for chunk in inner:
                yield chunk
        except Exception as exc:
            capture.failed(exc)
            raise
        finally:
            # Submit first: it is sync and does no I/O, so a second cancellation
            # during the awaited close below can't skip it.
            capture.submit()
            try:
                _current.reset(token)
            except ValueError:
                pass  # closed from another context; nothing leaks past this stream
            closer = getattr(inner, "aclose", None)
            if closer is not None:
                try:
                    await closer()
                except Exception:
                    pass

    response.body_iterator = stream()


def captures_turn(route: str) -> Callable:
    """Decorate an answer route so every turn is recorded exactly once.

    The outermost decorated call owns the capture and submits it. A nested
    decorated call (``/v1/chat/completions`` non-stream calls ``process_query``)
    reuses the owner's capture, still takes its answer from the response it
    returns, and does not submit.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            outer = _current.get()
            if outer is not None:
                result = await func(*args, **kwargs)
                _answer_from_response(outer, result)
                return result

            from orchestrator.nodes import _runtime  # lazy: the nodes package is fully loaded by request time

            capture = TurnCapture(route, _runtime.get_turn_capture())
            token = _current.set(capture)
            streaming = False
            try:
                result = await func(*args, **kwargs)
                body_iterator = getattr(result, "body_iterator", None)
                if body_iterator is not None:
                    _wrap_stream(capture, result)
                    streaming = True
                else:
                    _answer_from_response(capture, result)
                return result
            except BaseException as exc:
                if isinstance(exc, Exception):
                    capture.failed(exc)
                raise
            finally:
                _current.reset(token)
                if not streaming:
                    capture.submit()

        setattr(wrapper, ROUTE_MARKER, route)
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# Construction (called from the orchestrator lifespan)
# ---------------------------------------------------------------------------

def create_sink(database_url: Optional[str], environ: Optional[Dict[str, str]] = None) -> contract.CaptureSink:
    """Build (not start) the process-wide sink.

    An unconfigured or unsupported database yields a sink in state
    ``unconfigured`` that drops everything, with one WARNING naming the DSN
    ``source`` only (never the DSN).
    """
    environ = os.environ if environ is None else environ
    resolved = contract.resolve_database_dsn(database_url, environ)
    if resolved.legacy_disagrees:
        logger.warning("conversation_capture_legacy_vars_ignored", extra={"athena_db_vars_set": True, "differs_from_database_url": True})
    if not resolved.configured:
        logger.warning("conversation_capture_unconfigured", extra={"source": resolved.source})
        return contract.CaptureSink(None, on_result=_count)
    store = contract.AsyncpgCaptureStore(resolved.dsn, replica=contract.replica_id(environ.get("HOSTNAME") or socket.gethostname()))
    return contract.CaptureSink(store, on_result=_count, owner_command=detect_owner_mode_command)


def _count(result: str) -> None:
    conversation_capture_total.labels(result=result).inc()
