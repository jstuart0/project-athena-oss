"""ATHENA-89 Phase 3 — orchestrator-side F97/D11/D13 assertions.

Covers plan/contract O1-O9 (O10-O13 amend test_openai_session_key.py
instead, per the plan's own file assignment). Same orchestrator-import
harness as tests/unit/test_openai_session_key.py: stub heavy/absent deps
before the first orchestrator.* import, then use a real in-memory
SessionManager + TestClient(orchestrator.main.app) for the two tests (O1,
O2) that need a full request round-trip.
"""
from __future__ import annotations

import ast
import inspect
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, "src")

for _mod in ("langgraph", "langgraph.graph", "prometheus_client"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()
os.environ.setdefault("SERVICE_API_KEY", "test-key-oss-readiness-orchestrator")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")

from shared.config import get_config as _shared_get_config  # noqa: E402
import shared.config as _shared_config  # noqa: E402

_config_loader_mock = mock.MagicMock()
_config_loader_mock.get_config = _shared_get_config
_config_loader_mock.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
_config_loader_mock.get_feature_flag = mock.AsyncMock(return_value=False)
_config_loader_mock.get_feature_flags = mock.AsyncMock(return_value={})
_config_loader_mock.clear_cache = mock.AsyncMock()
sys.modules.setdefault("orchestrator.config_loader", _config_loader_mock)

import orchestrator.nodes  # noqa: E402,F401
from orchestrator.helpers import (  # noqa: E402
    log_continuation_decision,
    query_mentions_location,
    city_phrases,
)
import orchestrator.main as _main_module  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.session_manager import SessionManager  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import orchestrator.semantic_cache as semantic_cache  # noqa: E402
from orchestrator.search_providers.provider_router import ProviderRouter  # noqa: E402
from orchestrator.search_providers.eventbrite import EventbriteProvider  # noqa: E402
from orchestrator.search_providers.ticketmaster import TicketmasterProvider  # noqa: E402
from orchestrator.search_providers.parallel_search import ParallelSearchEngine  # noqa: E402
import structlog.testing  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "src" / "orchestrator" / "main.py"

_SERVICE_HEADERS = {"X-Service-Key": os.environ["SERVICE_API_KEY"]}


class _FakeSessionCacheClient:
    def __init__(self):
        self.client = SimpleNamespace(
            delete=mock.AsyncMock(),
            get=mock.AsyncMock(return_value=None),
            setex=mock.AsyncMock(),
        )


# ---------------------------------------------------------------------------
# O1: non-stream branch forwards room and temperature (3.7)
# ---------------------------------------------------------------------------


def test_O1_nonstream_branch_forwards_room_and_temperature():
    sm = SessionManager()
    sm.redis_client = None
    _runtime.set_session_manager(sm)
    _runtime.set_cache_client(_FakeSessionCacheClient())

    captured = []

    async def _fake_process_query(query_request):
        captured.append(query_request)
        return SimpleNamespace(request_id="req-fake", answer="ok")

    original = _main_module.process_query
    _main_module.process_query = _fake_process_query
    try:
        client = TestClient(_main_module.app)

        resp_with_room = client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "what's on my calendar"}],
                "stream": False,
                "room": "kitchen",
                "temperature": 0.3,
            },
            headers=_SERVICE_HEADERS,
        )
        resp_without_room = client.post(
            "/v1/chat/completions",
            json={
                "model": "m",
                "messages": [{"role": "user", "content": "different opener text"}],
                "stream": False,
            },
            headers=_SERVICE_HEADERS,
        )
    finally:
        _main_module.process_query = original

    assert resp_with_room.status_code == 200
    assert resp_without_room.status_code == 200
    assert len(captured) == 2
    assert captured[0].room == "kitchen"
    assert captured[0].temperature == 0.3
    assert captured[1].room == "unknown"


# ---------------------------------------------------------------------------
# O2: the one genuine cross-component test -- gateway's real payload builder
# feeding the orchestrator's real /v1/chat/completions, same session_id
# ---------------------------------------------------------------------------


def test_O2_gateway_payload_builder_resolves_same_session_id_in_orchestrator():
    sys.modules.setdefault("prometheus_client", mock.MagicMock())
    os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
    import gateway.main as gw
    from shared.output_channel import OutputChannel

    sm = SessionManager()
    sm.redis_client = None
    _runtime.set_session_manager(sm)
    _runtime.set_cache_client(_FakeSessionCacheClient())

    captured = []

    async def _fake_process_query(query_request):
        captured.append(query_request)
        return SimpleNamespace(request_id="req-fake", answer="ok")

    original = _main_module.process_query
    _main_module.process_query = _fake_process_query
    try:
        client = TestClient(_main_module.app)

        gw_request = gw.ChatCompletionRequest(
            model="m",
            messages=[gw.ChatMessage(role="system", content="s"), gw.ChatMessage(role="user", content="u1")],
            stream=False,
            user="alice",
        )
        turn1_payload = gw._orchestrator_openai_payload(gw_request, device_id="kitchen", stream=False, channel=OutputChannel.TEXT)
        resp1 = client.post("/v1/chat/completions", json=turn1_payload, headers=_SERVICE_HEADERS)

        gw_request2 = gw.ChatCompletionRequest(
            model="m",
            messages=[
                gw.ChatMessage(role="system", content="s"),
                gw.ChatMessage(role="user", content="u1"),
                gw.ChatMessage(role="assistant", content="a1"),
                gw.ChatMessage(role="user", content="u2"),
            ],
            stream=False,
            user="alice",
        )
        turn2_payload = gw._orchestrator_openai_payload(gw_request2, device_id="office", stream=False, channel=OutputChannel.TEXT)
        resp2 = client.post("/v1/chat/completions", json=turn2_payload, headers=_SERVICE_HEADERS)

        gw_request3 = gw.ChatCompletionRequest(
            model="m",
            messages=[gw.ChatMessage(role="system", content="s"), gw.ChatMessage(role="user", content="u1")],
            stream=False,
            user="bob",
        )
        turn3_payload = gw._orchestrator_openai_payload(gw_request3, device_id="kitchen", stream=False, channel=OutputChannel.TEXT)
        resp3 = client.post("/v1/chat/completions", json=turn3_payload, headers=_SERVICE_HEADERS)
    finally:
        _main_module.process_query = original

    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp3.status_code == 200
    assert len(captured) == 3

    session_id_1 = captured[0].session_id
    session_id_2 = captured[1].session_id
    session_id_3 = captured[2].session_id

    assert re.match(r"^oai-[0-9a-f]{32}$", session_id_1)
    assert session_id_1 == session_id_2  # same user, different room -> same id (D11)
    assert session_id_1 != session_id_3  # different user -> different id


# ---------------------------------------------------------------------------
# O3 / O3b: log_continuation_decision
# ---------------------------------------------------------------------------


def test_O3_logs_continuation_decision_with_expected_fields(monkeypatch):
    # Patch the module logger directly rather than structlog.testing.capture_logs():
    # this test file imports both orchestrator.helpers and gateway.main, and
    # this codebase's configure_logging() rebinds a process-wide "service"
    # context on each call, which makes capture_logs()'s scoping unreliable
    # once a second service module has been imported in the same process.
    import orchestrator.helpers as helpers_module

    calls = []
    monkeypatch.setattr(helpers_module.logger, "info", lambda event, **kw: calls.append({"event": event, **kw}))

    state = SimpleNamespace(continuation_decision={"decision": "declined", "reason": "strong_intent"})
    log_continuation_decision(state, "oai-abcdef0123456789")

    events = [e for e in calls if e["event"] == "continuation_decision"]
    assert len(events) == 1
    assert events[0]["decision"] == "declined"
    assert events[0]["reason"] == "strong_intent"
    assert events[0]["session_prefix"] == "oai-abcdef0123456789"[:12]


def test_O3_empty_dict_does_not_raise():
    with structlog.testing.capture_logs():
        log_continuation_decision({}, "sess")
        log_continuation_decision(None, "sess")


def test_O3b_all_four_graph_run_sites_followed_by_log_call():
    source = MAIN_PY.read_text()
    for anchor in (
        "final_state = await orchestrator_graph.ainvoke(initial_state)",
        "state = await run_orchestrator_for_streaming(initial_state)",
    ):
        occurrences = [m.start() for m in re.finditer(re.escape(anchor), source)]
        assert len(occurrences) == 2, f"expected 2 occurrences of {anchor!r}, found {len(occurrences)}"
        for idx in occurrences:
            following = source[idx: idx + len(anchor) + 200]
            assert "log_continuation_decision(" in following, (
                f"no log_continuation_decision( call found shortly after: {anchor!r}"
            )


# ---------------------------------------------------------------------------
# O4: query_mentions_location
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,location,expected",
    [
        ("what's the weather in Denver today", "Denver, CO", True),
        ("what's on my calendar", "Denver, CO", False),
        ("run the cmd line tool", "MD", False),  # word-boundary, not substring
        ("anything", "", False),
        ("", "Denver", False),
    ],
)
def test_O4_query_mentions_location(query, location, expected):
    assert query_mentions_location(query, location) is expected


# ---------------------------------------------------------------------------
# O5: city_phrases
# ---------------------------------------------------------------------------


def test_O5_city_phrases_empty_city_gives_empty_list():
    assert city_phrases("", ["represent {c}", "{c} experience"]) == []


def test_O5_city_phrases_nonempty_city_lowercased():
    assert city_phrases("Denver", ["represent {c}", "{c} experience"]) == [
        "represent denver",
        "denver experience",
    ]


# ---------------------------------------------------------------------------
# O6: semantic_cache._location_aliases / normalize_location
# ---------------------------------------------------------------------------


def test_O6_location_aliases_empty_city():
    assert semantic_cache._location_aliases("") == {}


def test_O6_location_aliases_denver():
    assert semantic_cache._location_aliases("Denver") == {"denver": "default_denver"}


def test_O6_normalize_location_word_boundary_retires_cmd_bug(monkeypatch):
    # Old bug: the alias loop used a bare substring check ("md" in
    # text_lower), so "cmd" (containing "md") false-matched the alias
    # BEFORE the LOCATION_INDICATORS loop ever ran, discarding an explicitly
    # mentioned different location. query_mentions_location's word-boundary
    # match retires this: "cmd" no longer satisfies the "md" alias, so
    # normalize_location falls through to the explicit "in Denver" mention.
    monkeypatch.setattr(semantic_cache, "DEFAULT_CITY", "MD")
    result = semantic_cache.normalize_location("in Denver, run the cmd line tool")
    assert result == "denver"


# ---------------------------------------------------------------------------
# O7 / O9: search providers default location=None, omit param entirely
# ---------------------------------------------------------------------------


_LOCATION_PROVIDERS = [EventbriteProvider, TicketmasterProvider, ParallelSearchEngine]


def test_O7_population_is_3():
    assert len(_LOCATION_PROVIDERS) == 3


@pytest.mark.parametrize("provider_cls", _LOCATION_PROVIDERS)
def test_O7_search_signature_location_default_is_none(provider_cls):
    sig = inspect.signature(provider_cls.search)
    assert sig.parameters["location"].default is None


def test_O9_eventbrite_omits_location_param_when_none():
    provider = EventbriteProvider(api_key="k")
    captured = {}

    async def _fake_get(url, params=None, **kwargs):
        captured["params"] = params
        resp = mock.MagicMock()
        resp.raise_for_status = mock.MagicMock()
        resp.json.return_value = {"events": []}
        return resp

    provider.client = mock.MagicMock(get=_fake_get)
    import asyncio
    asyncio.run(provider.search("concerts", location=None))
    assert "location.address" not in captured["params"]


def test_O9_ticketmaster_omits_location_param_when_none():
    provider = TicketmasterProvider(api_key="k")
    captured = {}

    async def _fake_get(url, params=None, **kwargs):
        captured["params"] = params
        resp = mock.MagicMock()
        resp.raise_for_status = mock.MagicMock()
        resp.json.return_value = {"_embedded": {"events": []}}
        return resp

    provider.client = mock.MagicMock(get=_fake_get)
    import asyncio
    asyncio.run(provider.search("concerts", location=None))
    assert "city" not in captured["params"]


def test_O9_parallel_search_engine_search_default_location_is_none():
    """ParallelSearchEngine.search's own location parameter also defaults to
    None (O7's population includes this class) -- forwarding to each
    provider's .search(location=...) is proven per-provider above."""
    sig = inspect.signature(ParallelSearchEngine.search)
    assert sig.parameters["location"].default is None


# ---------------------------------------------------------------------------
# O8: ProviderRouter registers SearXNG only when base_url is set
# ---------------------------------------------------------------------------


def test_O8_searxng_not_registered_when_empty(caplog):
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="orchestrator.search_providers.provider_router"):
        router = ProviderRouter(
            enable_ticketmaster=False,
            enable_eventbrite=False,
            enable_brave=False,
            enable_duckduckgo=False,
            enable_searxng=True,
            searxng_base_url="",
        )
    assert "searxng" not in router.all_providers
    assert any("searxng_not_configured" in rec.message for rec in caplog.records)


def test_O8_searxng_registered_when_set():
    router = ProviderRouter(
        enable_ticketmaster=False,
        enable_eventbrite=False,
        enable_brave=False,
        enable_duckduckgo=False,
        enable_searxng=True,
        searxng_base_url="https://searxng.example.com",
    )
    assert "searxng" in router.all_providers
    assert router.all_providers["searxng"].base_url == "https://searxng.example.com"
