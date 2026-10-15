"""The model-call and Redis budget of a turn, on the real graph and the real streaming runner.

Each scenario runs in a fresh interpreter with the real langgraph (other unit
modules stub it in-process): the real ``/query`` and ``/query/stream`` routes,
the real compiled graph (``/query``) and the real ``run_orchestrator_for_streaming``
(``/query/stream``), the real nodes, and only the edges of the world stubbed:

* a model router that counts every call by method and by the ``stage`` the
  caller names (``classify``, ``tool_selection``, ``tool_synthesis``,
  ``validation``, ...), and answers with canned text;
* a tool executor that counts what it runs;
* a Redis whose operations are counted by name.

The budgets:

* the fast path, an identity answer and a decline make 0 model calls;
* a fragment answer makes 0 answer-producing calls (the intent classifier has
  already run to know it is a fragment: one ``classify`` call, a finding below);
* a grounded forced-tool turn makes 1 classification, at most 1 tool
  selection, 1 tool run, 1 synthesis and 0 validation calls;
* an ordinary turn makes no Redis round trip that the base did not, except the
  ones named in ``DECLARED_EXTRA_REDIS``.

``BASE_REDIS`` is the count the same scenario made at the campaign base
(e9a1b0b), measured with this driver (``measure_base`` below).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ("query", "query_stream")
ANSWER_STAGES = {"tool_selection", "tool_synthesis", "synthesize", "stream_synthesis", "validation", "service_bypass"}
AMBIENT_REPLY = "Sorry, I didn't catch that."

SCENARIOS = {
    # name: (query, what the stub classifier says, conversation seed, ambient gate on)
    "fast_thanks": ("thanks", "dining", False, False),
    "fast_time": ("what time is it", "dining", False, False),
    "identity_name": ("what is my name", "dining", False, False),
    "identity_home": ("where do I live", "dining", False, False),
    "decline": ("no thanks", "dining", True, False),
    "fragment": ("mm hm uh", "unknown", False, True),
    "dining": ("recommend an italian place near me", "dining", False, False),
    "general": ("tell me a fun fact about owls", "general_info", False, False),
    "validate_control": ("what time is the game", "dining", False, False),
}

DRIVER = r'''
import asyncio, json, os, sys
from collections import Counter
from unittest import mock

ROOT = os.environ["P12_ROOT"]
sys.path[:0] = [os.path.join(ROOT, "src", "orchestrator"), os.path.join(ROOT, "src"), os.path.join(ROOT, "tests")]
os.environ.setdefault("SERVICE_API_KEY", "test-key-latency-budget")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
import langgraph.graph as _langgraph
assert isinstance(_langgraph.StateGraph, type), "the real langgraph is required"
from _pytest.monkeypatch import MonkeyPatch
from unit import _public_audience_harness as h
from unit import _fast_path_harness as fp

name, route, query, classify, seed, gate = sys.argv[1:7]
mp = MonkeyPatch()
CLASSIFIED = {
    "dining": '{"intent":"dining","confidence":0.9,"complexity":"simple","entities":{}}',
    "general_info": '{"intent":"general_info","confidence":0.9,"complexity":"simple","entities":{}}',
    "unknown": '{"intent":"unknown","confidence":0.1,"complexity":"simple","entities":{}}',
}
COUNTED = ("get", "setex", "set", "delete", "incr", "expire", "lpush", "rpush", "ltrim", "lrange", "hset", "hget",
           "hgetall", "exists", "eval", "evalsha", "scan", "keys", "mget", "pipeline", "ttl")


class CountingRedis(fp.FakeRedis):
    def __init__(self):
        super().__init__()
        self.ops = Counter()

    def __getattribute__(self, attr):
        if attr in COUNTED:
            object.__getattribute__(self, "ops")[attr] += 1
        return super().__getattribute__(attr)


class Cache:
    def __init__(self):
        self.client = CountingRedis()


# Specifics (a time and a price) so a validator that did not see the tool data would fact-check them.
DETAILED = "Dinner for two is about $40 and the kitchen seats until 9:30 PM tonight."
PLAIN = "Here are some nearby options."


class Router:
    def __init__(self):
        self.calls = []
        self.text = DETAILED if name == "dining" else PLAIN

    def _note(self, method, kwargs):
        caller = sys._getframe(2)
        self.calls.append({"method": method, "stage": kwargs.get("stage"), "caller": caller.f_code.co_name})

    async def generate(self, *args, **kwargs):
        self._note("generate", kwargs)
        reply = CLASSIFIED[classify] if kwargs.get("stage") == "classify" else '{"contains_hallucinations": false, "reason": "", "specific_claims": []}' if kwargs.get("stage") == "validation" else self.text
        return {"response": reply, "eval_count": 1, "prompt_eval_count": 1}

    async def generate_with_tools(self, *args, **kwargs):
        self._note("generate_with_tools", kwargs)
        return {"content": self.text, "eval_count": 1, "prompt_eval_count": 1}

    async def generate_stream(self, *args, **kwargs):
        self._note("generate_stream", kwargs)
        yield {"token": self.text}
        yield {"token": "", "done": True}

    def __getattr__(self, attr):
        if attr.startswith("_"):
            raise AttributeError(attr)

        async def other(*args, **kwargs):
            self._note(attr, kwargs)
            return {"response": "x", "eval_count": 1}

        return other


async def run():
    h.reset_runtime()
    h.patch_conversation_config(mp)
    h.install_mode_client(server_mode="owner")
    _admin, execute, _knowledge, _home = h.patch_tool_call_dependencies(mp)
    from orchestrator import helpers

    mp.setattr(h.main, "store_conversation_context", helpers.store_conversation_context)
    cache = Cache()
    h._runtime.set_cache_client(cache)
    router = Router()
    h._runtime.set_llm_router(router)
    mp.setattr(h.main, "record_intent_metric", mock.AsyncMock(return_value=True))
    mp.setattr(h.main, "orchestrator_graph", h.main.create_orchestrator_graph())
    if gate == "1":
        import orchestrator.fast_path as fast_path

        async def guardrails():
            return {"voice_response": {"ambient_fragment_gate": True}}

        mp.setattr(fast_path, "get_guardrails", guardrails)
    if seed == "1":
        offer = "I found three places. Want me to look for outdoor seating?"
        sid = fp.session_id_for(route)
        sm = h._runtime.get_session_manager()
        await sm.get_or_create_session(session_id=sid, caller_class="other")
        await sm.add_message(sid, "user", "Any good Italian places nearby?")
        await sm.add_message(sid, "assistant", offer)
        await helpers.store_conversation_context(
            session_id=sid, intent="dining", query="Any good Italian places nearby?", entities={},
            parameters={"grounded": True}, response=offer,
        )
        cache.client.ops.clear()
    if name == "validate_control":
        from orchestrator.nodes import validate_node
        from orchestrator.state import IntentCategory

        state = h.make_state(permissions=h.mode_permission.normalize_permissions({"mode": "owner"}), mode="owner",
                             intent=IntentCategory.SPORTS, query=query)
        state.answer = "The game starts at 7:30 PM tonight."
        guardrails = {"min_response_chars": 10, "max_response_chars": 2000}
        with mock.patch("orchestrator.nodes.validate.get_validation_guardrails", mock.AsyncMock(return_value=guardrails)), \
                mock.patch("orchestrator.nodes.validate.get_component_config",
                           mock.AsyncMock(return_value={"model_name": "m", "backend_type": "ollama"})):
            await validate_node(state)
        answer = state.answer
    else:
        response = await fp.send(route, query)
        answer = fp.answer_of(route, response)
        await h._runtime.drain_background()
    print("RESULT=" + json.dumps({"answer": answer, "calls": router.calls, "redis": dict(cache.client.ops),
                                  "tools": execute.await_count}))


asyncio.run(run())
'''


def run_scenario(root: Path, name: str, route: str) -> dict:
    query, classify, seed, gate = SCENARIOS[name]
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, name, route, query, classify, "1" if seed else "0", "1" if gate else "0"],
        capture_output=True, text=True, timeout=240, cwd=str(root),
        env={**os.environ, "P12_ROOT": str(root), "PYTHONPATH": ""},
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT=")]
    assert proc.returncode == 0 and lines, (name, route, proc.stdout[-1500:], proc.stderr[-2500:])
    return json.loads(lines[-1][len("RESULT="):])


@pytest.fixture(scope="module")
def turns() -> dict:
    wanted = [(name, route) for name in SCENARIOS for route in ROUTES if name != "validate_control" or route == "query"]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda key: run_scenario(ROOT, *key), wanted))
    return dict(zip(wanted, results))


def stages(turn: dict) -> list:
    return [call["stage"] for call in turn["calls"]]


# --- no model call -------------------------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("name,answer", [
    ("fast_thanks", "You're welcome."),
    ("identity_name", "I can't tell who's speaking, so I don't know your name."),
    ("decline", "Okay."),
])
def test_the_fast_path_an_identity_answer_and_a_decline_make_no_model_call(turns, route, name, answer):
    turn = turns[(name, route)]
    assert turn["answer"] == answer
    assert turn["calls"] == [] and turn["tools"] == 0


@pytest.mark.parametrize("route", ROUTES)
def test_the_time_and_a_home_question_make_no_model_call_either(turns, route):
    for name in ("fast_time", "identity_home"):
        turn = turns[(name, route)]
        assert turn["answer"] and turn["calls"] == [] and turn["tools"] == 0, name


@pytest.mark.parametrize("route", ROUTES)
def test_a_fragment_is_answered_without_an_answer_producing_call(turns, route):
    turn = turns[("fragment", route)]
    assert turn["answer"] == AMBIENT_REPLY and turn["tools"] == 0
    assert not ANSWER_STAGES & set(stages(turn))
    # The finding: the intent classifier runs first (it is how the turn is known to be a
    # fragment), so the turn costs exactly one model call, not none.
    assert stages(turn) == ["classify"]


@pytest.mark.xfail(strict=True, reason="finding: a fragment is classified by one model call before the gate answers it")
@pytest.mark.parametrize("route", ROUTES)
def test_a_fragment_answer_makes_no_model_call_at_all(turns, route):
    assert turns[("fragment", route)]["calls"] == []


# --- the grounded forced-tool turn -----------------------------------------------------------------------

@pytest.mark.parametrize("route", ROUTES)
def test_a_grounded_forced_tool_turn_stays_inside_its_budget(turns, route):
    turn = turns[("dining", route)]
    seen = stages(turn)
    assert turn["answer"] and turn["tools"] == 1                                  # the tool really ran, once
    assert seen.count("classify") == 1
    assert seen.count("tool_selection") <= 1
    assert seen.count("tool_synthesis") == 1
    assert seen.count("validation") == 0
    assert sorted(seen) == ["classify", "tool_selection", "tool_synthesis"], seen  # nothing else asked a model


def test_the_counter_sees_a_validation_call_when_one_is_made(turns):
    """Positive control: the same stub, a state validate_node must fact-check."""
    assert "validation" in stages(turns[("validate_control", "query")])


# --- Redis -----------------------------------------------------------------------------------------------

# The operations each scenario made at the campaign base (e9a1b0b), measured with run_scenario
# against a checkout of that commit.
BASE_REDIS = {
    ("fast_thanks", "query"): {"get": 1},
    ("fast_thanks", "query_stream"): {"get": 1},
    ("identity_name", "query"): {"get": 1, "setex": 1},
    ("identity_name", "query_stream"): {"get": 1, "setex": 1},
    ("decline", "query"): {"get": 1, "setex": 1},
    ("decline", "query_stream"): {"get": 1, "setex": 1},
    ("fragment", "query"): {"get": 3},
    ("fragment", "query_stream"): {"get": 2},
    ("dining", "query"): {"get": 1, "setex": 1},
    ("dining", "query_stream"): {"get": 1, "setex": 1},
    ("general", "query"): {"get": 1, "setex": 1},
    ("general", "query_stream"): {"get": 1},
}
# Round trips this campaign adds on purpose, each with its reason.
DECLARED_EXTRA_REDIS = {
    # Phase 8: the streamed synthesis saves the turn's context like the graph's does.
    ("general", "query_stream"): {"setex": 1},
    # The ambient-fragment gate reads the stored context to defer to the pipeline on an open question.
    ("fragment", "query"): {"get": 1},
    ("fragment", "query_stream"): {"get": 1},
}


@pytest.mark.parametrize("key", sorted(BASE_REDIS))
def test_a_turn_adds_no_redis_round_trip_beyond_the_base_and_the_declared_ones(turns, key):
    base = BASE_REDIS[key]
    extra = DECLARED_EXTRA_REDIS.get(key, {})
    allowed = {op: base.get(op, 0) + extra.get(op, 0) for op in set(base) | set(extra)}
    got = turns[key]["redis"]
    assert {op: n for op, n in got.items() if n > allowed.get(op, 0)} == {}, (key, got, allowed)


@pytest.mark.parametrize("key", sorted(DECLARED_EXTRA_REDIS))
def test_each_declared_extra_round_trip_is_really_made(turns, key):
    """Positive control for the table: a declared extra that vanished must be removed from it."""
    base = BASE_REDIS[key]
    got = turns[key]["redis"]
    assert all(got.get(op, 0) == base.get(op, 0) + n for op, n in DECLARED_EXTRA_REDIS[key].items()), (key, got)


def test_the_streamed_context_save_costs_exactly_one_write(turns):
    streamed = turns[("general", "query_stream")]["redis"]
    graph = turns[("general", "query")]["redis"]
    assert streamed.get("setex", 0) == graph.get("setex", 0) == 1
