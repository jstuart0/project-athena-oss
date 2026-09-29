"""Session binding (V1.8, D20, PP12).

A public caller presenting another caller's session id gets a fresh session
under a new id: it never reads that history and never overwrites it.
"""
from __future__ import annotations

import ast
import asyncio
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.fixture
def client(monkeypatch):
    h.patch_conversation_config(monkeypatch, enabled=True, history_mode="full")
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    return TestClient(h.main.app)


class _Graph:
    def __init__(self):
        self.states = []

    async def ainvoke(self, state):
        self.states.append(state)
        return {"intent": h.IntentCategory.WEATHER, "answer": "ok", "confidence": 1.0,
                "citations": [], "request_id": "r", "node_timings": {}}


def _household_session(sm, *, expired=False):
    async def _make():
        session = await sm.create_session(session_id="household-1", user_id="owner", zone="kitchen")
        session.add_message("user", "my garage code is 4417")
        session.add_message("assistant", "Noted.")
        if expired:
            session.last_activity = datetime.utcnow() - timedelta(days=2)
        await sm._save_session(session)
        return session
    return asyncio.run(_make())


def test_public_cannot_resume_household_session(client, monkeypatch):
    """Named: the public request gets a new id and empty history, and the
    household session is unchanged."""
    sm = h._runtime.get_session_manager()
    _household_session(sm)
    graph = _Graph()
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)

    resp = client.post(
        "/query",
        json={"query": "what did I tell you", "caller_trust": "web_public", "session_id": "household-1"},
        headers=h.service_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["session_id"] != "household-1"
    assert graph.states[0].conversation_history == []
    stored = asyncio.run(sm.get_session("household-1"))
    assert [m["content"] for m in stored.messages] == ["my garage code is 4417", "Noted."]


def test_public_does_not_clobber_expired_household_session(client, monkeypatch):
    sm = h._runtime.get_session_manager()
    _household_session(sm, expired=True)
    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    resp = client.post(
        "/query",
        json={"query": "hi", "caller_trust": "web_public", "session_id": "household-1"},
        headers=h.service_headers(),
    )
    assert resp.json()["session_id"] != "household-1"
    stored = asyncio.run(sm.get_session("household-1"))
    assert len(stored.messages) == 2


def test_household_resumes_own_session(client, monkeypatch):
    sm = h._runtime.get_session_manager()
    _household_session(sm)
    graph = _Graph()
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)
    resp = client.post(
        "/query",
        json={"query": "what did I tell you", "caller_trust": "household", "session_id": "household-1"},
        headers=h.service_headers(),
    )
    assert resp.json()["session_id"] == "household-1"
    assert graph.states[0].conversation_history


def test_public_resumes_public_session_and_others_may_too(monkeypatch):
    from orchestrator.session_manager import CALLER_CLASS_PUBLIC

    h.patch_conversation_config(monkeypatch)

    sm = h._runtime.get_session_manager()

    async def _run():
        created = await sm.get_or_create_session(session_id="pub-1", caller_class=CALLER_CLASS_PUBLIC)
        created.add_message("user", "hello")
        await sm._save_session(created)
        again = await sm.get_or_create_session(session_id="pub-1", caller_class=CALLER_CLASS_PUBLIC)
        other = await sm.get_or_create_session(session_id="pub-1")
        return created, again, other

    created, again, other = asyncio.run(_run())
    assert created.caller_class == "public"
    assert again.session_id == "pub-1" and len(again.messages) == 1
    assert other.session_id == "pub-1"


def test_session_caller_class_roundtrip():
    from orchestrator.session_manager import ConversationSession

    session = ConversationSession(session_id="s1")
    session.caller_class = "public"
    restored = ConversationSession.from_dict(session.to_dict())
    assert restored.caller_class == "public"
    legacy = session.to_dict()
    legacy.pop("caller_class")
    assert ConversationSession.from_dict(legacy).caller_class == "other"
    legacy["caller_class"] = "anything-else"
    assert ConversationSession.from_dict(legacy).caller_class == "other"


def test_query_entry_points_pass_caller_class():
    """PP12: every QueryRequest entry point passes caller_class; /v1 (no
    caller_trust field) is the only call that relies on the default."""
    tree = ast.parse(h.MAIN_PY.read_text(encoding="utf-8"))
    passing, defaulting = set(), set()
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "get_or_create_session":
                keywords = {k.arg for k in node.keywords}
                (passing if "caller_class" in keywords else defaulting).add(fn.name)
    assert {"process_query", "process_query_stream", "process_query_stream_v2"} <= passing
    assert defaulting <= {"chat_completions"}
