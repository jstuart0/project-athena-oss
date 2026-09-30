"""Answers addressed to a named caller are never cached or served from the
cache (the guest via SMS or the guest network; a signed-in member), so one
caller's name can't reach another through the semantic cache."""
from __future__ import annotations

import time
from unittest import mock

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


# row -> request body fields
ROW_2 = {"caller_trust": "household"}
ROW_10 = {"caller_trust": "web_authenticated", "mode": "guest", "context": {"speaker_first_name": "Pat"}}
ROW_15 = {"caller_trust": "sms", "mode": "guest", "context": {"guest_name": "Sam Texter", "guest_id": 3}}
ROW_8 = {"caller_trust": "web_guest_net", "mode": "guest", "context": {"guest_name": "Gina Guest", "guest_id": 7}}


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.fixture
def client(monkeypatch):
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="guest")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    return TestClient(h.main.app)


@pytest.fixture
def cache(monkeypatch):
    store = _MemoryCache()
    monkeypatch.setattr(semantic_cache, "get_cache_client", lambda: store)
    category, _ = semantic_cache.extract_semantic_intent(QUERY)
    assert semantic_cache.is_cacheable(category, QUERY), "precondition: the query is cacheable"
    return store


def _ask(client, monkeypatch, row, answer):
    graph = _Graph(answer)
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)
    body = {"query": QUERY, "interface_type": "chat", **row}
    resp = client.post("/query", json=body, headers=h.service_headers())
    assert resp.status_code == 200, resp.text
    return resp.json()["answer"], graph


def _wait_for_sets(cache, n):
    deadline = time.time() + 2
    while cache.sets < n and time.time() < deadline:
        time.sleep(0.01)


@pytest.mark.parametrize("row", [ROW_15, ROW_8, ROW_10], ids=["sms_guest", "guest_net", "member"])
def test_named_audience_neither_reads_nor_writes(client, monkeypatch, row):
    get_spy = mock.AsyncMock(return_value=None)
    set_spy = mock.AsyncMock()
    monkeypatch.setattr(h.main, "get_cached_response", get_spy)
    monkeypatch.setattr(h.main, "cache_response", set_spy)
    _ask(client, monkeypatch, row, "NAMED ANSWER")
    time.sleep(0.05)
    get_spy.assert_not_awaited()
    set_spy.assert_not_called()


def test_degraded_mode_neither_reads_nor_writes(client, monkeypatch):
    """Row 23: a degraded mode service resolves to owner; its answers are
    never cached, and a cached owner answer is never served."""
    h.install_mode_client(degraded=True)
    get_spy = mock.AsyncMock(return_value=None)
    set_spy = mock.AsyncMock()
    monkeypatch.setattr(h.main, "get_cached_response", get_spy)
    monkeypatch.setattr(h.main, "cache_response", set_spy)
    _ask(client, monkeypatch, ROW_2, "DEGRADED ANSWER")
    time.sleep(0.05)
    get_spy.assert_not_awaited()
    set_spy.assert_not_called()


def test_unnamed_household_still_uses_the_cache(client, monkeypatch):
    """Positive control: the same query from row 2 reads and writes."""
    get_spy = mock.AsyncMock(return_value=None)
    set_spy = mock.AsyncMock()
    monkeypatch.setattr(h.main, "get_cached_response", get_spy)
    monkeypatch.setattr(h.main, "cache_response", set_spy)
    _ask(client, monkeypatch, ROW_2, "HOUSEHOLD ANSWER")
    time.sleep(0.05)
    get_spy.assert_awaited_once()
    set_spy.assert_called_once()


def test_primed_household_answer_never_reaches_named_callers(client, monkeypatch, cache):
    answer, _ = _ask(client, monkeypatch, ROW_2, "HOUSEHOLD ANSWER")
    assert answer == "HOUSEHOLD ANSWER"
    _wait_for_sets(cache, 1)
    assert cache.sets == 1, "floor: the household answer was cached"
    for row, name in ((ROW_10, "PAT"), (ROW_15, "SAM")):
        answer, graph = _ask(client, monkeypatch, row, f"{name} ANSWER")
        assert answer == f"{name} ANSWER"
        assert graph.calls == 1
    time.sleep(0.05)
    assert cache.sets == 1, "a named answer was written to the cache"
    answer, graph = _ask(client, monkeypatch, ROW_2, "SECOND")
    assert answer == "HOUSEHOLD ANSWER"
    assert graph.calls == 0
