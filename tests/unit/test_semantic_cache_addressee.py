"""Answers addressed to a named caller are never cached or served from the
cache (the guest via SMS or the guest network; a signed-in member), so one
caller's name can't reach another through the semantic cache."""
from __future__ import annotations

import asyncio
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


# --- cache key shape (D8) ----------------------------------------------------

def _key(**kw):
    kw.setdefault("knowledge_digest", "abc123def456")
    return semantic_cache.get_cache_key("weather_current", QUERY, **kw)


def test_key_version_segment_sits_right_before_mode():
    parts = _key(mode="owner", guest_id=7, location_override={"address": "1 Main"}).split(":")
    assert parts[0] == "athena_semantic"
    # …:kv2:kb_<digest>:mode_…
    assert parts[parts.index("kv2") + 1] == "kb_abc123def456"
    assert parts[parts.index("kv2") + 2] == "mode_owner"
    assert semantic_cache.CACHE_KEY_VERSION == "kv2"


def test_a_later_interface_segment_stays_last():
    # The speech campaign appends iface_<type> after everything else; the
    # version segment must still precede mode_ and the location segment.
    parts = (_key(mode="guest", guest_id=3, location_override={"address": "1 Main"}) + ":iface_voice").split(":")
    assert parts[-1] == "iface_voice"
    assert parts.index("kv2") < parts.index("kb_abc123def456") < parts.index("mode_guest")
    assert parts.index("mode_guest") < next(i for i, p in enumerate(parts) if p.startswith("loc_"))


def test_invalidation_prefix_still_matches_a_new_key():
    import fnmatch

    key = semantic_cache.get_cache_key("weather_current", QUERY, mode="owner", knowledge_digest="abc123def456")
    assert fnmatch.fnmatch(key, "athena_semantic:weather_*")


# --- the knowledge digest (a narrowed row can't be replayed from the cache) ---------

def _digest(entries):
    from shared.base_knowledge_utils import knowledge_digest

    return knowledge_digest(entries)


ROWS = [
    {"id": 1, "category": "property", "key": "a", "value": "alpha", "applies_to": "household", "enabled": True,
     "priority": 1, "updated_at": "2026-01-01T00:00:00"},
    {"id": 2, "category": "property", "key": "b", "value": "beta", "applies_to": "both", "enabled": True,
     "priority": 2, "updated_at": "2026-01-02T00:00:00"},
]


def test_digest_is_stable_across_row_order_and_shape_is_short_hex():
    first = _digest(ROWS)
    assert first == _digest(list(reversed(ROWS)))
    assert len(first) == 12 and int(first, 16) >= 0


@pytest.mark.parametrize("change", [
    {"value": "alpha2"}, {"applies_to": "owner"}, {"enabled": False}, {"updated_at": "2026-02-01T00:00:00"},
    {"priority": 9}, {"key": "z"},
])
def test_digest_changes_when_a_visible_row_changes(change):
    changed = [{**ROWS[0], **change}, ROWS[1]]
    assert _digest(changed) != _digest(ROWS)
    assert _digest(ROWS[1:]) != _digest(ROWS)  # a deleted row


def test_digest_differs_per_audience():
    from shared.base_knowledge_utils import knowledge_cache_digest

    client = h.real_admin_client(ROWS)
    digests = {
        name: asyncio.run(knowledge_cache_digest(client, audience=aud))
        for name, aud in {
            "household": h.audience("owner"),
            "guest": h.audience("guest"),
            "degraded": h.audience("owner", degraded=True),
        }.items()
    }
    assert digests["household"] != digests["guest"] and digests["household"] != digests["degraded"]


def test_unloadable_knowledge_has_no_digest_and_an_empty_audience_has_a_constant_one():
    from shared.base_knowledge_utils import knowledge_cache_digest
    from shared.knowledge_tiers import KnowledgeAudience

    client = h.real_admin_client(ROWS)
    client.fail_fetch = True
    assert asyncio.run(knowledge_cache_digest(client, audience=h.audience("owner"))) is None
    assert asyncio.run(knowledge_cache_digest(client, audience=KnowledgeAudience.UNRESOLVED)) == _digest([])


def test_the_raw_list_window_is_a_few_seconds(monkeypatch):
    from shared import admin_config

    assert admin_config.BASE_KNOWLEDGE_CACHE_TTL_SECONDS <= 5
    client = h.real_admin_client(ROWS)
    clock = {"now": 1000.0}
    monkeypatch.setattr(admin_config.time, "time", lambda: clock["now"])
    first = asyncio.run(client.get_base_knowledge(tiers=frozenset({"household", "both"})))
    client.served[:] = ROWS[1:]
    clock["now"] += admin_config.BASE_KNOWLEDGE_CACHE_TTL_SECONDS - 0.5
    assert len(asyncio.run(client.get_base_knowledge(tiers=frozenset({"household", "both"})))) == len(first)
    clock["now"] += 1
    assert len(asyncio.run(client.get_base_knowledge(tiers=frozenset({"household", "both"})))) == 1


def test_an_answer_derived_from_a_narrowed_row_is_not_replayed(client, monkeypatch, cache):
    """Cache an answer while a row is visible to the household, narrow the row
    to Owner only, and ask the same question: a miss, not the old answer."""
    admin = h.real_admin_client([dict(r) for r in ROWS])
    admin._base_knowledge_cache_ttl = 0  # the digest sees the change at once
    monkeypatch.setattr(h.main, "get_admin_client", lambda: admin)
    h.install_mode_client(server_mode="owner")

    answer, graph = _ask(client, monkeypatch, ROW_2, "DERIVED FROM ALPHA")
    assert answer == "DERIVED FROM ALPHA" and graph.calls == 1
    _wait_for_sets(cache, 1)
    answer, graph = _ask(client, monkeypatch, ROW_2, "SHOULD NOT RUN")
    assert answer == "DERIVED FROM ALPHA" and graph.calls == 0, "positive control: same knowledge, cache hit"

    admin.served[0] = {**admin.served[0], "applies_to": "owner", "updated_at": "2026-03-01T00:00:00"}
    answer, graph = _ask(client, monkeypatch, ROW_2, "FRESH ANSWER")
    assert answer == "FRESH ANSWER" and graph.calls == 1, "the narrowed row's answer was replayed"
    _wait_for_sets(cache, 2)
    assert cache.sets == 2


def test_an_unloadable_knowledge_base_skips_the_cache_both_ways(client, monkeypatch):
    admin = h.real_admin_client([dict(r) for r in ROWS])
    admin._base_knowledge_cache_ttl = 0
    admin.fail_fetch = True
    monkeypatch.setattr(h.main, "get_admin_client", lambda: admin)
    h.install_mode_client(server_mode="owner")
    get_spy, set_spy = mock.AsyncMock(return_value=None), mock.AsyncMock()
    monkeypatch.setattr(h.main, "get_cached_response", get_spy)
    monkeypatch.setattr(h.main, "cache_response", set_spy)
    _ask(client, monkeypatch, ROW_2, "OUTAGE ANSWER")
    time.sleep(0.05)
    get_spy.assert_not_awaited()
    set_spy.assert_not_called()
