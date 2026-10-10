"""Deterministic fast path: which turns need no model, and when to defer.

`fast_path_reply` is the vocabulary lookup (pure, exact match, local clock).
`fast_path_open_question` is the guard that keeps the fast path from
answering a reply to an open question: a stored confirmation or clarification
context, or an assistant message that ended in a question. It reads the
conversation context once, tells "no context" from "read failed", and fails
closed (a read failure defers to the full pipeline).

R2-C3: imports `nodes._runtime`, `session_keys`, `shared.*` -- never
`orchestrator.main`.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from shared.fast_path_vocab import reply_for
from shared.local_time import local_now
from shared.logging_config import configure_logging

from orchestrator.nodes import _runtime
from orchestrator.session_keys import context_storage_key

logger = configure_logging("orchestrator.fast_path")

CONTEXT_READ_TIMEOUT_SECONDS = 2.0

REASON_PENDING_CONFIRMATION = "pending_confirmation"
REASON_AWAITING_CONTEXT = "awaiting_context"
REASON_OPEN_QUESTION = "open_question"
REASON_CONTEXT_UNREADABLE = "context_unreadable"

_AWAITING_PREFIX = "awaiting_"
_PENDING_KEY = "pending_write_confirmation"
_TRAILING_CLOSERS = "\"')]}”’»"
_QUESTION_MARKS = ("?", "？")


@dataclass(frozen=True)
class FastPathReply:
    kind: str
    text: str


def fast_path_reply(query: Optional[str]) -> Optional[FastPathReply]:
    """The deterministic reply for an exact-match trivial turn, else None.
    Never raises."""
    try:
        found = reply_for(query, local_now())
    except Exception:
        return None
    return FastPathReply(kind=found[0], text=found[1]) if found else None


def last_assistant_text(session: Any) -> Optional[str]:
    """The text of the session's last assistant message, or None.

    A message the fast path itself wrote ("Hello. How can I help?") is not an
    open question: it carries `metadata.fast_path` and reads as no question.
    """
    for message in reversed(getattr(session, "messages", None) or []):
        if message.get("role") != "assistant":
            continue
        if (message.get("metadata") or {}).get("fast_path"):
            return None
        content = message.get("content")
        return content if isinstance(content, str) else None
    return None


def _ends_with_question(text: Optional[str]) -> bool:
    if not text:
        return False
    return text.rstrip().rstrip(_TRAILING_CLOSERS).rstrip().endswith(_QUESTION_MARKS)


def _context_reason(parameters: Mapping[str, Any]) -> Optional[str]:
    if parameters.get(_PENDING_KEY):
        return REASON_PENDING_CONFIRMATION
    for key, value in parameters.items():
        if isinstance(key, str) and key.startswith(_AWAITING_PREFIX) and value:
            return REASON_AWAITING_CONTEXT
    return None


async def _redis_context_parameters(session_id: str) -> Optional[Mapping[str, Any]]:
    """The stored context's parameters; None when none is stored. Raises when
    the store can't be read."""
    cache = _runtime.get_cache_client()
    if not (cache and getattr(cache, "client", None)):
        return None
    raw = await asyncio.wait_for(
        cache.client.get(context_storage_key(session_id)), timeout=CONTEXT_READ_TIMEOUT_SECONDS
    )
    if not raw:
        return None
    data = json.loads(raw)
    parameters = data.get("parameters") or {}
    if not isinstance(parameters, dict):
        raise ValueError("context parameters are not a mapping")
    return parameters


def _memory_context_parameters(session_id: str) -> Mapping[str, Any]:
    entry = _runtime.get_memory_context().get(session_id)
    if not entry or entry.get("expires_at", 0) <= time.time():
        return {}
    return getattr(entry.get("context"), "parameters", None) or {}


async def fast_path_open_question(session_id: Optional[str], last_assistant: Optional[str]) -> Optional[str]:
    """Why the fast path must defer, or None when the turn can be answered.

    Reasons: `pending_confirmation` (any caller's pending write confirmation),
    `awaiting_context` (any `awaiting_*` clarification), `open_question` (the
    last assistant message ended with a question), `context_unreadable`
    (the store failed or held malformed data). Never raises.
    """
    try:
        if session_id:
            try:
                stored = await _redis_context_parameters(session_id)
            except Exception:
                return REASON_CONTEXT_UNREADABLE
            for parameters in (stored or {}, _memory_context_parameters(session_id)):
                reason = _context_reason(parameters)
                if reason:
                    return reason
        if _ends_with_question(last_assistant):
            return REASON_OPEN_QUESTION
        return None
    except Exception:
        return REASON_CONTEXT_UNREADABLE
