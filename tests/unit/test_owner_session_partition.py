"""Owner sessions are their own caller class: any crossing mints a fresh
session, the Redis namespaces differ, and ids carry their class."""
from __future__ import annotations

import asyncio
import itertools
import json
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h
from orchestrator import session_keys
from orchestrator.session_manager import (
    CALLER_CLASS_OTHER,
    CALLER_CLASS_OWNER,
    CALLER_CLASS_PUBLIC,
    ConversationSession,
    SessionManager,
)

CLASSES = [CALLER_CLASS_OWNER, CALLER_CLASS_OTHER, CALLER_CLASS_PUBLIC]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    h.reset_runtime()
    h.patch_conversation_config(monkeypatch)
    yield
    h.reset_runtime()


def _sm():
    sm = SessionManager()
    sm.redis_client = None
    return sm


def _run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("stored,caller", list(itertools.product(CLASSES, CLASSES)))
def test_class_crossing_mints_a_fresh_session(stored, caller):
    sm = _sm()
    first = _run(sm.create_session(caller_class=stored))
    assert first.caller_class == stored and session_keys.id_class(first.session_id) == stored
    again = _run(sm.get_or_create_session(session_id=first.session_id, caller_class=caller))
    if stored == caller:
        assert again.session_id == first.session_id
    else:
        assert again.session_id != first.session_id
        assert again.caller_class == caller
        assert session_keys.id_class(again.session_id) == caller
        assert _run(sm.get_session(first.session_id)).caller_class == stored  # the original is untouched


def test_owner_ids_carry_the_prefix_and_other_callers_never_adopt_one():
    sm = _sm()
    assert _run(sm.create_session(caller_class=CALLER_CLASS_OWNER)).session_id.startswith("own-")
    adopted = _run(sm.create_session(session_id="own-chosen", caller_class=CALLER_CLASS_OTHER))
    assert adopted.session_id != "own-chosen" and not adopted.session_id.startswith(("own-", "pub-"))
    assert _run(sm.create_session(session_id="plain-id", caller_class=CALLER_CLASS_OWNER)).session_id.startswith("own-")
    assert _run(sm.create_session(session_id="own-ok", caller_class=CALLER_CLASS_OWNER)).session_id == "own-ok"


def test_expired_or_evicted_owner_id_is_never_adopted_by_another_class():
    sm = _sm()
    owner = _run(sm.create_session(caller_class=CALLER_CLASS_OWNER))
    # still stored: the mismatch check fires first
    fresh = _run(sm.get_or_create_session(session_id=owner.session_id, caller_class=CALLER_CLASS_OTHER))
    assert fresh.session_id != owner.session_id
    # TTL-evicted: the recreate branch applies the same prefix rule
    _run(sm.delete_session(owner.session_id))
    again = _run(sm.get_or_create_session(session_id=owner.session_id, caller_class=CALLER_CLASS_OTHER))
    assert again.session_id != owner.session_id and not again.session_id.startswith("own-")


def test_expired_owner_session_is_recreated_under_the_same_owner_id(monkeypatch):
    sm = _sm()
    owner = _run(sm.create_session(caller_class=CALLER_CLASS_OWNER))
    monkeypatch.setattr(ConversationSession, "is_expired", lambda self, timeout: True)
    again = _run(sm.get_or_create_session(session_id=owner.session_id, caller_class=CALLER_CLASS_OWNER))
    assert again.session_id == owner.session_id and again.caller_class == CALLER_CLASS_OWNER


@pytest.mark.parametrize("stored", [CALLER_CLASS_OWNER, CALLER_CLASS_PUBLIC, CALLER_CLASS_OTHER])
def test_class_round_trips_through_to_dict(stored):
    session = ConversationSession(session_id=session_keys.new_session_id(stored))
    session.caller_class = stored
    assert ConversationSession.from_dict(json.loads(json.dumps(session.to_dict()))).caller_class == stored


@pytest.mark.parametrize("unknown", ["admin", "", None, "OWNER", 7])
def test_unknown_stored_class_fails_closed_to_other(unknown):
    data = ConversationSession(session_id="x").to_dict()
    data["caller_class"] = unknown
    assert ConversationSession.from_dict(data).caller_class == CALLER_CLASS_OTHER


def test_storage_keys_come_from_one_class_function():
    owner, public, other = "own-1", "pub-1", "1"
    assert session_keys.session_storage_key(owner) == "athena:owner_session:own-1"
    assert session_keys.context_storage_key(owner) == "athena:owner_context:own-1"
    assert session_keys.session_storage_key(other) == "athena:session:1"
    assert session_keys.context_storage_key(other) == "athena:context:1"
    assert session_keys.session_storage_key(public) == "athena:session:pub-1"
    assert session_keys.context_storage_key(public) == "athena:context:pub-1"
    # an old process reads only the plain namespaces
    assert not session_keys.session_storage_key(owner).startswith("athena:session:")
    assert not session_keys.context_storage_key(owner).startswith("athena:context:")


class _FakeRedis:
    def __init__(self):
        self.store = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)

    async def close(self):
        pass


def test_owner_session_lives_in_its_own_redis_namespace_and_is_invisible_to_a_plain_reader():
    sm = _sm()
    sm.redis_client = _FakeRedis()
    owner = _run(sm.create_session(caller_class=CALLER_CLASS_OWNER))
    other = _run(sm.create_session(caller_class=CALLER_CLASS_OTHER))
    keys = set(sm.redis_client.store)
    assert keys == {f"athena:owner_session:{owner.session_id}", f"athena:session:{other.session_id}"}
    # a process that only knows the old namespace finds nothing for the owner id
    assert f"athena:session:{owner.session_id}" not in sm.redis_client.store
    _run(sm.delete_session(owner.session_id))
    assert f"athena:owner_session:{owner.session_id}" not in sm.redis_client.store


# --- the entry points ---------------------------------------------------------

class _Graph:
    def __init__(self):
        self.states = []

    async def ainvoke(self, state):
        self.states.append(state)
        return {"intent": h.IntentCategory.WEATHER, "answer": "S_OWNER ANSWER", "confidence": 1.0,
                "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True}


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    admin = h.fake_admin_client()
    monkeypatch.setattr(h.main, "get_admin_client", lambda: admin)
    graph = _Graph()
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)
    monkeypatch.setattr(h.main, "get_cached_response", mock.AsyncMock(return_value=None))
    monkeypatch.setattr(h.main, "cache_response", mock.AsyncMock())
    return SimpleNamespace(client=TestClient(h.main.app), graph=graph)


def _post(rig, trust, session_id=None, headers=True):
    body = {"query": "what's the weather", "interface_type": "chat", "caller_trust": trust}
    if session_id:
        body["session_id"] = session_id
    resp = rig.client.post("/query", json=body, headers=h.service_headers() if headers else {})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


def _sm_runtime():
    return h._runtime.get_session_manager()


def test_a_proven_turn_resumes_its_session_and_everyone_else_gets_a_fresh_one(rig, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", "warn")
    h.shared_config._clear_cache_for_tests()
    first = _post(rig, "web_owner")
    assert first.startswith("own-")
    saved = _run(_sm_runtime().get_session(first))
    assert saved.caller_class == CALLER_CLASS_OWNER and any("S_OWNER" in m["content"] for m in saved.messages)
    assert _post(rig, "web_owner", first) == first  # positive control
    rig.graph.states.clear()
    for trust, headers in (("web_local", True), ("household", True), ("web_owner", False)):
        other = _post(rig, trust, first, headers=headers)
        assert other != first and not other.startswith("own-")
        assert rig.graph.states[-1].conversation_history == [], trust
        assert rig.graph.states[-1].history_summary == "", trust


def test_an_unproven_owner_turn_never_reads_or_writes_the_owner_session(rig):
    owner_sid = _post(rig, "web_owner")
    before = list(_run(_sm_runtime().get_session(owner_sid)).messages)
    h.install_mode_client(server_mode="guest")  # a stay: web_owner stays an owner caller, unproven
    rig.graph.states.clear()
    sid = _post(rig, "web_owner", owner_sid)
    assert sid != owner_sid and not sid.startswith("own-")
    assert rig.graph.states[-1].conversation_history == []
    assert list(_run(_sm_runtime().get_session(owner_sid)).messages) == before


def test_mode_service_failure_degrades_instead_of_raising_before_the_session(rig):
    client = mock.AsyncMock()
    client.get = mock.AsyncMock(side_effect=RuntimeError("mode service down"))
    h._runtime.set_mode_client(client)
    sid = _post(rig, "web_owner")
    assert not sid.startswith("own-")  # degraded: owner_caller but never proven
    state = rig.graph.states[-1]
    assert state.mode_degraded and state.knowledge_audience.owner_caller and not state.knowledge_audience.owner_proven


def test_authorization_precedes_session_creation_in_every_handler():
    import ast

    source = h.MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in ("process_query", "process_query_stream", "process_query_stream_v2"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        auth = min(n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "resolve_request_authorization")
        sessions = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get_or_create_session"]
        assert sessions and all(auth < n.lineno for n in sessions), name
        for call in sessions:
            kw = {k.arg: ast.get_source_segment(source, k.value) for k in call.keywords}
            assert kw["caller_class"] == "_session_caller_class(request, authz.knowledge_audience)", name


def test_chat_completions_never_creates_an_owner_session():
    import ast

    tree = ast.parse(h.MAIN_PY.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "chat_completions")
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get_or_create_session"):
        assert "caller_class" not in {k.arg for k in call.keywords}  # default: other
