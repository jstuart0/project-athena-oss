"""SequenceExecutor waits are real elapsed time to a local wall-clock target."""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

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
