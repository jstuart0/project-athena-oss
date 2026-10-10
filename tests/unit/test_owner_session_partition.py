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
    CALLER_CLASS_GUEST,
    CALLER_CLASS_OTHER,
    CALLER_CLASS_OWNER,
    CALLER_CLASS_PUBLIC,
    ConversationSession,
    SessionManager,
)

CLASSES = [CALLER_CLASS_OWNER, CALLER_CLASS_OTHER, CALLER_CLASS_PUBLIC, CALLER_CLASS_GUEST]


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    h.reset_runtime()
    h.patch_conversation_config(monkeypatch, enabled=True)  # history actually loads
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
    elif caller == CALLER_CLASS_GUEST and stored == CALLER_CLASS_OTHER:
        assert again.session_id == "gst-" + first.session_id  # derived, so a fixed-id caller keeps a conversation
        assert again.caller_class == CALLER_CLASS_GUEST
        assert _run(sm.get_session(first.session_id)).caller_class == stored
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

    async def eval(self, script, numkeys, key, score, member, max_count):
        zset = self.__dict__.setdefault("zset", {})
        zset[member] = float(score)
        evicted = []
        while len(zset) > int(max_count):
            oldest = min(zset, key=zset.get)
            del zset[oldest]
            evicted.append(oldest)
        return evicted


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
    return SimpleNamespace(client=TestClient(h.main.app), graph=graph, admin=admin)


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
    assert len(rig.graph.states[-1].conversation_history) >= 2, "positive control: a resumed session loads its turns"
    rig.graph.states.clear()
    for trust, headers in (("web_local", True), ("household", True), ("web_owner", False)):  # all household-class
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
    source = h.MAIN_PY.read_text(encoding="utf-8")
    for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "get_or_create_session"):
        kw = {k.arg: ast.get_source_segment(source, k.value) for k in call.keywords}
        # no caller_trust on this route, so the audience can never be an owner
        assert kw["caller_class"] == "session_class"
    assigns = [n for n in ast.walk(fn) if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "session_class" for t in n.targets)]
    assert [ast.get_source_segment(source, n.value) for n in assigns] == ["_audience_session_class(authz.knowledge_audience)"]
    authz_calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "resolve_request_authorization"]
    assert len(authz_calls) == 1, "one authorization, before the session id is prepared"


def test_a_signed_owner_id_from_jarvis_web_is_kept_across_proven_turns(rig):
    """The id shape jarvis-web mints for an owner (own-<hex>.<mac>) is
    recognised as owner and resumed, not re-minted, by a proven turn."""
    jarvis_style = "own-" + "a" * 32 + "." + "b" * 24
    first = _post(rig, "web_owner", jarvis_style)
    assert first == jarvis_style
    assert _post(rig, "web_owner", jarvis_style) == jarvis_style
    saved = _run(_sm_runtime().get_session(jarvis_style))
    assert saved.caller_class == CALLER_CLASS_OWNER and len(saved.messages) >= 4


# --- the household / guest boundary (a stay starting or ending) ----------------------

def _history(rig):
    return rig.graph.states[-1].conversation_history


def test_a_stay_starting_gives_the_guest_a_fresh_conversation_on_the_same_session_id(rig):
    satellite = "sat-living-room"
    assert _post(rig, "household", satellite) == satellite
    assert _post(rig, "household", satellite) == satellite
    assert len(_history(rig)) >= 2, "positive control: the household conversation resumes"

    h.install_mode_client(server_mode="guest")  # a stay begins
    rig.graph.states.clear()
    guest_sid = _post(rig, "household", satellite)
    assert guest_sid == "gst-" + satellite
    assert _history(rig) == [] and rig.graph.states[-1].history_summary == ""
    saved = _run(_sm_runtime().get_session(guest_sid))
    assert saved.caller_class == CALLER_CLASS_GUEST
    # the guest keeps a conversation of their own across turns
    assert _post(rig, "household", satellite) == guest_sid
    assert len(_history(rig)) >= 2


def test_a_stay_ending_gives_the_household_a_fresh_conversation_and_never_the_guests(rig):
    satellite = "sat-kitchen"
    h.install_mode_client(server_mode="guest")
    guest_sid = _post(rig, "household", satellite)
    _post(rig, "household", satellite)
    assert len(_history(rig)) >= 2

    h.install_mode_client(server_mode="owner")  # the stay ends
    rig.graph.states.clear()
    assert _post(rig, "household", satellite) == satellite  # the household id has no history of its own
    assert _history(rig) == []
    # a caller that adopted the guest id and presents it after the stay gets a fresh session
    again = _post(rig, "household", guest_sid)
    assert again != guest_sid and not again.startswith("gst-") and _history(rig) == []


def test_a_device_matched_stay_is_a_guest_session_but_a_mode_hint_is_not(rig):
    rig.admin.get_user_session_by_device = mock.AsyncMock(return_value={"guest_id": 9, "guest_name": "Gina Guest"})
    body = {"query": "hi", "interface_type": "chat", "caller_trust": "household", "session_id": "dev-sat", "device_id": "dev1"}
    resp = rig.client.post("/query", json=body, headers=h.service_headers())
    assert resp.json()["session_id"] == "gst-dev-sat"

    rig.admin.get_user_session_by_device = mock.AsyncMock(return_value=None)
    body = {"query": "hi", "interface_type": "chat", "caller_trust": "household", "session_id": "hint-sat", "mode": "guest"}
    resp = rig.client.post("/query", json=body, headers=h.service_headers())
    assert resp.json()["session_id"] == "hint-sat"  # a client hint doesn't make a guest conversation


def test_guest_ids_are_not_adopted_by_other_classes():
    from orchestrator import session_keys as keys

    assert keys.id_class("gst-x") == CALLER_CLASS_GUEST
    assert keys.guest_session_id("x") == "gst-x" and keys.guest_session_id("gst-x") == "gst-x"
    assert not keys.guest_session_id("own-x").startswith("own-") and not keys.guest_session_id("pub-x").startswith("pub-")
    for cls in (CALLER_CLASS_OTHER, CALLER_CLASS_OWNER, CALLER_CLASS_PUBLIC):
        assert keys.usable_session_id("gst-x", cls) != "gst-x"
    assert keys.session_storage_key("gst-x") == "athena:guest_session:gst-x"
    assert keys.context_storage_key("gst-x") == "athena:guest_context:gst-x"


# --- client chat_history must agree with the server's audience ------------------------

HISTORY = [{"role": "user", "content": "EARLIER USER TURN"}, {"role": "assistant", "content": "EARLIER ANSWER"}]


def _post_history(rig, session_id, trust="household", headers=True):
    body = {"query": "what's the weather", "interface_type": "chat", "caller_trust": trust,
            "session_id": session_id, "chat_history": HISTORY}
    resp = rig.client.post("/query", json=body, headers=h.service_headers() if headers else {})
    assert resp.status_code == 200, resp.text
    return [m["content"] for m in rig.graph.states[-1].conversation_history]


def test_chat_history_is_dropped_when_the_client_thinks_household_and_the_server_says_guest(rig):
    h.install_mode_client(server_mode="guest")  # a stay began; jarvis-web still sent a household id
    assert _post_history(rig, "a" * 32 + "." + "b" * 24) == []


def test_chat_history_is_dropped_when_the_client_thinks_guest_and_the_server_says_household(rig):
    h.install_mode_client(server_mode="owner")  # the stay ended; jarvis-web still sent a guest id
    assert _post_history(rig, "gst-" + "a" * 32 + "." + "b" * 24) == []


@pytest.mark.parametrize("server,session_id,trust", [
    ("owner", "a" * 32 + "." + "b" * 24, "household"),
    ("guest", "gst-" + "a" * 32 + "." + "b" * 24, "household"),
    ("owner", "own-" + "a" * 32 + "." + "b" * 24, "web_owner"),
], ids=["household", "guest", "proven_owner"])
def test_chat_history_is_accepted_when_the_classes_agree(rig, server, session_id, trust):
    h.install_mode_client(server_mode=server)
    assert _post_history(rig, session_id, trust=trust) == ["EARLIER USER TURN", "EARLIER ANSWER"]


def test_chat_history_is_dropped_for_an_owner_id_when_the_owner_is_not_proven(rig):
    h.install_mode_client(server_mode="guest")
    assert _post_history(rig, "own-" + "a" * 32 + "." + "b" * 24, trust="web_owner") == []


# --- OpenAI-compatible route: one class-qualified id end to end -----------------------

OPENER = "turn on the hallway lights please"


def _oai(rig, content=OPENER, stream=False, **extra):
    body = {"model": "m", "messages": [{"role": "user", "content": content}], "stream": stream, **extra}
    resp = rig.client.post("/v1/chat/completions", json=body, headers=h.service_headers())
    assert resp.status_code == 200, resp.text
    return resp


@pytest.fixture
def oai(rig, monkeypatch):
    redis = _FakeRedis()
    _sm_runtime().redis_client = redis
    monkeypatch.setenv("NEW_CONVERSATION_RESET_GRACE_SECONDS", "0")
    sm = _sm_runtime()
    real_register = sm.register_bounded_session

    async def _small_cap(session_id, max_count):  # the configured minimum is 100; use 2 to keep the test small
        return await real_register(session_id, 2)

    monkeypatch.setattr(sm, "register_bounded_session", _small_cap)
    h.shared_config._clear_cache_for_tests()
    rig.redis = redis
    yield rig
    h.shared_config._clear_cache_for_tests()


def _session_keys(redis):
    return {k for k in redis.store}


def test_a_guest_mode_openai_conversation_is_stored_and_indexed_under_the_guest_id(oai):
    h.install_mode_client(server_mode="guest")
    _oai(oai)
    keys = _session_keys(oai.redis)
    assert len(keys) == 1
    (key,) = keys
    assert key.startswith("athena:guest_session:gst-oai-"), key
    guest_id = key.split(":", 2)[2]
    assert set(oai.redis.zset) == {guest_id}, "the OpenAI index holds the id the session is stored under"


def test_a_fresh_guest_conversation_with_the_same_opener_gets_no_stale_history(oai):
    h.install_mode_client(server_mode="guest")
    _oai(oai)
    guest_id = next(iter(_session_keys(oai.redis))).split(":", 2)[2]
    saved = _run(_sm_runtime().get_session(guest_id))
    assert len(saved.messages) >= 2
    # a new conversation with the same opener: the first-turn reset must hit the guest-qualified keys
    oai.graph.states.clear()
    _oai(oai)
    assert oai.graph.states[-1].conversation_history == [], "the stale guest history was resumed"
    again = _run(_sm_runtime().get_session(guest_id))
    assert len(again.messages) == 2, "reset cleared the old turns, the new conversation has its own"


def test_the_reset_clears_the_guest_context_key(oai):
    h.install_mode_client(server_mode="guest")
    _oai(oai)
    guest_id = next(iter(_session_keys(oai.redis))).split(":", 2)[2]
    from orchestrator import session_keys

    class _Cache:
        def __init__(self):
            self.deleted = []
            self.client = self

        async def delete(self, key):
            self.deleted.append(key)

        async def get(self, key):
            return None

    cache = _Cache()
    h._runtime.set_cache_client(cache)
    _oai(oai)
    assert session_keys.context_storage_key(guest_id) in cache.deleted
    assert all(not k.startswith("athena:context:oai-") for k in cache.deleted)


def test_the_openai_cap_counts_and_evicts_guest_sessions(oai):
    h.install_mode_client(server_mode="guest")
    for i in range(4):
        _oai(oai, content=f"distinct opener number {i} for the cap")
    assert len(oai.redis.zset) == 2, "the index is capped at SESSION_MAX_COUNT"
    live = {k.split(":", 2)[2] for k in _session_keys(oai.redis)}
    assert live == set(oai.redis.zset), "evicted guest sessions were deleted, not orphaned"
    assert all(i.startswith("gst-oai-") for i in live)


@pytest.mark.parametrize("server", ["owner"])
def test_household_openai_ids_are_unchanged(oai, server):
    h.install_mode_client(server_mode=server)
    _oai(oai)
    (key,) = _session_keys(oai.redis)
    assert key.startswith("athena:session:oai-"), key
    guest_free = next(iter(oai.redis.zset))
    assert guest_free.startswith("oai-") and not guest_free.startswith("gst-")


def test_openai_streaming_branch_uses_the_same_qualified_id(oai, monkeypatch):
    h.install_mode_client(server_mode="guest")
    seen = []
    real = _sm_runtime().get_or_create_session

    async def _spy(session_id=None, **kw):
        seen.append(session_id)
        return await real(session_id=session_id, **kw)

    monkeypatch.setattr(_sm_runtime(), "get_or_create_session", _spy)
    try:
        _oai(oai, stream=True)
    except Exception:
        pass
    assert seen and all(i.startswith("gst-oai-") for i in seen)
    assert set(oai.redis.zset) <= {i for i in seen}


@pytest.mark.parametrize("cls,prefix,namespace", [
    (CALLER_CLASS_OWNER, "own-", "athena:owner_session:"),
    (CALLER_CLASS_PUBLIC, "pub-", "athena:session:pub-"),
])
def test_the_openai_path_never_splits_the_id_for_owner_or_public_classes(oai, monkeypatch, cls, prefix, namespace):
    """chat_completions can't resolve these today (no caller_trust); if it ever
    did, prepare, the index and the stored session must still agree."""
    monkeypatch.setattr(h.main, "_audience_session_class", lambda audience: cls)
    _oai(oai)
    (key,) = _session_keys(oai.redis)
    assert key.startswith(namespace), key
    stored_id = key.split(":", 2)[2]
    assert stored_id.startswith(prefix)
    assert set(oai.redis.zset) == {stored_id}


def test_class_qualified_id_never_returns_an_id_of_another_class():
    from orchestrator import session_keys as keys

    for presented in ("plain", "gst-x", "own-x", "pub-x"):
        for cls in (CALLER_CLASS_OWNER, CALLER_CLASS_PUBLIC, CALLER_CLASS_GUEST, CALLER_CLASS_OTHER):
            qualified = keys.class_qualified_id(presented, cls)
            if cls != CALLER_CLASS_OTHER or keys.id_class(presented) == CALLER_CLASS_OTHER:
                assert keys.id_class(qualified) == cls, (presented, cls, qualified)
