"""Follow-up handling through the real nodes and the real routes.

The real classify_node, route_after_classify and tool_call_node run over a
stored conversation (a session with its last exchange, and the stored context
the previous turn wrote). The streaming routes run the real
run_orchestrator_for_streaming with spies on the work nodes, so the logic
cannot be wired into the graph only. Also: the cache clauses, the context-store
drift test, and the music retry.
"""
from __future__ import annotations

import ast
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from . import _fast_path_harness as fp
from . import _public_audience_harness as h
from orchestrator import followup
from orchestrator.helpers import store_conversation_context
from orchestrator.state import IntentCategory

I = IntentCategory
REQUEST = "Any good Italian places nearby?"
QUESTION = "I found three places. Want me to look for outdoor seating?"
SRC = Path(h.REPO_ROOT) / "src" / "orchestrator"


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _run(coro):
    return asyncio.run(coro)


# --- a stored conversation -------------------------------------------------------------------------------

async def _seed(session_id, *, user=REQUEST, assistant=QUESTION, intent="dining", parameters=None, query=None,
                caller_class="other"):
    sm = h._runtime.get_session_manager()
    await sm.get_or_create_session(session_id=session_id, caller_class=caller_class)
    await sm.add_message(session_id, "user", user)
    await sm.add_message(session_id, "assistant", assistant)
    if intent:
        await store_conversation_context(
            session_id=session_id, intent=intent, query=query or user, entities={"cuisine": "italian"},
            parameters=parameters if parameters is not None else {"grounded": True}, response=assistant,
        )


def _state_for(query, session_id, *, last=None, mode="owner"):
    state = h.make_state(permissions=h.mode_permission.normalize_permissions({"mode": mode}), mode=mode,
                         intent=None, query=query)
    state.session_id = session_id
    session = _run(h._runtime.get_session_manager().get_session(session_id))
    state.last_exchange = last if last is not None else followup.last_exchange_from_session(session)
    return state


def _classify(monkeypatch, query, session_id, **kw):
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    state = _state_for(query, session_id, **kw)
    try:
        return _run(h.main.classify_node(state))
    except ModuleNotFoundError as error:
        # Past the open-task block the node imports the bare-path `semantic_cache`,
        # which only the deployed image resolves: reaching it IS "fell through".
        if error.name != "semantic_cache":
            raise
        return state


def _make_cache():
    cache = fp.FakeCache()
    h._runtime.set_cache_client(cache)
    return cache


@pytest.fixture
def stored(monkeypatch):
    _make_cache()
    h.patch_conversation_config(monkeypatch)
    return "wire-s1"


# --- classify_node ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("query", ["Yes.", "YES!", "yeah, sure.", "sure"])
def test_an_affirmation_pins_the_stored_intent_and_carries_the_request(monkeypatch, stored, query):
    _run(_seed(stored))
    state = _classify(monkeypatch, query, stored)
    assert state.intent == I.DINING and state.query == query
    assert "Italian places nearby" in state.resolved_request and "outdoor seating" in state.resolved_request
    assert state.continuation_decision == {"decision": "continued", "reason": "open_task"}
    assert state.complexity in ("simple", "complex", "super_complex")


def test_the_resolved_request_is_judged_for_complexity_as_a_new_request(monkeypatch, stored):
    _run(_seed(stored))
    seen = {}
    real = h.main.get_complexity_with_override

    def spy(text, intent=None, cached_complexity=None, is_followup=False):
        seen.update(text=text, is_followup=is_followup)
        return real(text, intent=intent, cached_complexity=cached_complexity, is_followup=is_followup)

    monkeypatch.setattr(h.main, "get_complexity_with_override", spy)
    state = _classify(monkeypatch, "Yes.", stored)
    assert seen == {"text": state.resolved_request, "is_followup": False}


def test_a_slot_answer_is_appended_to_the_original_request(monkeypatch, stored):
    _run(_seed(stored, assistant="Which part of town?"))
    state = _classify(monkeypatch, "in the north district", stored)
    assert state.intent == I.DINING
    assert state.resolved_request.startswith("Any good Italian places nearby") and "north district" in state.resolved_request


def test_a_decline_ends_the_topic_with_okay(monkeypatch, stored):
    _run(_seed(stored))
    state = _classify(monkeypatch, "Nah", stored)
    assert state.answer == "Okay." and state.skip_synthesis is True and state.resolved_request is None
    assert _run(h.main.route_after_classify(state)) == "finalize"


def test_a_command_right_after_a_question_is_a_command_not_a_resume(monkeypatch, stored):
    _run(_seed(stored))
    state = _classify(monkeypatch, "turn off the kitchen lights", stored)
    assert state.resolved_request is None and state.intent == I.CONTROL


def test_a_yes_to_an_ha_pending_is_left_to_control(monkeypatch, stored):
    pending = {"pending_write_confirmation": {"nonce": "n", "fingerprint": "f"}}
    _run(_seed(stored, intent="control", parameters=pending, assistant="That would turn off 2 lights. Should I go ahead?"))
    state = _classify(monkeypatch, "yes", stored)
    assert state.resolved_request is None and state.intent == I.CONTROL


def test_an_ha_pending_on_an_info_context_still_owns_the_reply(monkeypatch, stored):
    _run(_seed(stored, parameters={"grounded": True, "awaiting_state_question": {"ttl": 60}}))
    state = _classify(monkeypatch, "yes", stored)
    assert state.resolved_request is None


@pytest.mark.parametrize("name,kwargs", [
    ("a stale exchange", {"age": 1200}),
    ("another session class", {"session_class": "guest"}),
])
def test_a_yes_does_not_resume_a_stale_or_foreign_exchange(monkeypatch, stored, name, kwargs):
    _run(_seed(stored))
    session = _run(h._runtime.get_session_manager().get_session(stored))
    last = followup.last_exchange_from_session(session)
    if "age" in kwargs:
        last["at"] = time.time() - kwargs["age"]
    if "session_class" in kwargs:
        last["session_class"] = kwargs["session_class"]
    state = _classify(monkeypatch, "yes", stored, last=last)
    assert state.resolved_request is None


MUSIC_TURNS = [
    ("pause", "music_play"), ("next", "music_play"), ("louder", "music_play"), ("turn it up", "music_play"),
    ("play the Beatles", "music_control"), ("play some jazz", "music_control"),
]


@pytest.mark.parametrize("query,stored_intent", MUSIC_TURNS, ids=[q for q, _ in MUSIC_TURNS])
def test_a_music_request_of_the_other_kind_is_classified_fresh(monkeypatch, stored, query, stored_intent):
    _run(_seed(stored, user="play jazz", assistant="Playing jazz in the kitchen.", intent=stored_intent,
               parameters={"media_id": "jazz"}))
    state = _classify(monkeypatch, query, stored)
    assert state.continuation_decision["decision"] == "declined", state.continuation_decision
    assert state.continuation_decision["reason"] in ("fresh_intent", "strong_intent")


def test_a_reply_that_names_no_transport_word_still_continues_the_music_turn(monkeypatch, stored):
    _run(_seed(stored, user="play jazz", assistant="Playing jazz in the kitchen.", intent="music_play",
               parameters={"media_id": "jazz"}))
    state = _classify(monkeypatch, "in the den", stored)
    assert state.continuation_decision["decision"] == "continued" and state.intent == I.MUSIC_PLAY


def test_the_music_fast_path_words_are_one_list():
    from orchestrator.context import detector

    assert len(detector.MUSIC_CONTROL_FAST_PATTERNS) >= 30 and "louder" in detector.MUSIC_CONTROL_FAST_PATTERNS
    assert "MUSIC_CONTROL_FAST_PATTERNS" in (SRC / "main.py").read_text()
    assert "music_control_fast_patterns = [" not in (SRC / "main.py").read_text()


def test_a_yes_after_a_music_turn_does_not_resume_it(monkeypatch, stored):
    _run(_seed(stored, user="play some jazz", assistant="Playing jazz in the kitchen. Want it louder?", intent="music_play",
               parameters={"media_id": "jazz"}))
    assert _classify(monkeypatch, "yes", stored).resolved_request is None


def test_an_open_task_without_a_matching_stored_intent_is_not_resumed(monkeypatch, stored):
    _run(_seed(stored, intent=None))  # no stored context: nothing says what the task was
    assert _classify(monkeypatch, "yes", stored).resolved_request is None


def test_a_yes_after_a_statement_is_not_a_resume(monkeypatch, stored):
    _run(_seed(stored, assistant="There are three places nearby."))
    assert _classify(monkeypatch, "yes", stored).resolved_request is None


def test_a_yes_after_a_fallback_does_not_resume_a_non_task(monkeypatch, stored):
    _run(_seed(stored, user="blorp", intent="general_info",
               assistant="I'm not sure how to help with that. Could you rephrase your question?"))
    assert _classify(monkeypatch, "yes", stored).resolved_request is None


def test_a_short_fresh_topic_is_not_a_slot_answer(monkeypatch, stored):
    _run(_seed(stored, assistant="Which part of town?"))
    state = _classify(monkeypatch, "weather in Denver", stored)
    assert state.resolved_request is None


def test_a_chained_reply_carries_the_original_request(monkeypatch, stored):
    """Turn 3 answers turn 2, which was itself a resumed task: the context holds the resolved text."""
    first = _classify(monkeypatch, "yes", (_run(_seed(stored)), stored)[1])
    _run(_seed(stored, user="yes", assistant="Here is one. Want directions?", intent="dining",
               query=first.resolved_request))
    second = _classify(monkeypatch, "yes", stored)
    assert "Italian places nearby" in second.resolved_request and "Want directions" in second.resolved_request


# --- the real tool node ------------------------------------------------------------------------------------

def test_turn_two_yes_searches_with_the_turn_one_constraints(monkeypatch, stored):
    _run(_seed(stored))
    state = _classify(monkeypatch, "Yes.", stored)
    assert _run(h.main.route_after_classify(state)) == "tool_call"
    admin, execute, _knowledge, _home = h.patch_tool_call_dependencies(monkeypatch)
    llm = h.CapturingLLM(content="Here are some options with outdoor seating.")
    h._runtime.set_llm_router(llm)
    out = _run(h.main.tool_call_node(state))
    called = [tc["function"]["name"] for call in execute.call_args_list for tc in call.args[0]]
    assert called == ["search_restaurants"]
    assert "Italian places nearby" in llm.captured[0] and "outdoor seating" in llm.captured[0]
    arguments = out.tool_calls_emitted[0]["arguments"]
    assert "italian" in arguments["term"].lower()
    assert out.query == "Yes."  # the user's own words are unchanged


def test_the_synthesis_prompts_read_the_resolved_request(monkeypatch, stored):
    _run(_seed(stored))
    state = _classify(monkeypatch, "Yes.", stored)
    state.retrieved_data = {"tool_results": {"c1": {"results": [{"name": "Test Trattoria"}]}}}
    prompt, _model, _system = _run(_streaming_prompt(monkeypatch, state))
    assert "Italian places nearby" in prompt and "Question: Yes." not in prompt


async def _streaming_prompt(monkeypatch, state):
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    monkeypatch.setattr(h.main, "get_model_for_component", mock.AsyncMock(return_value="m"))
    monkeypatch.setattr(h.main, "get_component_config", mock.AsyncMock(return_value={"model_name": "m"}))
    return await h.main.build_synthesis_prompt_for_streaming(state)


def _prompt_state(monkeypatch, *, intent, retrieved=None, history=True, resolved=True):
    state = h.make_state(permissions=h.mode_permission.normalize_permissions({"mode": "owner"}), mode="owner",
                         intent=intent, query="Yes.")
    state.resolved_request = "Any good Italian places nearby. The user answered: yes." if resolved else None
    state.retrieved_data = retrieved or {}
    state.context_ref_info = {"is_continuation": True, "has_context_ref": True}
    if history:
        state.history_summary = "Previous context: asked about Italian places."
    return state


@pytest.mark.parametrize("name,intent,retrieved", [
    ("data", I.DINING, {"tool_results": {"c": {"results": [1]}}}),
    ("general", I.GENERAL_INFO, None),
    ("history", I.DINING, None),
], ids=["data", "general", "history"])
def test_every_streaming_prompt_branch_reads_the_resolved_request(monkeypatch, name, intent, retrieved):
    state = _prompt_state(monkeypatch, intent=intent, retrieved=retrieved)
    prompt, _model, _system = _run(_streaming_prompt(monkeypatch, state))
    assert "Italian places nearby. The user answered: yes." in prompt, prompt[-600:]
    assert "continuing a conversation" not in prompt


def test_the_streaming_continuation_prompt_still_serves_a_plain_reply_with_only_a_summary(monkeypatch):
    """Positive control: with no resumed task, a summarized-mode continuation gets its prompt."""
    state = _prompt_state(monkeypatch, intent=I.GENERAL_INFO, resolved=False)
    prompt, _model, _system = _run(_streaming_prompt(monkeypatch, state))
    assert "continuing a conversation" in prompt


# --- answer_route: a resumed task is a search, not a question about the talk --------------------------------

def _routed(*, resolved, ref, history=True, query="Yes."):
    from orchestrator import answer_route

    state = _prompt_state(None, intent=I.DINING, history=history, resolved=resolved)
    state.query = query
    state.context_ref_info = ref
    return answer_route.decide(state, tool_trigger=False, ambient=False)


def test_a_resumed_task_goes_to_the_tool_node_even_with_a_conversation():
    from orchestrator.answer_route import Route

    ref = {"is_continuation": True, "has_context_ref": True}
    assert _routed(resolved=True, ref=ref) == Route.TOOL_CALL
    assert _routed(resolved=False, ref=ref) == Route.TOOL_CALL  # a continuation word alone is no history question
    slot_ref = {"is_continuation": False, "has_context_ref": True}  # a slot answer is no continuation word
    assert _routed(resolved=True, ref=slot_ref, query="in the north district") == Route.TOOL_CALL


def test_a_question_about_the_conversation_is_still_answered_from_it():
    """Positive control: the history redirect survives for a referential question."""
    from orchestrator.answer_route import Route

    ref = {"is_continuation": False, "has_context_ref": True}
    assert _routed(resolved=False, ref=ref, query="what did you say about that restaurant") == Route.SYNTHESIZE


def test_a_resumed_turns_context_stores_the_request_not_the_reply(monkeypatch, stored):
    _run(_seed(stored))
    state = _classify(monkeypatch, "Yes.", stored)
    state.retrieved_data = {"tool_results": {"c1": {"results": [1]}}}
    state.answer = "Test Trattoria has a patio."
    store = mock.AsyncMock()
    monkeypatch.setattr(h.synthesize_module, "store_conversation_context", store)
    _run(h.synthesize_module.save_synthesis_context(state))
    assert "Italian places nearby" in store.await_args.kwargs["query"]


# --- the streaming routes run the same logic -------------------------------------------------------------

STREAM_ROUTES = ("query_stream", "v1_stream")


def _stream_rig(monkeypatch):
    rig = fp.install(monkeypatch)
    nodes = []
    monkeypatch.setattr(h.main, "classify_node", _REAL_CLASSIFY)
    monkeypatch.setattr(h.main, "run_orchestrator_for_streaming", _REAL_RUNNER)

    async def tool_call(state):
        nodes.append(("tool_call", state.query, state.resolved_request, state.intent))
        state.answer = "Test Trattoria has a patio."
        return state

    async def validate(state):
        nodes.append(("validate",))
        return state

    async def no_trigger(state, trigger_context="classify"):
        return False

    async def no_ambient(state):
        return False

    for name, fn in (("tool_call_node", tool_call), ("validate_node", validate),
                     ("should_use_tool_calling", no_trigger), ("ambient_fragment_applies", no_ambient)):
        monkeypatch.setattr(h.main, name, fn)
    return rig, nodes


_REAL_CLASSIFY = h.main.classify_node
_REAL_RUNNER = h.main.run_orchestrator_for_streaming


@pytest.mark.parametrize("route", STREAM_ROUTES)
def test_a_yes_resumes_the_task_on_the_streaming_routes(monkeypatch, route):
    rig, nodes = _stream_rig(monkeypatch)
    sid = fp.session_id_for(route)
    _run(_seed(sid))
    resp = _run(fp.send(route, "Yes."))
    assert nodes[0][0] == "tool_call", nodes
    _, query, resolved, intent = nodes[0]
    assert query == "Yes." and intent == I.DINING and "Italian places nearby" in resolved
    assert "Test Trattoria" in fp.answer_of(route, resp)


@pytest.mark.parametrize("route", STREAM_ROUTES)
def test_a_decline_is_final_on_the_streaming_routes(monkeypatch, route):
    rig, nodes = _stream_rig(monkeypatch)
    sid = fp.session_id_for(route)
    _run(_seed(sid))
    resp = _run(fp.send(route, "no thanks"))
    assert nodes == []
    assert fp.answer_of(route, resp) == "Okay."


@pytest.mark.parametrize("route", STREAM_ROUTES)
def test_a_command_after_a_question_is_not_resumed_on_the_streaming_routes(monkeypatch, route):
    rig, nodes = _stream_rig(monkeypatch)
    sid = fp.session_id_for(route)
    _run(_seed(sid))
    _run(fp.send(route, "turn off the kitchen lights"))
    assert all(n[0] != "tool_call" or n[2] is None for n in nodes), nodes


def _graph_spy(monkeypatch, seen):
    """The graph /query and /query/stream/v2 invoke, with the real classify_node in it."""
    class _Graph:
        async def ainvoke(self, state):
            out = await _REAL_CLASSIFY(state)
            seen.append(out)
            return {"intent": out.intent, "answer": out.answer or "Test Trattoria has a patio.", "confidence": 0.9,
                    "citations": [], "request_id": "r", "node_timings": {}, "validation_passed": True,
                    "resolved_request": out.resolved_request, "skip_synthesis": out.skip_synthesis}

    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())


@pytest.mark.parametrize("route,reply", [("query", "Yes."), ("query_stream_v2", "Yes."), ("query_stream_v2", "no thanks")])
def test_the_graph_routes_hand_the_session_exchange_to_classify(monkeypatch, route, reply):
    rig = fp.install(monkeypatch, slow="record")
    seen = []
    _graph_spy(monkeypatch, seen)
    _run(_seed(fp.session_id_for(route)))
    _run(fp.send(route, reply))
    assert len(seen) == 1
    if reply == "Yes.":
        assert seen[0].intent == I.DINING and "Italian places nearby" in seen[0].resolved_request
    else:
        assert seen[0].answer == "Okay." and seen[0].resolved_request is None


# --- the cache --------------------------------------------------------------------------------------------

def _query_rig(monkeypatch, *, cached):
    rig = fp.install(monkeypatch, slow="record")
    reads = []

    async def get_cached(**kwargs):
        reads.append(kwargs["query"])
        return cached

    writes = []

    async def cache_response(**kwargs):
        writes.append(kwargs["query"])

    monkeypatch.setattr(h.main, "get_cached_response", get_cached)
    monkeypatch.setattr(h.main, "cache_response", cache_response)
    monkeypatch.setattr(h.main, "knowledge_cache_digest", mock.AsyncMock(return_value="kb_x"))

    class _Cfg:
        pass

    path_body = lambda text: fp.request_for("query", text)  # noqa: E731
    return rig, reads, writes, path_body


async def _post_query(text, *, skip_cache=False):
    from httpx import ASGITransport, AsyncClient

    path, body = fp.request_for("query", text)
    body["skip_semantic_cache"] = skip_cache
    async with AsyncClient(transport=ASGITransport(app=h.main.app), base_url="http://test") as client:
        return await client.post(path, json=body, headers=h.service_headers())


@pytest.mark.parametrize("text", ["yes", "Yes.", "no thanks."])
def test_a_bare_reply_never_reads_the_cache(monkeypatch, text):
    rig, reads, _writes, _ = _query_rig(monkeypatch, cached=None)
    _run(_post_query(text))
    assert reads == []


def test_a_short_answer_to_a_question_never_reads_the_cache(monkeypatch):
    rig, reads, _writes, _ = _query_rig(monkeypatch, cached=None)
    _run(_seed(fp.session_id_for("query"), assistant="Which part of town?"))
    _run(_post_query("in the north district"))
    assert reads == []


def test_the_same_words_read_the_cache_when_no_question_is_open(monkeypatch):
    """Positive control: the clause is what skips the read, not the rig."""
    rig, reads, _writes, _ = _query_rig(monkeypatch, cached=None)
    _run(_seed(fp.session_id_for("query"), assistant="There are three places nearby."))
    _run(_post_query("in the north district"))
    assert reads == ["in the north district"]


def test_a_resumed_turn_is_never_written_to_the_cache(monkeypatch):
    rig, reads, writes, _ = _query_rig(monkeypatch, cached=None)
    rig.graph_state = None

    class _Graph:
        async def ainvoke(self, state):
            return {"intent": I.DINING, "answer": "Test Trattoria has a patio.", "confidence": 0.9, "citations": [],
                    "request_id": "r", "node_timings": {}, "validation_passed": True,
                    "resolved_request": "Any good Italian places nearby. The user answered: yes.",
                    "retrieved_data": {"tool_results": {"c": {"results": [1]}}}}

    monkeypatch.setattr(h.main, "orchestrator_graph", _Graph())
    _run(_post_query("yes please do that"))
    _run(h._runtime.drain_background())
    assert writes == []


# --- the context stores: every one stores a real intent ------------------------------------------------------

STORE_CALLS = ("store_conversation_context", "_store_context")
# A value read back from a stored or cached record (its own writer was checked):
# a context cleared of a pending keeps the intent it was stored under.
ROUND_TRIP_CALLS = ("get",)


def _intent_values():
    return {i.value for i in IntentCategory}


def _assignments(func, name, before):
    """The value the name held at the call: its nearest assignment above it."""
    found = [
        node for node in ast.walk(func)
        if isinstance(node, ast.Assign) and node.lineno < before
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    return [max(found, key=lambda n: n.lineno).value] if found else []


def _resolves(node, func, valid, before, depth=0):
    """Whether an `intent=` expression can only be an IntentCategory value."""
    if depth > 6:
        return False
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in valid
    if isinstance(node, ast.Attribute) and node.attr == "value":
        inner = node.value
        if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) and inner.value.id == "IntentCategory":
            return inner.attr in IntentCategory.__members__
        if isinstance(inner, ast.Attribute) and inner.attr == "intent":
            return True  # state.intent.value
        if isinstance(inner, ast.BoolOp):
            return all(_resolves(ast.Attribute(value=v, attr="value", ctx=ast.Load()), func, valid, before, depth + 1) for v in inner.values)
        if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) and inner.value.id == "IntentCategory":
            return True
        return False
    if isinstance(node, ast.Attribute):
        return isinstance(node.value, ast.Name) and node.value.id == "IntentCategory" and node.attr in IntentCategory.__members__
    if isinstance(node, ast.IfExp):
        return _resolves(node.body, func, valid, before, depth + 1) and _resolves(node.orelse, func, valid, before, depth + 1)
    if isinstance(node, ast.BoolOp):
        return all(_resolves(v, func, valid, before, depth + 1) for v in node.values)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ROUND_TRIP_CALLS:
        return all(_resolves(a, func, valid, before, depth + 1) for a in node.args[1:])
    if isinstance(node, ast.Name):
        values = _assignments(func, node.id, before)
        return bool(values) and all(_resolves(v, func, valid, node.lineno, depth + 1) for v in values)
    return False


def _store_sites(tree):
    """(call node, enclosing function) for every context-store call."""
    sites = []
    for func in ast.walk(tree):
        if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in ast.walk(func):
            if isinstance(node, ast.Call):
                name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
                if name in STORE_CALLS:
                    sites.append((node, func))
    return sites


def _bad_sites(source):
    tree = ast.parse(source)
    valid = _intent_values()
    bad = []
    for call, func in _store_sites(tree):
        kwargs = {k.arg: k.value for k in call.keywords}
        if None in kwargs:  # **fields: the pass-through inside _store_context itself
            continue
        if "intent" not in kwargs or not _resolves(kwargs["intent"], func, valid, call.lineno):
            bad.append((call.lineno, ast.unparse(kwargs["intent"]) if "intent" in kwargs else "<no intent>"))
    return bad


def _orchestrator_sources():
    return {p: p.read_text() for p in SRC.rglob("*.py")}


def test_every_context_store_resolves_to_an_intent_category_value():
    problems = {}
    direct = wrapped = passthrough = 0
    for path, source in _orchestrator_sources().items():
        bad = _bad_sites(source)
        if bad:
            problems[str(path.relative_to(SRC))] = bad
        for call, _func in _store_sites(ast.parse(source)):
            kwargs = {k.arg for k in call.keywords}
            name = call.func.id if isinstance(call.func, ast.Name) else call.func.attr
            if None in kwargs:
                passthrough += 1
            elif name == "store_conversation_context":
                direct += 1
            else:
                wrapped += 1
    assert problems == {}
    assert direct >= 6 and wrapped >= 9 and passthrough == 1  # the floors at the time of writing


def test_route_music_is_a_named_member_and_stores_the_turns_own_intent():
    source = (SRC / "nodes" / "route_music.py").read_text()
    sites = [(c, f) for c, f in _store_sites(ast.parse(source))]
    assert len(sites) == 1
    call, _ = sites[0]
    intent = {k.arg: k.value for k in call.keywords}["intent"]
    assert "state.intent" in ast.unparse(intent) and ast.unparse(intent).endswith(".value")


PLANTED = {
    "a made-up intent": 'store_conversation_context(session_id=s, intent="music", query=q)',
    "no intent": 'store_conversation_context(session_id=s, query=q)',
    "an unbound name": 'store_conversation_context(session_id=s, intent=who_knows, query=q)',
    "the wrapper with a made-up intent": '_store_context(session_id=s, intent="tv", query=q)',
}
GOOD = {
    "a literal value": 'store_conversation_context(session_id=s, intent="music_play", query=q)',
    "a member": 'store_conversation_context(session_id=s, intent=IntentCategory.MUSIC_PLAY.value, query=q)',
    "the turn's intent": 'store_conversation_context(session_id=s, intent=state.intent.value, query=q)',
    "a bound name": 'x = state.intent.value if state.intent else "general_info"\nstore_conversation_context(session_id=s, intent=x, query=q)',
}


@pytest.mark.parametrize("name", PLANTED)
def test_the_drift_check_catches_a_planted_bad_store(name):
    assert _bad_sites(f"async def f(state, s, q):\n    await {PLANTED[name]}\n"), name


@pytest.mark.parametrize("name", GOOD)
def test_the_drift_check_passes_a_good_store(name):
    body = "\n    ".join(f"{'await ' if line.startswith(('store', '_store')) else ''}{line}" for line in GOOD[name].split("\n"))
    assert _bad_sites(f"async def f(state, s, q):\n    {body}\n") == [], name


# --- music ---------------------------------------------------------------------------------------------------

def _music_handler(monkeypatch, *, failures, seeds=("Artist A", "Artist B", "Artist C")):
    from orchestrator import music_handler as mh

    handler = mh.MusicHandler.__new__(mh.MusicHandler)
    handler.ha = mock.MagicMock()
    handler.account_pool = mock.MagicMock()
    handler.account_pool.get_account_for_room.return_value = "acct"
    handler.playback_manager = mock.MagicMock()
    attempts = []

    async def call_service(domain, service, service_data=None, **kwargs):
        if service == "play_media":
            attempts.append(service_data["media_id"])
            if len(attempts) <= failures:
                raise RuntimeError("service down")

    handler.ha.call_service = call_service
    monkeypatch.setattr(mh, "check_music_assistant_players", mock.AsyncMock(return_value=True))
    monkeypatch.setattr(mh, "get_room_configs", mock.AsyncMock(return_value={"kitchen": {}}))
    monkeypatch.setattr(mh, "get_room_entity", lambda room, cfg: "media_player.kitchen")
    monkeypatch.setattr(mh, "get_room_secondary_entity", lambda room, cfg: None)
    monkeypatch.setattr(mh, "GENRE_TO_ARTISTS", {"jazz": list(seeds)})
    monkeypatch.setattr(handler, "_normalize_room", lambda room: "kitchen", raising=False)
    return handler, attempts


def test_a_genre_that_fails_once_is_retried_with_another_seed(monkeypatch):
    handler, attempts = _music_handler(monkeypatch, failures=1)
    answer = _run(handler.handle_play("genre", "jazz", room="kitchen"))
    assert len(attempts) == 2 and attempts[0] != attempts[1]
    assert answer.startswith("Playing ") and answer.endswith("in the kitchen.")


def test_a_genre_that_fails_twice_says_so_and_offers_to_try_again(monkeypatch):
    handler, attempts = _music_handler(monkeypatch, failures=5)
    answer = _run(handler.handle_play("genre", "jazz", room="kitchen"))
    assert len(attempts) == 2
    assert answer == "The music service didn't start that in the kitchen. Say 'play jazz' to try again."
    assert not answer.endswith("?")
    handler.account_pool.release_room.assert_called_once_with("kitchen")


def test_an_artist_that_fails_is_not_retried(monkeypatch):
    handler, attempts = _music_handler(monkeypatch, failures=5)
    answer = _run(handler.handle_play("artist", "Someone", room="kitchen"))
    assert attempts == ["Someone"] and answer.startswith("The music service didn't start that") and "Say 'play Someone'" in answer


def test_a_denied_write_is_never_retried(monkeypatch):
    from orchestrator.mode_permission import HAWritePermissionDenied

    handler, attempts = _music_handler(monkeypatch, failures=0)

    async def denied(domain, service, service_data=None, **kwargs):
        attempts.append(service_data and service_data.get("media_id"))
        raise HAWritePermissionDenied("denied")

    handler.ha.call_service = denied
    _run(handler.handle_play("genre", "jazz", room="kitchen"))
    assert len(attempts) == 1


def test_a_genre_with_one_seed_has_nothing_to_retry_with(monkeypatch):
    handler, attempts = _music_handler(monkeypatch, failures=5, seeds=("Only One",))
    answer = _run(handler.handle_play("genre", "jazz", room="kitchen"))
    assert attempts == ["Only One"] and "Say 'play jazz' to try again." in answer


def test_get_genre_artist_excludes_what_was_tried():
    from orchestrator.music_handler import GENRE_TO_ARTISTS, get_genre_artist

    artists = GENRE_TO_ARTISTS["jazz"]
    for _ in range(20):
        assert get_genre_artist("jazz", exclude=artists[:-1]) == artists[-1]
    assert get_genre_artist("jazz", exclude=artists) is None
