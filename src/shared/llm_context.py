"""Context-window planning for Ollama generate calls.

Pure and stdlib-only. `LLMRouter` asks `plan_context` what `options.num_ctx`
to send and `truncation_suspected` whether the answer came from a cut-off
prompt; this module never does I/O, never logs and never raises on a plan.

The rule (monotone: a call never gets less context than it gets today):

- Floor `F` is the row's `num_ctx` when valid, else the operator-stated
  server default, else unknown.
- A default call sends `max(F, HW)`. `HW` is the largest size a strict call
  has sent for that `(model, F)` in this process, so only callers that opt in
  and bound their own input can move a model's context.
- A strict call (`require_full`) plans a power-of-two bucket for the prompt
  plus the output reserve, bounded by the ceiling. When `F` is unknown it
  sends nothing and relies on the after-the-fact truncation check.

Ollama's `/api/generate` truncates silently: the reported `prompt_eval_count`
lands on `num_ctx // 2 + 2`. `truncation_suspected` recognises that, and a
prompt count far below the estimate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Tuple

BYTES_PER_TOKEN_NUM = 5  # 2.8 bytes per token == 14/5, kept in integers
BYTES_PER_TOKEN_DEN = 14
ESTIMATE_OVERHEAD_TOKENS = 32
DEFAULT_RESERVE_TOKENS = 512
MIN_BUCKET = 2048
MAX_BUCKET = 131072
# Rule (b) of the truncation signal: below 35% of the estimate is a cut prompt.
RATIO_MIN_ESTIMATE = 256
RATIO_PERCENT = 35
# Rule (a): the reported count lands within this of `num_ctx // 2 + 2`.
SIGNATURE_TOLERANCE = 1

Outcome = Literal["unchanged", "sticky", "raised", "over_ceiling", "unknown_default"]


@dataclass(frozen=True)
class ContextSettings:
    mode: str  # "enforce" or "observe"
    ceiling: int
    server_default: int  # 0 = unknown


@dataclass(frozen=True)
class ContextPlan:
    send: Optional[int]
    effective: Optional[int]
    estimate: int
    reserve: int
    outcome: Outcome
    floor: Optional[int] = None


class ContextBudgetExceeded(RuntimeError):
    """A strict call can't be answered from a complete prompt.

    `reason` is `over_ceiling`, `floor_unknown` or `unsupported_backend`
    (all refused before any request) or `truncated` (the response shows the
    prompt was cut). The message never carries prompt
    text.
    """

    def __init__(
        self,
        plan: ContextPlan,
        reason: str,
        *,
        prompt_eval_count: Optional[int] = None,
        eval_count: Optional[int] = None,
    ):
        super().__init__(f"llm context budget exceeded: {reason}")
        self.plan = plan
        self.reason = reason
        # Tokens a refused (truncated) response already consumed, for accounting.
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count


class ContextMarks:
    """Per-process high-water marks keyed by (model, floor). Never decrease."""

    def __init__(self) -> None:
        self._marks: Dict[Tuple[str, int], int] = {}

    def get(self, model: str, floor: Optional[int]) -> int:
        if floor is None:
            return 0
        return self._marks.get((model, floor), 0)

    def record(self, model: str, floor: Optional[int], sent: int) -> None:
        if floor is None:
            return
        key = (model, floor)
        if sent > self._marks.get(key, 0):
            self._marks[key] = sent


def estimate_tokens(text: Optional[str]) -> int:
    """ceil(utf8_bytes / 2.8) + 32: never under a measured smart-home prompt."""
    size = len((text or "").encode("utf-8"))
    return -(-size * BYTES_PER_TOKEN_NUM // BYTES_PER_TOKEN_DEN) + ESTIMATE_OVERHEAD_TOKENS


def valid_ctx(value: Any) -> Optional[int]:
    """A positive int that isn't a bool; anything else is absent."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def context_floor(row_ctx: Any, settings: ContextSettings) -> Optional[int]:
    row = valid_ctx(row_ctx)
    if row is not None:
        return row
    return settings.server_default if settings.server_default > 0 else None


def reserve_tokens(num_predict: Any, max_tokens: Any) -> int:
    """The merged num_predict, else the caller's max_tokens, else 512."""
    for candidate in (num_predict, max_tokens):
        if valid_ctx(candidate) is not None:
            return candidate
    return DEFAULT_RESERVE_TOKENS


def _next_pow2(value: int) -> int:
    return 1 << max(value - 1, 0).bit_length()


def plan_context(
    *,
    prompt_text: Optional[str],
    row_ctx: Any,
    num_predict: Any,
    max_tokens: Any,
    require_full: bool,
    marks: ContextMarks,
    model: str,
    settings: ContextSettings,
) -> ContextPlan:
    estimate = estimate_tokens(prompt_text)
    reserve = reserve_tokens(num_predict, max_tokens)
    floor = context_floor(row_ctx, settings)
    mark = marks.get(model, floor)
    base = max(floor, mark) if floor is not None else None
    sticky = floor is not None and mark > floor

    send: Optional[int] = base
    effective: Optional[int] = base
    outcome: Outcome = "sticky" if sticky else "unchanged"

    if require_full:
        need = estimate + reserve
        bucket = max(_next_pow2(need), MIN_BUCKET)
        if base is None:
            # Nothing is sent without a known floor: a need above the smallest
            # bucket can't be guaranteed, so the router refuses it (the
            # post-hoc check can't see a non-power-of-two server context).
            effective = None
            if bucket > settings.ceiling:
                outcome = "over_ceiling"
            elif need > MIN_BUCKET:
                outcome = "unknown_default"
        elif bucket > settings.ceiling:
            # The ceiling bounds raises only: a prompt that doesn't fit what
            # would be sent anyway is refused; one that fits keeps the base.
            if need > base:
                outcome = "over_ceiling"
        else:
            send = effective = max(base, min(bucket, settings.ceiling))
            if send > base:
                outcome = "raised"

    if settings.mode != "enforce":
        # Observe computes and reports, but sends what is sent today.
        send = valid_ctx(row_ctx)
        effective = floor

    return ContextPlan(
        send=send,
        effective=effective,
        estimate=estimate,
        reserve=reserve,
        outcome=outcome,
        floor=floor,
    )


def _power_of_two_sizes() -> Tuple[int, ...]:
    sizes = []
    size = MIN_BUCKET
    while size <= MAX_BUCKET:
        sizes.append(size)
        size *= 2
    return tuple(sizes)


_SCAN_SIZES = _power_of_two_sizes()


def _signature_matches(estimate: int, count: int, size: int) -> bool:
    return estimate > size and abs(count - (size // 2 + 2)) <= SIGNATURE_TOLERANCE


def truncation_suspected(
    plan: ContextPlan, prompt_eval_count: Optional[int], *, backend: str = "ollama"
) -> bool:
    """Whether the reported prompt count shows the prompt was cut.

    (a) the count sits on `num_ctx // 2 + 2` (+-1) while the estimate says the
    prompt was bigger than `num_ctx`; with the size unknown, any power of two
    is tried. (b) with an estimate of at least 256 tokens, the count is under
    35% of it.
    """
    if backend != "ollama" or prompt_eval_count is None:
        return False
    if isinstance(prompt_eval_count, bool) or not isinstance(prompt_eval_count, int):
        return False
    sizes = (plan.effective,) if plan.effective is not None else _SCAN_SIZES
    if any(_signature_matches(plan.estimate, prompt_eval_count, size) for size in sizes):
        return True
    return (
        plan.estimate >= RATIO_MIN_ESTIMATE
        and prompt_eval_count * 100 < RATIO_PERCENT * plan.estimate
    )
