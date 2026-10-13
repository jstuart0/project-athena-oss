"""Pure tests for the Ollama context planner (`shared.llm_context`) and the
three `AthenaConfig` fields that steer it.

The router wiring is in `test_llm_router_context_wiring.py`. Everything here
runs without a router, a transport or a clock.
"""
from __future__ import annotations

import json
import logging
import pathlib
import sys

import pytest
from pydantic_settings import SettingsConfigDict

sys.path.insert(0, "src")

from shared.config import AthenaConfig  # noqa: E402
from shared.llm_context import (  # noqa: E402
    ContextBudgetExceeded,
    ContextMarks,
    ContextPlan,
    ContextSettings,
    estimate_tokens,
    plan_context,
    truncation_suspected,
    valid_ctx,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "llm_context_captures.json"
MODEL = "qwen3:8b"
SH_INTENT_BYTES = 12919  # the captured sh_intent prompt, composed
SH_INTENT_ESTIMATE = 4646  # ceil(12919 / 2.8) + 32


def prompt_for_estimate(target: int) -> str:
    size = 0
    while estimate_tokens("a" * size) < target:
        size += 1
    assert estimate_tokens("a" * size) == target
    return "a" * size


def settings(mode="enforce", ceiling=16384, server_default=0) -> ContextSettings:
    return ContextSettings(mode=mode, ceiling=ceiling, server_default=server_default)


def plan(
    prompt="",
    *,
    row=2048,
    num_predict=384,
    max_tokens=400,
    require_full=True,
    marks=None,
    model=MODEL,
    **settings_kwargs,
) -> ContextPlan:
    return plan_context(
        prompt_text=prompt,
        row_ctx=row,
        num_predict=num_predict,
        max_tokens=max_tokens,
        require_full=require_full,
        marks=marks if marks is not None else ContextMarks(),
        model=model,
        settings=settings(**settings_kwargs),
    )


class TestEstimator:
    def test_ceil_bytes_over_2_8_plus_32(self):
        assert estimate_tokens("") == 32
        assert estimate_tokens(None) == 32
        assert estimate_tokens("a" * 14) == 5 * 1 + 32  # 14 bytes / 2.8 = 5 exactly
        assert estimate_tokens("a" * 15) == 6 + 32  # 5.36 rounds up
        assert estimate_tokens("a" * SH_INTENT_BYTES) == SH_INTENT_ESTIMATE

    def test_multibyte_counts_utf8_bytes_not_characters(self):
        assert estimate_tokens("é" * 1000) == estimate_tokens("a" * 2000)
        assert estimate_tokens("日本" * 100) == estimate_tokens("a" * 600)

    def test_never_under_a_measured_smart_home_prompt(self):
        for entry in json.loads(FIXTURE.read_text())["captures"]:
            if entry["role"].startswith("sh_"):
                assert estimate_tokens("a" * entry["prompt_bytes"]) >= entry["prompt_eval_count"]


class TestValidCtx:
    @pytest.mark.parametrize("value", [True, False, "2048", 0, -1, 2048.0, None, [], {}])
    def test_invalid_is_absent(self, value):
        assert valid_ctx(value) is None

    def test_positive_int_is_kept(self):
        assert valid_ctx(2048) == 2048


class TestDefaultCalls:
    def test_known_floor_is_sent_as_is(self):
        result = plan("x", row=2048, require_full=False)
        assert (result.send, result.effective, result.outcome) == (2048, 2048, "unchanged")

    def test_a_default_call_never_raises_even_for_a_huge_prompt(self):
        result = plan("a" * 100_000, row=2048, require_full=False)
        assert (result.send, result.outcome) == (2048, "unchanged")

    def test_sticky_mark_applies_at_the_same_floor_only(self):
        marks = ContextMarks()
        marks.record(MODEL, 2048, 8192)
        result = plan("x", row=2048, require_full=False, marks=marks)
        assert (result.send, result.outcome) == (8192, "sticky")

    def test_unknown_floor_sends_nothing_even_after_strict_calls(self):
        marks = ContextMarks()
        strict = plan(prompt_for_estimate(3000), row=None, marks=marks)
        assert strict.send is None
        default = plan("x", row=None, require_full=False, marks=marks)
        assert (default.send, default.effective) == (None, None)

    def test_server_default_is_the_floor_for_a_model_without_a_row(self):
        result = plan("x", row=None, require_full=False, server_default=4096)
        assert (result.send, result.floor) == (4096, 4096)

    def test_invalid_row_value_falls_back_to_the_server_default(self):
        assert plan("x", row="2048", require_full=False, server_default=4096).send == 4096
        assert plan("x", row="2048", require_full=False).send is None


class TestStrictPlanning:
    def test_need_at_a_bucket_boundary_and_one_over(self):
        # reserve 100, so need = estimate + 100
        at_2048 = plan(prompt_for_estimate(1948), num_predict=100)
        over = plan(prompt_for_estimate(1949), num_predict=100)
        assert (at_2048.send, at_2048.outcome) == (2048, "unchanged")
        assert (over.send, over.outcome) == (4096, "raised")

    @pytest.mark.parametrize("num_predict", [None, 0, -1, True, "384"])
    def test_reserve_falls_back_to_max_tokens(self, num_predict):
        assert plan("x", num_predict=num_predict, max_tokens=400).reserve == 400

    @pytest.mark.parametrize("max_tokens", [None, 0, -5])
    def test_reserve_falls_back_to_512(self, max_tokens):
        assert plan("x", num_predict=None, max_tokens=max_tokens).reserve == 512

    def test_merged_num_predict_wins_over_max_tokens(self):
        assert plan("x", num_predict=384, max_tokens=900).reserve == 384

    def test_bucket_exactly_at_the_ceiling_is_allowed(self):
        result = plan(prompt_for_estimate(4096 - 384), ceiling=4096)
        assert (result.send, result.outcome) == (4096, "raised")

    def test_bucket_over_the_ceiling_is_refused(self):
        result = plan(prompt_for_estimate(4096 - 384 + 1), ceiling=4096)
        assert result.outcome == "over_ceiling"

    def test_server_default_above_the_bucket_wins(self):
        result = plan("x", row=None, server_default=65536)
        assert (result.send, result.outcome) == (65536, "unchanged")

    def test_a_row_above_the_ceiling_is_sent_unchanged(self):
        result = plan("x", row=32768, ceiling=16384)
        assert (result.send, result.outcome) == (32768, "unchanged")

    def test_a_big_prompt_that_fits_a_row_above_the_ceiling_is_not_refused(self):
        result = plan(prompt_for_estimate(20000), row=32768, ceiling=16384)
        assert (result.send, result.outcome) == (32768, "unchanged")

    def test_ceiling_never_lowers_a_known_server_default(self):
        result = plan(prompt_for_estimate(5000), row=None, ceiling=4096, server_default=65536)
        assert (result.send, result.outcome) == (65536, "unchanged")

    def test_send_is_never_below_the_row(self):
        for row in (1024, 2048, 3000, 8192, 32768):
            assert plan(prompt_for_estimate(500), row=row).send >= row

    def test_empty_prompt_and_none_prompt(self):
        for prompt in ("", None):
            result = plan(prompt)
            assert (result.estimate, result.send) == (32, 2048)

    def test_multibyte_prompt_is_planned_on_bytes(self):
        ascii_plan = plan("a" * 9000)
        multibyte = plan("é" * 4500)
        assert multibyte.estimate == ascii_plan.estimate

    def test_sh_intent_on_a_2048_row_plans_8192(self):
        result = plan("a" * SH_INTENT_BYTES, row=2048)
        assert (result.send, result.effective, result.outcome) == (8192, 8192, "raised")
        assert result.estimate == SH_INTENT_ESTIMATE
        assert result.reserve == 384

    def test_maximal_bounded_inputs_plan_exactly_8192_and_the_mark_stays_there(self):
        # The template (12,919 bytes with its own placeholders) plus the strict
        # callers' input bounds: query 1,000, prev_query and prev_response 400
        # each, four entity fields of 100.
        bounded = "a" * (SH_INTENT_BYTES + 1000 + 400 + 400 + 4 * 100)
        marks = ContextMarks()
        for _ in range(3):
            result = plan(bounded, row=2048, marks=marks)
            assert (result.send, result.outcome) in ((8192, "raised"), (8192, "sticky"))
            marks.record(MODEL, result.floor, result.send)
        assert marks.get(MODEL, 2048) == 8192


class TestUnknownFloor:
    def test_small_need_sends_nothing_and_records_nothing(self):
        marks = ContextMarks()
        result = plan("x", row=None, marks=marks)
        assert (result.send, result.effective, result.outcome, result.floor) == (None, None, "unchanged", None)
        assert marks.get(MODEL, None) == 0

    def test_large_need_sends_nothing_and_reports_unknown_default(self):
        marks = ContextMarks()
        result = plan(prompt_for_estimate(3000), row=None, marks=marks)
        assert (result.send, result.outcome) == (None, "unknown_default")
        assert marks.get(MODEL, None) == 0

    def test_over_ceiling_still_refuses_with_an_unknown_floor(self):
        result = plan(prompt_for_estimate(20000), row=None)
        assert result.outcome == "over_ceiling"

    def test_marks_ignore_an_unknown_floor(self):
        marks = ContextMarks()
        marks.record(MODEL, None, 8192)
        assert marks.get(MODEL, None) == 0


class TestStickyMarks:
    def test_mark_is_monotone(self):
        marks = ContextMarks()
        marks.record(MODEL, 2048, 8192)
        marks.record(MODEL, 2048, 4096)
        assert marks.get(MODEL, 2048) == 8192

    def test_a_row_decrease_starts_a_fresh_mark(self):
        marks = ContextMarks()
        marks.record(MODEL, 4096, 8192)
        assert plan("x", row=4096, require_full=False, marks=marks).send == 8192
        assert plan("x", row=2048, require_full=False, marks=marks).send == 2048
        assert plan("x", row=2048, marks=marks).send == 2048

    def test_marks_are_per_model(self):
        marks = ContextMarks()
        marks.record(MODEL, 2048, 8192)
        assert plan("x", row=2048, require_full=False, marks=marks, model="other").send == 2048

    def test_a_strict_call_reuses_the_mark_without_a_raise(self):
        marks = ContextMarks()
        marks.record(MODEL, 2048, 8192)
        result = plan(prompt_for_estimate(1000), row=2048, marks=marks)
        assert (result.send, result.outcome) == (8192, "sticky")

    def test_planning_itself_never_records(self):
        class Counting(ContextMarks):
            calls = 0

            def record(self, model, floor, sent):
                type(self).calls += 1
                super().record(model, floor, sent)

        marks = Counting()
        plan(prompt_for_estimate(5000), marks=marks)
        plan(prompt_for_estimate(5000), marks=marks, mode="observe")
        assert Counting.calls == 0


class TestObserveMode:
    def test_observe_sends_todays_value(self):
        result = plan("a" * SH_INTENT_BYTES, row=2048, mode="observe")
        assert (result.send, result.effective) == (2048, 2048)
        assert result.outcome == "raised"  # computed, not applied

    def test_observe_without_a_row_sends_nothing(self):
        result = plan("a" * SH_INTENT_BYTES, row=None, mode="observe", server_default=4096)
        assert (result.send, result.effective) == (None, 4096)

    def test_observe_ignores_the_sticky_mark(self):
        marks = ContextMarks()
        marks.record(MODEL, 2048, 8192)
        assert plan("x", row=2048, require_full=False, marks=marks, mode="observe").send == 2048

    def test_observe_strict_over_ceiling_sends_todays_value(self):
        result = plan(prompt_for_estimate(20000), row=2048, mode="observe")
        assert (result.send, result.outcome) == (2048, "over_ceiling")


def count_plan(prompt, effective, *, row=None, **kw) -> ContextPlan:
    return plan(prompt, row=effective if row is None else row, require_full=False, **kw)


class TestTruncationSignal:
    sh_intent = "a" * SH_INTENT_BYTES

    def test_bench_truncation_at_2048(self):
        assert truncation_suspected(count_plan(self.sh_intent, 2048), 1026) is True

    def test_bench_truncation_at_4096(self):
        assert truncation_suspected(count_plan(self.sh_intent, 4096), 2050) is True

    def test_complete_prompt_at_8192(self):
        assert truncation_suspected(count_plan(self.sh_intent, 8192), 4441) is False

    def test_motion_prompt_is_not_truncated(self):
        assert truncation_suspected(count_plan("a" * 3418, 2048), 866) is False

    @pytest.mark.parametrize("count,expected", [(1025, True), (1026, True), (1027, True), (1024, False), (1028, False)])
    def test_signature_edges(self, count, expected):
        # estimate 2100 keeps the ratio rule out of the way (1028 / 2100 > 0.35)
        assert truncation_suspected(count_plan(prompt_for_estimate(2100), 2048), count) is expected

    def test_estimate_equal_to_the_context_is_not_a_signature(self):
        assert truncation_suspected(count_plan(prompt_for_estimate(2048), 2048), 1026) is False

    def test_ratio_exactly_035_is_not_truncated(self):
        result = count_plan(prompt_for_estimate(1000), 8192)
        assert truncation_suspected(result, 350) is False
        assert truncation_suspected(result, 349) is True

    def test_ratio_needs_an_estimate_of_256(self):
        assert truncation_suspected(count_plan(prompt_for_estimate(255), 8192), 1) is False
        low = count_plan(prompt_for_estimate(256), 8192)
        assert truncation_suspected(low, 89) is True
        assert truncation_suspected(low, 90) is False

    def test_no_count_is_never_truncated(self):
        assert truncation_suspected(count_plan(self.sh_intent, 2048), None) is False

    def test_a_non_ollama_backend_is_never_truncated(self):
        assert truncation_suspected(count_plan(self.sh_intent, 2048), 1026, backend="mlx") is False

    @pytest.mark.parametrize("count", [1026, 2050])
    def test_unknown_size_scans_every_power_of_two(self, count):
        result = plan(self.sh_intent, row=None, require_full=False)
        assert result.effective is None
        assert truncation_suspected(result, count) is True

    def test_unknown_size_complete_prompt_is_false(self):
        result = plan(self.sh_intent, row=None, require_full=False)
        assert truncation_suspected(result, 4441) is False

    def test_unknown_size_scan_stops_at_the_estimate(self):
        # 8194 == 16384 // 2 + 2, but a 4,647-token estimate can't fill 16384
        result = plan(self.sh_intent, row=None, require_full=False)
        assert truncation_suspected(result, 8194) is False


class TestCapturedPromptsAreNotFlagged:
    @pytest.fixture(scope="module")
    def captures(self):
        return json.loads(FIXTURE.read_text())["captures"]

    def test_floors_and_named_member(self, captures):
        fact_check = [c for c in captures if c["role"] == "fact_check"]
        assert len(fact_check) >= 38
        assert len(captures) >= 100
        assert captures[0]["role"] == "fact_check"
        assert (captures[0]["case_id"], captures[0]["prompt_bytes"], captures[0]["prompt_eval_count"]) == ("fc_p01", 810, 196)

    @pytest.mark.parametrize("effective", [2048, None])
    def test_no_capture_is_flagged(self, captures, effective):
        flagged = []
        for entry in captures:
            result = plan("a" * entry["prompt_bytes"], row=effective, require_full=False)
            if truncation_suspected(result, entry["prompt_eval_count"]):
                flagged.append((entry["role"], entry["case_id"]))
        assert flagged == []

    def test_every_role_is_present(self, captures):
        assert {c["role"] for c in captures} >= {"fact_check", "intent", "synthesis", "sh_motion", "sh_sequence", "sh_occupancy"}


def test_budget_exceeded_carries_plan_and_reason_without_prompt_text():
    result = plan("secret-marker " * 100, row=2048, ceiling=2048)
    error = ContextBudgetExceeded(result, "over_ceiling")
    assert error.plan is result and error.reason == "over_ceiling"
    assert "secret-marker" not in str(error)
    assert isinstance(error, RuntimeError)


class _TestConfig(AthenaConfig):
    model_config = SettingsConfigDict(env_file=None, env_ignore_empty=False, extra="ignore")


@pytest.fixture
def clean_env(monkeypatch):
    for name in ("LLM_CONTEXT_GUARD", "LLM_CONTEXT_CEILING", "LLM_SERVER_DEFAULT_CONTEXT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _load(caplog):
    with caplog.at_level(logging.WARNING, logger="shared.config"):
        config = _TestConfig()
    return config, [r for r in caplog.records if r.name == "shared.config"]


class TestConfigFields:
    def test_defaults(self, clean_env, caplog):
        config, records = _load(caplog)
        assert (config.llm_context_guard, config.llm_context_ceiling, config.llm_server_default_context) == ("enforce", 16384, 0)
        assert not [r for r in records if "llm_" in r.getMessage()]

    def test_valid_values_are_kept(self, clean_env, caplog):
        clean_env.setenv("LLM_CONTEXT_GUARD", "Observe")
        clean_env.setenv("LLM_CONTEXT_CEILING", "8192")
        clean_env.setenv("LLM_SERVER_DEFAULT_CONTEXT", "4096")
        config, records = _load(caplog)
        assert (config.llm_context_guard, config.llm_context_ceiling, config.llm_server_default_context) == ("observe", 8192, 4096)
        assert not [r for r in records if "llm_" in r.getMessage()]

    def test_unknown_guard_becomes_enforce_with_one_error(self, clean_env, caplog):
        clean_env.setenv("LLM_CONTEXT_GUARD", "bogus")
        config, records = _load(caplog)
        assert config.llm_context_guard == "enforce"
        errors = [r for r in records if r.levelno == logging.ERROR and "llm_context_guard_invalid" in r.getMessage()]
        assert len(errors) == 1
        assert "bogus" not in errors[0].getMessage()

    @pytest.mark.parametrize("value", ["10", "x", "2047", "-1", "", "1.5"])
    def test_bad_ceiling_becomes_16384_with_one_error(self, clean_env, caplog, value):
        clean_env.setenv("LLM_CONTEXT_CEILING", value)
        config, records = _load(caplog)
        assert config.llm_context_ceiling == 16384
        assert len([r for r in records if r.levelno == logging.ERROR and "llm_context_ceiling_invalid" in r.getMessage()]) == 1

    @pytest.mark.parametrize("value", ["-5", "x", "1.5"])
    def test_bad_server_default_becomes_unknown_with_one_error(self, clean_env, caplog, value):
        clean_env.setenv("LLM_SERVER_DEFAULT_CONTEXT", value)
        config, records = _load(caplog)
        assert config.llm_server_default_context == 0
        assert len([r for r in records if r.levelno == logging.ERROR and "llm_server_default_context_invalid" in r.getMessage()]) == 1

    def test_ceiling_below_a_known_server_default_is_kept_with_one_warning(self, clean_env, caplog):
        clean_env.setenv("LLM_CONTEXT_CEILING", "4096")
        clean_env.setenv("LLM_SERVER_DEFAULT_CONTEXT", "65536")
        config, records = _load(caplog)
        assert (config.llm_context_ceiling, config.llm_server_default_context) == (4096, 65536)
        warnings = [r for r in records if r.levelno == logging.WARNING and "llm_context_ceiling_below_server_default" in r.getMessage()]
        assert len(warnings) == 1
        assert not [r for r in records if r.levelno >= logging.ERROR and "llm_" in r.getMessage()]
