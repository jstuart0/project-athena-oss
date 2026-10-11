"""What counts as an answer too short to stand.

One rule (`helpers.answer_is_too_short`) serves validate's Layer 1 and the
post-synthesis insufficient-answer detector: an LLM answer is too short when it
is empty after strip, or shorter than the minimum and not a finished sentence.
A deterministic answer (`state.skip_synthesis`) is exempt from both.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from unittest.mock import AsyncMock, patch

import pytest

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator.nodes import _runtime, validate_node  # noqa: E402  (nodes before helpers: import order)
from orchestrator import helpers  # noqa: E402
from orchestrator.helpers import answer_is_too_short, detect_insufficient_response  # noqa: E402
from orchestrator.state import IntentCategory, OrchestratorState  # noqa: E402

MIN = 10


@pytest.mark.parametrize("answer,too_short", [
    ("Done.", False),                    # 5 chars, finished sentence
    ("Ok", True),                        # short, no terminator
    ("Okay", True),
    ("abcdefghi", True),                 # 9, unterminated
    ("abcdefghi.", False),               # 10 with terminator
    ("abcdefghij", False),               # 10 unterminated: at the minimum
    ("abcdefghijk", False),              # 11
    ("Yes!", False),
    ("Really?", False),
    ('He said "no."', False),            # closing quote after the terminator
    ("(Yes.)", False),
    ("It's 9.", False),
    ("   ", True),
    ("", True),
    (None, True),
    ("…", True),                    # an ellipsis alone is not an answer
    ("...", True),
    ("?!", True),
    (" Done. ", False),                  # measured after strip
])
def test_boundaries_with_min_10(answer, too_short):
    assert answer_is_too_short(answer, MIN) is too_short


def test_min_zero_only_empty_is_too_short():
    assert answer_is_too_short("Ok", 0) is False
    assert answer_is_too_short("a", 0) is False
    assert answer_is_too_short("...", 0) is False
    assert answer_is_too_short("", 0) is True
    assert answer_is_too_short("   ", 0) is True


def test_a_larger_minimum_still_lets_a_finished_sentence_stand():
    assert answer_is_too_short("Done.", 50) is False
    assert answer_is_too_short("Done", 50) is True


# --- validate Layer 1 -----------------------------------------------------------------


def _state(answer, *, skip_synthesis=False):
    state = OrchestratorState(query="thanks, that's all")
    state.answer = answer
    state.intent = IntentCategory.GENERAL_INFO
    state.skip_synthesis = skip_synthesis
    state.mode = "guest"
    state.room = "kitchen"
    state.session_id = "s"
    state.request_id = "r"
    return state


def _validate(state, min_chars=MIN):
    guardrails = {"min_response_chars": min_chars, "max_response_chars": 5000}
    router = mock.MagicMock()
    router.generate = AsyncMock(return_value={"response": '{"contains_hallucinations": false}', "eval_count": 1})
    _runtime.set_llm_router(router)
    with patch("orchestrator.nodes.validate.get_validation_guardrails", new=AsyncMock(return_value=guardrails)), \
         patch("orchestrator.nodes.validate.get_component_config",
               new=AsyncMock(return_value={"model_name": "m", "system_prompt": ""})):
        return asyncio.run(validate_node(state))


@pytest.fixture(autouse=True)
def _reset_runtime():
    _runtime.reset_for_test()
    yield
    _runtime.reset_for_test()


@pytest.mark.parametrize("answer", ["Done.", "Yes!", "Really?", 'He said "no."'])
def test_a_short_finished_llm_answer_passes_validate(answer):
    result = _validate(_state(answer))
    assert result.validation_passed is True, result.validation_reason


@pytest.mark.parametrize("answer", ["Ok", "abcdefghi", "...", "   ", ""])
def test_a_short_unfinished_llm_answer_fails_validate(answer):
    result = _validate(_state(answer))
    assert result.validation_passed is False
    assert result.validation_reason == "Response too short"


def test_ten_characters_unterminated_pass_validate():
    assert _validate(_state("abcdefghij")).validation_passed is True


def test_a_deterministic_answer_is_exempt():
    result = _validate(_state("Okay.", skip_synthesis=True))
    assert result.validation_passed is True
    # Even an unfinished deterministic answer stands (templated status lines).
    assert _validate(_state("Ok", skip_synthesis=True)).validation_passed is True


@pytest.mark.parametrize("answer", ["", "   "])
def test_a_deterministic_flag_does_not_excuse_an_empty_answer(answer):
    result = _validate(_state(answer, skip_synthesis=True))
    assert result.validation_passed is False
    assert result.validation_reason == "Response too short"


def test_min_response_chars_zero_accepts_a_short_unfinished_answer():
    assert _validate(_state("Ok"), min_chars=0).validation_passed is True
    assert _validate(_state(""), min_chars=0).validation_passed is False


def test_the_admin_label_describes_the_new_meaning():
    from pathlib import Path

    html = (Path(__file__).resolve().parents[2] / "admin/frontend/index.html").read_text(encoding="utf-8")
    assert "Minimum length for an unfinished answer" in html
    assert "Min Response Chars" not in html


# --- the insufficient-answer detector and the web-search fallback ---------------------------


def test_done_is_not_insufficient():
    assert detect_insufficient_response("Done.", {"min_response_length": 10}) is None


@pytest.mark.parametrize("answer,expected", [
    ("", "empty_response"),
    ("   ", "empty_response"),
    ("Ok", "response_too_short"),
    ("abcdefghi", "response_too_short"),
    ("abcdefghij", None),
    ("Yes!", None),
    ("I couldn't find that.", "couldn't find"),        # a pattern still triggers, however short or long
    ("I don't know.", "I don't know"),
])
def test_detector_triggers(answer, expected):
    assert detect_insufficient_response(answer, {"min_response_length": 10}) == expected


def _fallback_rig(monkeypatch):
    engine = mock.MagicMock()
    engine.search = AsyncMock(return_value=(None, []))
    _runtime.set_parallel_search_engine(engine)
    config = {"enabled": True, "config": {"min_response_length": 10}}
    monkeypatch.setattr(helpers, "get_post_synthesis_fallback_config", AsyncMock(return_value=config))
    return engine


def test_a_deterministic_answer_never_calls_the_fallback(monkeypatch):
    engine = _fallback_rig(monkeypatch)
    state = _state("Ok", skip_synthesis=True)
    assert asyncio.run(helpers.maybe_post_synthesis_fallback(state)) is False
    engine.search.assert_not_awaited()


def test_the_fallback_still_runs_for_an_unfinished_llm_answer(monkeypatch):
    """Positive control for the test above."""
    engine = _fallback_rig(monkeypatch)
    asyncio.run(helpers.maybe_post_synthesis_fallback(_state("Ok")))
    engine.search.assert_awaited_once()


def test_a_finished_short_llm_answer_does_not_search(monkeypatch):
    engine = _fallback_rig(monkeypatch)
    assert asyncio.run(helpers.maybe_post_synthesis_fallback(_state("Done."))) is False
    engine.search.assert_not_awaited()
