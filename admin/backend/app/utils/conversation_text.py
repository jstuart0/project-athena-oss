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
