"""An unknown speaker's identity question, black-box through every query route.

Unlike the fast-path rig, `classify_node`, `route_after_classify`, the graph
and the streaming runner are the REAL ones. (langgraph is stubbed in unit tests, so the graph here is a three-step driver over
the real nodes and the real router function: classify, `route_after_classify`,
then whatever node it names, which must be `finalize`.) The LLM router raises on any call
and the semantic cache is a spy, so a passing answer proves the fixed reply,
zero model calls, no cache read and no cache write. Synthetic names only.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import httpx
import pytest

from . import _fast_path_harness as fp
from . import _public_audience_harness as h
from orchestrator import identity_answer as ia

OWNER = "Olive Owner"
ADDRESS = "42 Example Lane, Sampletown, ST 00000"
ROWS = [
    {"id": 1, "category": "owner", "key": "owner_name", "value": OWNER, "applies_to": "both"},
    {"id": 2, "category": "property", "key": "address", "value": ADDRESS, "applies_to": "both"},
]
PLANTED = "PLANTED-CACHE-ANSWER"


class RealNodeGraph:
    def __init__(self):
        self.routed = []

    async def ainvoke(self, state):
        from orchestrator.nodes import finalize_node

        state = await h.main.classify_node(state)
        destination = await h.main.route_after_classify(state)
        self.routed.append(destination)
        assert destination == "finalize", f"identity turn routed to {destination}"
        return (await finalize_node(state)).model_dump()


class Rig:
    def __init__(self, monkeypatch):
        self.llm = fp.RaisingLLM()
        self.read = mock.AsyncMock(return_value={
            "answer": PLANTED, "intent": "general_info", "confidence": 0.9, "citations": [], "request_id": "r", "processing_time": 0.0,
        })
        self.write = mock.AsyncMock()
        self.graph = RealNodeGraph()
        h.reset_runtime()
        h.patch_conversation_config(monkeypatch)
        h.install_mode_client(server_mode="owner")
        admin = h.fake_admin_client()
        admin.get_base_knowledge = mock.AsyncMock(return_value=[dict(r) for r in ROWS])
        monkeypatch.setattr(h.main, "get_admin_client", lambda: admin)
        h._runtime.set_cache_client(fp.FakeCache())
        h._runtime.set_llm_router(self.llm)
        monkeypatch.setattr(h.main, "record_intent_metric", mock.AsyncMock(return_value=True))
        monkeypatch.setattr(h.main, "get_cached_response", self.read)
        monkeypatch.setattr(h.main, "cache_response", self.write)
        monkeypatch.setattr(h.main, "orchestrator_graph", self.graph)
        from shared import assistant_profile

        monkeypatch.setattr(assistant_profile, "get_assistant_profile",
                            mock.AsyncMock(return_value=dict(assistant_profile.DEFAULT_ASSISTANT_PROFILE)))
        from orchestrator import helpers

        helpers._assistant_names_cache.update(names=(), expires_at=0.0)
        monkeypatch.setattr(helpers, "get_assistant_profile",
                            mock.AsyncMock(return_value={"assistant_name": "Jarvis"}))


async def _send(route: str, text: str, *, caller_trust: str = "household") -> str:
    path, body = fp.request_for(route, text, caller_trust=caller_trust)
    body.pop("skip_semantic_cache", None)
    transport = httpx.ASGITransport(app=h.main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(path, json=body, headers=h.service_headers())
    answer = fp.answer_of(route, resp)
    await h._runtime.drain_background()
    return answer


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


@pytest.mark.parametrize("route", fp.ROUTES)
@pytest.mark.parametrize(
    "query,reply",
    [
        ("What is my name?", ia.NAME_REPLY), ("where do I live", ia.HOME_REPLY), ("Jarvis, say my name.", ia.NAME_REPLY),
        ("do you know my address", ia.HOME_REPLY), ("and my address?", ia.HOME_REPLY),
        ("please jarvis what's my name", ia.NAME_REPLY), ("is my name Sam?", ia.NAME_REPLY),
    ],
)
def test_unknown_speaker_gets_the_fixed_reply(monkeypatch, route, query, reply):
    rig = Rig(monkeypatch)
    answer = asyncio.run(_send(route, query))
    assert answer == reply
    assert OWNER.split()[0] not in answer and "42" not in answer
    assert rig.llm.calls == []
    rig.read.assert_not_awaited()
    rig.write.assert_not_awaited()


@pytest.mark.parametrize("route", ["query", "v1_nonstream"])
def test_a_planted_cache_entry_for_the_query_is_not_served(monkeypatch, route):
    rig = Rig(monkeypatch)
    answer = asyncio.run(_send(route, "who am I"))
    assert answer == ia.NAME_REPLY
    assert answer != PLANTED
    rig.read.assert_not_awaited()


def test_positive_control_a_normal_query_is_not_routed_to_the_identity_reply(monkeypatch):
    """A first-person question about something else is not an identity question:
    the planted cache entry is read (so the plant can be served) and returned."""
    rig = Rig(monkeypatch)
    answer = asyncio.run(_send("query", "tell me something interesting about owls"))
    assert answer == PLANTED
    rig.read.assert_awaited_once()
    assert rig.graph.routed == []


@pytest.mark.parametrize("query", ["what is the address here", "who lives here", "who's the owner"])
def test_house_questions_do_not_take_the_identity_path(monkeypatch, query):
    """D11: house questions follow the tiers. The planted cache entry is read
    for them, which is only possible when the identity skip did not fire."""
    rig = Rig(monkeypatch)
    answer = asyncio.run(_send("query", query))
    assert answer == PLANTED
    rig.read.assert_awaited_once()


def test_a_deployment_vocative_still_skips_the_cache_and_answers_fixed(monkeypatch):
    """The skip uses the configured names too: a name the default list doesn't
    know ("Friday, what's my name") neither reads a planted entry nor writes."""
    rig = Rig(monkeypatch)
    from orchestrator import helpers

    monkeypatch.setattr(helpers, "get_assistant_profile",
                        mock.AsyncMock(return_value={"assistant_name": "Friday"}))
    assert ia.identity_question("Friday, what's my name?") is None
    answer = asyncio.run(_send("query", "Friday, what's my name?"))
    assert answer == ia.NAME_REPLY
    rig.read.assert_not_awaited()
    rig.write.assert_not_awaited()


class _FlaggedGraph:
    """A graph whose turn ends with the backstop's replacement (flag set) for a
    query the identity vocabulary does not match."""

    def __init__(self, flagged: bool):
        self.flagged = flagged

    async def ainvoke(self, state):
        return {
            "intent": h.IntentCategory.GENERAL_INFO, "answer": "Sure, a reply.", "confidence": 0.9, "citations": [],
            "request_id": "r", "node_timings": {}, "validation_passed": True, "identity_answer": self.flagged,
        }


@pytest.mark.parametrize("flagged,written", [(True, False), (False, True)])
def test_a_backstopped_answer_is_never_cached(monkeypatch, flagged, written):
    """The flag alone stops the write; the unflagged turn is the positive control."""
    rig = Rig(monkeypatch)
    monkeypatch.setattr(h.main, "orchestrator_graph", _FlaggedGraph(flagged))
    rig.read.return_value = None
    answer = asyncio.run(_send("query", "who do you think I am"))
    assert answer == "Sure, a reply."
    rig.read.assert_awaited_once()
    assert (rig.write.await_count == 1) is written


def test_the_streaming_runner_stops_after_the_identity_reply(monkeypatch):
    """Without the early return the runner would go on to tool selection."""
    Rig(monkeypatch)
    reached = []

    async def _tool_call(state):
        reached.append("tool_call_node")
        raise AssertionError("tool_call_node reached after the identity reply")

    monkeypatch.setattr(h.main, "should_use_tool_calling", mock.AsyncMock(return_value=True))
    monkeypatch.setattr(h.main, "tool_call_node", _tool_call)
    state = h.make_state(
        permissions={"audience": "household", "mode": "owner"}, mode="owner", intent=None, query="who am I",
    )
    result = asyncio.run(h.main.run_orchestrator_for_streaming(state))
    assert result.answer == ia.NAME_REPLY and result.identity_answer is True and reached == []


@pytest.mark.parametrize("query,retrieved", [
    ("what's my name and where do I live", False),
    ("remind me what address I gave you", False),
    ("what do you remember about my cats", True),
])
def test_an_identity_turn_for_an_unknown_speaker_retrieves_no_memory_and_skips_the_cache(monkeypatch, query, retrieved):
    """H2: neither pattern is a fixed-reply identity question, but a stored memory such as
    "the user's address is ..." must not reach the prompt, and the answer is never cached.
    The cats question is the positive control: the same plumbing does retrieve and read the cache."""
    rig = Rig(monkeypatch)
    rig.read.return_value = None
    monkeypatch.setattr(h.main, "orchestrator_graph", _FlaggedGraph(False))
    manager = mock.MagicMock()
    manager.get_relevant_memories = mock.AsyncMock(return_value=[])
    monkeypatch.setattr(h.main, "get_memory_manager", mock.AsyncMock(return_value=manager))
    assert ia.identity_question(query) is None
    asyncio.run(_send("query", query))
    assert (manager.get_relevant_memories.await_count == 1) is retrieved
    assert (rig.read.await_count == 1) is retrieved
