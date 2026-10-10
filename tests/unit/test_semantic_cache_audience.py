"""Semantic cache partitioning, black-box through /query (V1.6, D17).

The cache storage is an in-memory dict behind the real key logic, so a
replay across audiences shows up as the wrong answer in the response.
"""
from __future__ import annotations

import ast
import time

import pytest
from fastapi.testclient import TestClient

from . import _public_audience_harness as h
import orchestrator.semantic_cache as semantic_cache

QUERY = "what's the weather in Anytown today"


class _MemoryCache:
    def __init__(self):
        self.data = {}
        self.gets = 0
        self.sets = 0

    async def get(self, key):
        self.gets += 1
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.sets += 1
        self.data[key] = value


class _Graph:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    async def ainvoke(self, state):
        self.calls += 1
        return {"intent": h.IntentCategory.WEATHER, "answer": self.answer, "confidence": 1.0,
                "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True}


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.fixture
def cache(monkeypatch):
    store = _MemoryCache()
    monkeypatch.setattr(semantic_cache, "get_cache_client", lambda: store)
    category, _ = semantic_cache.extract_semantic_intent(QUERY)
    assert semantic_cache.is_cacheable(category, QUERY), "precondition: the query is cacheable"
    return store


@pytest.fixture
def client(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    return TestClient(h.main.app)


def _ask(client, monkeypatch, *, answer, guest=None, caller_trust="household", server_mode=None):
    if server_mode:
        h.install_mode_client(server_mode=server_mode)
    admin = h.fake_admin_client(guest_info=guest)
    monkeypatch.setattr(h.main, "get_admin_client", lambda: admin)
    graph = _Graph(answer)
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)
    body = {"query": QUERY, "caller_trust": caller_trust, "interface_type": "chat"}
    if guest:
        body["device_id"] = "device-" + str(guest["guest_id"])
    resp = client.post("/query", json=body, headers=h.service_headers())
    assert resp.status_code == 200
    return resp.json()["answer"], graph


def _wait_for_sets(cache, n):
    deadline = time.time() + 2
    while cache.sets < n and time.time() < deadline:
        time.sleep(0.01)


def test_guest_never_gets_owner_cached_answer(client, monkeypatch, cache):
    """Named: the owner asks, then a device-identified guest asks the same
    thing and must not receive the owner's cached answer."""
    answer, _ = _ask(client, monkeypatch, answer="OWNER ANSWER")
    assert answer == "OWNER ANSWER"
    _wait_for_sets(cache, 1)
    assert cache.sets == 1, "floor: the owner answer was cached"
    answer, graph = _ask(client, monkeypatch, answer="GUEST ANSWER", guest={"guest_id": 7, "guest_name": "A"})
    assert answer == "GUEST ANSWER"
    assert graph.calls == 1


def test_guest_ids_partition(client, monkeypatch, cache):
    _ask(client, monkeypatch, answer="GUEST A", guest={"guest_id": 1, "guest_name": "A"})
    _wait_for_sets(cache, 1)
    answer, _ = _ask(client, monkeypatch, answer="GUEST B", guest={"guest_id": 2, "guest_name": "B"})
    assert answer == "GUEST B"


def test_same_audience_still_hits(client, monkeypatch, cache):
    """Positive control: the cache still works within one audience."""
    _ask(client, monkeypatch, answer="OWNER ANSWER")
    _wait_for_sets(cache, 1)
    answer, graph = _ask(client, monkeypatch, answer="SECOND")
    assert answer == "OWNER ANSWER"
    assert graph.calls == 0


def test_public_never_reads_cache(client, monkeypatch, cache):
    _ask(client, monkeypatch, answer="OWNER ANSWER")
    _wait_for_sets(cache, 1)
    gets_before = cache.gets
    answer, graph = _ask(client, monkeypatch, answer="PUBLIC ANSWER", caller_trust="web_public")
    assert answer == "PUBLIC ANSWER"
    assert cache.gets == gets_before


def test_public_never_writes_cache(client, monkeypatch, cache):
    _ask(client, monkeypatch, answer="PUBLIC ANSWER", caller_trust="web_public")
    time.sleep(0.1)
    assert cache.sets == 0
    assert cache.gets == 0


def test_owner_and_guest_keys_differ():
    owner = semantic_cache.get_cache_key("weather_current", QUERY, mode="owner", interface_type="chat")
    guest = semantic_cache.get_cache_key("weather_current", QUERY, mode="guest", interface_type="chat")
    guest7 = semantic_cache.get_cache_key("weather_current", QUERY, mode="guest", guest_id=7, interface_type="chat")
    guest8 = semantic_cache.get_cache_key("weather_current", QUERY, mode="guest", guest_id=8, interface_type="chat")
    assert len({owner, guest, guest7, guest8}) == 4


def test_cache_calls_have_one_site_each():
    tree = ast.parse(h.MAIN_PY.read_text(encoding="utf-8"))
    counts = {"get_cached_response": 0, "cache_response": 0}
    for path in sorted(h.ORCH_DIR.rglob("*.py")):
        if path.name == "semantic_cache.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in counts:
                counts[node.func.id] += 1
                assert {k.arg for k in node.keywords} >= {"mode", "guest_id"}, f"{path.name}:{node.lineno}"
    assert counts == {"get_cached_response": 1, "cache_response": 1}


def test_house_flips_to_guest_then_nobody_else_gets_owner_answer(client, monkeypatch, cache):
    """Owner asks while the house is owner; the house flips to guest; an
    anonymous caller and a household caller now both get a fresh answer."""
    _ask(client, monkeypatch, answer="OWNER ANSWER", server_mode="owner")
    _wait_for_sets(cache, 1)
    answer, graph = _ask(client, monkeypatch, answer="PUBLIC ANSWER", caller_trust="web_public", server_mode="guest")
    assert answer == "PUBLIC ANSWER" and graph.calls == 1
    answer, graph = _ask(client, monkeypatch, answer="GUEST HOUSE ANSWER", server_mode="guest")
    assert answer == "GUEST HOUSE ANSWER" and graph.calls == 1


def test_guest_house_guest_answer_stays_with_that_guest(client, monkeypatch, cache):
    """House in guest mode: guest 1's cached answer is not served to an
    anonymous caller nor to an unidentified guest-mode caller."""
    _ask(client, monkeypatch, answer="GUEST ONE", guest={"guest_id": 1, "guest_name": "A"}, server_mode="guest")
    _wait_for_sets(cache, 1)
    answer, graph = _ask(client, monkeypatch, answer="PUBLIC ANSWER", caller_trust="web_public", server_mode="guest")
    assert answer == "PUBLIC ANSWER" and graph.calls == 1
    answer, graph = _ask(client, monkeypatch, answer="UNIDENTIFIED", server_mode="guest")
    assert answer == "UNIDENTIFIED" and graph.calls == 1
    answer, graph = _ask(client, monkeypatch, answer="SECOND", guest={"guest_id": 1, "guest_name": "A"}, server_mode="guest")
    assert answer == "GUEST ONE" and graph.calls == 0
