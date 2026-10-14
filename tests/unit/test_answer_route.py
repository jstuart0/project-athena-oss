"""One answer route for the graph and the streaming runner.

The real route_after_classify and the real run_orchestrator_for_streaming run
over a table of classified turns with their nodes replaced by spies. For every
row both paths must pick the same route and do the same node work, and both
must have asked answer_route.decide. The rows pin the five places the two paths
used to disagree, (a) to (e) in orchestrator/answer_route.py.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest

from . import _public_audience_harness as h
from orchestrator import answer_route
from orchestrator.answer_route import Route

I = h.IntentCategory

WORK_NODES = (
    "route_control_node", "route_music_node", "route_tv_node", "send_sms_node", "notification_pref_node",
    "route_info_node", "retrieve_node", "tool_call_node", "validate_node", "intent_refused_node",
)
GRAPH_NAME_TO_NODE = {
    "route_control": "route_control_node", "route_music": "route_music_node", "route_tv": "route_tv_node",
    "send_sms": "send_sms_node", "notification_pref": "notification_pref_node", "intent_refused": "intent_refused_node",
}


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


class Rig:
    """Spies for every node the routes can reach; `work` is the node calls in order."""

    def __init__(self, monkeypatch, row):
        self.row = row
        self.work: list[str] = []
        self.decisions: list[Route] = []
        real_decide = answer_route.decide

        def decide(state, *, tool_trigger, ambient):
            route = real_decide(state, tool_trigger=tool_trigger, ambient=ambient)
            self.decisions.append(route)
            return route

        monkeypatch.setattr(answer_route, "decide", decide)
        real_handler_route = answer_route.handler_route

        def handler_route(state):
            route = real_handler_route(state)
            if route is not None:
                self.decisions.append(route)
            return route

        monkeypatch.setattr(answer_route, "handler_route", handler_route)

        for name in WORK_NODES:
            monkeypatch.setattr(h.main, name, self._spy(name))

        async def trigger(state, trigger_context="classify"):
            return bool(row.get("trigger")) if trigger_context == "classify" else False

        async def ambient(state):
            return bool(row.get("ambient"))

        monkeypatch.setattr(h.main, "should_use_tool_calling", trigger)
        monkeypatch.setattr(h.main, "ambient_fragment_applies", ambient)

        async def classify(state):
            self.apply(state)
            return state

        monkeypatch.setattr(h.main, "classify_node", classify)

    def _spy(self, name):
        async def node(state):
            self.work.append(name)
            if name == "tool_call_node":
                state.answer = "tool answer"
            return state

        return node

    def apply(self, state):
        state.intent = self.row["intent"]
        state.confidence = 0.9
        for key, value in self.row.get("state", {}).items():
            setattr(state, key, value)
        return state

    def fresh_state(self):
        state = h.make_state(
            permissions=self.row.get("permissions") or h.mode_permission.normalize_permissions({"mode": "owner"}),
            mode=self.row.get("mode", "owner"), intent=None, query=self.row["query"],
        )
        return state


def run_graph(rig):
    """The graph path: the real router, then the work nodes the graph's edges lead to."""
    state = rig.apply(rig.fresh_state())
    name = asyncio.run(h.main.route_after_classify(state))
    if name in GRAPH_NAME_TO_NODE:
        rig.work.append(GRAPH_NAME_TO_NODE[name])
    elif name == "route_info":
        rig.work += ["route_info_node", "retrieve_node"]
    elif name == "tool_call":
        rig.work += ["tool_call_node", "validate_node"]  # graph.add_edge("tool_call", "validate")
    return name, state


def run_stream(rig):
    state = rig.fresh_state()
    out = asyncio.run(h.main.run_orchestrator_for_streaming(state))
    return out


def terminal(name):
    return name if name in ("synthesize", "finalize") else None


def expected_stream_terminal(rig, state):
    """What the stream does when no work node ran: send a preset answer or synthesize."""
    return "finalize" if state.answer else "synthesize"


OWNER = None  # filled lazily: permissions need the reset runtime

ROWS = {
    # handler intents
    "control": dict(intent=I.CONTROL, query="turn on the lamp", work=["route_control_node"], route=Route.CONTROL),
    "music": dict(intent=I.MUSIC_PLAY, query="play some jazz", work=["route_music_node"], route=Route.MUSIC),
    "music control": dict(intent=I.MUSIC_CONTROL, query="pause", work=["route_music_node"], route=Route.MUSIC),
    "tv": dict(intent=I.TV_CONTROL, query="open the app", work=["route_tv_node"], route=Route.TV),
    "sms": dict(intent=I.TEXT_ME_THAT, query="text me that", work=["send_sms_node"], route=Route.SMS),
    "notification": dict(intent=I.NOTIFICATION_PREF, query="stop the alerts", work=["notification_pref_node"], route=Route.NOTIFICATION_PREF),
    # (a) a media request beats home control, owner mode only, in both paths
    "a: media request over control": dict(
        intent=I.CONTROL, query="show my pending requests", work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "a: media request over unknown": dict(
        intent=I.UNKNOWN, query="add the movie to plex", work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "a: guest mode keeps control": dict(
        intent=I.CONTROL, query="show my pending requests", mode="guest", work=["route_control_node"], route=Route.CONTROL),
    # (b) general info without a trigger synthesizes; with one it uses tools
    "b: general info": dict(intent=I.GENERAL_INFO, query="what is a stock split", work=[], route=Route.SYNTHESIZE),
    "b: general info with trigger": dict(
        intent=I.GENERAL_INFO, query="what is a stock split", trigger=True, work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    # (c) a referential question about the conversation is answered from it
    "c: referential dining question": dict(
        intent=I.DINING, query="what city was that restaurant in",
        state=dict(conversation_history=[{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}]),
        work=[], route=Route.SYNTHESIZE, redirected=True),
    "c: fresh dining request": dict(
        intent=I.DINING, query="find an italian place",
        state=dict(conversation_history=[{"role": "user", "content": "x"}]),
        work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "c: dining without history": dict(
        intent=I.DINING, query="what city was that restaurant in", work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    # (d) a local-business request reaches the tool node in both paths
    "d: general info local business": dict(
        intent=I.GENERAL_INFO, query="recommend an italian place near me", work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "d: unknown local business": dict(
        intent=I.UNKNOWN, query="find me a good pizza place nearby", work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "d: a knowledge question as unknown": dict(intent=I.UNKNOWN, query="what is a stock split", work=[], route=Route.FINALIZE),
    # (e) a preset answer ends the turn
    "e: preset answer": dict(
        intent=I.GENERAL_INFO, query="okay", state=dict(answer="Okay.", skip_synthesis=True), work=[], route=Route.FINALIZE),
    "e: preset answer beats a trigger": dict(
        intent=I.GENERAL_INFO, query="okay", trigger=True, state=dict(answer="Okay.", skip_synthesis=True), work=[], route=Route.FINALIZE),
    # the rest of the information routes
    "dining": dict(intent=I.DINING, query="find an italian place", work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "stocks": dict(intent=I.STOCKS, query="how is the market", work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "weather": dict(intent=I.WEATHER, query="weather tomorrow", work=["route_info_node", "retrieve_node"], route=Route.ROUTE_INFO),
    "sports": dict(intent=I.SPORTS, query="score of the game", work=["route_info_node", "retrieve_node"], route=Route.ROUTE_INFO),
    "weather with trigger": dict(
        intent=I.WEATHER, query="weather tomorrow", trigger=True, work=["tool_call_node", "validate_node"], route=Route.TOOL_CALL),
    "unknown": dict(intent=I.UNKNOWN, query="mumble mumble", work=[], route=Route.FINALIZE),
    "unknown continuation": dict(
        intent=I.UNKNOWN, query="yes", state=dict(
            conversation_history=[{"role": "assistant", "content": "want more?"}], context_ref_info={"is_continuation": True}),
        work=[], route=Route.SYNTHESIZE),
    "ambient fragment": dict(intent=I.UNKNOWN, query="mm hm", ambient=True, work=[], route=Route.AMBIENT),
    "ambient beats a trigger": dict(intent=I.GENERAL_INFO, query="mm hm", ambient=True, trigger=True, work=[], route=Route.AMBIENT),
}


@pytest.mark.parametrize("name", list(ROWS))
def test_the_graph_and_the_stream_take_the_same_route(monkeypatch, name):
    row = dict(ROWS[name])
    graph_rig = Rig(monkeypatch, row)
    graph_name, graph_state = run_graph(graph_rig)
    graph_work, graph_decisions = list(graph_rig.work), list(graph_rig.decisions)

    stream_rig = Rig(monkeypatch, row)
    stream_state = run_stream(stream_rig)

    assert graph_work == stream_rig.work == row["work"]
    expected_route = row["route"]
    assert graph_decisions == stream_rig.decisions == [expected_route], "both paths ask answer_route once"
    assert graph_name == ("finalize" if expected_route is Route.AMBIENT else expected_route.value)
    if row.get("redirected"):
        assert graph_state.intent is I.GENERAL_INFO and stream_state.intent is I.GENERAL_INFO
    if expected_route is Route.AMBIENT:
        assert stream_state.answer


def test_every_route_value_is_a_graph_edge_target():
    """route_after_classify returns Route values as node names; each must be a conditional-edge key."""
    import ast

    tree = ast.parse(h.MAIN_PY.read_text())
    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_conditional_edges" and node.args:
            if isinstance(node.args[0], ast.Constant) and node.args[0].value == "classify":
                mapping = node.args[2]
                keys = {k.value for k in mapping.keys if isinstance(k, ast.Constant)}
    reachable = {r.value for r in Route if r is not Route.AMBIENT} | {"finalize", "intent_refused"}
    assert reachable <= keys, reachable - keys


def test_a_disallowed_intent_is_refused_by_both_paths_before_any_route(monkeypatch):
    guest = h.mode_permission.normalize_permissions({
        "mode": "guest", "allowed_intents": ["weather"], "restricted_entities": [], "allowed_domains": [],
    })
    row = dict(intent=I.DINING, query="find an italian place", permissions=guest, mode="guest")
    graph_rig = Rig(monkeypatch, row)
    name, _ = run_graph(graph_rig)
    assert name == "intent_refused" and graph_rig.decisions == []
    stream_rig = Rig(monkeypatch, row)
    run_stream(stream_rig)
    assert stream_rig.work == ["intent_refused_node"] and stream_rig.decisions == []


def test_the_identity_reply_is_final_in_both_paths(monkeypatch):
    row = dict(intent=I.GENERAL_INFO, query="what is my name", state=dict(
        answer="I can't tell who's speaking, so I don't know your name.", skip_synthesis=True, identity_answer=True))
    graph_rig = Rig(monkeypatch, row)
    assert run_graph(graph_rig)[0] == "finalize"
    stream_rig = Rig(monkeypatch, row)
    assert run_stream(stream_rig).answer.startswith("I can't tell")
    assert graph_rig.work == stream_rig.work == []


def test_a_failed_stream_tool_node_with_no_answer_is_not_validated(monkeypatch):
    row = dict(intent=I.DINING, query="find an italian place")
    rig = Rig(monkeypatch, row)

    async def failing_tool_node(state):
        rig.work.append("tool_call_node")
        state.error = "Tool calling disabled"
        return state

    monkeypatch.setattr(h.main, "tool_call_node", failing_tool_node)
    out = run_stream(rig)
    assert rig.work == ["tool_call_node"] and out.answer in (None, "")  # the stream then synthesizes as before


# --- the two streaming endpoints ------------------------------------------------------------------------

def _stream_rig(monkeypatch, tool_nodes):
    h.reset_runtime()
    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    h._runtime.set_cache_client(SimpleNamespace(client=SimpleNamespace(
        delete=mock.AsyncMock(), get=mock.AsyncMock(return_value=None), setex=mock.AsyncMock())))
    h._runtime.set_llm_router(h.CapturingLLM(content="a streamed answer"))

    async def classify(state):
        state.intent = I.GENERAL_INFO
        state.confidence = 0.9
        return state

    async def tool_call(state):
        tool_nodes.append(state.query)
        state.answer = "Test Trattoria is nearby."
        return state

    async def validate(state):
        tool_nodes.append("validate")
        return state

    async def no_trigger(state, trigger_context="classify"):
        return False

    async def no_ambient(state):
        return False

    monkeypatch.setattr(h.main, "classify_node", classify)
    monkeypatch.setattr(h.main, "tool_call_node", tool_call)
    monkeypatch.setattr(h.main, "validate_node", validate)
    monkeypatch.setattr(h.main, "should_use_tool_calling", no_trigger)
    monkeypatch.setattr(h.main, "ambient_fragment_applies", no_ambient)
    monkeypatch.setattr(h.main, "record_intent_metric", mock.AsyncMock())


async def _post(path, body):
    transport = httpx.ASGITransport(app=h.main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(path, json=body, headers=h.service_headers())


LOCAL_QUERY = "recommend an italian place near me"


def test_a_local_business_general_info_reaches_the_tool_node_on_query_stream(monkeypatch):
    nodes = []
    _stream_rig(monkeypatch, nodes)
    resp = asyncio.run(_post("/query/stream", {"query": LOCAL_QUERY, "interface_type": "text", "caller_trust": "household"}))
    events = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ")]
    assert nodes == [LOCAL_QUERY, "validate"]
    assert "Test Trattoria" in "".join(e.get("content", "") for e in events if e.get("stage") == "answer_chunk")


def test_a_local_business_general_info_reaches_the_tool_node_on_v1_stream(monkeypatch):
    nodes = []
    _stream_rig(monkeypatch, nodes)
    body = {"model": "m", "stream": True, "messages": [{"role": "user", "content": LOCAL_QUERY}],
            "extra_body": {"interface_type": "text"}}
    resp = asyncio.run(_post("/v1/chat/completions", body))
    chunks = [json.loads(c[6:]) for c in resp.text.split("\n\n") if c.startswith("data: ") and "[DONE]" not in c]
    assert nodes == [LOCAL_QUERY, "validate"]
    assert "Test Trattoria" in "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)


def test_a_knowledge_question_does_not_reach_the_tool_node_on_the_stream(monkeypatch):
    nodes = []
    _stream_rig(monkeypatch, nodes)
    asyncio.run(_post("/query/stream", {"query": "what is a stock split", "interface_type": "text", "caller_trust": "household"}))
    assert nodes == []  # the positive control above can fail for the right reason


def test_the_two_paths_have_no_routing_rule_of_their_own():
    """Neither the router nor the runner names an intent set or a keyword list."""
    import ast

    tree = ast.parse(h.MAIN_PY.read_text())
    for fn in ("route_after_classify", "run_orchestrator_for_streaming", "decide_answer_route"):
        node = next(n for n in ast.walk(tree) if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == fn)
        names = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "IntentCategory"}
        assert not names, f"{fn} names intents {names}: routing rules belong in answer_route"
