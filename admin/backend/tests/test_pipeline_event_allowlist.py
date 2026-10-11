"""Pipeline events carry an allowlist of scalar fields, per event type.

An event says what the pipeline did, never what was said. Unknown keys, nested values and
anything shaped like text are dropped (a new field can't leak by default); text-bearing
fields become lengths. The same function runs when an event is emitted (before it is stored),
when it is broadcast, and when a stored event is read.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime

import pytest

from app.models import PipelineEvent
from app.routes import websocket as websocket_routes
from app.utils.conversation_text import sanitize_event, sanitize_event_data
from shared.events import EventType

SECRET = "turn on the secret garage light SECRET-WORDS"

# What each real emitter sends, plus text-bearing fields an older or custom producer might add.
PAYLOADS = {
    "session_start": ({"interface": "voice", "mode": "owner", "room": "porch", "query_len": 12, "query_preview": SECRET, "query": SECRET},
                      {"interface": "voice", "mode": "owner", "room": "porch", "query_len": 12}),
    "session_end": ({"success": False, "error": SECRET, "duration_ms": 40},
                    {"success": False, "duration_ms": 40, "error_len": len(SECRET)}),
    "stt_start": ({"engine": "whisper", "text": SECRET}, {"engine": "whisper", "text_len": len(SECRET)}),
    "stt_progress": ({"engine": "whisper", "text": SECRET, "partial": True, "progress": 0.5},
                     {"engine": "whisper", "partial": True, "progress": 0.5, "text_len": len(SECRET)}),
    "stt_complete": ({"text": SECRET, "engine": "whisper", "duration_ms": 90},
                     {"text_len": len(SECRET), "engine": "whisper", "duration_ms": 90}),
    "intent_classifying": ({"query": SECRET}, {"query_len": len(SECRET)}),
    "intent_classified": ({"intent": "control", "confidence": 0.9, "entities": {"room": SECRET}, "requires_llm": True},
                          {"intent": "control", "confidence": 0.9, "requires_llm": True, "entity_count": 1}),
    "tool_selecting": ({"tool_name": "websearch", "args": {"query": SECRET}},
                       {"tool_name": "websearch", "arg_count": 1}),
    "tool_selected": ({"tool_name": "websearch", "tool_source": "rag", "args": {"query": SECRET}},
                      {"tool_name": "websearch", "tool_source": "rag", "arg_count": 1}),
    "tool_executing": ({"tool_name": "websearch", "args": [SECRET]}, {"tool_name": "websearch", "arg_count": 1}),
    "tool_complete": ({"tool_name": "websearch", "success": True, "result_summary": SECRET, "execution_time_ms": 7},
                      {"tool_name": "websearch", "success": True, "execution_time_ms": 7, "result_len": len(SECRET)}),
    "tool_error": ({"tool_name": "websearch", "error": SECRET}, {"tool_name": "websearch", "error_len": len(SECRET)}),
    "llm_generating": ({"model": "qwen3:4b", "prompt": SECRET}, {"model": "qwen3:4b"}),
    "llm_streaming": ({"model": "qwen3:4b", "token": SECRET, "chunk": SECRET, "tokens": 5, "chunk_count": 2},
                      {"model": "qwen3:4b", "tokens": 5, "chunk_count": 2}),
    "llm_complete": ({"model": "qwen3:4b", "tokens": 9, "duration_ms": 300, "response": SECRET},
                     {"model": "qwen3:4b", "tokens": 9, "duration_ms": 300, "response_len": len(SECRET)}),
    "tts_start": ({"engine": "piper", "text": SECRET}, {"engine": "piper", "text_len": len(SECRET)}),
    "tts_complete": ({"engine": "piper", "text_length": 22, "audio_duration_ms": 900, "text": SECRET},
                     {"engine": "piper", "text_length": 22, "audio_duration_ms": 900, "text_len": len(SECRET)}),
    "response_ready": ({"response_len": 30, "total_duration_ms": 800, "response_preview": SECRET},
                       {"response_len": 30, "total_duration_ms": 800}),
}


def test_the_payload_table_covers_every_real_event_type():
    assert {t.value for t in EventType} <= set(PAYLOADS)


@pytest.mark.parametrize("event_type", sorted(PAYLOADS))
def test_each_event_type_keeps_only_its_scalar_fields(event_type):
    raw, expected = PAYLOADS[event_type]
    got = sanitize_event_data(event_type, raw)
    assert got == expected
    assert "SECRET-WORDS" not in json.dumps(got)


@pytest.mark.parametrize("variant", ["STT_COMPLETE", "Stt_Complete", " stt_complete "])
def test_event_types_match_case_insensitively(variant):
    assert sanitize_event_data(variant, {"text": SECRET, "engine": "w"}) == {"text_len": len(SECRET), "engine": "w"}


def test_an_enum_member_is_accepted_as_the_type():
    assert sanitize_event_data(EventType.STT_COMPLETE, {"text": SECRET}) == {"text_len": len(SECRET)}


def test_an_unknown_type_keeps_only_the_generic_safe_keys_and_lengths():
    raw = {"engine": "x", "duration_ms": 5, "prompt": SECRET, "tool_name": "t", "free_text": SECRET, "note_len": 3,
           "payload": {"a": SECRET}, "items": [SECRET]}
    assert sanitize_event_data("brand_new_event", raw) == {"engine": "x", "duration_ms": 5, "tool_name": "t", "note_len": 3}
    assert sanitize_event_data(None, {"text": SECRET}) == {"text_len": len(SECRET)}


def test_strings_shaped_like_text_or_json_are_dropped_even_in_allowed_keys():
    nested = json.dumps({"query": SECRET})
    got = sanitize_event_data("tool_selected", {"tool_name": nested, "tool_source": SECRET + "!?", "args": {"q": nested}})
    assert got == {"arg_count": 1}
    long_label = "x" * 101
    assert sanitize_event_data("llm_generating", {"model": long_label}) == {}


def test_values_of_the_wrong_type_are_dropped():
    assert sanitize_event_data("llm_complete", {"model": ["a"], "tokens": "9", "duration_ms": True, "response_len": "5"}) == {}


def test_data_that_is_not_a_dict_becomes_empty():
    for bad in (None, SECRET, [SECRET], 5, json.dumps({"text": SECRET})):
        assert sanitize_event_data("stt_complete", bad) == {}


def test_top_level_event_fields_are_allowlisted_too():
    event = {"event_type": "stt_complete", "session_id": "s", "interface": "voice", "timestamp": 1.0, "id": 4,
             "data": {"text": SECRET}, "transcript": SECRET, "text": SECRET, "debug": {"q": SECRET}}
    assert sanitize_event(event) == {"event_type": "stt_complete", "session_id": "s", "interface": "voice",
                                     "timestamp": 1.0, "id": 4, "data": {"text_len": len(SECRET)}}
    assert sanitize_event({"event_type": "x", "data": [SECRET]}) == {"event_type": "x", "data": {}}
    assert sanitize_event("not a dict") == "not a dict"


# --- the three places it is applied -------------------------------------------------

def test_broadcast_sanitises_the_whole_event(monkeypatch):
    seen = []

    async def capture(event):
        seen.append(event)

    monkeypatch.setattr(websocket_routes.ws_manager, "broadcast", capture)
    asyncio.run(websocket_routes.broadcast_to_admin_jarvis(
        {"event_type": "STT_COMPLETE", "session_id": "s", "data": {"text": SECRET, "engine": "w"}, "transcript": SECRET}))
    assert seen == [{"event_type": "STT_COMPLETE", "session_id": "s", "data": {"text_len": len(SECRET), "engine": "w"}}]


def test_the_emit_route_stores_and_broadcasts_the_allowlisted_form(client, db, owner_client, monkeypatch):
    broadcasts = []

    async def capture(event):
        broadcasts.append(event)

    monkeypatch.setattr(websocket_routes.ws_manager, "broadcast", capture)
    resp = owner_client.post("/api/pipeline-events/emit?event_type=tool_selected&session_id=al1",
                             json={"tool_name": "websearch", "args": {"query": SECRET}, "extra": {"x": SECRET}})
    assert resp.status_code == 200, resp.text
    db.expire_all()
    stored = db.query(PipelineEvent).filter(PipelineEvent.session_id == "al1").one().event_data
    assert stored == {"tool_name": "websearch", "arg_count": 1}
    assert "SECRET-WORDS" not in json.dumps(broadcasts)


def test_stored_events_written_by_older_versions_are_sanitised_on_every_read(client, db, owner_client):
    for event_type, data in (("stt_complete", {"text": SECRET, "engine": "w"}),
                             ("tool_error", {"tool_name": "t", "error": SECRET, "trace": [SECRET]}),
                             ("mystery", {"blob": {"a": SECRET}, "duration_ms": 3})):
        db.add(PipelineEvent(session_id="old-al", event_type=event_type, event_data=data, timestamp=datetime.utcnow()))
    db.commit()
    for path in ("/api/pipeline-events", "/api/pipeline-events/old-al"):
        resp = owner_client.get(path)
        assert resp.status_code == 200 and "SECRET-WORDS" not in resp.text, path
    listing = {e["event_type"]: e["data"] for e in owner_client.get("/api/pipeline-events").json() if e["session_id"] == "old-al"}
    assert listing == {"stt_complete": {"text_len": len(SECRET), "engine": "w"},
                       "tool_error": {"tool_name": "t", "error_len": len(SECRET)},
                       "mystery": {"duration_ms": 3}}
