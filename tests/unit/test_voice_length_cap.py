"""The voice length cap: seam helpers, cap-hit signals, trimming, clamp and prompt line.

Pure functions in `shared.output_channel` and `shared.assistant_profile`, plus
the real backend parsers in `shared.llm_router` run against recorded response
bodies (httpx.MockTransport), so the `stop_reason` each backend produces is
exercised, not assumed.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from unittest import mock

import httpx
import pytest

sys.path.insert(0, "src")

from shared import assistant_profile as ap  # noqa: E402
from shared import llm_router  # noqa: E402
from shared.output_channel import (  # noqa: E402
    CAP_HIT_TOKEN_MARGIN,
    SPEECH_SINK_MAX_CHARS,
    OutputChannel,
    answer_hit_cap,
    channel_for_interface_type,
    normalize_stop_reason,
    speech_max_tokens,
    trim_to_complete_sentence,
)

SPEECH, TEXT = OutputChannel.SPEECH, OutputChannel.TEXT


# --- speech_max_tokens ---------------------------------------------------------------------


@pytest.mark.parametrize("requested,expected", [(None, 200), (0, 200), (50, 50), (200, 200), (800, 200), (2048, 200)])
def test_speech_takes_the_smaller_of_the_request_and_the_cap(requested, expected):
    assert speech_max_tokens(SPEECH, requested, 200) == expected


@pytest.mark.parametrize("requested", [None, 0, 50, 200, 800, 3000])
def test_text_gets_exactly_what_the_site_asked_for(requested):
    assert speech_max_tokens(TEXT, requested, 200) == requested


@pytest.mark.parametrize("interface_type", ["voice", "text", "chat", "kiosk", None, ""])
def test_the_channel_decides_not_the_interface_string(interface_type):
    channel = channel_for_interface_type(interface_type)
    assert (speech_max_tokens(channel, 800, 200) == 200) is (interface_type == "voice")


# --- trim_to_complete_sentence ----------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("It's 3.5 miles. Turn left", "It's 3.5 miles."),
    ("Use e.g. the side door. Then", "Use e.g. the side door."),
    ("Ask Dr. Smith. He", "Ask Dr. Smith."),
    ("Dr. Smith", "Dr. Smith"),
    ("Mrs. Jones said hello. She", "Mrs. Jones said hello."),
    ("It is vs. the wall. And", "It is vs. the wall."),
    ("1. Milk\n2. Eggs", "1. Milk\n2. Eggs"),
    ("Buy these:\n1. Milk\n2. Eggs", "Buy these:\n1. Milk\n2. Eggs"),
    ('She said "go." Then', 'She said "go."'),
    ("(Yes.) Then", "(Yes.)"),
    ("Wait... then", "Wait..."),
    ("Wait… then", "Wait…"),
    ("", ""),
    ("no terminator", "no terminator"),
    ("Hello. How are you? I am", "Hello. How are you?"),
    ("Really?! Yes", "Really?!"),
    ("Costs $3.50 total", "Costs $3.50 total"),
    ("Visit example.com for more", "Visit example.com for more"),
    ("Done.", "Done."),
    ("A. B. C", "A. B."),
])
def test_trim_boundaries(text, expected):
    assert trim_to_complete_sentence(text) == expected


def test_trim_handles_none_and_is_idempotent():
    assert trim_to_complete_sentence(None) == ""
    once = trim_to_complete_sentence("One. Two. Thr")
    assert trim_to_complete_sentence(once) == once == "One. Two."


# --- cap-hit signals, per backend and result shape -----------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("length", "length"), ("LENGTH", "length"), ("max_tokens", "length"), ("MAX_TOKENS", "length"),
    ("stop", "stop"), ("end_turn", "stop"), ("tool_calls", "stop"), ("tool_use", "stop"), ("STOP", "stop"),
    ("unknown", None), ("", None), (None, None), (5, None), ({}, None),
])
def test_normalize_stop_reason(raw, expected):
    assert normalize_stop_reason(raw) == expected


CAP = 200


@pytest.mark.parametrize("shape,hit", [
    ({"backend": "ollama", "stop_reason": "length", "eval_count": 3}, True),            # normalized, wins over a small count
    ({"backend": "ollama", "done_reason": "length", "eval_count": 3}, True),            # raw Ollama
    ({"backend": "ollama", "done_reason": "stop", "eval_count": 200}, False),           # said stop: not hit, even at the cap
    ({"backend": "openai", "finish_reason": "length"}, True),
    ({"backend": "openai", "finish_reason": "stop", "output_tokens": 500}, False),
    ({"backend": "anthropic", "finish_reason": "max_tokens"}, True),
    ({"backend": "anthropic", "stop_reason": "max_tokens"}, True),
    ({"backend": "anthropic", "finish_reason": "end_turn"}, False),
    ({"backend": "google", "finish_reason": "MAX_TOKENS"}, True),
    ({"backend": "google", "finish_reason": "STOP"}, False),
    ({"backend": "mlx", "finish_reason": "length"}, True),
    ({"backend": "ollama", "finish_reason": "unknown", "eval_count": 10}, False),       # unusable reason: falls to tokens
    ({}, False),
    (None, False),
])
def test_cap_hit_from_each_result_shape(shape, hit):
    assert answer_hit_cap(shape, CAP) is hit


@pytest.mark.parametrize("tokens,hit", [(193, False), (194, False), (195, True), (196, True), (200, True), (0, False)])
def test_fallback_heuristic_boundaries_with_no_backend_signal(tokens, hit):
    assert CAP - CAP_HIT_TOKEN_MARGIN == 195
    assert answer_hit_cap({"eval_count": tokens}, CAP) is hit


def test_fallback_reads_the_other_token_keys_and_needs_a_cap():
    assert answer_hit_cap({"output_tokens": 199}, CAP) is True
    assert answer_hit_cap({"tokens": 199}, CAP) is True
    assert answer_hit_cap({"eval_count": 199}, None) is False
    assert answer_hit_cap({"eval_count": True}, CAP) is False
    assert answer_hit_cap({"eval_count": "199"}, CAP) is False


# --- the real backend parsers produce the signal ---------------------------------------------------


def _patch_http(monkeypatch, handler):
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(llm_router.httpx, "AsyncClient", factory)


def _router():
    return object.__new__(llm_router.LLMRouter)


def _ollama_generate(monkeypatch, body):
    _patch_http(monkeypatch, lambda request: httpx.Response(200, json=body))
    return asyncio.run(_router()._generate_ollama(
        endpoint_url="http://ollama.test", model="m", prompt="p", temperature=0.1, max_tokens=200, timeout=5,
    ))


@pytest.mark.parametrize("done_reason,hit", [("length", True), ("stop", False)])
def test_ollama_generate_carries_stop_reason(monkeypatch, done_reason, hit):
    result = _ollama_generate(monkeypatch, {"response": "Hello there.", "done": True, "done_reason": done_reason, "eval_count": 42})
    assert result["stop_reason"] == ("length" if hit else "stop")
    assert answer_hit_cap(result, 200) is hit


def test_ollama_generate_without_a_reason_has_none_and_falls_back_to_tokens(monkeypatch):
    result = _ollama_generate(monkeypatch, {"response": "x", "done": True, "eval_count": 198})
    assert result["stop_reason"] is None
    assert answer_hit_cap(result, 200) is True


def test_ollama_stream_final_chunk_carries_stop_reason(monkeypatch):
    lines = [
        json.dumps({"response": "Hello", "done": False}),
        json.dumps({"response": "", "done": True, "done_reason": "length", "eval_count": 200}),
    ]
    _patch_http(monkeypatch, lambda request: httpx.Response(200, text="\n".join(lines)))

    async def run():
        return [c async for c in _router()._generate_ollama_stream(
            endpoint_url="http://ollama.test", model="m", prompt="p", temperature=0.1, max_tokens=200, timeout=5,
        )]

    chunks = asyncio.run(run())
    assert chunks[-1]["done"] is True and chunks[-1]["stop_reason"] == "length"
    assert answer_hit_cap(chunks[-1], 200) is True


@pytest.mark.parametrize("finish,hit", [("length", True), ("stop", False)])
def test_mlx_generate_carries_stop_reason(monkeypatch, finish, hit):
    body = {"choices": [{"message": {"content": "Hi."}, "finish_reason": finish}], "usage": {"completion_tokens": 12}}
    _patch_http(monkeypatch, lambda request: httpx.Response(200, json=body))
    result = asyncio.run(_router()._generate_mlx(
        endpoint_url="http://mlx.test", model="m", prompt="p", temperature=0.1, max_tokens=200, timeout=5,
    ))
    assert answer_hit_cap(result, 200) is hit


def test_mlx_stream_final_chunk_carries_stop_reason(monkeypatch):
    sse = "\n".join([
        'data: ' + json.dumps({"choices": [{"delta": {"content": "Hi"}, "finish_reason": None}]}),
        'data: ' + json.dumps({"choices": [{"delta": {}, "finish_reason": "length"}]}),
        "data: [DONE]",
    ])
    _patch_http(monkeypatch, lambda request: httpx.Response(200, text=sse))

    async def run():
        return [c async for c in _router()._generate_mlx_stream(
            endpoint_url="http://mlx.test", model="m", prompt="p", temperature=0.1, max_tokens=200, timeout=5,
        )]

    chunks = asyncio.run(run())
    assert chunks[-1]["done"] is True and chunks[-1]["stop_reason"] == "length"


def test_ollama_tool_calling_result_carries_stop_reason(monkeypatch):
    body = {"message": {"role": "assistant", "content": "Hi"}, "done": True, "done_reason": "length", "eval_count": 200}
    _patch_http(monkeypatch, lambda request: httpx.Response(200, json=body))
    result = asyncio.run(_router()._generate_ollama_with_tools(
        model="m", messages=[{"role": "user", "content": "x"}], tools=None,
        temperature=0.1, max_tokens=200, request_id=None, timeout=5,
    ))
    assert result["stop_reason"] == "length"


# --- the read-time clamp ----------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("300", 300), (300, 300), (300.9, 300), (None, 200), (float("nan"), 200), (float("inf"), 200), (-5, 32), (0, 32),
    (99999, 1024), ("abc", 200), ("", 200), (True, 200), ([], 200), ("1e3", 1000), (" 64 ", 64),
])
def test_max_tokens_is_clamped_and_tolerant(value, expected):
    assert ap.clamp_voice_response({"max_tokens": value})["max_tokens"] == expected


@pytest.mark.parametrize("value,expected", [("2", 2), (None, 3), (0, 1), (50, 10), (math.nan, 3), ("x", 3), (4.7, 4)])
def test_max_sentences_is_clamped_and_tolerant(value, expected):
    assert ap.clamp_voice_response({"max_sentences": value})["max_sentences"] == expected


@pytest.mark.parametrize("section", [None, {}, "loud", 5, [], {"unknown": 1}])
def test_a_missing_or_malformed_section_gives_the_defaults(section):
    assert ap.clamp_voice_response(section) == {"max_sentences": 3, "max_tokens": 200, "max_tokens_long": 600, "ambient_fragment_gate": False}


def test_get_voice_response_limits_reads_the_active_guardrails(monkeypatch):
    monkeypatch.setattr(ap, "get_guardrails", mock.AsyncMock(return_value={"voice_response": {"max_tokens": "99999"}}))
    assert asyncio.run(ap.get_voice_response_limits())["max_tokens"] == 1024
    monkeypatch.setattr(ap, "get_guardrails", mock.AsyncMock(side_effect=RuntimeError("admin down")))
    assert asyncio.run(ap.get_voice_response_limits())["max_tokens"] == 200


def test_the_largest_voice_cap_sits_under_the_speech_sink_limit():
    """At ~4 characters a token the ceiling stays below what the sink will normalize."""
    assert ap.VOICE_MAX_TOKENS_RANGE[1] * 4 < SPEECH_SINK_MAX_CHARS


# --- the prompt line ----------------------------------------------------------------------------------


@pytest.fixture
def profile(monkeypatch):
    monkeypatch.setattr(ap, "get_assistant_profile", mock.AsyncMock(return_value=dict(ap.DEFAULT_ASSISTANT_PROFILE)))
    monkeypatch.setattr(ap, "get_guardrails", mock.AsyncMock(return_value=json.loads(json.dumps(ap.DEFAULT_GUARDRAILS))))


@pytest.mark.parametrize("interface_type,present", [("voice", True), ("text", False), ("chat", False), (None, False), ("kiosk", False)])
def test_the_speech_line_is_in_the_prompt_for_speech_only(profile, interface_type, present):
    prompt = asyncio.run(ap.build_core_assistant_prompt(interface_type=interface_type))
    line = "Keep spoken answers to at most 3 short sentences. No lists, headings or markdown."
    assert (line in prompt) is present


def test_the_speech_line_follows_the_configured_sentence_limit(monkeypatch, profile):
    guardrails = json.loads(json.dumps(ap.DEFAULT_GUARDRAILS))
    guardrails["voice_response"]["max_sentences"] = "99"
    monkeypatch.setattr(ap, "get_guardrails", mock.AsyncMock(return_value=guardrails))
    prompt = asyncio.run(ap.build_core_assistant_prompt(interface_type="voice"))
    assert "at most 10 short sentences" in prompt


# --- finishing a spoken answer ---------------------------------------------------------------------------


def _finish(*args, **kwargs):
    sys.modules.setdefault("prometheus_client", mock.MagicMock())
    from orchestrator.nodes import _runtime  # noqa: F401  (nodes before helpers: import order)
    from orchestrator.helpers import finish_spoken_answer

    return finish_spoken_answer(*args, **kwargs)


def test_a_think_only_answer_that_hit_the_cap_becomes_empty():
    assert _finish("<think>reasoning that never ended", "voice", {"stop_reason": "length"}, 200, stage="t") == ""


def test_a_think_block_is_dropped_before_the_trim():
    text = "<think>plan</think>First sentence. Second sent"
    assert _finish(text, "voice", {"stop_reason": "length"}, 200, stage="t") == "First sentence."


def test_text_and_uncut_answers_are_returned_untouched():
    cut = {"stop_reason": "length"}
    assert _finish("One. Two. Thr", "text", cut, 200, stage="t") == "One. Two. Thr"
    assert _finish("One. Two. Thr", "chat", cut, 200, stage="t") == "One. Two. Thr"
    assert _finish("One. Two. Thr", "voice", {"stop_reason": "stop"}, 200, stage="t") == "One. Two. Thr"
    assert _finish("It's 3.5 miles. Turn left", "voice", {"eval_count": 100}, 200, stage="t") == "It's 3.5 miles. Turn left"
    assert _finish("", "voice", cut, 200, stage="t") == ""
    assert _finish(None, "voice", cut, 200, stage="t") is None


# --- long-form turns -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value,expected", [
    ("900", 900), (None, 600), (float("nan"), 600), (1, 64), (99999, 2048), ("abc", 600), (True, 600), (64, 64), (2048, 2048),
])
def test_max_tokens_long_is_clamped_and_tolerant(value, expected):
    assert ap.clamp_voice_response({"max_tokens_long": value})["max_tokens_long"] == expected


def test_the_defaults_and_ranges_for_long_form():
    assert ap.DEFAULT_GUARDRAILS["voice_response"]["max_tokens_long"] == 600
    assert ap.VOICE_MAX_TOKENS_LONG_RANGE == (64, 2048)


def test_even_the_largest_long_cap_is_bounded_by_the_speech_sink():
    """2048 tokens can exceed the sink's input cap; the sink still bounds what reaches TTS."""
    from shared.output_channel import render_for_channel

    text = "word " * 3000
    assert len(render_for_channel(text, SPEECH)) <= SPEECH_SINK_MAX_CHARS


def _helpers():
    sys.modules.setdefault("prometheus_client", mock.MagicMock())
    from orchestrator.nodes import _runtime  # noqa: F401  (nodes before helpers: import order)
    from orchestrator import helpers

    return helpers


@pytest.mark.parametrize("intent,query,expected", [
    ("recipes", "give me something", True),
    ("directions", "x", True),
    ("RECIPES", "x", True),
    (None, "walk me through changing a tire", True),
    (None, "give me step by step instructions", True),
    (None, "plan my day tomorrow", True),
    (None, "what's the itinerary", True),
    (None, "how do I make pancakes", True),
    ("general_info", "what's the capital of France", False),
    ("weather", "how's the weather", False),
    (None, "", False),
    (None, None, False),
])
def test_is_long_form_turn(intent, query, expected):
    assert _helpers().is_long_form_turn(intent, query) is expected


def test_is_long_form_turn_accepts_an_intent_enum():
    from orchestrator.state import IntentCategory

    helpers = _helpers()
    assert helpers.is_long_form_turn(IntentCategory.RECIPES, "x") is True
    assert helpers.is_long_form_turn(IntentCategory.WEATHER, "x") is False


@pytest.mark.parametrize("interface_type,long_form,tool_calling,requested,expected", [
    ("voice", False, False, 2048, 200),
    ("voice", True, False, 2048, 600),
    ("voice", True, False, 300, 300),
    ("voice", True, True, 2048, 600),
    ("voice", False, True, 2048, 512),
    ("text", True, False, 2048, 2048),
    ("chat", True, True, None, None),
])
def test_answer_max_tokens_long_form(monkeypatch, interface_type, long_form, tool_calling, requested, expected):
    helpers = _helpers()
    monkeypatch.setattr(helpers, "get_voice_response_limits", mock.AsyncMock(
        return_value={"max_sentences": 3, "max_tokens": 200, "max_tokens_long": 600, "ambient_fragment_gate": False}))
    result = asyncio.run(helpers.answer_max_tokens(interface_type, requested, tool_calling=tool_calling, long_form=long_form))
    assert result == expected


def test_the_long_form_prompt_drops_the_short_sentence_limit(profile):
    prompt = asyncio.run(ap.build_core_assistant_prompt(interface_type="voice", long_form=True))
    assert "at most 3 short sentences" not in prompt
    assert "may run longer than usual" in prompt
    text = asyncio.run(ap.build_core_assistant_prompt(interface_type="text", long_form=True))
    assert "may run longer" not in text


# --- think blocks -------------------------------------------------------------------------------------------


def test_an_unclosed_think_block_after_real_text_is_dropped_on_a_cut_off_answer():
    text = "Preheat the oven. Mix the flour. <think>now I should consider whether the user wants"
    assert _finish(text, "voice", {"stop_reason": "length"}, 200, stage="t") == "Preheat the oven. Mix the flour."


def test_a_closed_think_block_then_a_cut_off_answer():
    text = "<think>plan</think>Preheat the oven. Mix the flo"
    assert _finish(text, "voice", {"stop_reason": "length"}, 200, stage="t") == "Preheat the oven."


def test_an_unclosed_think_block_in_an_uncut_answer_is_left_alone():
    text = "Real answer. <think>musing"
    assert _finish(text, "voice", {"stop_reason": "stop"}, 200, stage="t") == text
