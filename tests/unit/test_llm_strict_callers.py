"""The smart-home JSON callers demand the whole prompt and fail closed.

`extract_intent`, `extract_sequence_intent` and `_extract_motion_control_intent`
pick a device and a write from the model's JSON, so each passes
`require_full_prompt=True` to the router, and a refusal, a cut-off prompt, an
answer that hit its token limit or JSON that isn't an object gives
"unprocessable": a fixed phrase and zero Home Assistant writes. It never falls
into the JSON-decode fallback, which turns an imperative into turn_on/turn_off.

Two halves: AST pins on the call sites (with planted negatives), and the whole
path through ``route_control_node`` over a fictional house, with writes read off
the recording client below the permission guard.
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest
from structlog.testing import capture_logs

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import room_context  # noqa: E402
from shared.llm_context import (  # noqa: E402
    ContextBudgetExceeded,
    ContextMarks,
    ContextPlan,
    ContextSettings,
    plan_context,
)

from ._room_chat_harness import Chat  # noqa: E402
from .test_access_intent_policy import DoorChat  # noqa: E402
from .test_llm_router_context_wiring import MODEL, Wire  # noqa: E402

FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
STRICT_CALLERS = {"extract_intent", "extract_sequence_intent", "_extract_motion_control_intent"}
CONTROLLER = SRC / "orchestrator" / "smart_home_controller.py"

LIGHT_QUERY = "make the study lights go on"
SEQUENCE_QUERY = "turn on the study lights then turn them off in 5 seconds"
MOTION_QUERY = "disable motion in the study"
UNLOCK_QUERY = "would you mind unlocking the front door"
LIGHT_ON = {"device_type": "light", "room": "study", "action": "turn_on", "target_scope": "group", "parameters": {}}
SEQUENCE = {
    "type": "sequence", "acknowledge": "On, then off.",
    "steps": [
        {"action": "turn_on", "target": {"device_type": "light", "room": "study"}, "parameters": {}, "delay_after": 5, "at_time": None},
        {"action": "turn_off", "target": {"device_type": "light", "room": "study"}, "parameters": {}, "delay_after": 0, "at_time": None},
    ],
}
MOTION = {"device_type": "motion_control", "room": "office", "action": "disable_motion", "parameters": {}, "reason": "off"}
UNLOCK_FRONT = {"device_type": "lock", "room": "front_door", "action": "unlock", "target_scope": "group", "parameters": {}}


# ---------------------------------------------------------------------------
# The call sites
# ---------------------------------------------------------------------------

def generate_calls(source: str):
    """(enclosing function, call) for every `.generate(` call in `source`."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "generate":
            fn = node
            while fn in parents and not isinstance(fn, FUNCTIONS):
                fn = parents[fn]
            found.append((fn.name, node))
    return found


def strict_keyword(call: ast.Call):
    return next((k for k in call.keywords if k.arg == "require_full_prompt"), None)


def literal_strict_functions(source: str) -> set:
    """Functions with a `.generate(` call that passes the literal True."""
    return {
        name for name, call in generate_calls(source)
        if (kw := strict_keyword(call)) is not None and isinstance(kw.value, ast.Constant) and kw.value.value is True
    }


def non_literal_strict_calls(source: str) -> list:
    """Calls that mention the keyword with anything but the literal True."""
    return [
        (name, call.lineno) for name, call in generate_calls(source)
        if (kw := strict_keyword(call)) is not None and not (isinstance(kw.value, ast.Constant) and kw.value.value is True)
    ]


def broad_handler_before_budget(source: str, function: str) -> list:
    """Try statements around a strict generate call where ContextBudgetExceeded
    isn't the first handler (a broad `except Exception` would otherwise catch it)."""
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, FUNCTIONS) and n.name == function)
    bad = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Try) and any(
            isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "generate" and strict_keyword(c)
            for stmt in node.body for c in ast.walk(stmt)
        ):
            first = node.handlers[0].type if node.handlers else None
            if not (isinstance(first, ast.Name) and first.id == "ContextBudgetExceeded"):
                bad.append(node.lineno)
    return bad


def test_population_floor_and_named_members():
    callers = literal_strict_functions(CONTROLLER.read_text(encoding="utf-8"))
    assert len(callers) >= 3
    assert "extract_intent" in callers


def test_exactly_the_three_smart_home_callers_demand_the_full_prompt():
    strict = set()
    for path in sorted((SRC).rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "require_full_prompt" not in source:
            continue
        assert non_literal_strict_calls(source) == [] or path.name == "llm_router.py", path
        strict |= {(path.relative_to(SRC).as_posix(), name) for name in literal_strict_functions(source)}
    assert strict == {("orchestrator/smart_home_controller.py", name) for name in STRICT_CALLERS}


def test_the_occupancy_call_and_the_retry_stay_default():
    source = CONTROLLER.read_text(encoding="utf-8")
    for name, call in generate_calls(source):
        if name in ("_llm_occupancy_reasoning", "_retry_without_full_prompt"):
            assert strict_keyword(call) is None, f"{name} must call generate with the default"
    assert {n for n, _ in generate_calls(source)} >= {"_llm_occupancy_reasoning", "_retry_without_full_prompt"}


@pytest.mark.parametrize("name", sorted(STRICT_CALLERS))
def test_budget_refusal_is_caught_before_any_broad_handler(name):
    assert broad_handler_before_budget(CONTROLLER.read_text(encoding="utf-8"), name) == []


class TestPlantedNegatives:
    TEMPLATE = "class C:\n    async def {fn}(self):\n        return await self.llm_router.generate(model=1, {kw})\n"

    def test_a_fourth_strict_caller_is_seen(self):
        source = self.TEMPLATE.format(fn="extract_fourth", kw="require_full_prompt=True")
        assert literal_strict_functions(source) == {"extract_fourth"}

    def test_a_caller_without_the_keyword_is_not_strict(self):
        assert literal_strict_functions(self.TEMPLATE.format(fn="extract_intent", kw="max_tokens=1")) == set()

    @pytest.mark.parametrize("value", ["False", "strict", "not False"])
    def test_a_non_literal_or_false_keyword_is_not_strict_and_is_reported(self, value):
        source = self.TEMPLATE.format(fn="extract_intent", kw=f"require_full_prompt={value}")
        assert literal_strict_functions(source) == set()
        assert non_literal_strict_calls(source) == [("extract_intent", 3)]

    def test_a_broad_handler_first_is_seen(self):
        source = (
            "class C:\n    async def f(self):\n        try:\n"
            "            await self.llm_router.generate(require_full_prompt=True)\n"
            "        except Exception:\n            pass\n"
            "        except ContextBudgetExceeded:\n            pass\n"
        )
        assert broad_handler_before_budget(source, "f") == [3]
        good = (
            "class C:\n    async def f(self):\n        try:\n"
            "            await self.llm_router.generate(require_full_prompt=True)\n"
            "        except ContextBudgetExceeded:\n            pass\n"
            "        except Exception:\n            pass\n"
        )
        assert broad_handler_before_budget(good, "f") == []


# ---------------------------------------------------------------------------
# The whole path: model double -> extract -> route_control -> Home Assistant
# ---------------------------------------------------------------------------

def plan_for_exceeded() -> ContextPlan:
    return ContextPlan(send=None, effective=None, estimate=9000, reserve=400, outcome="over_ceiling")


class Scripted:
    """A router double. ``strict`` is what a strict call does: ("raise", reason),
    ("reply", text-or-dict, stop_reason). ``default`` answers a call without the
    full-prompt demand."""

    def __init__(self, strict, default=None):
        self.strict = strict
        self.default = default
        self.calls = []

    @staticmethod
    def respond(spec):
        kind = spec[0]
        if kind == "raise":
            raise ContextBudgetExceeded(plan_for_exceeded(), spec[1])
        body = spec[1] if isinstance(spec[1], str) else json.dumps(spec[1])
        response = {"response": body, "backend": "ollama"}
        if len(spec) > 2:
            response["stop_reason"] = spec[2]
        return response

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        strict = kwargs.get("require_full_prompt", False)
        spec = self.strict if strict else self.default
        if spec is None:
            raise AssertionError("an unexpected non-strict call")
        return self.respond(spec)


def writes(chat):
    return [(c.args[0], c.args[1]) for c in chat.rig.raw.call_service.await_args_list]


def light_chat(monkeypatch, llm):
    chat = Chat(monkeypatch)
    chat.rig.controller.llm_router = llm
    return chat


def door_chat(monkeypatch, llm):
    door = DoorChat(monkeypatch)
    door.chat.rig.controller.llm_router = llm
    return door


CASES = {
    "intent": (LIGHT_QUERY, LIGHT_ON),
    "sequence": (SEQUENCE_QUERY, SEQUENCE),
    "motion": (MOTION_QUERY, MOTION),
}
REFUSALS = [
    ("raise", "over_ceiling"), ("raise", "truncated"), ("raise", "floor_unknown"),
]


@pytest.mark.parametrize("caller", sorted(CASES))
@pytest.mark.parametrize("spec", REFUSALS + [("reply", LIGHT_ON, "length")], ids=lambda s: "-".join(map(str, s[:2]))[:30])
def test_a_refused_or_cut_off_call_is_unprocessable_with_zero_writes(monkeypatch, caller, spec):
    query, _ = CASES[caller]
    llm = Scripted(spec)
    chat = light_chat(monkeypatch, llm)
    out = chat.turn(query)
    assert llm.calls, "the model call was made: the query reached the strict caller"
    assert llm.calls[0]["require_full_prompt"] is True
    assert out.answer == room_context.UNPROCESSABLE_TEXT
    assert writes(chat) == []
    assert chat.stores == [], "an unprocessable turn stores no context"


@pytest.mark.parametrize("spec", REFUSALS + [("reply", UNLOCK_FRONT, "length")], ids=lambda s: "-".join(map(str, s[:2]))[:30])
def test_an_unlock_with_the_budget_exceeded_opens_nothing(monkeypatch, spec):
    llm = Scripted(spec)
    door = door_chat(monkeypatch, llm)
    out = door.say(UNLOCK_QUERY)
    assert llm.calls, "the unlock reached the model"
    assert out.answer == room_context.UNPROCESSABLE_TEXT
    assert door.calls == []


def test_positive_control_the_same_queries_act_when_the_model_answers_whole(monkeypatch):
    for query, reply, expected in (
        (LIGHT_QUERY, LIGHT_ON, "light"),
        (MOTION_QUERY, MOTION, "input_boolean"),
    ):
        llm = Scripted(("reply", reply, "stop"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn(query)
        assert out.answer != room_context.UNPROCESSABLE_TEXT
        assert any(domain == expected for domain, _ in writes(chat)), (query, writes(chat))
    llm = Scripted(("reply", SEQUENCE, "stop"))
    out = light_chat(monkeypatch, llm).turn(SEQUENCE_QUERY)
    assert out.answer == "Sequence executor not available.", "the sequence got as far as the executor"
    llm = Scripted(("reply", UNLOCK_FRONT, "stop"))
    door = door_chat(monkeypatch, llm)
    door.say(UNLOCK_QUERY)
    assert door.openings == [("lock", "unlock", "lock.front_door")]


@pytest.mark.parametrize("caller", sorted(CASES))
@pytest.mark.parametrize("junk", ["[1, 2]", '"turn_on"', "42", "null"])
def test_json_that_is_not_an_object_is_unprocessable(monkeypatch, caller, junk):
    query, _ = CASES[caller]
    llm = Scripted(("reply", junk, "stop"))
    chat = light_chat(monkeypatch, llm)
    out = chat.turn(query)
    assert llm.calls
    assert out.answer == room_context.UNPROCESSABLE_TEXT
    assert writes(chat) == []


def test_plain_unparseable_text_on_an_ollama_call_keeps_the_fallback(monkeypatch):
    llm = Scripted(("reply", "turn it on, sure", "stop"))
    chat = light_chat(monkeypatch, llm)
    out = chat.turn(LIGHT_QUERY)
    assert out.answer != room_context.UNPROCESSABLE_TEXT
    assert writes(chat), "today's IMPERATIVE fallback still acts when the whole prompt was read"


@pytest.mark.parametrize("caller", sorted(CASES))
def test_the_direct_extract_result_is_the_closed_unprocessable_shape(monkeypatch, caller):
    query, _ = CASES[caller]
    llm = Scripted(("raise", "over_ceiling"))
    chat = light_chat(monkeypatch, llm)
    controller = chat.rig.controller
    call = {
        "intent": lambda: controller.extract_intent(query),
        "sequence": lambda: controller.extract_sequence_intent(query),
        "motion": lambda: controller._extract_motion_control_intent(query),
    }[caller]
    assert asyncio.run(call()) == {"unprocessable": True, "unprocessable_reason": "budget"}


# ---------------------------------------------------------------------------
# MLX and cloud: the guarantee is Ollama-only (D22)
# ---------------------------------------------------------------------------

class TestBackendsThatCannotHonourTheDemand:
    def test_one_non_strict_retry_then_the_answer_is_acted_on(self, monkeypatch):
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", LIGHT_ON, "stop"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn(LIGHT_QUERY)
        assert [c.get("require_full_prompt", False) for c in llm.calls] == [True, False]
        assert out.answer != room_context.UNPROCESSABLE_TEXT
        assert any(domain == "light" for domain, _ in writes(chat))

    @pytest.mark.parametrize("caller", sorted(CASES))
    def test_a_retried_answer_that_hit_its_limit_is_unprocessable(self, monkeypatch, caller):
        query, reply = CASES[caller]
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", reply, "length"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn(query)
        assert len(llm.calls) == 2
        assert out.answer == room_context.UNPROCESSABLE_TEXT and writes(chat) == []

    @pytest.mark.parametrize("caller", sorted(CASES))
    def test_a_retried_answer_that_does_not_parse_is_unprocessable_not_the_fallback(self, monkeypatch, caller):
        query, _ = CASES[caller]
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", "turn on the lights, sure", "stop"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn(query)
        assert out.answer == room_context.UNPROCESSABLE_TEXT and writes(chat) == []

    @pytest.mark.parametrize("caller", sorted(CASES))
    def test_a_retried_non_object_is_unprocessable(self, monkeypatch, caller):
        query, _ = CASES[caller]
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", "[]", "stop"))
        chat = light_chat(monkeypatch, llm)
        assert chat.turn(query).answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []

    def test_only_unsupported_backend_is_retried(self, monkeypatch):
        for reason in ("over_ceiling", "truncated", "floor_unknown"):
            llm = Scripted(("raise", reason), default=("reply", LIGHT_ON, "stop"))
            chat = light_chat(monkeypatch, llm)
            assert chat.turn(LIGHT_QUERY).answer == room_context.UNPROCESSABLE_TEXT
            assert len(llm.calls) == 1, reason
            assert writes(chat) == []

    def test_the_warning_is_logged_once_per_process_without_prompt_text(self, monkeypatch):
        monkeypatch.setattr(shc, "_full_prompt_warned", False)
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", LIGHT_ON, "stop"))
        chat = light_chat(monkeypatch, llm)
        with capture_logs() as logs:
            chat.turn(LIGHT_QUERY)
            chat.turn(LIGHT_QUERY)
        warnings = [e for e in logs if e["event"] == "smart_home_full_prompt_guarantee_unavailable"]
        assert len(warnings) == 1 and warnings[0]["log_level"] == "warning"
        assert "Ollama-only" in warnings[0]["note"]
        assert LIGHT_QUERY not in json.dumps(logs, default=str)

    def test_a_refusal_logs_the_reason_and_counts_never_the_query(self, monkeypatch):
        marker = "PLANTED-QUERY-MARKER-91c2"
        llm = Scripted(("raise", "over_ceiling"))
        chat = light_chat(monkeypatch, llm)
        with capture_logs() as logs:
            chat.turn(f"make the study lights go on {marker}")
        refused = [e for e in logs if e["event"] == "smart_home_llm_refused"]
        assert len(refused) == 1 and refused[0]["reason"] == "over_ceiling"
        assert marker not in json.dumps(logs, default=str)

    @pytest.mark.parametrize("backend", ["mlx", "openai", "anthropic", "google"])
    def test_a_real_router_with_the_component_on_that_backend_serves_the_command(self, monkeypatch, backend):
        wire = Wire(monkeypatch)

        async def backend_config(model):
            return {"backend_type": backend, "endpoint_url": "http://backend.test", "timeout_seconds": 30}

        served = []

        async def serve(*args, **kwargs):
            served.append(backend)
            return {"response": json.dumps(LIGHT_ON), "backend": backend, "stop_reason": "stop"}

        async def creds(provider):
            return {"api_key": "k"}

        monkeypatch.setattr(wire.router, "_get_backend_config", backend_config)
        monkeypatch.setattr(wire.router, "_get_cloud_credentials", creds)
        for attr in ("_generate_mlx", "_generate_openai", "_generate_anthropic", "_generate_google"):
            monkeypatch.setattr(wire.router, attr, serve)
        chat = light_chat(monkeypatch, wire.router)
        out = chat.turn(LIGHT_QUERY)
        assert served == [backend], "one backend call: the strict attempt was refused before any request"
        assert wire.requests == [], "nothing went to Ollama"
        assert any(domain == "light" for domain, _ in writes(chat))
        assert out.answer != room_context.UNPROCESSABLE_TEXT

    @pytest.mark.parametrize("backend", ["mlx", "openai"])
    def test_a_real_router_on_that_backend_whose_answer_hit_its_limit_writes_nothing(self, monkeypatch, backend):
        wire = Wire(monkeypatch)

        async def backend_config(model):
            return {"backend_type": backend, "endpoint_url": "http://backend.test", "timeout_seconds": 30}

        async def serve(*args, **kwargs):
            return {"response": json.dumps(LIGHT_ON), "backend": backend, "stop_reason": "length"}

        async def creds(provider):
            return {"api_key": "k"}

        monkeypatch.setattr(wire.router, "_get_backend_config", backend_config)
        monkeypatch.setattr(wire.router, "_get_cloud_credentials", creds)
        monkeypatch.setattr(wire.router, "_generate_mlx", serve)
        monkeypatch.setattr(wire.router, "_generate_openai", serve)
        chat = light_chat(monkeypatch, wire.router)
        assert chat.turn(LIGHT_QUERY).answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []


# ---------------------------------------------------------------------------
# Input bounds
# ---------------------------------------------------------------------------

def padded(base: str, size: int, filler: str = " really") -> str:
    """``base`` extended with ``filler`` to at most ``size`` UTF-8 bytes (exactly, for ASCII)."""
    out = base
    while room_context.utf8_len(out) < size:
        out += filler
    return room_context.cut_bytes(out, size)


class TestInputBounds:
    @pytest.fixture
    def controller(self, monkeypatch):
        chat = light_chat(monkeypatch, Scripted(("reply", LIGHT_ON, "stop")))
        return chat, chat.rig.controller

    def test_1000_bytes_reach_the_model(self, controller):
        chat, ctl = controller
        query = padded(LIGHT_QUERY, 1000)
        assert room_context.utf8_len(query) == 1000
        intent = asyncio.run(ctl.extract_intent(query))
        assert len(ctl.llm_router.calls) == 1 and not intent.get("unprocessable")

    def test_1001_bytes_are_refused_before_any_call(self, controller):
        chat, ctl = controller
        intent = asyncio.run(ctl.extract_intent(padded(LIGHT_QUERY, 1001)))
        assert intent == {"unprocessable": True, "unprocessable_reason": "too_long"}
        assert ctl.llm_router.calls == []

    @pytest.mark.parametrize("ch,fits,over", [("é", 500, 501), ("日", 333, 334), ("😀", 250, 251)], ids=["2-byte", "3-byte", "4-byte"])
    def test_the_cap_counts_utf8_bytes_not_characters(self, controller, ch, fits, over):
        chat, ctl = controller
        assert asyncio.run(ctl.extract_intent(LIGHT_QUERY + " " + ch * over)) == {"unprocessable": True, "unprocessable_reason": "too_long"}
        assert ctl.llm_router.calls == []
        asyncio.run(ctl.extract_intent(LIGHT_QUERY + " " + ch * ((1000 - len(LIGHT_QUERY) - 1) // len(ch.encode()))))
        assert len(ctl.llm_router.calls) == 1

    def test_a_long_query_is_refused_before_the_fast_paths(self, controller):
        """A command a fast path would answer by itself, padded past the cap."""
        chat, ctl = controller
        intent = asyncio.run(ctl.extract_intent(padded("turn on the study lights", 1001)))
        assert intent == {"unprocessable": True, "unprocessable_reason": "too_long"}

    @pytest.mark.parametrize("caller", ["sequence", "motion"])
    def test_the_other_two_callers_apply_the_same_cap(self, controller, caller):
        chat, ctl = controller
        call = {
            "sequence": lambda q: ctl.extract_sequence_intent(q),
            "motion": lambda q: ctl._extract_motion_control_intent(q),
        }[caller]
        assert asyncio.run(call(padded("x", 1001))) == {"unprocessable": True, "unprocessable_reason": "too_long"}
        assert ctl.llm_router.calls == []
        asyncio.run(call(padded("x", 1000)))
        assert len(ctl.llm_router.calls) == 1

    def test_an_over_cap_turn_gets_the_too_long_phrase_writes_nothing_and_stores_nothing(self, monkeypatch):
        llm = Scripted(("reply", LIGHT_ON, "stop"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn(padded(LIGHT_QUERY, 1001))
        assert out.answer == room_context.QUERY_TOO_LONG_TEXT == "That's too long for a device command."
        assert llm.calls == [] and writes(chat) == [] and chat.stores == []

    def test_a_100k_query_moves_no_high_water_mark_and_sends_nothing(self, monkeypatch):
        wire = Wire(monkeypatch)
        chat = light_chat(monkeypatch, wire.router)
        out = chat.turn(padded(LIGHT_QUERY, 100_000))
        assert out.answer == room_context.QUERY_TOO_LONG_TEXT
        assert wire.requests == [] and wire.router._context_marks.get("llama3.1:8b", 2048) == 0

    def test_a_stored_context_for_a_long_query_is_not_written_by_the_control_store(self, monkeypatch):
        """Past the fast paths a long query is refused, but the store site also
        refuses on its own: a fast-path answer to a long utterance stores none."""
        chat = light_chat(monkeypatch, Scripted(("reply", LIGHT_ON, "stop")))
        long_query = padded("turn on the study lights please", 1500)
        chat.turn(long_query)
        assert all(room_context.utf8_len(s["query"]) <= room_context.SMART_HOME_MAX_QUERY_BYTES for s in chat.stores)

    @staticmethod
    def follow_up(monkeypatch, *, prev_query, prev_response, entities, ch="x"):
        """The prompt a follow-up sends, and the router's high-water mark, with
        a real router over a mock Ollama socket."""
        prompts = []

        def responder(body):
            prompts.append(body)
            return httpx.Response(
                200,
                json={"response": json.dumps(LIGHT_ON), "done": True, "done_reason": "stop",
                      "prompt_eval_count": 5000, "eval_count": 5},
            )

        wire = Wire(monkeypatch, responder=responder)
        chat = light_chat(monkeypatch, wire.router)
        query = padded(LIGHT_QUERY + " ", 1000, ch)
        asyncio.run(
            chat.rig.controller.extract_intent(
                query, prev_query=prev_query, prev_response=prev_response, prev_intent_entities=entities,
            )
        )
        return wire, prompts

    def test_a_20k_stored_prev_query_is_cut_and_the_mark_stays_at_most_8192(self, monkeypatch):
        marker = "PREV-TAIL-MARKER-5e1"
        wire, prompts = self.follow_up(
            monkeypatch,
            prev_query="p" * 20_000 + marker,
            prev_response="r" * 20_000 + marker,
            entities={"device_type": "d" * 500, "room": "r" * 500, "action": "a" * 500, "parameters": {"k": "v" * 500}},
        )
        prompt = prompts[0]["prompt"]
        assert marker not in prompt
        assert prompt.count("p") < 20_000
        assert "p" * 401 not in prompt and "r" * 401 not in prompt
        assert "d" * 101 not in prompt and "a" * 101 not in prompt and "v" * 101 not in prompt
        assert prompts[0]["options"]["num_ctx"] <= 8192
        assert wire.router._context_marks.get("llama3.1:8b", 2048) <= 8192

    def test_the_prev_text_is_cut_at_exactly_400_characters(self, monkeypatch):
        wire, prompts = self.follow_up(
            monkeypatch, prev_query="q" * 400 + "Q" * 50, prev_response="s" * 400 + "S" * 50,
            entities={"device_type": "light", "room": "study", "action": "turn_on", "parameters": {}},
        )
        prompt = prompts[0]["prompt"]
        assert 'Previous request: "' + "q" * 400 + '"' in prompt
        assert 'Previous response: "' + "s" * 400 + '"' in prompt
        assert "Q" not in prompt.split("Previous request:")[1].split("Previous response:")[0]

    def test_the_prev_intent_is_four_fields_each_cut_at_100(self, monkeypatch):
        wire, prompts = self.follow_up(
            monkeypatch, prev_query="a", prev_response="b",
            entities={"device_type": "D" * 150, "room": "R" * 150, "action": "A" * 150,
                      "parameters": {"k": "V" * 150}, "extra": "EXTRA-FIELD", "decided_room": "forged"},
        )
        prompt = prompts[0]["prompt"]
        assert "D" * 100 in prompt and "D" * 101 not in prompt
        assert "R" * 100 in prompt and "R" * 101 not in prompt
        assert "A" * 100 in prompt and "A" * 101 not in prompt
        assert "V" * 101 not in prompt
        assert "EXTRA-FIELD" not in prompt and "forged" not in prompt

    @pytest.mark.parametrize("ch", ["x", "é", "日", "😀"], ids=["ascii", "2-byte", "3-byte", "4-byte"])
    def test_the_worst_case_plan_is_at_most_8192_for_every_prompt(self, monkeypatch, ch):
        """1000 bytes of query (it appears twice in a follow-up), 400 bytes of
        each previous turn, four 100-byte intent fields: whatever the encoding,
        a 2048 row plans no more than 8192 for all three prompts."""
        prompts = {}

        class Capture:
            async def generate(self, **kwargs):
                prompts[kwargs["max_tokens"]] = kwargs["prompt"]
                return {"response": "{}", "stop_reason": "stop"}

        chat = light_chat(monkeypatch, Capture())
        ctl = chat.rig.controller
        query = padded("make the study lights go on ", 1000, ch)
        asyncio.run(ctl.extract_intent(
            query, prev_query=ch * 20_000, prev_response=ch * 20_000,
            prev_intent_entities={"device_type": ch * 500, "room": ch * 500, "action": ch * 500, "parameters": {"k": ch * 500}},
        ))
        asyncio.run(ctl.extract_sequence_intent(padded("turn on the lights then off ", 1000, ch)))
        asyncio.run(ctl._extract_motion_control_intent(padded("disable motion ", 1000, ch)))
        assert set(prompts) == {400, 1500, 300}
        for max_tokens, prompt in prompts.items():
            plan = plan_context(
                prompt_text="/no_think\n\n" + prompt, row_ctx=2048, num_predict=max(384, max_tokens), max_tokens=max_tokens,
                require_full=True, marks=ContextMarks(), model="m",
                settings=ContextSettings(mode="enforce", ceiling=16384, server_default=0),
            )
            assert plan.send is not None and plan.send <= 8192, (max_tokens, plan.send, plan.estimate)

    def test_previous_turns_are_cut_at_400_bytes_not_characters(self, monkeypatch):
        wire, prompts = self.follow_up(
            monkeypatch, prev_query="é" * 300, prev_response="日" * 300,
            entities={"device_type": "é" * 80, "room": "light", "action": "turn_on", "parameters": {}},
        )
        prompt = prompts[0]["prompt"]
        assert "é" * 200 in prompt and "é" * 201 not in prompt.split("Previous response:")[0].split("Previous device")[0]
        assert "日" * 133 in prompt and "日" * 134 not in prompt
        assert "é" * 50 in prompt and "é" * 51 not in prompt.split("Previous device controlled:")[1]


# ---------------------------------------------------------------------------
# A whole prompt, an unparseable answer: nothing writes unless the words do
# ---------------------------------------------------------------------------

CALMER_QUERY = "set the study lights to a calmer look"
GARBLED = "this is not json at all"


class TestUnparseableAnswers:
    def test_a_sequence_with_garbled_json_writes_nothing(self, monkeypatch):
        llm = Scripted(("reply", GARBLED, "stop"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn(SEQUENCE_QUERY)
        assert llm.calls and llm.calls[0]["require_full_prompt"] is True
        assert out.answer == room_context.UNPROCESSABLE_TEXT and writes(chat) == []

    @pytest.mark.parametrize("query", [MOTION_QUERY, "turn off motion detection", "disable motion in the study after coffee"])
    def test_motion_with_garbled_json_writes_nothing(self, monkeypatch, query):
        """The old fallback matched 'off' inside "coffee" and picked a helper to switch."""
        llm = Scripted(("reply", GARBLED, "stop"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn(query)
        assert llm.calls, "the motion path reached the model"
        assert out.answer == room_context.UNPROCESSABLE_TEXT and writes(chat) == []

    def test_an_imperative_with_an_explicit_target_still_acts(self, monkeypatch):
        llm = Scripted(("reply", GARBLED, "stop"))
        chat = light_chat(monkeypatch, llm)
        chat.turn(LIGHT_QUERY)
        assert ("light", "turn_on") in writes(chat)

    def test_an_imperative_without_one_asks_for_a_status_instead_of_guessing(self, monkeypatch):
        """No explicit on/off in the words: the old substring legs are gone."""
        llm = Scripted(("reply", GARBLED, "stop"))
        chat = light_chat(monkeypatch, llm)
        controller = chat.rig.controller
        from orchestrator.utterance_kind import classify_utterance
        assert classify_utterance(CALMER_QUERY).kind.name == "IMPERATIVE" and classify_utterance(CALMER_QUERY).target_state is None
        intent = asyncio.run(controller.extract_intent(CALMER_QUERY))
        assert llm.calls and intent["action"] == "get_status"
        chat.turn(CALMER_QUERY)
        assert writes(chat) == []

    def test_a_substring_that_looks_like_on_does_not_turn_anything_on(self, monkeypatch):
        """An exclusion the room parser can't resolve sends this to the model; with
        garbled JSON and no explicit state the words alone justify no write."""
        llm = Scripted(("reply", GARBLED, "stop"))
        chat = light_chat(monkeypatch, llm)
        from orchestrator.utterance_kind import classify_utterance
        query = "make the lights lit except the mudroom"
        assert classify_utterance(query).kind.name == "IMPERATIVE" and classify_utterance(query).target_state is None
        chat.turn(query)
        assert llm.calls and writes(chat) == []

    @pytest.mark.parametrize("caller", sorted(CASES))
    def test_every_json_decode_branch_writes_nothing_unless_explicitly_targeted(self, monkeypatch, caller):
        query, _ = CASES[caller]
        llm = Scripted(("reply", GARBLED, "stop"))
        chat = light_chat(monkeypatch, llm)
        chat.turn(query)
        explicit = caller == "intent"  # LIGHT_QUERY says "go on"
        assert bool(writes(chat)) == explicit


# ---------------------------------------------------------------------------
# After a non-strict retry the answer must match the user's own words
# ---------------------------------------------------------------------------

OFFICE_ON = {"device_type": "light", "room": "office", "action": "turn_on", "target_scope": "group", "parameters": {}}


LOCK_FRONT = {"device_type": "lock", "room": "front_door", "action": "lock", "target_scope": "group", "parameters": {}}
LOCK_QUERY = "would you mind locking the front door lock"




class TestNonOllamaRetryIsWordChecked:
    """The explicit trade-off of D22: on MLX or a cloud model an answer is acted on only when the
    user's own words agree with it. Phrasings the classifier can't read as a command get a refusal."""


    def retried(self, monkeypatch, reply, *, extra=None):
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", reply, "stop"))
        return light_chat(monkeypatch, llm), llm

    def test_a_prompt_example_echoed_for_a_query_that_says_nothing_of_it_writes_nothing(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, OFFICE_ON)
        out = chat.turn("make the hall lights brighter please")
        assert len(llm.calls) == 2 or not llm.calls  # fast paths may answer a plain "brighter"
        out = chat.turn("set the study lights to a calmer look")
        assert len(llm.calls) >= 2
        assert out.answer == room_context.UNPROCESSABLE_TEXT and writes(chat) == []

    def test_the_opposite_state_is_refused(self, monkeypatch):
        off = {**LIGHT_ON, "action": "turn_off"}
        chat, llm = self.retried(monkeypatch, off)
        assert chat.turn(LIGHT_QUERY).answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []

    def test_the_wrong_device_is_refused(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, {**LIGHT_ON, "device_type": "climate"})
        assert chat.turn(LIGHT_QUERY).answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []

    def test_a_read_passes(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, {**LIGHT_ON, "action": "get_status"})
        assert chat.turn(LIGHT_QUERY).answer != room_context.UNPROCESSABLE_TEXT

    def test_the_matching_answer_acts(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, LIGHT_ON)
        chat.turn(LIGHT_QUERY)
        assert ("light", "turn_on") in writes(chat)

    def test_an_utterance_that_is_not_a_command_gets_no_write(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, LIGHT_ON)
        out = chat.turn("release the lights in the study somehow")
        assert writes(chat) == []

    def test_a_retried_sequence_for_a_non_command_is_unverified(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, SEQUENCE)
        controller = chat.rig.controller
        got = asyncio.run(controller.extract_sequence_intent("the lights, then off, in a while"))
        assert got == {"unprocessable": True, "unprocessable_reason": "unverified"}

    def test_a_retried_sequence_on_another_device_is_unverified(self, monkeypatch):
        other = {**SEQUENCE, "steps": [{**SEQUENCE["steps"][0], "target": {"device_type": "climate", "room": None}}]}
        chat, llm = self.retried(monkeypatch, other)
        got = asyncio.run(chat.rig.controller.extract_sequence_intent(SEQUENCE_QUERY))
        assert got["unprocessable_reason"] == "unverified"

    def test_a_retried_sequence_that_matches_is_returned(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, SEQUENCE)
        got = asyncio.run(chat.rig.controller.extract_sequence_intent(SEQUENCE_QUERY))
        assert got["steps"]

    def test_a_retried_motion_answer_for_a_non_command_is_unverified(self, monkeypatch):
        chat, llm = self.retried(monkeypatch, MOTION)
        got = asyncio.run(chat.rig.controller._extract_motion_control_intent("motion lights in the office are odd"))
        assert got["unprocessable_reason"] == "unverified"

    def test_a_retried_prompt_far_below_the_estimate_is_a_cut_prompt(self, monkeypatch):
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", LIGHT_ON, "stop"))
        original = llm.respond

        def with_count(spec):
            response = original(spec)
            response["prompt_eval_count"] = 120
            return response

        llm.respond = with_count
        chat = light_chat(monkeypatch, llm)
        assert chat.turn(LIGHT_QUERY).answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []

    def test_a_retried_prompt_near_the_estimate_is_fine(self, monkeypatch):
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", LIGHT_ON, "stop"))
        original = llm.respond

        def with_count(spec):
            response = original(spec)
            response["prompt_eval_count"] = 4800
            return response

        llm.respond = with_count
        chat = light_chat(monkeypatch, llm)
        chat.turn(LIGHT_QUERY)
        assert ("light", "turn_on") in writes(chat)

    def retried_door(self, monkeypatch, reply):
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", reply, "stop"))
        return door_chat(monkeypatch, llm), llm

    def test_a_retried_unlock_for_a_lock_command_does_not_unlock(self, monkeypatch):
        door, llm = self.retried_door(monkeypatch, UNLOCK_FRONT)
        out = door.say(LOCK_QUERY)
        assert len(llm.calls) == 2
        assert out.answer == room_context.UNPROCESSABLE_TEXT
        assert door.calls == [] and door.openings == []

    def test_the_matching_retried_lock_is_accepted(self, monkeypatch):
        door, llm = self.retried_door(monkeypatch, LOCK_FRONT)
        door.say(LOCK_QUERY)
        assert ("lock", "lock", "lock.front_door") in door.calls

    def test_a_retried_lock_for_an_unlock_command_is_refused(self, monkeypatch):
        door, llm = self.retried_door(monkeypatch, LOCK_FRONT)
        door.say("would you mind unlocking the front door lock")
        assert door.calls == []

    def test_a_retried_unlock_after_a_negation_is_refused(self, monkeypatch):
        door, llm = self.retried_door(monkeypatch, UNLOCK_FRONT)
        door.say("would you mind not unlocking the front door lock")
        assert door.openings == []

    @pytest.mark.parametrize("action", ["lock", "unlock"])
    def test_the_verb_rule_is_the_classifier_independent_second_layer(self, action):
        from orchestrator.utterance_kind import UtteranceClassification, UtteranceKind

        imperative = UtteranceClassification(kind=UtteranceKind.IMPERATIVE, device_type="lock")
        intent = {**LOCK_FRONT, "action": action}
        said = {"lock": "lock the front door", "unlock": "unlock the front door"}
        other = {"lock": "unlock the front door", "unlock": "lock the front door"}
        matches = shc.SmartHomeController._retried_intent_matches_words
        assert matches(intent, imperative, said[action]) is True
        assert matches(intent, imperative, other[action]) is False
        assert matches(intent, imperative, "do something with the front door") is False

    @pytest.mark.parametrize("kind", ["UNKNOWN", "STATE_QUESTION"])
    def test_a_write_needs_an_imperative_utterance_even_when_state_and_device_agree(self, kind):
        from orchestrator.utterance_kind import UtteranceClassification, UtteranceKind

        said = UtteranceClassification(kind=UtteranceKind[kind], device_type="light", target_state="on")
        matches = shc.SmartHomeController._retried_intent_matches_words
        assert matches(LIGHT_ON, said, "the lights on") is False
        assert matches({**LIGHT_ON, "action": "get_status"}, said, "the lights on") is True

    def test_a_follow_up_like_make_it_a_bit_warmer_is_refused_on_the_retried_path(self, monkeypatch):
        """No device in the words: the follow-up's referent is not in them either."""
        warmer = {**LIGHT_ON, "action": "set_color", "parameters": {"hs_colors": [[30, 100]]}}
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", warmer, "stop"))
        chat = light_chat(monkeypatch, llm)
        got = asyncio.run(chat.rig.controller.extract_intent(
            "make it a bit warmer", prev_query="turn on the study lights", prev_response="Done",
            prev_intent_entities={"device_type": "light", "room": "study", "action": "turn_on", "parameters": {}},
        ))
        assert len(llm.calls) == 2, "the follow-up reached the model and its retry"
        assert got == {"unprocessable": True, "unprocessable_reason": "unverified"}

    def test_with_the_state_question_kill_switch_on_a_retried_write_is_refused(self, monkeypatch):
        """The kill switch classifies every utterance UNKNOWN: nothing in the words can verify a write."""
        from orchestrator.utterance_kind import UNKNOWN_CLASSIFICATION

        for reply in (LIGHT_ON, {**LIGHT_ON, "action": "set_color"}):
            llm = Scripted(("raise", "unsupported_backend"), default=("reply", reply, "stop"))
            chat = light_chat(monkeypatch, llm)
            got = asyncio.run(chat.rig.controller.extract_intent(LIGHT_QUERY, utterance=UNKNOWN_CLASSIFICATION))
            assert got == {"unprocessable": True, "unprocessable_reason": "unverified"}
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", {**LIGHT_ON, "action": "get_status"}, "stop"))
        chat = light_chat(monkeypatch, llm)
        got = asyncio.run(chat.rig.controller.extract_intent(LIGHT_QUERY, utterance=UNKNOWN_CLASSIFICATION))
        assert got["action"] == "get_status", "reads still pass"

    def test_a_mixed_device_sequence_is_refused_on_the_retried_path(self, monkeypatch):
        mixed = {**SEQUENCE, "steps": [
            {"action": "turn_off", "target": {"device_type": "light", "room": "study"}, "parameters": {}, "delay_after": 0, "at_time": None},
            {"action": "lock", "target": {"device_type": "lock", "room": "front_door"}, "parameters": {}, "delay_after": 0, "at_time": None},
        ]}
        llm = Scripted(("raise", "unsupported_backend"), default=("reply", mixed, "stop"))
        chat = light_chat(monkeypatch, llm)
        got = asyncio.run(chat.rig.controller.extract_sequence_intent("turn off the lights then lock the door"))
        assert len(llm.calls) == 2
        assert got == {"unprocessable": True, "unprocessable_reason": "unverified"}


# ---------------------------------------------------------------------------
# Logs and stores
# ---------------------------------------------------------------------------

class TestLogsAndStores:
    def test_model_chosen_key_names_are_never_logged(self, monkeypatch, caplog):
        marker = "PLANTED-KEY-NAME-77b"
        for query, reply in ((LIGHT_QUERY, {**LIGHT_ON, marker: 1}), (MOTION_QUERY, {**MOTION, marker: 1})):
            chat = light_chat(monkeypatch, Scripted(("reply", reply, "stop")))
            with capture_logs() as logs, caplog.at_level("DEBUG"):
                chat.turn(query)
            assert marker not in json.dumps(logs, default=str)
            assert marker not in caplog.text

    def test_the_store_wrapper_refuses_an_over_cap_query_and_passes_others(self, monkeypatch):
        import orchestrator.nodes.route_control as rc

        stored = []

        async def fake(**fields):
            stored.append(fields)
            return True

        monkeypatch.setattr(rc, "store_conversation_context", fake)
        assert asyncio.run(rc._store_context(session_id="s", query="x" * 1001)) is False
        assert asyncio.run(rc._store_context(session_id="s", query="é" * 501)) is False
        assert asyncio.run(rc._store_context(session_id="s", query="x" * 1000)) is True
        assert asyncio.run(rc._store_context(session_id="s", query=None)) is True
        assert [f["query"] for f in stored] == ["x" * 1000, None]

    def test_route_control_stores_only_through_the_wrapper(self):
        source = (SRC / "orchestrator" / "nodes" / "route_control.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        parents = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        direct = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "store_conversation_context":
                fn = node
                while fn in parents and not isinstance(fn, FUNCTIONS):
                    fn = parents[fn]
                direct.append(fn.name)
        assert direct == ["_store_context"]
        assert "_store_context" in source

    def test_the_state_question_path_stores_no_over_cap_query(self, monkeypatch):
        chat = light_chat(monkeypatch, Scripted(("reply", LIGHT_ON, "stop")))
        chat.turn(padded("is the study light on", 1200, " really"))
        assert all(room_context.utf8_len(s["query"]) <= 1000 for s in chat.stores)


def test_a_component_on_the_auto_backend_serves_the_strict_caller_from_ollama_not_mlx(monkeypatch):
    """Caller level: extract_intent -> real router with backend 'auto': MLX is skipped."""
    from shared.config import _clear_cache_for_tests

    monkeypatch.setenv("OLLAMA_URL", "http://ollama.test")
    _clear_cache_for_tests()
    ollama_reply = {"response": json.dumps(LIGHT_ON), "done": True, "done_reason": "stop", "prompt_eval_count": 5000, "eval_count": 5}
    wire = Wire(monkeypatch, responder=lambda body: httpx.Response(200, json=ollama_reply))
    mlx_calls = []

    async def backend_config(model):
        return {"backend_type": "auto", "endpoint_url": "http://mlx.test", "timeout_seconds": 30}

    async def mlx(*args, **kwargs):
        mlx_calls.append(1)
        return {"response": json.dumps({**LIGHT_ON, "action": "turn_off"}), "backend": "mlx"}

    monkeypatch.setattr(wire.router, "_get_backend_config", backend_config)
    monkeypatch.setattr(wire.router, "_generate_mlx", mlx)
    chat = light_chat(monkeypatch, wire.router)
    chat.turn(LIGHT_QUERY)
    _clear_cache_for_tests()
    assert mlx_calls == [] and len(wire.requests) == 1
    assert wire.requests[0]["options"]["num_ctx"] == 8192
    assert ("light", "turn_on") in writes(chat)


# ---------------------------------------------------------------------------
# A follow-up never replays the earlier command when the model call fails
# ---------------------------------------------------------------------------

FOLLOW_UP = {"has_context_ref": True, "ref_types": ["pronoun"], "anaphora_types": ["pronoun"], "is_inquiry": False}
DIFFERENT_COLOR = {"has_context_ref": True, "ref_types": ["modifier"], "anaphora_types": [], "is_inquiry": False}


class Sequenced:
    """A router double that answers each call with the next spec."""

    def __init__(self, *specs):
        self.specs = list(specs)
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        return Scripted.respond(self.specs.pop(0))


class TestFollowUps:
    def first_turn(self, monkeypatch, llm):
        chat = Chat(monkeypatch)
        chat.turn("turn on the galley lights")
        assert writes(chat), "positive control: the first command acted"
        chat.reset_writes()
        chat.rig.controller.llm_router = llm
        return chat

    @pytest.mark.parametrize("spec", REFUSALS + [("reply", LIGHT_ON, "length"), ("reply", "[]", "stop")], ids=lambda s: "-".join(map(str, s[:2]))[:30])
    def test_a_failed_model_call_on_a_follow_up_writes_nothing(self, monkeypatch, spec):
        llm = Sequenced(spec)
        chat = self.first_turn(monkeypatch, llm)
        out = chat.turn("turn them off", room="study", context_ref_info=FOLLOW_UP)
        assert llm.calls, "the follow-up went to the model"
        assert out.answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == [], "the earlier command is not replayed with a made-up action"

    def test_positive_control_the_follow_up_acts_when_the_model_answers_whole(self, monkeypatch):
        off = {"device_type": "light", "room": None, "action": "turn_off", "target_scope": "group", "parameters": {}}
        chat = self.first_turn(monkeypatch, Sequenced(("reply", off, "stop")))
        chat.turn("turn them off", room="study", context_ref_info=FOLLOW_UP)
        assert ("light", "turn_off") in writes(chat)

    def test_a_state_question_that_needs_its_referent_gets_the_phrase_not_a_device_question(self, monkeypatch):
        llm = Sequenced(("raise", "over_ceiling"))
        chat = self.first_turn(monkeypatch, llm)
        out = chat.turn("did those come back on", room="study", context_ref_info={})
        assert llm.calls, "the question reached the model through the normal extraction"
        assert out.answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []

    def test_a_state_question_about_an_unknown_device_gets_the_phrase_not_a_device_question(self, monkeypatch):
        llm = Scripted(("raise", "over_ceiling"))
        chat = light_chat(monkeypatch, llm)
        out = chat.turn("is the heater on")
        assert llm.calls, "the classifier could not name the device, so the extractor was asked"
        assert out.answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []

    def test_model_chosen_key_names_are_not_logged_on_a_follow_up(self, monkeypatch, caplog, capsys):
        marker = "PLANTED-FOLLOWUP-KEY-3c9"
        off = {"device_type": "light", "room": None, "action": "turn_off", "target_scope": "group", "parameters": {}, marker: 1}
        chat = self.first_turn(monkeypatch, Sequenced(("reply", off, "stop")))
        with capture_logs() as logs, caplog.at_level("DEBUG"):
            chat.turn("turn them off", room="study", context_ref_info=FOLLOW_UP)
        seen = json.dumps(logs, default=str) + caplog.text + capsys.readouterr().out
        assert "interpreted follow-up" in seen, "the merge log line was emitted and observed"
        assert marker not in seen

    def test_a_failed_second_call_for_different_colors_writes_nothing(self, monkeypatch):
        ok = {"device_type": "light", "room": None, "action": "turn_on", "target_scope": "group", "parameters": {}}
        llm = Sequenced(("reply", ok, "stop"), ("raise", "over_ceiling"))
        chat = self.first_turn(monkeypatch, llm)
        out = chat.turn("try a different color", room="study", context_ref_info=DIFFERENT_COLOR)
        assert len(llm.calls) == 2, "both the follow-up and the re-extraction went to the model"
        assert out.answer == room_context.UNPROCESSABLE_TEXT
        assert writes(chat) == []


def test_execute_intent_answers_an_unprocessable_result_without_touching_home_assistant(monkeypatch):
    chat = Chat(monkeypatch)
    for reason in ("too_long", "budget", "length", "not_object", "unparseable", "unverified"):
        answer = asyncio.run(
            chat.rig.controller.execute_intent(room_context.unprocessable_intent(reason), chat.rig.raw, original_query="turn on the lights")
        )
        assert answer == (room_context.QUERY_TOO_LONG_TEXT if reason == "too_long" else room_context.UNPROCESSABLE_TEXT)
    assert chat.rig.raw.call_service.await_args_list == []
    assert chat.rig.raw.get_state.await_args_list == []
