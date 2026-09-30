"""Gateway fast-path time/date answers use DEFAULT_TIMEZONE."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ._clock_fixture import UTC_POD, clock  # noqa: F401  (fixture)


def _run(command: str) -> str:
    from gateway.simple_commands import execute_simple_command

    return asyncio.run(execute_simple_command(command, {}, None, "", ""))


def _hour_words(now: datetime) -> str:
    return f"It's {now.strftime('%I').lstrip('0')} "


def test_time_on_a_utc_pod_is_the_property_hour(clock):
    clock.use(UTC_POD)
    before = datetime.now(ZoneInfo(UTC_POD[1]))
    answer = _run("time")
    after = datetime.now(ZoneInfo(UTC_POD[1]))
    assert any(answer.startswith(_hour_words(t)) for t in (before, after)), answer


def test_frozen_time_and_date(clock):
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 9, 30, 3, 17, tzinfo=timezone.utc))
    assert _run("time") == "It's 11 17 in the evening."
    assert _run("date") == "Today is Tuesday, September 29, 2026."
