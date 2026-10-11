"""Keep conversation text out of the legacy analytics and live-event paths.

Captured conversations are governed by Analytics Mode, retention, owner-only
text and PIN withholding (see services/conversation_capture.py). The older
``conversation_analytics`` log, the pipeline events and the Admin Jarvis live
feed carry no conversation text at all, whatever an older service sends: this
strips it on the way in and on the way out, so a mixed-version rollout and
rows stored by earlier versions can't expose it.
"""
from __future__ import annotations

from typing import Any, Optional

# Keys that hold what a person said or what the assistant answered, in any payload.
TEXT_KEYS = frozenset({
    "query", "query_text", "query_preview", "response", "response_text", "response_preview", "answer",
    "transcript", "utterance",
})

# Generic key names that are text only inside a transcript-bearing event; other
# events use "text", "content" or "message" for status and error strings.
TRANSCRIPT_EVENT_TYPES = frozenset({"stt_complete", "stt_partial", "query_received", "transcript"})
TRANSCRIPT_EVENT_KEYS = frozenset({"text", "content", "message", "transcript", "utterance"})


def strip_conversation_text(data: Any, event_type: Optional[str] = None) -> Any:
    """A copy of ``data`` without text, through dicts and lists at any depth.

    ``query`` / ``text`` become ``query_len`` / ``text_len`` (a length is not
    text, so dashboards that size things keep working). ``event_type`` widens
    the key set for transcript events.
    """
    extra = TRANSCRIPT_EVENT_KEYS if event_type in TRANSCRIPT_EVENT_TYPES else frozenset()
    return _strip(data, extra)


def _strip(value: Any, extra: frozenset) -> Any:
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            if key in TEXT_KEYS or key in extra:
                if isinstance(item, str) and key in ("query", "text") and f"{key}_len" not in value:
                    clean[f"{key}_len"] = len(item)
                continue
            clean[key] = _strip(item, extra)
        return clean
    if isinstance(value, list):
        return [_strip(item, extra) for item in value]
    return value


# ---------------------------------------------------------------------------
# Pipeline events: a per-event-type allowlist (not a denylist)
# ---------------------------------------------------------------------------
#
# A pipeline event describes what the pipeline did, never what was said. Only the
# keys listed here survive, and only as scalars: ids, lengths, durations, counts,
# tool/model/intent names, success flags and short status values. Unknown keys,
# nested values, lists and anything shaped like text are dropped, so a new or
# renamed field can't leak by default. Strings that would carry text become
# lengths (``error`` -> ``error_len``, ``result_summary`` -> ``result_len``,
# ``text`` -> ``text_len``); structures become counts.

import re

_LABEL_MAX_CHARS = 100
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_ .:/\-]{1,%d}$" % _LABEL_MAX_CHARS)

# Keys whose string value is a name or short status (checked against _SAFE_LABEL).
_LABEL_KEYS = frozenset({
    "interface", "engine", "model", "tool_name", "tool_source", "intent", "mode", "room", "room_name",
    "protocol", "status", "stage",
})
# Keys that are plain numbers or booleans.
_NUMBER_KEYS = frozenset({
    "confidence", "tokens", "token_count", "chunk_count", "chunks", "progress", "audio_duration_ms",
    "duration_ms", "total_duration_ms", "execution_time_ms", "count",
})
_FLAG_KEYS = frozenset({"success", "requires_llm", "interrupted", "is_final", "partial"})
# Length/count suffixes are always safe numbers.
_SAFE_SUFFIXES = ("_len", "_length", "_count", "_ms")
# value -> (new key, how to measure it)
_MEASURED = {
    "text": "text_len", "transcript": "text_len", "utterance": "text_len", "query": "query_len",
    "query_preview": "query_len", "response": "response_len", "response_preview": "response_len",
    "answer": "response_len", "result_summary": "result_len", "error": "error_len", "message": "message_len",
    "content": "content_len",
}
_COUNTED = {"entities": "entity_count", "args": "arg_count"}

_EVENT_KEYS = {
    "session_start": {"interface", "protocol", "interrupted", "mode", "room", "room_name"},
    "session_end": {"success", "duration_ms", "room_name"},
    "stt_start": {"engine"},
    "stt_progress": {"engine", "progress", "is_final", "partial", "duration_ms"},
    "stt_complete": {"engine", "duration_ms"},
    "intent_classifying": set(),
    "intent_classified": {"intent", "confidence", "requires_llm"},
    "tool_selecting": {"tool_name", "tool_source"},
    "tool_selected": {"tool_name", "tool_source"},
    "tool_executing": {"tool_name"},
    "tool_complete": {"tool_name", "success", "execution_time_ms"},
    "tool_error": {"tool_name"},
    "llm_generating": {"model"},
    "llm_streaming": {"model", "tokens", "token_count", "chunk_count", "chunks", "duration_ms"},
    "llm_complete": {"model", "tokens", "duration_ms"},
    "tts_start": {"engine"},
    "tts_complete": {"engine", "audio_duration_ms"},
    "response_ready": {"total_duration_ms"},
    "query_received": {"interface", "room"},
    "response_generated": {"intent"},
}
_GENERIC_KEYS = frozenset({"interface", "engine", "model", "tool_name", "intent", "status", "success", "duration_ms",
                           "total_duration_ms", "tokens", "confidence"})
# Event-level fields a broadcast or stored event may carry.
_EVENT_FIELDS = frozenset({"event_type", "session_id", "interface", "timestamp", "data", "id", "created_at", "offset_ms"})


def _event_key(event_type: Any) -> str:
    return str(getattr(event_type, "value", event_type) or "").strip().lower()


def _measure(value: Any) -> Optional[int]:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, (dict, list)):
        return len(value)
    return None


def sanitize_event_data(event_type: Any, data: Any) -> dict:
    """The allowlisted, scalar-only form of an event's ``data`` (``{}`` if it isn't a dict)."""
    if not isinstance(data, dict):
        return {}
    allowed = _EVENT_KEYS.get(_event_key(event_type), _GENERIC_KEYS)
    clean: dict = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        if key in _MEASURED and (key not in allowed):
            measured = _measure(value)
            if measured is not None and _MEASURED[key] not in data:
                clean[_MEASURED[key]] = measured
            continue
        if key in _COUNTED:
            measured = _measure(value)
            if measured is not None:
                clean[_COUNTED[key]] = measured
            continue
        if key.endswith(_SAFE_SUFFIXES):
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = value
            continue
        if key not in allowed:
            continue
        if key in _LABEL_KEYS:
            if isinstance(value, str) and _SAFE_LABEL.match(value):
                clean[key] = value
        elif key in _NUMBER_KEYS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = value
        elif key in _FLAG_KEYS:
            if isinstance(value, bool):
                clean[key] = value
    return clean


def sanitize_event(event: Any) -> Any:
    """A whole event (broadcast or read shape): known event-level fields only, ``data`` allowlisted."""
    if not isinstance(event, dict):
        return event
    clean = {k: v for k, v in event.items() if k in _EVENT_FIELDS and k != "data"}
    clean["data"] = sanitize_event_data(event.get("event_type"), event.get("data"))
    return clean


# ---------------------------------------------------------------------------
# conversation_analytics metadata: an allowlist as well
# ---------------------------------------------------------------------------

_ANALYTICS_LABELS = frozenset({
    "intent", "system_mapping", "user_id", "room", "zone", "mode", "status", "interface", "model", "tool_name",
    "stage", "reason", "decision", "category", "kind", "source",
})
_ANALYTICS_FLAGS = frozenset({
    "has_rag_service", "is_novel", "success", "is_followup", "triggered", "resolved", "requires_clarification",
})
_ANALYTICS_NUMBERS = frozenset({"confidence", "similarity", "tokens", "count", "turn_count", "latency_ms", "duration_ms"})


def sanitize_analytics_metadata(metadata: Any) -> Optional[dict]:
    """The allowlisted form of a ``conversation_analytics`` event's metadata.

    Names, flags and numbers only. Text-bearing fields become lengths (an ``error`` string keeps
    its truthiness as ``error: true`` plus ``error_len``, which the error-rate report reads).
    Applied when an event is stored and again when rows are read, so rows written by earlier
    versions never show their old text.
    """
    if metadata is None:
        return None
    if isinstance(metadata, str):
        import json

        try:
            metadata = json.loads(metadata)
        except ValueError:
            return {}
    if not isinstance(metadata, dict):
        return {}
    clean: dict = {}
    for key, value in metadata.items():
        if not isinstance(key, str):
            continue
        if key == "error":
            if value:
                clean["error"] = True
                measured = _measure(value)
                if measured is not None:
                    clean["error_len"] = measured
        elif key in _MEASURED:
            measured = _measure(value)
            if measured is not None and _MEASURED[key] not in metadata:
                clean[_MEASURED[key]] = measured
        elif key.endswith(_SAFE_SUFFIXES) or key in _ANALYTICS_NUMBERS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                clean[key] = value
        elif key in _ANALYTICS_FLAGS:
            if isinstance(value, bool):
                clean[key] = value
        elif key in _ANALYTICS_LABELS:
            if isinstance(value, str) and _SAFE_LABEL.match(value):
                clean[key] = value
    return clean
