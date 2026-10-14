"""The grounding contract through the real tool_call_node, validate_node and finalize_node.

The tool node runs for real over a stub model router and a stub tool executor
(the harness); the real-stack smoke at the end runs it over the real executor,
the real RAG client and a fake RAG HTTP server.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

import pytest

from . import _public_audience_harness as h
from orchestrator import grounding
from orchestrator.model_safe_errors import tool_error_result
from orchestrator.nodes import finalize_node, validate_node
from orchestrator.state import IntentCategory
import orchestrator.semantic_cache as semantic_cache_module
from shared import assistant_profile

I = IntentCategory
INVENTED = "Try Luigi's Trattoria on Main Street. It's open until 10 PM and the pasta is around $18."


@pytest.fixture(autouse=True)
def _reset():
    h.reset_runtime()
    yield
    h.reset_runtime()


def _owner():
    return h.mode_permission.normalize_permissions({"mode": "owner"})


def _state(query="recommend an italian place near me", intent=I.DINING, **fields):
    state = h.make_state(permissions=_owner(), mode="owner", intent=intent, query=query)
    for name, value in fields.items():
        setattr(state, name, value)
    return state


def _run_tool_node(monkeypatch, state, *, llm=None, executor_results=None):
    admin, execute, _knowledge, _home = h.patch_tool_call_dependencies(
        monkeypatch, executor_results=executor_results,
    )
    llm = llm or h.CapturingLLM(content="Here are some options nearby.")
    h._runtime.set_llm_router(llm)
    out = asyncio.run(h.main.tool_call_node(state))
    return out, execute, llm


def _executed(execute):
    return [tc["function"]["name"] for call in execute.call_args_list for tc in call.args[0]]


def _validate(state):
    guardrails = {"min_response_chars": 10, "max_response_chars": 2000}
    with mock.patch("orchestrator.nodes.validate.get_validation_guardrails", mock.AsyncMock(return_value=guardrails)):
        return asyncio.run(validate_node(state))


# --- forced calls --------------------------------------------------------------

def test_a_dining_query_the_model_answered_without_a_tool_runs_exactly_one_place_search(monkeypatch):
    state, execute, llm = _run_tool_node(monkeypatch, _state())
    assert _executed(execute) == ["search_restaurants"]
    assert state.tool_call_attempted is True
    assert [c["name"] for c in state.tool_calls_emitted] == ["search_restaurants"]
    arguments = state.tool_calls_emitted[0]["arguments"]
    assert arguments["term"] == "italian place" and arguments["location"] == h.HOME_STREET
    assert grounding.grounded(state)
    assert llm._with_tools_calls == 2  # selection, then synthesis from the tool result


def test_the_models_own_tool_call_is_not_doubled(monkeypatch):
    llm = h.CapturingLLM(tool_calls=[h.tool_call("search_restaurants", arguments={"term": "thai"})], content="Done.")
    state, execute, _ = _run_tool_node(monkeypatch, _state(), llm=llm)
    assert _executed(execute) == ["search_restaurants"]
    assert state.tool_calls_emitted[0]["arguments"] == {"term": "thai"}


def test_a_tool_the_caller_is_not_offered_is_not_forced(monkeypatch):
    """The public audience is offered weather, news, recipes and streaming only."""
    public = h.mode_permission.public_permissions()
    state = h.make_state(permissions=public, mode="guest", intent=I.DINING, query="recommend an italian place near me")
    state, execute, _ = _run_tool_node(monkeypatch, state)
    assert _executed(execute) == []
    assert state.tool_calls_emitted == []


def test_an_off_stay_sms_caller_is_forced_only_into_the_tools_it_may_use(monkeypatch):
    permissions = {**_owner(), "stay_read_only": True}
    sports = h.make_state(permissions=permissions, mode="owner", intent=I.SPORTS, query="how did the home team do")
    sports.entities = {"team": "Anytown Owls"}
    _out, execute, _ = _run_tool_node(monkeypatch, sports)
    assert _executed(execute) == []  # sports is not a travel tool
    dining = h.make_state(permissions=permissions, mode="owner", intent=I.DINING, query="recommend an italian place")
    _out, execute, _ = _run_tool_node(monkeypatch, dining)
    assert _executed(execute) == ["search_restaurants"]  # the positive control


def test_a_follow_up_on_earlier_tool_results_is_not_forced(monkeypatch):
    previous = {"intent": "dining", "query": "q", "response": "Luigi's Trattoria is nice.",
                "parameters": {"tool_calls": ["search_restaurants"]}}
    state = _state("do they take reservations", prev_context=previous)
    state, execute, _ = _run_tool_node(monkeypatch, state)
    assert _executed(execute) == []
    assert state.answer == "Here are some options nearby."


# --- the prompt ---------------------------------------------------------------------

def test_the_tool_prompt_has_no_clarification_loop_and_no_example_that_teaches_one(monkeypatch):
    _state_out, _execute, llm = _run_tool_node(monkeypatch, _state(intent=I.GENERAL_INFO, query="tell me about yourself"))
    text = llm.text().lower()
    assert "ask for clarification" not in text and "peruvian" not in text
    assert "pick sensible defaults" in text
    assert "recommendations of restaurants" in text


def test_the_default_persona_prompt_says_act_and_never_claims_experiences(monkeypatch):
    monkeypatch.setattr(assistant_profile, "get_admin_client", lambda: mock.Mock(get_assistant_profile=mock.AsyncMock(return_value=None)))
    prompt = asyncio.run(assistant_profile.build_core_assistant_prompt()).lower()
    assert "act on clear requests with sensible defaults" in prompt
    assert "never claim personal experiences" in prompt
    assert "always ask for clarification" not in prompt and "peruvian" not in prompt


def test_a_saved_profile_cannot_bring_back_the_ask_first_rule_without_the_fixed_policy(monkeypatch):
    saved = {"communication_style": ["Always ask for clarification when a request is ambiguous"]}
    monkeypatch.setattr(assistant_profile, "get_admin_client", lambda: mock.Mock(get_assistant_profile=mock.AsyncMock(return_value=saved)))
    prompt = asyncio.run(assistant_profile.build_core_assistant_prompt())
    assert "Always ask for clarification" in prompt  # the operator's saved text is theirs
    assert assistant_profile.TOOL_DEFAULTS_POLICY in prompt and assistant_profile.NO_EXPERIENCE_POLICY in prompt


# --- locations -----------------------------------------------------------------------

def test_the_device_position_beats_the_classifier_location_at_execution(monkeypatch):
    state = _state(
        "an italian place with outdoor seating",
        entities={"location": "outdoor seating"},
        context={"location_override": {"use_device_location": True, "latitude": 40.0, "longitude": -75.0}},
    )
    _out, execute, _ = _run_tool_node(monkeypatch, state)
    assert execute.call_args.kwargs["location"] == "40.0000, -75.0000"
    assert state.tool_calls_emitted[0]["arguments"]["location"] == "40.0000, -75.0000"


def test_an_amenity_phrase_never_becomes_the_search_location(monkeypatch):
    state = _state("an italian place with outdoor seating", entities={"location": "outdoor seating"})
    _out, execute, _ = _run_tool_node(monkeypatch, state)
    assert execute.call_args.kwargs["location"] == h.HOME_STREET


def test_a_place_the_user_named_is_still_used(monkeypatch):
    state = _state("an italian place in Springfield", entities={"location": "Springfield"})
    _out, execute, _ = _run_tool_node(monkeypatch, state)
    assert execute.call_args.kwargs["location"] == "Springfield"


# --- declines ---------------------------------------------------------------------------

def test_a_direct_answer_with_an_invented_venue_and_no_data_becomes_the_decline(monkeypatch):
    """The caller is offered no place search (public audience), so nothing is forced
    and the model's own text is judged."""
    public = h.mode_permission.public_permissions()
    state = h.make_state(permissions=public, mode="guest", intent=I.DINING, query="recommend an italian place near me")
    out, execute, _ = _run_tool_node(monkeypatch, state, llm=h.CapturingLLM(content=INVENTED))
    assert _executed(execute) == []
    assert out.answer == grounding.DECLINE_TEXT and out.is_fallback is True
    h.main.store_conversation_context.assert_not_called()


def test_a_knowledge_answer_is_never_declined(monkeypatch):
    answer = "A stock split divides each share into several shares and lowers the price of each one."
    state = _state("what is a stock split", intent=I.GENERAL_INFO)
    out, execute, _ = _run_tool_node(monkeypatch, state, llm=h.CapturingLLM(content=answer))
    assert _executed(execute) == [] and out.answer == answer and out.is_fallback is False


def test_a_first_person_experience_in_a_direct_answer_is_declined(monkeypatch):
    state = _state("is that movie any good", intent=I.GENERAL_INFO)
    out, _execute, _ = _run_tool_node(monkeypatch, state, llm=h.CapturingLLM(content="I've been to see it and I love it."))
    assert out.answer == grounding.DECLINE_TEXT


def test_a_failing_tool_with_an_invented_synthesis_is_the_decline_and_is_not_cacheable(monkeypatch):
    out, execute, _ = _run_tool_node(
        monkeypatch, _state(), llm=h.CapturingLLM(content=INVENTED),
        executor_results={"search_restaurants": tool_error_result("boom")},
    )
    assert _executed(execute) == ["search_restaurants"]
    assert out.answer == grounding.DECLINE_TEXT and out.is_fallback is True
    assert not grounding.grounded(out)
    assert not h.main.cacheable_turn(
        out.intent, grounded=grounding.grounded(out), is_fallback=out.is_fallback,
        identity_answer=False, local_business_cue=True, ungrounded=False,
    )
    # the decline is a refusal, never an exchange to follow up on
    h.main.store_conversation_context.assert_not_called()


def test_a_failing_tool_with_a_plain_apology_is_kept_but_not_cacheable(monkeypatch):
    out, _execute, _ = _run_tool_node(
        monkeypatch, _state(), llm=h.CapturingLLM(content="Sorry, the restaurant search is down."),
        executor_results={"search_restaurants": tool_error_result("boom")},
    )
    assert out.answer == "Sorry, the restaurant search is down."
    assert not h.main.cacheable_turn(
        out.intent, grounded=grounding.grounded(out), is_fallback=out.is_fallback,
        identity_answer=False, local_business_cue=False, ungrounded=False,
    )


def test_a_successful_search_is_cacheable(monkeypatch):
    out, _execute, _ = _run_tool_node(
        monkeypatch, _state(), llm=h.CapturingLLM(content=INVENTED),
        executor_results={"search_restaurants": {"places": [{"name": "Luigi's Trattoria"}]}},
    )
    assert out.answer == INVENTED and out.is_fallback is False
    assert h.main.cacheable_turn(
        out.intent, grounded=grounding.grounded(out), is_fallback=False,
        identity_answer=False, local_business_cue=True, ungrounded=False,
    )


# --- validate ----------------------------------------------------------------------------

def test_a_2500_character_grounded_answer_passes_validate():
    answer = "Step one: chop the onions and fry them in butter until golden. " * 40
    state = _state("a recipe for onion soup", I.RECIPES, answer=answer[:2500], tool_call_attempted=True,
                   retrieved_data={"tool_calls": ["search_recipes"], "tool_results": {"a": {"recipes": [{"name": "soup"}]}}})
    assert len(state.answer) == 2500
    assert _validate(state).validation_passed is True


def test_a_2500_character_ungrounded_creative_text_answer_passes_validate():
    story = "Once upon a time a lamp learned to sing, and the whole house listened. " * 36
    state = _state("tell me a long story about a lamp", I.GENERAL_INFO, answer=story[:2500], tool_call_attempted=True)
    assert len(state.answer) == 2500
    assert _validate(state).validation_passed is True


def test_the_length_rule_still_stops_a_long_answer_that_no_tool_node_produced():
    state = _state("tell me a long story about a lamp", I.GENERAL_INFO, answer="A lamp sang. " * 300)
    out = _validate(state)
    assert out.validation_passed is False and out.validation_reason == "Response too long"


def test_an_invented_specific_after_a_failed_tool_is_the_decline_text():
    state = _state(
        answer=INVENTED, tool_call_attempted=True,
        retrieved_data={"tool_calls": ["search_restaurants"], "tool_results": {"a": tool_error_result("boom")}},
    )
    router = mock.Mock(generate=mock.AsyncMock(return_value={
        "response": json.dumps({"contains_hallucinations": True, "reason": "no data", "specific_claims": ["10 PM"]}),
    }))
    h._runtime.set_llm_router(router)
    with mock.patch("orchestrator.nodes.validate.get_component_config", mock.AsyncMock(return_value={"model_name": "m"})):
        out = _validate(state)
    assert out.validation_passed is False
    assert out.answer == grounding.DECLINE_TEXT and out.is_fallback is True
    # the fact-check model was shown no data, not the error object
    prompt = router.generate.call_args.kwargs["prompt"]
    assert "No data was retrieved" in prompt and "boom" not in prompt


def test_the_fact_check_is_shown_a_bounded_summary_of_the_tool_data():
    big = {"places": [{"name": "n" * 400} for _ in range(60)]}
    state = _state(
        answer="Luigi's Trattoria is open until 10:30 PM.", tool_call_attempted=False, base_knowledge_populated=False,
        retrieved_data={"tool_calls": ["search_restaurants"], "tool_results": {"a": big}},
    )
    state.intent = I.DINING
    # grounded, so the fact-check does not run; force the unsupported path to read the prompt
    router = mock.Mock(generate=mock.AsyncMock(return_value={"response": '{"contains_hallucinations": false}'}))
    h._runtime.set_llm_router(router)
    with mock.patch("orchestrator.nodes.validate.grounded", return_value=False), \
         mock.patch("orchestrator.nodes.validate.get_component_config", mock.AsyncMock(return_value={"model_name": "m"})):
        _validate(state)
    prompt = router.generate.call_args.kwargs["prompt"]
    assert "Tools used: search_restaurants" in prompt
    assert len(prompt) < 6500


def test_a_data_intents_base_knowledge_does_not_vouch_for_specifics():
    state = _state(answer="It will be 72 degrees at 3:30 PM.", intent=I.WEATHER, base_knowledge_populated=True)
    router = mock.Mock(generate=mock.AsyncMock(return_value={
        "response": json.dumps({"contains_hallucinations": True, "reason": "none", "specific_claims": []}),
    }))
    h._runtime.set_llm_router(router)
    with mock.patch("orchestrator.nodes.validate.get_component_config", mock.AsyncMock(return_value={"model_name": "m"})):
        out = _validate(state)
    router.generate.assert_called_once()
    assert out.validation_passed is False


def test_a_data_intent_with_history_does_not_take_the_training_knowledge_bypass():
    state = _state(answer="Luigi's Trattoria has pasta for about $18.", intent=I.DINING,
                   conversation_history=[{"role": "user", "content": "hi"}])
    router = mock.Mock(generate=mock.AsyncMock(return_value={"response": '{"contains_hallucinations": true, "reason": "x"}'}))
    h._runtime.set_llm_router(router)
    with mock.patch("orchestrator.nodes.validate.get_component_config", mock.AsyncMock(return_value={"model_name": "m"})):
        out = _validate(state)
    router.generate.assert_called_once()
    assert out.validation_passed is False


def test_a_first_person_experience_fails_validation_and_declines_on_a_tool_turn():
    state = _state(answer="I've been there and the pasta is wonderful.", tool_call_attempted=True)
    out = _validate(state)
    assert out.validation_passed is False and out.answer == grounding.DECLINE_TEXT


def test_an_empty_answer_on_a_tool_turn_is_left_to_finalize():
    state = _state(answer=None, tool_call_attempted=True)
    out = _validate(state)
    assert out.validation_passed is True and out.answer is None


def test_the_decline_text_survives_finalize():
    state = _state(answer=INVENTED, tool_call_attempted=True)
    state.validation_passed = False
    state.validation_reason = "Hallucination detected: x"
    state.answer, state.is_fallback = grounding.DECLINE_TEXT, True
    with mock.patch("orchestrator.nodes.finalize.maybe_post_synthesis_fallback", mock.AsyncMock(return_value=False)):
        h._runtime.set_cache_client(mock.Mock(set=mock.AsyncMock()))
        out = asyncio.run(finalize_node(state))
    assert out.answer == grounding.DECLINE_TEXT


# --- the tool node runs once per turn -----------------------------------------------------------

def test_a_validation_failure_after_the_tool_node_does_not_loop_back_to_it(monkeypatch):
    calls = []

    async def counting_tool_node(state):
        calls.append(1)
        state.tool_call_attempted = True
        state.answer = INVENTED
        return state

    trigger = mock.AsyncMock(return_value=True)  # the validation-failure trigger fires
    monkeypatch.setattr(h.main, "should_use_tool_calling", trigger)
    state = _state(answer=INVENTED)
    state.validation_passed = False

    async def drive(state):
        route = await h.main.route_after_validate(state)
        for _ in range(5):  # bounded: a loop shows as extra tool-node runs, not a hang
            if route != "tool_call":
                break
            state = await counting_tool_node(state)
            route = await h.main.route_after_validate(state)
        return route

    assert asyncio.run(drive(state)) == "finalize"
    assert len(calls) == 1  # the first validation may send the turn to the tool node once
    trigger.assert_awaited_once()  # and the second never asks the trigger


def test_the_graph_edge_from_the_tool_node_goes_to_validate():
    import ast

    tree = ast.parse(h.MAIN_PY.read_text())
    edges = [
        tuple(a.value for a in node.args[:2])
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "add_edge" and len(node.args) >= 2
        and all(isinstance(a, ast.Constant) for a in node.args[:2])
    ]
    assert ("tool_call", "validate") in edges and ("synthesize", "validate") in edges
    assert ("tool_call", "finalize") not in edges


# --- multi-intent reset ----------------------------------------------------------------------------

def test_a_two_part_query_resets_the_per_intent_flags_for_the_next_part():
    state = _state("what is the weather and find me a pizza place", I.WEATHER, answer="Sunny.")
    state.is_multi_intent = True
    state.intent_parts = ["what is the weather", "find me a pizza place"]
    state.current_intent_index = 0
    state.skip_synthesis = True
    state.tool_call_attempted = True
    state.identity_answer = True
    h._runtime.set_cache_client(mock.Mock(set=mock.AsyncMock()))
    out = asyncio.run(finalize_node(state))
    assert out.query == "find me a pizza place" and out.current_intent_index == 1
    assert (out.skip_synthesis, out.tool_call_attempted, out.identity_answer) == (False, False, False)
    assert out.answer is None and out.intent is None and out.retrieved_data == {}


def test_the_second_part_reaches_the_tool_node_again(monkeypatch):
    """With the flag reset, the validation route after part two behaves like a first turn."""
    state = _state("find me a pizza place", I.DINING)
    state.tool_call_attempted = False
    state.validation_passed = False
    monkeypatch.setattr(h.main, "should_use_tool_calling", mock.AsyncMock(return_value=True))
    assert asyncio.run(h.main.route_after_validate(state)) == "tool_call"
    state.tool_call_attempted = True
    assert asyncio.run(h.main.route_after_validate(state)) == "finalize"


# --- real-stack smoke: a real tool call against a fake RAG HTTP server -------------------------------

class _FakeDining(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):  # noqa: N802
        type(self).seen.append(self.path)
        body = json.dumps({"places": [{"name": "Test Trattoria", "rating": 4.5}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_rag():
    _FakeDining.seen = []
    server = HTTPServer(("127.0.0.1", 0), _FakeDining)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", _FakeDining
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_real_stack_a_forced_place_search_reaches_a_fake_rag_server(monkeypatch, fake_rag):
    """The model emits no tool; the real tool node forces the call; the real executor
    and the real RAG client send it over HTTP to a server; the answer is grounded."""
    from orchestrator.rag_client import RAGClient

    from ._model_safe_harness import REAL_EXECUTE_TOOLS_PARALLEL

    url, server = fake_rag
    admin, _execute, _k, _h = h.patch_tool_call_dependencies(monkeypatch)
    admin.get_api_keys_for_tool = mock.AsyncMock(return_value={})
    admin.record_tool_metric = mock.AsyncMock()
    monkeypatch.setattr(h.main, "execute_tools_parallel", REAL_EXECUTE_TOOLS_PARALLEL)
    monkeypatch.setattr(h.main, "record_tool_execution", mock.MagicMock(), raising=False)

    async def registry(function_name):
        return url

    monkeypatch.setattr("orchestrator.rag_tools.get_tool_service_url_from_registry", registry)
    h._runtime.set_rag_client(RAGClient())
    h._runtime.set_llm_router(h.CapturingLLM(content="Test Trattoria is a nice option."))

    state = _state("recommend an italian place near me")
    out = asyncio.run(h.main.tool_call_node(state))
    asyncio.run(h._runtime.get_rag_client().close()) if hasattr(h._runtime.get_rag_client(), "close") else None

    assert len(server.seen) == 1 and server.seen[0].startswith("/dining/search?")
    assert "term=italian+place" in server.seen[0] or "term=italian%20place" in server.seen[0]
    assert grounding.grounded(out)
    assert out.answer == "Test Trattoria is a nice option." and out.is_fallback is False
    assert out.tool_call_attempted is True
    assert _validate(out).validation_passed is True


def test_real_stack_a_dead_rag_server_gives_the_decline_for_an_invented_answer(monkeypatch):
    from orchestrator.rag_client import RAGClient

    from ._model_safe_harness import REAL_EXECUTE_TOOLS_PARALLEL

    admin, _execute, _k, _h = h.patch_tool_call_dependencies(monkeypatch)
    admin.get_api_keys_for_tool = mock.AsyncMock(return_value={})
    admin.record_tool_metric = mock.AsyncMock()
    monkeypatch.setattr(h.main, "execute_tools_parallel", REAL_EXECUTE_TOOLS_PARALLEL)
    monkeypatch.setattr(h.main, "record_tool_execution", mock.MagicMock(), raising=False)

    async def registry(function_name):
        return "http://127.0.0.1:9"  # the discard port: nothing listens

    monkeypatch.setattr("orchestrator.rag_tools.get_tool_service_url_from_registry", registry)
    h._runtime.set_rag_client(RAGClient())
    h._runtime.set_llm_router(h.CapturingLLM(content=INVENTED))

    out = asyncio.run(h.main.tool_call_node(_state()))
    assert not grounding.grounded(out)
    assert out.answer == grounding.DECLINE_TEXT and out.is_fallback is True


# --- synthesis prompts: the strict no-data prompt and the sports status rule ----------------------------------

HISTORY = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]
SPORTS_DATA = {"team": "Anytown Owls", "status": {"state": "live", "summary": "Owls 3 - 2 Hawks", "winner": None}}


def _synth_prompt(monkeypatch, state):
    admin, _execute, _knowledge, _home = h.patch_tool_call_dependencies(monkeypatch)
    monkeypatch.setattr(h.synthesize_module, "get_component_config",
                        mock.AsyncMock(return_value={"model_name": "m", "backend_type": "ollama"}))
    monkeypatch.setattr(h.synthesize_module, "store_conversation_context", mock.AsyncMock(), raising=False)
    llm = h.CapturingLLM(content="An answer of reasonable length.")
    h._runtime.set_llm_router(llm)
    asyncio.run(h.synthesize_module.synthesize_node(state))
    return llm.text()


def _stream_prompt(monkeypatch, state):
    h.patch_tool_call_dependencies(monkeypatch)
    monkeypatch.setattr(h.main, "get_knowledge_context_for_user", mock.AsyncMock(return_value=""))
    prompt, _model, system = asyncio.run(h.main.build_synthesis_prompt_for_streaming(state))
    return f"{system or ''}\n{prompt}"


BUILDERS = {"graph synthesize": _synth_prompt, "streaming builder": _stream_prompt}


@pytest.mark.parametrize("builder", BUILDERS)
def test_a_data_intent_with_no_data_never_gets_the_built_in_knowledge_prompt(monkeypatch, builder):
    state = _state("find an italian place", I.DINING, conversation_history=HISTORY, answer=None)
    text = BUILDERS[builder](monkeypatch, state)
    assert "You do NOT have access to current or specific information" in text
    assert "your built-in knowledge" not in text


@pytest.mark.parametrize("builder", BUILDERS)
def test_general_info_with_history_still_gets_the_built_in_knowledge_prompt(monkeypatch, builder):
    state = _state("what did I just say", I.UNKNOWN, conversation_history=HISTORY, answer=None)
    text = BUILDERS[builder](monkeypatch, state)
    assert "your built-in knowledge" in text and "You do NOT have access" not in text


@pytest.mark.parametrize("builder", BUILDERS)
def test_a_sports_status_gets_the_in_progress_rule(monkeypatch, builder):
    state = _state("how are the owls doing", I.SPORTS, retrieved_data=SPORTS_DATA, answer=None)
    text = BUILDERS[builder](monkeypatch, state)
    assert "SPORTS STATUS" in text
    assert "never call it the last result" in text and "status.winner" in text


@pytest.mark.parametrize("builder", BUILDERS)
def test_other_data_does_not_get_the_sports_rule(monkeypatch, builder):
    state = _state("weather", I.WEATHER, retrieved_data={"current": {"temp": 70}}, answer=None)
    text = BUILDERS[builder](monkeypatch, state)
    assert "ANTI-HALLUCINATION" in text and "SPORTS STATUS" not in text


# --- the cache clauses in /query ---------------------------------------------------------------------------------

class _MemoryCache:
    def __init__(self):
        self.data = {}
        self.sets = 0

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ttl=None):
        self.sets += 1
        self.data[key] = value


class _Graph:
    def __init__(self, **result):
        self.result = result
        self.calls = 0

    async def ainvoke(self, state):
        self.calls += 1
        base = {"intent": I.WEATHER, "answer": "It is sunny and warm today.", "confidence": 1.0, "citations": [],
                "request_id": "r", "node_timings": {}, "validation_passed": True,
                "retrieved_data": {"current": {"temperature": 70}}}
        return {**base, **self.result}


def _ask(monkeypatch, cache, query="what's the weather in Anytown today", **result):
    from fastapi.testclient import TestClient

    h.patch_conversation_config(monkeypatch)
    monkeypatch.setattr(h.main, "_direct_general_info_response", lambda q: True)
    h.install_mode_client(server_mode="owner")
    monkeypatch.setattr(h.main, "get_admin_client", lambda: h.fake_admin_client())
    monkeypatch.setattr(semantic_cache_module, "get_cache_client", lambda: cache)
    graph = _Graph(**result)
    monkeypatch.setattr(h.main, "orchestrator_graph", graph)
    response = TestClient(h.main.app).post(
        "/query", json={"query": query, "caller_trust": "household", "interface_type": "chat"}, headers=h.service_headers(),
    )
    assert response.status_code == 200
    _wait_for_sets(cache)
    return response.json()["answer"], graph


def _wait_for_sets(cache, window=0.25):
    """The write is a background task: wait for it to land, or for the window to end."""
    import time

    before, deadline = cache.sets, time.time() + window
    while cache.sets == before and time.time() < deadline:
        time.sleep(0.01)


def test_a_grounded_turn_is_stored_and_replayed(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache)
    assert cache.sets == 1, "floor: the cache clauses can say yes"
    answer, graph = _ask(monkeypatch, cache, answer="SECOND")
    assert answer == "It is sunny and warm today." and graph.calls == 0
    (entry,) = cache.data.values()
    assert entry["_cache_metadata"]["grounded"] is True


def test_a_tool_owned_turn_without_tool_data_is_not_stored(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache, retrieved_data={"tool_calls": ["get_weather"], "tool_results": {"a": tool_error_result("down")}})
    assert cache.sets == 0


def test_a_decline_is_not_stored(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache, answer=grounding.DECLINE_TEXT, is_fallback=True)
    assert cache.sets == 0


@pytest.mark.parametrize("intent", [I.WEBSEARCH, I.MUSIC_PLAY, I.TV_CONTROL, I.TEXT_ME_THAT, I.UNKNOWN])
def test_an_unmapped_intent_is_not_stored(monkeypatch, intent):
    cache = _MemoryCache()
    _ask(monkeypatch, cache, intent=intent)
    assert cache.sets == 0


def test_a_follow_up_turn_is_not_stored(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache, continuation_decision={"decision": "continued", "reason": "open_task"})
    assert cache.sets == 0
    _ask(monkeypatch, cache, resolved_request="what about tomorrow")
    assert cache.sets == 0
    _ask(monkeypatch, cache, continuation_decision={"decision": "declined", "reason": "new topic"})
    assert cache.sets == 1  # the control: a declined continuation is a fresh question


def test_a_general_knowledge_answer_is_stored_but_a_local_business_request_is_not(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache, query="what is a stock split", intent=I.GENERAL_INFO, answer="A stock split divides one share into several.", retrieved_data={})
    assert cache.sets == 1
    _ask(monkeypatch, cache, query="recommend an italian place near me", intent=I.GENERAL_INFO, answer="Try an Italian kitchen downtown.", retrieved_data={})
    assert cache.sets == 1


def test_a_bare_reply_never_reads_the_cache(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache, query="yes")
    assert cache.sets == 1
    _answer, graph = _ask(monkeypatch, cache, query="yes", answer="SECOND")
    assert graph.calls == 1  # a hit would have answered with calls == 0
    _answer, graph = _ask(monkeypatch, cache, query="what's the weather in Anytown today")
    _answer, graph = _ask(monkeypatch, cache, query="what's the weather in Anytown today")
    assert graph.calls == 0  # the control: a full question still hits


def test_an_entry_stored_before_the_grounding_flag_is_not_replayed(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache)
    (entry,) = cache.data.values()
    del entry["_cache_metadata"]["grounded"]  # written by the release before this one
    _answer, graph = _ask(monkeypatch, cache, answer="FRESH")
    assert graph.calls == 1


def test_an_entry_stored_under_a_no_longer_cacheable_intent_is_not_replayed(monkeypatch):
    cache = _MemoryCache()
    _ask(monkeypatch, cache)
    (entry,) = cache.data.values()
    entry["intent"] = "websearch"
    _answer, graph = _ask(monkeypatch, cache, answer="FRESH")
    assert graph.calls == 1
