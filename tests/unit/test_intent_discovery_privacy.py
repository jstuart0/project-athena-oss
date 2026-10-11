"""Intent discovery sends no query text that capture wouldn't store, and hashes with a key.

* sample queries: none while Analytics Mode is off or for a PIN-shaped query, else digit-redacted;
* query_hash: HMAC-SHA256 keyed on SERVICE_API_KEY with its own domain, NULL for a PIN-shaped query.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from orchestrator import intent_discovery as idisc  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from shared import conversation_capture as cc  # noqa: E402


class FakeSink:
    def __init__(self, analytics):
        self.analytics = analytics

    def analytics_enabled(self):
        return self.analytics


@pytest.fixture(autouse=True)
def _reset():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


@pytest.fixture
def key(monkeypatch):
    from shared import config as shared_config

    monkeypatch.setenv("SERVICE_API_KEY", "test-key-intent-hash")
    shared_config._clear_cache_for_tests()
    yield "test-key-intent-hash"
    shared_config._clear_cache_for_tests()


def test_no_sample_without_a_sink_or_while_analytics_is_off():
    assert idisc.discovery_sample("what's the weather") is None
    _runtime.set_turn_capture(FakeSink(False))
    assert idisc.discovery_sample("what's the weather") is None


def test_pin_shaped_queries_send_no_sample():
    _runtime.set_turn_capture(FakeSink(True))
    for query in ("switch to owner mode", "my code is one two three four five six", "pin 123456"):
        assert idisc.discovery_sample(query) is None, query


def test_an_ordinary_query_is_sent_redacted_and_capped():
    _runtime.set_turn_capture(FakeSink(True))
    assert idisc.discovery_sample("what is the weather in 2026") == "what is the weather in [number]"
    assert len(idisc.discovery_sample("word " * 200)) <= idisc.SAMPLE_MAX_CHARS


def test_the_real_sink_reports_the_cached_flag():
    async def run():
        class Store:
            async def read_flags(self):
                return cc.CaptureFlags(analytics=True)

            async def write(self, record):
                return cc.RESULT_WRITTEN

            async def report(self, status):
                pass

            async def close(self):
                pass

        sink = cc.CaptureSink(Store(), flag_ttl=60)
        assert sink.analytics_enabled() is False  # not started
        sink.start()
        for _ in range(100):
            if sink._flags is not None:
                break
            await asyncio.sleep(0.005)
        enabled = sink.analytics_enabled()
        await sink.stop(drain_s=0.2)
        return enabled, sink.analytics_enabled()

    assert asyncio.run(run()) == (True, False)
    assert cc.CaptureSink(None).analytics_enabled() is False


def test_query_hash_is_a_keyed_hmac_with_its_own_domain(key):
    got = idisc.query_hash("What's the weather ")
    expected = hmac.new(key.encode(), b"athena:intent-metric:query-hash:v1:" + b"what's the weather", hashlib.sha256).hexdigest()
    assert got == expected and len(got) == 64
    assert got != hashlib.md5(b"what's the weather").hexdigest()
    assert idisc.query_hash("what's the weather") == got, "stable per phrasing, so identical phrasings still group"


def test_query_hash_changes_with_the_key(key, monkeypatch):
    from shared import config as shared_config

    first = idisc.query_hash("turn on the light")
    monkeypatch.setenv("SERVICE_API_KEY", "another-key")
    shared_config._clear_cache_for_tests()
    assert idisc.query_hash("turn on the light") != first


def test_query_hash_is_none_for_a_pin_shaped_query_or_without_a_key(key, monkeypatch):
    from shared import config as shared_config

    assert idisc.query_hash("switch to owner mode") is None
    assert idisc.query_hash("pin one two three four five six") is None
    monkeypatch.setenv("SERVICE_API_KEY", "")
    shared_config._clear_cache_for_tests()
    assert idisc.query_hash("turn on the light") is None


class FakeResponse:
    status_code = 200
    text = ""

    def json(self):
        return {"id": 1}


def _capture_post(monkeypatch):
    posted = []

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            posted.append((url, json))
            return FakeResponse()

    monkeypatch.setattr(idisc.httpx, "AsyncClient", Client)
    return posted


def test_the_metric_payload_has_a_keyed_hash_and_no_text(key, monkeypatch):
    posted = _capture_post(monkeypatch)
    asyncio.run(idisc.record_intent_metric("control", 0.9, "turn on the porch light", "s", "owner", "porch", "r", 5, admin_api_url="http://a"))
    asyncio.run(idisc.record_intent_metric("control", 0.9, "pin 123456", "s", "owner", "porch", "r", 5, admin_api_url="http://a"))
    ordinary, pin = posted[0][1], posted[1][1]
    assert "raw_query" not in ordinary and ordinary["query_len"] == len("turn on the porch light")
    assert ordinary["query_hash"] == idisc.query_hash("turn on the porch light")
    assert pin["query_hash"] is None and "pin 123456" not in str(pin)


def test_emerging_intent_calls_send_no_sample_when_there_is_none(monkeypatch):
    posted = _capture_post(monkeypatch)
    asyncio.run(idisc.create_emerging_intent("n", "N", "d", [0.1], "other", [], None, "http://a"))
    asyncio.run(idisc.create_emerging_intent("n", "N", "d", [0.1], "other", [], "weather in [number]", "http://a"))
    asyncio.run(idisc.increment_intent_count(3, None, "http://a"))
    asyncio.run(idisc.increment_intent_count(3, "a sample", "http://a"))
    assert posted[0][1]["sample_queries"] == [] and posted[1][1]["sample_queries"] == ["weather in [number]"]
    assert posted[2][1] == {} and posted[3][1] == {"sample_query": "a sample"}


# ---------------------------------------------------------------------------
# Generated labels: withheld turns get no intent; labels are redacted and capped
# ---------------------------------------------------------------------------

class SpyEmbedder:
    def __init__(self):
        self.inputs = []

    def embed_intent(self, name, description):
        self.inputs.append((name, description))
        return [0.1, 0.2]


def _wire_discovery(monkeypatch, novel):
    calls = {"generate": 0, "created": []}

    async def generate(query, llm):
        calls["generate"] += 1
        return novel

    async def embedder():
        calls["embedder"] = SpyEmbedder()
        return calls["embedder"]

    async def similar(embedding, url, threshold=0.8):
        return None

    async def create(**kwargs):
        calls["created"].append(kwargs)
        return 7

    monkeypatch.setattr(idisc, "generate_novel_intent", generate)
    monkeypatch.setattr(idisc, "get_embedder", embedder)
    monkeypatch.setattr(idisc, "find_similar_emerging_intent", similar)
    monkeypatch.setattr(idisc, "create_emerging_intent", create)
    return calls


@pytest.mark.parametrize("query", ["switch to owner mode", "pin one two three four five six", "my code is 123456"])
def test_a_withheld_turn_never_reaches_the_model_and_creates_no_intent(monkeypatch, query):
    calls = _wire_discovery(monkeypatch, {"canonical_name": "x", "display_name": "X", "description": "d"})
    result = asyncio.run(idisc.discover_intent(query, "unknown", 0.1, object(), "http://a"))
    assert result.is_novel is False and result.emerging_intent_id is None
    assert calls["generate"] == 0 and calls["created"] == []


def test_analytics_mode_off_does_not_stop_discovery_but_sends_no_sample(monkeypatch):
    novel = {"canonical_name": "garage_plan", "display_name": "Garage plan for 20261", "description": "wants 5551234567 " + "x" * 600}
    calls = _wire_discovery(monkeypatch, novel)
    _runtime.set_turn_capture(FakeSink(False))
    result = asyncio.run(idisc.discover_intent("tell me about the garage plan", "unknown", 0.1, object(), "http://a"))
    assert result.is_novel and result.emerging_intent_id == 7 and calls["generate"] == 1
    created = calls["created"][0]
    assert created["sample_query"] is None
    assert created["display_name"] == "Garage plan for [number]"
    assert "[number]" in created["description"] and "5551234567" not in created["description"]
    assert len(created["description"]) <= idisc.LABEL_MAX_CHARS["description"]


def test_the_embedding_input_uses_the_redacted_description(monkeypatch):
    calls = _wire_discovery(monkeypatch, {"canonical_name": "call_back", "display_name": "Call back", "description": "call 5551234567 later"})
    asyncio.run(idisc.discover_intent("please call me back", "unknown", 0.1, object(), "http://a"))
    assert calls["embedder"].inputs == [("call_back", "call [number] later")]


def test_labels_are_capped():
    assert len(idisc._clean_label("a" * 900, 100)) == 100
    assert idisc._clean_label(None, 100) == ""
