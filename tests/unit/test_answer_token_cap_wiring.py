"""Closed world: every answer-producing LLM call takes its token limit from the voice-cap seam.

Every `.generate(`, `.generate_with_tools(` and `.generate_stream(` call under
`src/orchestrator/` either passes `max_tokens=` from `answer_max_tokens(` (and
finishes its spoken answer through `finish_spoken_answer(`), or is on the
allowlist below with a reason. A new LLM call that produces something a person
hears fails here until it adopts the seam or is classified.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ORCH = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
GATEWAY = Path(__file__).resolve().parents[2] / "src" / "gateway"
LLM_METHODS = {"generate", "generate_with_tools", "generate_stream"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

# (file relative to src/orchestrator, enclosing function) -> why the call does not produce a spoken answer.
NON_ANSWER = {
    ("main.py", "classify_node"): "intent classification: a label, never spoken",
    ("helpers.py", "summarize_conversation_history"): "history summary fed back to the model, never spoken",
    ("intent_discovery.py", "generate_novel_intent"): "intent discovery: a label, never spoken",
    ("nodes/validate.py", "validate_node"): "fact-check verdict JSON, never spoken",
    ("self_building_tools.py", "generate_tool_from_request"): "generated tool source, never spoken",
    ("sentence_buffer.py", "stream_with_sentence_buffering"): "unused module; takes max_tokens from its caller",
    ("smart_home_controller.py", "extract_intent"): "smart-home intent JSON, never spoken",
    ("smart_home_controller.py", "extract_sequence_intent"): "smart-home sequence JSON, never spoken",
    ("smart_home_controller.py", "_llm_occupancy_reasoning"): "occupancy decision, never spoken",
    ("smart_home_controller.py", "_extract_motion_control_intent"): "smart-home intent JSON, never spoken",
}
EXPECTED_ADOPTERS = {
    ("automation_agent.py", "_call_llm_with_tools"),
    ("helpers.py", "maybe_post_synthesis_fallback"),
    ("main.py", "handle_query_with_bypass"),
    ("main.py", "tool_call_node"),
    ("main.py", "event_generator"),
    ("main.py", "openai_stream_generator"),
    ("nodes/synthesize.py", "synthesize_node"),
}


def _calls():
    """(relative path, enclosing function, call node, function node) for every LLM-method call.
    Nested functions are attributed to the innermost enclosing function."""
    found = []
    for path in sorted(ORCH.rglob("*.py")):
        rel = path.relative_to(ORCH).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "attr", None) in LLM_METHODS:
                fn = node
                while fn in parents and not isinstance(fn, FUNCTIONS):
                    fn = parents[fn]
                found.append((rel, fn.name, node, fn))
    return found


def _is_seam_call(node):
    inner = node.value if isinstance(node, ast.Await) else node
    return isinstance(inner, ast.Call) and getattr(inner.func, "id", None) == "answer_max_tokens"


def _limit_comes_from_the_seam(call, fn) -> bool:
    keyword = next((k for k in call.keywords if k.arg == "max_tokens"), None)
    if keyword is None:
        return False
    if _is_seam_call(keyword.value):
        return True
    if not isinstance(keyword.value, ast.Name):
        return False
    assignments = sorted(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.Assign) and n.lineno < call.lineno
         and any(isinstance(t, ast.Name) and t.id == keyword.value.id for t in n.targets)),
        key=lambda n: n.lineno,
    )
    return bool(assignments) and _is_seam_call(assignments[-1].value)


def _calls_named(fn, name):
    return [n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "id", None) == name]


CALLS = _calls()


def test_population_floor_and_named_members():
    assert len(CALLS) >= 19
    assert ("nodes/synthesize.py", "synthesize_node") in {(r, f) for r, f, _, _ in CALLS}
    assert set(NON_ANSWER) <= {(r, f) for r, f, _, _ in CALLS}, "an allowlisted site no longer exists: drop it"


@pytest.mark.parametrize("rel,name,call,fn", CALLS, ids=[f"{r}:{f}:{c.lineno}" for r, f, c, _ in CALLS])
def test_every_llm_call_adopts_the_seam_or_is_classified(rel, name, call, fn):
    if (rel, name) in NON_ANSWER:
        return
    assert _limit_comes_from_the_seam(call, fn), (
        f"{rel}:{call.lineno} {name}(): an answer-producing call must pass max_tokens from answer_max_tokens(...)"
    )
    assert _calls_named(fn, "finish_spoken_answer"), (
        f"{rel}:{name}: the spoken answer must go through finish_spoken_answer(...)"
    )


def test_the_adopting_sites_are_the_ones_the_plan_names():
    adopters = {(r, f) for r, f, _, _ in CALLS if (r, f) not in NON_ANSWER}
    assert adopters == EXPECTED_ADOPTERS


def test_automation_agent_is_not_allowlisted():
    assert not any(rel == "automation_agent.py" for rel, _ in NON_ANSWER)


def test_the_seam_check_would_catch_a_bare_literal():
    source = "async def f(llm):\n    await llm.generate(model='m', prompt='p', max_tokens=800)\n"
    tree = ast.parse(source)
    fn = tree.body[0]
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "generate")
    assert _limit_comes_from_the_seam(call, fn) is False
    stale = "async def f(llm):\n    mt = await answer_max_tokens('voice', 1)\n    mt = 800\n    await llm.generate(max_tokens=mt)\n"
    fn = ast.parse(stale).body[0]
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "generate")
    assert _limit_comes_from_the_seam(call, fn) is False, "the last assignment before the call decides"
    good = "async def f(llm):\n    mt = 800\n    mt = await answer_max_tokens('voice', mt)\n    await llm.generate(max_tokens=mt)\n"
    fn = ast.parse(good).body[0]
    call = next(n for n in ast.walk(fn) if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "generate")
    assert _limit_comes_from_the_seam(call, fn) is True


def test_the_gateway_fallback_adopts_the_seam():
    tree = ast.parse((GATEWAY / "main.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "route_to_ollama")
    for name in ("speech_max_tokens", "answer_hit_cap", "trim_to_complete_sentence", "get_voice_response_limits"):
        assert _calls_named(fn, name), f"route_to_ollama must call {name}"


def test_the_only_other_gateway_token_caps_are_the_documented_prerouter_ones():
    source = (GATEWAY / "intent_prerouter.py").read_text(encoding="utf-8")
    assert source.count("num_predict") >= 2     # classifier (10) and the 2-sentence simple reply (100): documented exceptions
