"""Keep conversation text out of the legacy analytics and live-event paths.

Captured conversations are governed by Analytics Mode, retention, owner-only
text and PIN withholding (see services/conversation_capture.py). The older
``conversation_analytics`` log and the Admin Jarvis live feed carry no
conversation text at all, whatever an older orchestrator sends: this strips
it on the way in, so a mixed-version rollout can't reintroduce it.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# Keys that hold what a person said or what the assistant answered.
TEXT_KEYS = frozenset({
    "query", "query_text", "query_preview", "response", "response_text", "response_preview", "answer",
    "transcript", "utterance",
})


def strip_conversation_text(data: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A copy of ``data`` without text keys, at any depth; ``query`` becomes ``query_len``.

    Lengths are kept (a length is not text), so dashboards that count or size
    queries keep working.
    """
    if not isinstance(data, dict):
        return data
    clean: Dict[str, Any] = {}
    for key, value in data.items():
        if key in TEXT_KEYS:
            if key == "query" and isinstance(value, str) and "query_len" not in data:
                clean["query_len"] = len(value)
            continue
        clean[key] = strip_conversation_text(value) if isinstance(value, dict) else value
    return clean
