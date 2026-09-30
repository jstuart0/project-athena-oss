"""SequenceExecutor waits are real elapsed time to a local wall-clock target."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from ._clock_fixture import UTC_POD, clock  # noqa: F401  (fixture)

from orchestrator.sequence_executor import SequenceExecutor


def _wait(time_str: str) -> float:
    return SequenceExecutor._calculate_wait_until(MagicMock(), time_str)


def test_wait_across_fall_back_is_real_elapsed_time(clock):
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 11, 1, 1, 30, tzinfo=timezone.utc))
    assert _wait("07:00") == 37800.0


def test_wait_later_the_same_local_day(clock):
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 9, 30, 3, 17, tzinfo=timezone.utc))
    assert _wait("23:30") == 780.0


def test_wait_for_a_passed_time_is_tomorrow(clock):
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 9, 30, 3, 17, tzinfo=timezone.utc))
    assert _wait("23:00") == 24 * 3600 - 17 * 60


def test_fall_back_repeated_hour_targets_the_next_occurrence(clock):
    """01:30 EST is the second 01:30 of the fall-back night (fold=1). Its
    first 01:45 (EDT) has passed; the next 01:45 is 15 minutes away."""
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))
    assert _wait("01:45") == 900.0


def test_fall_back_ambiguous_target_resolves_to_the_first_occurrence(clock):
    """Before the change, an ambiguous time means its first (EDT, fold=0)
    occurrence."""
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 11, 1, 4, 30, tzinfo=timezone.utc))  # 00:30 EDT
    assert _wait("01:30") == 3600.0


def test_spring_forward_gap_moves_forward(clock):
    """02:30 doesn't exist on 2026-03-08: it resolves forward to 03:30 EDT."""
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 3, 8, 6, 30, tzinfo=timezone.utc))  # 01:30 EST
    assert _wait("02:30") == 3600.0


def test_spring_forward_gap_already_passed_rolls_to_tomorrow(clock):
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 3, 8, 8, 0, tzinfo=timezone.utc))  # 04:00 EDT
    assert _wait("02:30") == 22.5 * 3600


@pytest.mark.parametrize("instant", [
    datetime(2026, 11, 1, 5, 0, tzinfo=timezone.utc),
    datetime(2026, 11, 1, 5, 59, tzinfo=timezone.utc),
    datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc),
    datetime(2026, 11, 1, 6, 59, tzinfo=timezone.utc),
    datetime(2026, 3, 8, 6, 59, tzinfo=timezone.utc),
    datetime(2026, 3, 8, 7, 0, tzinfo=timezone.utc),
])
@pytest.mark.parametrize("target", ["00:59", "01:00", "01:15", "01:59", "02:00", "02:30", "03:00"])
def test_wait_is_never_negative_and_under_two_days(clock, instant, target):
    clock.use(UTC_POD)
    clock.frozen_utc(instant)
    wait = _wait(target)
    assert 0 <= wait <= 25 * 3600
