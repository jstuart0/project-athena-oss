"""Shared rig for the fast-path route tests.

Builds on `_public_audience_harness`: fakes for the graph, the streaming
runner and every LLM-router method that either raise (the fast path must not
reach them) or record (a deferred turn must reach them), a dict-backed Redis,
and an in-process ASGI client that keeps one event loop for the whole
scenario, so the background persistence task and the session read live in the
same loop.
"""
from __future__ import annotations

import asyncio
import json
from unittest import mock

import httpx

from . import _public_audience_harness as h

SLOW_ANSWER = "SLOW-PATH-ANSWER"
ROUTES = ("query", "query_stream", "query_stream_v2", "v1_stream", "v1_nonstream")
SESSION_ID = "fp-sess-1"
V1_SESSION_ID = "explicit-fp-sess-1"


def session_id_for(route: str) -> str:
    """OpenAI-shaped routes only accept caller-managed ids in the explicit- namespace."""
    return V1_SESSION_ID if route.startswith("v1") else SESSION_ID


class FakeRedis:
    """The subset of redis.asyncio the orchestrator's context, session-prep and
    nonce code use, over a dict."""

    def __init__(self):
        self.data = {}
        self.fail_reads = False

    async def get(self, key):
        if self.fail_reads:
            raise ConnectionError("redis down")
        return self.data.get(key)

    async def setex(self, key, ttl, value):
        self.data[key] = value

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    async def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)


class FakeCache:
    def __init__(self):
        self.client = FakeRedis()


class RaisingLLM:
    """Any attribute is a coroutine that fails the test when awaited."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        async def _boom(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"LLM router method {name} called on a fast-path turn")

        return _boom


class Rig:
    def __init__(self):
        self.reached = []          # pipeline entry points a turn went through
        self.llm = RaisingLLM()
        self.cache = FakeCache()
        self.metric = mock.AsyncMock(return_value=True)


def install(monkeypatch, *, server_mode: str = "owner", guest_profile=None, slow: str = "raise") -> Rig:
    """slow='raise': the pipeline fails the test if reached. slow='record':
    it records and answers SLOW_ANSWER."""
    rig = Rig()
    h.reset_runtime()
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode=server_mode, guest_profile=guest_profile)
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    h._runtime.set_cache_client(rig.cache)
    # Session writes are I/O in production (Redis); make them suspend so a write
    # that isn't awaited before the response is visibly late.
    sm = h._runtime.get_session_manager()
    real_add_message = sm.add_message

    async def _slow_add_message(*args, **kwargs):
        await asyncio.sleep(0.02)
        return await real_add_message(*args, **kwargs)

    monkeypatch.setattr(sm, "add_message", _slow_add_message)
    h._runtime.set_llm_router(rig.llm)
    monkeypatch.setattr(h.main, "record_intent_metric", rig.metric)

    class _Graph:
        async def ainvoke(self, state):
            rig.reached.append("graph")
            if slow == "raise":
                raise AssertionError("graph reached on a fast-path turn")
            return {"intent": h.IntentCategory.GENERAL_INFO, "answer": SLOW_ANSWER, "confidence": 1.0,
                    "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True}

    async def _stream_run(state):
        rig.reached.append("stream_runner")
        if slow == "raise":
            raise AssertionError("streaming runner reached on a fast-path turn")
        state.intent = h.IntentCategory.GENERAL_INFO
        state.answer = SLOW_ANSWER
        return state

    async def _classify(state):
        rig.reached.append("classify_node")
        raise AssertionError("classify_node reached on a fast-path turn")

    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", _stream_run)
    monkeypatch.setattr(h.main, "classify_node", _classify)
    return rig


def request_for(route: str, text: str, *, session_id: str = "", interface_type: str = "voice",
                caller_trust: str = "household", stream_options=None):
    session_id = session_id or session_id_for(route)
    if route.startswith("v1"):
        body = {
            "model": "m", "stream": route == "v1_stream", "session_id": session_id,
            "messages": [{"role": "user", "content": text}],
            "extra_body": {"interface_type": interface_type},
        }
        if stream_options is not None:
            body["stream_options"] = stream_options
        return "/v1/chat/completions", body
    path = {"query": "/query", "query_stream": "/query/stream", "query_stream_v2": "/query/stream/v2"}[route]
    body = {"query": text, "session_id": session_id, "caller_trust": caller_trust,
            "interface_type": interface_type, "skip_semantic_cache": True}
    return path, body


def _events(text):
    return [chunk[6:] for chunk in text.split("\n\n") if chunk.startswith("data: ")]


def answer_of(route: str, response: httpx.Response) -> str:
    """The text a client would show for this route's response."""
    assert response.status_code == 200, response.text
    if route == "query":
        return response.json()["answer"]
    if route == "v1_nonstream":
        return response.json()["choices"][0]["message"]["content"]
    events = _events(response.text)
    if route == "v1_stream":
        assert events[-1] == "[DONE]"
        chunks = [json.loads(e) for e in events[:-1]]
        return "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    parsed = [json.loads(e) for e in events]
    if route == "query_stream":
        return "".join(e["content"] for e in parsed if e.get("stage") == "answer_chunk")
    return next(e["full_response"] for e in parsed if e.get("stage") == "complete")


async def send(route: str, text: str, *, headers=None, **kwargs) -> httpx.Response:
    path, body = request_for(route, text, **kwargs)
    transport = httpx.ASGITransport(app=h.main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body, headers={**h.service_headers(), **(headers or {})})


async def stored_messages(route: str = "query"):
    """The session's messages after background tasks drain."""
    await h._runtime.drain_background()
    session = await h._runtime.get_session_manager().get_session(session_id_for(route))
    return list(session.messages) if session else []


async def send_then_disconnect_at_final_event(route: str, text: str, **kwargs) -> list:
    """POST over raw ASGI and fail the transport on the chunk that carries the
    final event ("complete" or [DONE]), as a client that hangs up the moment it
    has its answer. Returns the body chunks the server managed to send."""
    import json as _json

    path, body = request_for(route, text, **kwargs)
    raw = _json.dumps(body).encode()
    sent_request = False
    chunks: list = []

    async def receive():
        nonlocal sent_request
        if not sent_request:
            sent_request = True
            return {"type": "http.request", "body": raw, "more_body": False}
        await asyncio.sleep(3600)

    async def send(message):
        if message["type"] != "http.response.body":
            return
        chunk = message.get("body", b"").decode()
        chunks.append(chunk)
        if '"stage": "complete"' in chunk or "[DONE]" in chunk:
            raise OSError("client disconnected")

    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    headers += [(k.lower().encode(), v.encode()) for k, v in h.service_headers().items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": headers,
             "client": ("127.0.0.1", 5000), "server": ("test", 80)}
    try:
        await h.main.app(scope, receive, send)
    except OSError:
        pass
    return chunks
