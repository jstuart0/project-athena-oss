"""Owner proof: who counts as a proven owner, and what that changes.

owner_caller needs caller_trust == "web_owner" AND a request that carried a
valid X-Service-Key (service_authenticated, recomputed from the header, so it
is False under DEV_MODE and warn-mode passthrough). owner_proven additionally
needs owner mode (effective and server) and a healthy mode service.
"""
from __future__ import annotations

import ast
import asyncio
import itertools
import time
import typing
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h
import orchestrator.ingress_auth as ingress_auth
import orchestrator.semantic_cache as semantic_cache

MAIN = h.MAIN_PY
ORCH = h.ORCH_DIR
SRC = h.REPO_ROOT / "src"
APPS = h.REPO_ROOT / "apps"
QUERY = "what's the weather in Anytown today"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    h.reset_runtime()
    yield
    h.reset_runtime()
    h.shared_config._clear_cache_for_tests()


def _ingress(monkeypatch, *, mode="enforce", dev=False):
    monkeypatch.setenv("ORCHESTRATOR_INGRESS_AUTH", mode)
    monkeypatch.setenv("DEV_MODE", "true" if dev else "false")
    h.shared_config._clear_cache_for_tests()


# --- (b) the proof matrix ---------------------------------------------------

TRUSTS = [*typing.get_args(typing.get_args(typing.get_type_hints(h.main.QueryRequest)["caller_trust"])[0]), None]
SERVERS = ["owner", "guest", "degraded"]
REQUEST_MODES = ["owner", "guest", None]
DEVICES = [None, {"guest_id": 9, "guest_name": "Bob Device", "device_type": "voice"}]
PROOF_CASES = list(itertools.product(TRUSTS, [True, False], SERVERS, REQUEST_MODES, range(len(DEVICES))))


def _authorize(trust, authenticated, server, request_mode, device):
    if server == "degraded":
        h.install_mode_client(degraded=True)
    else:
        h.install_mode_client(server_mode=server)
    return asyncio.run(h.mode_permission.resolve_request_authorization(
        request_mode, device, caller_trust=trust, service_authenticated=authenticated,
    ))


def test_proof_matrix_floor_and_named_member():
    assert len(PROOF_CASES) >= 288
    assert ("household", True, "owner", None, 0) in PROOF_CASES
    assert "web_owner" in TRUSTS


@pytest.mark.parametrize("trust,authenticated,server,request_mode,device_index", PROOF_CASES)
def test_owner_proven_only_for_the_one_combination(trust, authenticated, server, request_mode, device_index):
    authz = _authorize(trust, authenticated, server, request_mode, DEVICES[device_index])
    aud = authz.knowledge_audience
    expected_caller = trust == "web_owner" and authenticated
    expected_proven = expected_caller and server == "owner" and request_mode != "guest" and device_index == 0
    assert aud.owner_caller is expected_caller
    assert aud.owner_proven is expected_proven
    assert ("owner" in aud.visible_tiers()) is expected_proven
    if expected_proven:
        assert aud.mode == "owner" and not aud.degraded


def test_named_member_household_with_an_authenticated_hop_is_not_proven():
    assert not _authorize("household", True, "owner", None, None).knowledge_audience.owner_proven


def test_web_public_is_never_an_owner_caller():
    aud = _authorize("web_public", True, "owner", None, None).knowledge_audience
    assert aud.public and not aud.owner_caller


def test_a_non_bool_service_flag_is_not_authentication():
    for flag in (None, 1, "yes", object()):
        h.install_mode_client(server_mode="owner")
        authz = asyncio.run(h.mode_permission.resolve_request_authorization(
            None, None, caller_trust="web_owner", service_authenticated=flag,
        ))
        assert not authz.knowledge_audience.owner_caller


# --- (c) PIN tiers ----------------------------------------------------------

def test_pin_tier_maps_web_owner_to_web_authenticated():
    pin_tier = h.mode_permission._pin_tier
    assert pin_tier("web_owner") == "web_authenticated"
    assert pin_tier("web_authenticated") == "web_authenticated"
    assert pin_tier("household") == "household" and pin_tier(None) is None
    assert h.mode_permission.SIGNED_IN_TRUST == frozenset({"web_authenticated", "web_owner"})


def test_pin_utterance_posts_the_derived_tier_for_web_owner():
    client = h.install_mode_client(server_mode="owner")
    client.post = mock.AsyncMock(return_value=h.make_response(200, {"success": True, "expires_at": "x", "duration_minutes": 5}))
    asyncio.run(h.mode_permission.handle_owner_mode_utterance("switch to owner mode pin 123456", "web_owner", "kitchen"))
    sent = [c for c in client.post.await_args_list if "caller_tier" in (c.kwargs.get("json") or {})]
    assert sent and sent[0].kwargs["json"]["caller_tier"] == "web_authenticated"


# --- (d)/(e) AST drift guards ----------------------------------------------

def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _calls(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", None)) == name]


def _enclosing(tree):
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    def name_of(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return "<module>"
    return name_of


def test_every_orchestrator_state_in_main_passes_the_audience():
    tree = _tree(MAIN)
    calls = _calls(tree, "OrchestratorState")
    assert len(calls) >= 4
    enclosing = _enclosing(tree)
    assert "process_query" in {enclosing(c) for c in calls}
    for call in calls:
        assert "knowledge_audience" in {k.arg for k in call.keywords}, f"main.py:{call.lineno}"


def test_every_authorization_and_entry_call_passes_service_authenticated():
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree = _tree(path)
        except SyntaxError:
            continue
        for call in _calls(tree, "resolve_request_authorization"):
            assert "service_authenticated" in {k.arg for k in call.keywords}, f"{path.name}:{call.lineno}"
    tree = _tree(MAIN)
    enclosing = _enclosing(tree)
    direct = [c for name in ("process_query", "process_query_stream", "process_query_stream_v2") for c in _calls(tree, name)]
    assert len(direct) >= 1
    assert "chat_completions" in {enclosing(c) for c in direct}
    for call in direct:
        kw = {k.arg: k.value for k in call.keywords}
        assert isinstance(kw.get("service_authenticated"), ast.Constant) and kw["service_authenticated"].value is False


def test_handlers_use_the_dependency_and_test_it_with_is_true():
    tree = _tree(MAIN)
    source = MAIN.read_text(encoding="utf-8")
    for name in ("process_query", "process_query_stream", "process_query_stream_v2"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == name)
        args = {a.arg for a in fn.args.args}
        assert "service_authenticated" in args, name
        segment = ast.get_source_segment(source, fn)
        assert "service_authenticated=service_authenticated is True" in segment, name


def test_only_the_authorization_builders_construct_a_proven_audience():
    allowed = {SRC / "shared" / "knowledge_tiers.py", ORCH / "mode_permission.py"}
    offenders = []
    for root in (SRC, APPS):
        for path in sorted(root.rglob("*.py")):
            if path in allowed:
                continue
            try:
                tree = _tree(path)
            except SyntaxError:
                continue
            for call in _calls(tree, "KnowledgeAudience"):
                for k in call.keywords:
                    if k.arg == "owner_proven" and not (isinstance(k.value, ast.Constant) and k.value.value is False):
                        offenders.append(f"{path}:{call.lineno}")
    assert offenders == []


# --- (f) the dependency -----------------------------------------------------

def _request(headers=None):
    return SimpleNamespace(headers=headers or {}, url=SimpleNamespace(path="/query"), client=None)


def test_service_key_matches_needs_a_correct_nonempty_header(monkeypatch):
    key = h.shared_config.get_config().service_api_key
    assert key
    assert ingress_auth.service_key_matches(_request({"X-Service-Key": key})) is True
    assert ingress_auth.service_key_matches(_request({"X-Service-Key": key + "x"})) is False
    assert ingress_auth.service_key_matches(_request({"X-Service-Key": ""})) is False
    _ingress(monkeypatch, mode="warn")
    assert ingress_auth.service_key_matches(_request()) is False
    _ingress(monkeypatch, dev=True)
    assert ingress_auth.service_key_matches(_request()) is False
    assert asyncio.run(ingress_auth.service_authenticated(_request())) is False
    monkeypatch.setenv("SERVICE_API_KEY", "")
    h.shared_config._clear_cache_for_tests()
    assert ingress_auth.service_key_matches(_request({"X-Service-Key": ""})) is False


def test_a_direct_call_without_the_dependency_fails_closed(monkeypatch):
    spy = mock.AsyncMock(side_effect=RuntimeError("stop after authorization"))
    monkeypatch.setattr(h.main, "resolve_request_authorization", spy)
    h.install_mode_client(server_mode="owner")
    request = h.main.QueryRequest(query="hi", caller_trust="web_owner")
    with pytest.raises(Exception):
        asyncio.run(h.main.process_query(request))
    flag = spy.await_args.kwargs["service_authenticated"]
    assert flag is False


# --- (g) memory writers -----------------------------------------------------

def test_process_query_is_the_only_memory_writer():
    tree = _tree(MAIN)
    writers = set()
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _calls(fn, "should_create_memory"):
            writers.add(fn.name)
    assert writers == {"process_query"}
    creators = set()
    for path in sorted(ORCH.rglob("*.py")):
        if path.name == "memory_manager.py":
            continue
        tree = _tree(path)
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and _calls(fn, "create_memory"):
                creators.add((path.name, fn.name))
    assert creators == {("main.py", "process_query")}


# --- end to end over HTTP ---------------------------------------------------

class _Graph:
    def __init__(self, answer="PLAIN ANSWER"):
        self.answer = answer
        self.states = []

    async def ainvoke(self, state):
        self.states.append(state)
        # Run the real prompt builders against the real initial state.
        state.intent = h.IntentCategory.WEATHER
        state = await h.main.tool_call_node(state)
        synth = state.model_copy()
        synth.answer = None
        synth.skip_synthesis = False
        synth.retrieved_data = {"weather": {"current": {"temp": 70}}}
        await h.synthesize_module.synthesize_node(synth)
        return {"intent": h.IntentCategory.WEATHER, "answer": self.answer, "confidence": 1.0,
                "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True}


@pytest.fixture
def rig(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    admin = h.fake_admin_client()
    h.use_real_knowledge_readers(monkeypatch, h.real_admin_client())
    h.patch_tool_call_dependencies  # noqa: B018  (harness already applied by use_real_knowledge_readers)
    llm = h.CapturingLLM()
    h._runtime.set_llm_router(llm)
    graph = _Graph()
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)
    monkeypatch.setattr(h.main, "should_use_tool_calling", mock.AsyncMock(return_value=True))
    monkeypatch.setattr(h.main, "get_admin_client", lambda: admin)
    return SimpleNamespace(client=TestClient(h.main.app), llm=llm, graph=graph, monkeypatch=monkeypatch, admin=admin)


def _post(rig, path="/query", trust="web_owner", headers=True, **extra):
    body = {"query": QUERY, "interface_type": "chat", "caller_trust": trust, **extra}
    resp = rig.client.post(path, json=body, headers=h.service_headers() if headers else {})
    assert resp.status_code == 200, resp.text
    return resp


def _owner_state(rig):
    return rig.graph.states[-1].knowledge_audience


@pytest.mark.parametrize("mode,dev", [("warn", False), ("enforce", True)], ids=["warn", "dev_mode"])
def test_unauthenticated_web_owner_body_is_never_proven(rig, monkeypatch, mode, dev):
    _ingress(monkeypatch, mode=mode, dev=dev)
    _post(rig, headers=False)
    aud = _owner_state(rig)
    assert aud.owner_caller is False and aud.owner_proven is False
    text = rig.llm.text()
    assert h.S_OWNER not in text and h.OWNER_NAME not in text
    assert h.S_HOUSEHOLD in text


def test_authenticated_web_owner_is_proven_and_sees_owner_rows(rig, monkeypatch):
    """Positive control for the forgery tests above: the correct header flips
    exactly the proof, and the real compiled prompts see the owner sentinel."""
    _ingress(monkeypatch, mode="warn")
    resp = _post(rig)
    aud = _owner_state(rig)
    assert aud.owner_caller and aud.owner_proven
    text = rig.llm.text()
    assert h.S_OWNER in text and h.S_OWNERCAT_BOTH in text
    assert f"You are speaking with {h.OWNER_NAME}" in text
    assert resp.json()["session_id"].startswith("own-")


def test_degraded_web_owner_with_an_authenticated_hop_gets_nothing_owner(rig, monkeypatch):
    h.install_mode_client(degraded=True)
    _post(rig)
    aud = _owner_state(rig)
    assert aud.owner_caller and not aud.owner_proven
    text = rig.llm.text()
    for hidden in (h.S_OWNER, h.S_OWNERCAT_BOTH, h.S_HOUSEHOLD, h.OWNER_NAME):
        assert hidden not in text


def test_chat_history_is_ignored_for_an_unproven_owner_caller(rig):
    h.install_mode_client(server_mode="guest")  # a stay: web_owner is a caller, not proven
    history = [{"role": "user", "content": "SECRET EARLIER TURN"}, {"role": "assistant", "content": "SECRET ANSWER"}]
    _post(rig, chat_history=history)
    assert _owner_state(rig).owner_caller and not _owner_state(rig).owner_proven
    assert rig.graph.states[-1].conversation_history == []
    assert "SECRET" not in rig.llm.text()


def test_chat_history_still_reaches_an_ordinary_caller(rig):
    history = [{"role": "user", "content": "EARLIER TURN"}, {"role": "assistant", "content": "EARLIER ANSWER"}]
    _post(rig, trust="household", chat_history=history)
    assert [m["content"] for m in rig.graph.states[-1].conversation_history] == ["EARLIER TURN", "EARLIER ANSWER"]


class _MemoryCache:
    def __init__(self):
        self.data, self.gets, self.sets = {}, 0, 0

    async def get(self, key):
        self.gets += 1
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.sets += 1
        self.data[key] = value


@pytest.mark.parametrize("authed_proven", [True, False], ids=["proven", "stay_unproven"])
def test_owner_caller_turns_never_read_or_write_the_semantic_cache(rig, authed_proven):
    if not authed_proven:
        h.install_mode_client(server_mode="guest")
    get_spy, set_spy = mock.AsyncMock(return_value=None), mock.AsyncMock()
    rig.monkeypatch.setattr(h.main, "get_cached_response", get_spy)
    rig.monkeypatch.setattr(h.main, "cache_response", set_spy)
    _post(rig)
    time.sleep(0.05)
    get_spy.assert_not_awaited()
    set_spy.assert_not_called()


def test_a_primed_cache_entry_is_never_served_to_a_proven_owner(rig):
    store = _MemoryCache()
    rig.monkeypatch.setattr(semantic_cache, "get_cache_client", lambda: store)
    _post(rig, trust="household")
    deadline = time.time() + 2
    while store.sets < 1 and time.time() < deadline:
        time.sleep(0.01)
    assert store.sets == 1, "floor: the household answer was cached"
    calls = len(rig.graph.states)
    _post(rig)
    assert len(rig.graph.states) == calls + 1, "the proven owner ran the pipeline instead of reading the cache"
    time.sleep(0.05)
    assert store.sets == 1


def _memory_manager():
    manager = mock.MagicMock()
    manager.should_create_memory.return_value = True
    manager.extract_memorable_fact.return_value = "fact"
    manager.calculate_importance.return_value = 0.5
    manager.classify_memory_category.return_value = "fact"
    manager.should_forget_memory.return_value = False
    manager.create_memory = mock.AsyncMock(return_value=True)
    manager.get_relevant_memories = mock.AsyncMock(return_value=[{"content": "OWNER SCOPE MEMORY"}])
    manager.format_memory_context.return_value = "MEMORY: OWNER SCOPE MEMORY"
    manager.get_active_guest_session = mock.AsyncMock(return_value=None)
    return manager


@pytest.mark.parametrize("trust,authed,expect_created", [
    ("web_owner", True, False),
    ("web_owner", False, True),  # no authenticated hop: an ordinary caller, not an owner caller
    ("household", True, True),
], ids=["proven_owner", "unauthenticated_owner_claim_is_ordinary", "household_control"])
def test_memories_are_created_only_for_non_owner_callers(rig, monkeypatch, trust, authed, expect_created):
    _ingress(monkeypatch, mode="warn")
    manager = _memory_manager()
    rig.monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
    rig.monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: False)
    _post(rig, trust=trust, headers=authed)
    time.sleep(0.05)
    assert manager.create_memory.called is expect_created


def test_owner_scope_memories_stay_household_level(rig, monkeypatch):
    """D7/D17: owner-scope memories are heard by everyone at home, as before.
    An unproven owner-mode caller still retrieves them (positive control for
    the guest and public cases below); only the WRITE side is gated."""
    _ingress(monkeypatch, mode="warn")
    manager = _memory_manager()
    rig.monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
    rig.monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: False)
    for trust, headers in (("household", True), ("web_owner", False), ("web_owner", True)):
        manager.get_relevant_memories.reset_mock()
        _post(rig, trust=trust, headers=headers)
        manager.get_relevant_memories.assert_awaited_once()
        assert manager.get_relevant_memories.await_args.kwargs["mode"] == "owner", (trust, headers)


def test_guest_and_public_callers_do_not_get_owner_scope_memories(rig, monkeypatch):
    manager = _memory_manager()
    rig.monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
    rig.monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: False)
    h.install_mode_client(server_mode="guest")
    _post(rig, trust="household")
    assert manager.get_relevant_memories.await_args.kwargs["mode"] == "guest"
    manager.get_relevant_memories.reset_mock()
    h.install_mode_client(server_mode="owner")
    _post(rig, trust="web_public")
    manager.get_relevant_memories.assert_not_awaited()


# --- unverified guest hint: cache and forget ---------------------------------

def test_a_guest_mode_hint_in_an_owner_house_neither_reads_nor_writes_the_cache(rig):
    get_spy, set_spy = mock.AsyncMock(return_value=None), mock.AsyncMock()
    rig.monkeypatch.setattr(h.main, "get_cached_response", get_spy)
    rig.monkeypatch.setattr(h.main, "cache_response", set_spy)
    _post(rig, trust="web_local", mode="guest")
    time.sleep(0.05)
    get_spy.assert_not_awaited()
    set_spy.assert_not_called()


def test_a_real_guest_stay_still_uses_the_cache(rig):
    """Positive control: server guest mode is a verified guest."""
    h.install_mode_client(server_mode="guest")
    get_spy, set_spy = mock.AsyncMock(return_value=None), mock.AsyncMock()
    rig.monkeypatch.setattr(h.main, "get_cached_response", get_spy)
    rig.monkeypatch.setattr(h.main, "cache_response", set_spy)
    _post(rig, trust="web_local", mode="guest")
    time.sleep(0.05)
    get_spy.assert_awaited_once()
    set_spy.assert_called_once()


def _forget_manager():
    manager = _memory_manager()
    manager.should_forget_memory.return_value = True
    manager.extract_forget_content.return_value = "my secret"
    manager.delete_memory_by_content = mock.AsyncMock(return_value={"deleted": 1})
    return manager


@pytest.mark.parametrize("setup,kwargs", [
    ("stay_owner_caller", dict(trust="web_owner")),          # unproven owner caller during a stay
    ("owner_house_guest_hint", dict(trust="web_local", mode="guest")),
], ids=["unproven_owner_caller", "guest_hint"])
def test_forget_cannot_delete_for_callers_outside_their_audience(rig, setup, kwargs):
    manager = _forget_manager()
    rig.monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
    if setup == "stay_owner_caller":
        h.install_mode_client(server_mode="guest")
    _post(rig, **kwargs)
    manager.delete_memory_by_content.assert_not_awaited()


def test_forget_still_works_for_a_household_caller_and_a_proven_owner(rig):
    for trust in ("household", "web_owner"):
        manager = _forget_manager()
        rig.monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
        resp = _post(rig, trust=trust)
        manager.delete_memory_by_content.assert_awaited_once()
        assert resp.json()["intent"] == "memory_forget"


# --- non-ASCII service key header --------------------------------------------

def test_a_non_ascii_service_key_header_is_rejected_not_a_500(rig):
    resp = rig.client.post(
        "/query", json={"query": QUERY, "caller_trust": "web_owner"},
        headers={"X-Service-Key": "café-key".encode("utf-8")},
    )
    assert resp.status_code == 401
    assert ingress_auth.service_key_matches(_request({"X-Service-Key": "café"})) is False
    assert ingress_auth.service_key_matches(_request({"X-Service-Key": "☃"})) is False
