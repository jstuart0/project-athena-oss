"""Speech rendering at every orchestrator egress, black-box through the routes.

The fake answer is `Winds 25mph, gusts 15 km/h.`: a speech caller gets
`Winds 25 miles per hour, gusts 15 kilometers per hour.`, every other caller
gets the string as written. Sessions and the semantic cache keep the raw text.
"""
from __future__ import annotations

import fnmatch
import json
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h
import orchestrator.semantic_cache as semantic_cache

QUERY = "what's the weather in Anytown today"
RAW = "Winds 25mph, gusts 15 km/h."
SPOKEN = "Winds 25 miles per hour, gusts 15 kilometers per hour."
PIN_RAW = "Owner mode on. Winds 25mph."
PIN_SPOKEN = "Owner mode on. Winds 25 miles per hour."


class _MemoryCache:
    def __init__(self):
        self.data = {}
        self.sets = 0

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.sets += 1
        self.data[key] = value


class _SessionCacheClient:
    def __init__(self):
        self.client = SimpleNamespace(
            delete=mock.AsyncMock(), get=mock.AsyncMock(return_value=None), setex=mock.AsyncMock(),
        )


class _Graph:
    def __init__(self, answer=RAW):
        self.answer = answer
        self.calls = 0

    async def ainvoke(self, state):
        self.calls += 1
        return {"intent": h.IntentCategory.WEATHER, "answer": self.answer, "confidence": 1.0,
                "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True}


class _TokenLLM:
    def __init__(self, tokens):
        self.tokens = tokens

    async def generate_stream(self, model=None, prompt=None, system_prompt=None, **kwargs):
        for token in self.tokens:
            yield {"token": token}
        yield {"token": "", "done": True}


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.fixture
def memory_cache(monkeypatch):
    store = _MemoryCache()
    monkeypatch.setattr(semantic_cache, "get_cache_client", lambda: store)
    category, _ = semantic_cache.extract_semantic_intent(QUERY)
    assert semantic_cache.is_cacheable(category, QUERY), "precondition: the query is cacheable"
    return store


@pytest.fixture
def app(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    h._runtime.set_cache_client(_SessionCacheClient())
    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    return TestClient(h.main.app)


def _wait_for_sets(cache, n):
    deadline = time.time() + 2
    while cache.sets < n and time.time() < deadline:
        time.sleep(0.01)


def _query_body(interface_type, **extra):
    return {"query": QUERY, "caller_trust": "household", "interface_type": interface_type, **extra}


def _post_query(client, interface_type, **extra):
    resp = client.post("/query", json=_query_body(interface_type, **extra), headers=h.service_headers())
    assert resp.status_code == 200
    return resp.json()["answer"]


def _sse_events(text):
    return [chunk[6:] for chunk in text.split("\n\n") if chunk.startswith("data: ")]


def _stream(client, path, body):
    with client.stream("POST", path, json=body, headers=h.service_headers()) as resp:
        assert resp.status_code == 200
        return "".join(resp.iter_text())


def _openai_stream(client, extra_body):
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": QUERY}],
            "extra_body": extra_body}
    events = _sse_events(_stream(client, "/v1/chat/completions", body))
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    contents = [c["choices"][0]["delta"].get("content") for c in chunks]
    contents = [c for c in contents if c is not None]
    assert all(contents), "no chunk has empty content"
    return "".join(contents)


def _query_stream(client, path, interface_type):
    text = _stream(client, path, _query_body(interface_type))
    events = [json.loads(e) for e in _sse_events(text)]
    assert events[-1]["stage"] == "complete"
    if path == "/query/stream":
        contents = [e["content"] for e in events if e.get("stage") == "answer_chunk"]
        assert all(contents), "no chunk has empty content"
        return "".join(contents)
    return events[-1]["full_response"]


def _stream_handlers(monkeypatch, *, answer=None, tokens=None):
    """/query/stream and /v1 streaming run the stream pipeline; give it a
    precomputed handler answer, or no answer and an LLM that emits tokens."""
    async def _run(state):
        state.intent = h.IntentCategory.WEATHER
        state.answer = answer or ""
        return state

    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", _run)
    if tokens is not None:
        async def _prompt(state):
            return ("prompt", "model", "system")

        monkeypatch.setattr(h.main, "build_synthesis_prompt_for_streaming", _prompt)
        monkeypatch.setattr(h.main, "get_component_config", mock.AsyncMock(return_value={}))
        h._runtime.set_llm_router(_TokenLLM(tokens))


# --- (a) /query ---------------------------------------------------------------

QUERY_ROUTE_IDS = [("voice", SPOKEN), ("chat", RAW), ("text", RAW)]


@pytest.mark.parametrize("interface_type,expected", QUERY_ROUTE_IDS, ids=[i for i, _ in QUERY_ROUTE_IDS])
def test_query_renders_by_interface_type(app, interface_type, expected):
    assert _post_query(app, interface_type, skip_semantic_cache=True) == expected


# --- (b) an early return --------------------------------------------------------


@pytest.mark.parametrize("interface_type,expected", [("voice", PIN_SPOKEN), ("chat", PIN_RAW)], ids=["voice", "chat"])
def test_query_early_return_is_rendered_too(app, monkeypatch, interface_type, expected):
    monkeypatch.setattr(
        h.main, "handle_owner_mode_utterance",
        mock.AsyncMock(return_value=SimpleNamespace(message=PIN_RAW, success=True, override_data=None, refused_reason=None)),
    )
    assert _post_query(app, interface_type) == expected


# --- (c) the semantic cache, through the real key ----------------------------------


def _cache_round(app, cache, first, second):
    graph = h.main.orchestrator_graph
    first_answer = _post_query(app, first)
    _wait_for_sets(cache, 1)
    second_answer = _post_query(app, second)
    return first_answer, second_answer, graph


@pytest.mark.parametrize(
    "first,second,expected",
    [("voice", "chat", (SPOKEN, RAW)), ("chat", "voice", (RAW, SPOKEN))],
    ids=["voice_then_chat", "chat_then_voice"],
)
def test_cache_never_replays_across_interface_types(app, memory_cache, first, second, expected):
    answers_graph = _cache_round(app, memory_cache, first, second)
    assert answers_graph[:2] == expected
    assert answers_graph[2].calls == 2, "the second interface type was a miss, not a hit"


def test_cache_repeat_voice_request_is_a_hit_and_is_rendered(app, memory_cache):
    first, second, graph = _cache_round(app, memory_cache, "voice", "voice")
    assert (first, second) == (SPOKEN, SPOKEN)
    assert graph.calls == 1


def test_cache_stores_raw_text_under_a_key_that_ends_with_the_interface_type(app, memory_cache):
    _post_query(app, "voice")
    _wait_for_sets(memory_cache, 1)
    _post_query(app, "chat")
    _wait_for_sets(memory_cache, 2)
    assert {entry["answer"] for entry in memory_cache.data.values()} == {RAW}
    keys = sorted(memory_cache.data)
    assert [k.rsplit(":", 1)[1] for k in keys] == ["iface_chat", "iface_voice"]
    category, _ = semantic_cache.extract_semantic_intent(QUERY)
    for key in keys:
        assert ":mode_" in key
        assert fnmatch.fnmatch(key, f"athena_semantic:{category}_*"), "category invalidation still matches"


def test_cache_key_is_the_real_one_and_ends_with_the_interface_type_whatever_precedes_it():
    voice = semantic_cache.get_cache_key("weather_current", QUERY, mode="owner", guest_id=7,
                                         location_override={"address": "Somewhere"}, interface_type="voice",
                                         knowledge_digest="abc123def456")
    chat = semantic_cache.get_cache_key("weather_current", QUERY, mode="owner", guest_id=7,
                                        location_override={"address": "Somewhere"}, interface_type="chat",
                                        knowledge_digest="abc123def456")
    assert voice.endswith(":iface_voice") and chat.endswith(":iface_chat")
    assert voice.rsplit(":", 1)[0] == chat.rsplit(":", 1)[0]
    assert ":mode_owner" in voice and ":guest:7" in voice and ":loc_" in voice
    assert fnmatch.fnmatch(voice, "athena_semantic:weather_*")


# --- (d) /v1/chat/completions, non-stream --------------------------------------------


def _openai_nonstream(client, extra_body):
    body = {"model": "m", "stream": False, "messages": [{"role": "user", "content": QUERY}]}
    if extra_body is not None:
        body["extra_body"] = extra_body
    resp = client.post("/v1/chat/completions", json=body, headers=h.service_headers())
    assert resp.status_code == 200
    return resp.json()["choices"][0]["message"]["content"]


OPENAI_NONSTREAM = [
    ({"interface_type": "voice"}, SPOKEN),
    ({"interface_type": "text"}, RAW),
    ({"interface_type": "chat"}, RAW),
    ({}, RAW),
    (None, RAW),
    ({"interface_type": "sms"}, RAW),
]


@pytest.mark.parametrize("extra_body,expected", OPENAI_NONSTREAM, ids=["voice", "text", "chat", "empty", "absent", "invalid"])
def test_openai_nonstream_voice_is_rendered_and_absent_is_raw(app, extra_body, expected):
    assert _openai_nonstream(app, extra_body) == expected


# --- (e) streaming, precomputed handler answer -----------------------------------------

OPENAI_STREAM_PRECOMPUTED = [({"interface_type": "voice"}, SPOKEN), ({"interface_type": "text"}, RAW), ({}, RAW)]


@pytest.mark.parametrize("extra_body,expected", OPENAI_STREAM_PRECOMPUTED, ids=["voice", "text", "absent"])
def test_openai_stream_precomputed_answer_follows_the_channel(app, monkeypatch, extra_body, expected):
    _stream_handlers(monkeypatch, answer=RAW)
    assert _openai_stream(app, extra_body) == expected


def test_openai_stream_precomputed_text_keeps_newlines(app, monkeypatch):
    _stream_handlers(monkeypatch, answer="Line one 25mph\n\nLine two")
    assert _openai_stream(app, {"interface_type": "text"}) == "Line one 25mph\n\nLine two"


# --- (f) streaming, LLM tokens --------------------------------------------------------------


@pytest.mark.parametrize("extra_body,expected", [({"interface_type": "voice"}, "Winds 25 miles per hour."), ({"interface_type": "text"}, "Winds 25mph.")], ids=["voice", "text"])
def test_openai_stream_llm_tokens_follow_the_channel(app, monkeypatch, extra_body, expected):
    _stream_handlers(monkeypatch, tokens=["Winds ", "25mph."])
    assert _openai_stream(app, extra_body) == expected


# --- (g) /query/stream ---------------------------------------------------------------------------


@pytest.mark.parametrize("interface_type,expected", [("voice", SPOKEN), ("chat", RAW)], ids=["voice", "chat"])
def test_query_stream_precomputed_answer(app, monkeypatch, interface_type, expected):
    _stream_handlers(monkeypatch, answer=RAW)
    assert _query_stream(app, "/query/stream", interface_type) == expected


@pytest.mark.parametrize("interface_type,expected,chunks", [("voice", "Winds 25 miles per hour.", 1), ("chat", "Winds 25mph.", 2)], ids=["voice", "chat"])
def test_query_stream_llm_tokens(app, monkeypatch, interface_type, expected, chunks):
    _stream_handlers(monkeypatch, tokens=["Winds ", "25mph."])
    text = _stream(app, "/query/stream", _query_body(interface_type))
    events = [json.loads(e) for e in _sse_events(text)]
    contents = [e["content"] for e in events if e.get("stage") == "answer_chunk"]
    assert "".join(contents) == expected
    assert len(contents) == chunks, "speech is sent once, at the end; text keeps token streaming"
    assert events[-1]["stage"] == "complete"


# --- (h) /query/stream/v2 -----------------------------------------------------------------------


@pytest.mark.parametrize("interface_type,expected", [("voice", SPOKEN), ("chat", RAW)], ids=["voice", "chat"])
def test_query_stream_v2_renders_before_the_sentence_split(app, interface_type, expected):
    assert _query_stream(app, "/query/stream/v2", interface_type) == expected


def test_query_stream_v2_sentences_join_to_the_rendered_answer(app):
    text = _stream(app, "/query/stream/v2", _query_body("voice"))
    events = [json.loads(e) for e in _sse_events(text)]
    sentences = [e["sentence"] for e in events if e.get("stage") == "streaming"]
    assert " ".join(sentences) == SPOKEN


# --- the owner-PIN message on every streaming route ------------------------------------------------


@pytest.mark.parametrize("path", ["/query/stream", "/query/stream/v2", "/v1/chat/completions"])
@pytest.mark.parametrize("interface_type,expected", [("voice", PIN_SPOKEN), ("chat", PIN_RAW)], ids=["voice", "chat"])
def test_streamed_pin_message_follows_the_channel(app, monkeypatch, path, interface_type, expected):
    monkeypatch.setattr(
        h.main, "handle_owner_mode_utterance",
        mock.AsyncMock(return_value=SimpleNamespace(message=PIN_RAW, success=True, override_data=None, refused_reason=None)),
    )
    if path == "/v1/chat/completions":
        assert _openai_stream(app, {"interface_type": interface_type}) == expected
    else:
        text = _stream(app, path, _query_body(interface_type))
        events = [json.loads(e) for e in _sse_events(text)]
        if path == "/query/stream":
            assert "".join(e["content"] for e in events if e.get("stage") == "answer_chunk") == expected
        else:
            assert events[-1]["full_response"] == expected
            assert [e["sentence"] for e in events if e.get("stage") == "streaming"] == [expected]


# --- (i) automation_agent notifications -------------------------------------------------------------


def _agent():
    from orchestrator.automation_agent import AutomationAgent

    ha = mock.MagicMock()
    ha.call_service = mock.AsyncMock()
    return AutomationAgent(ha, mock.MagicMock(), admin_client=mock.MagicMock()), ha


def _messages_by_service(ha):
    return {call.args[0]: call.args[2]["message"] for call in ha.call_service.await_args_list if "message" in call.args[2]}


NOTIFY_TARGETS = [
    ("tts", {"tts": SPOKEN}),
    ("mobile", {"notify": RAW}),
    ("all", {"tts": SPOKEN, "notify": RAW}),
]


@pytest.mark.parametrize("target,expected", NOTIFY_TARGETS, ids=[t for t, _ in NOTIFY_TARGETS])
def test_notification_speech_is_rendered_and_the_push_stays_raw(target, expected):
    import asyncio

    agent, ha = _agent()
    asyncio.run(agent._send_notification({"message": RAW, "target": target, "room": "kitchen"}, {}))
    assert _messages_by_service(ha) == expected


def test_notification_all_renders_speech_and_leaves_the_push_raw_in_one_call():
    import asyncio

    agent, ha = _agent()
    asyncio.run(agent._send_notification({"message": RAW, "target": "all", "room": "kitchen"}, {}))
    assert _messages_by_service(ha) == {"tts": SPOKEN, "notify": RAW}


# --- floors ---------------------------------------------------------------------------------------------


def test_this_file_has_at_least_nine_parametrized_ids(request):
    ids = [i for i in request.session.items if i.originalname and "[" in i.name and i.module is request.module]
    assert len(ids) >= 9
