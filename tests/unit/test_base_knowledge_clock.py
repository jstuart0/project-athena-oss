"""{dynamic:current_time}/{dynamic:current_date} follow DEFAULT_TIMEZONE, not the process TZ."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ._clock_fixture import DATE_DIVERGENCE, UTC_POD, clock  # noqa: F401  (fixture)


def resolve_dynamic_value(value: str) -> str:
    # Imported per call: base_knowledge_utils reads get_config() at import,
    # which would cache the config during collection for every later module.
    from shared.base_knowledge_utils import resolve_dynamic_value as resolve

    return resolve(value)


def _bracket(zone: str, fmt: str, call):
    before = datetime.now(ZoneInfo(zone)).strftime(fmt)
    got = call()
    after = datetime.now(ZoneInfo(zone)).strftime(fmt)
    return got, {before, after}


def test_current_time_on_a_utc_pod(clock):
    clock.use(UTC_POD)
    got, expected = _bracket(UTC_POD[1], "%-I:%M %p", lambda: resolve_dynamic_value("{dynamic:current_time}"))
    assert got in expected


def test_current_date_when_process_date_differs(clock):
    clock.use(DATE_DIVERGENCE)
    got, expected = _bracket(
        DATE_DIVERGENCE[1], "%A, %B %d, %Y", lambda: resolve_dynamic_value("{dynamic:current_date}")
    )
    assert got in expected


def test_frozen_instant_renders_exact_strings(clock):
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 9, 30, 3, 17, tzinfo=timezone.utc))
    assert resolve_dynamic_value("{dynamic:current_date}") == "Tuesday, September 29, 2026"
    assert resolve_dynamic_value("{dynamic:current_time}") == "11:17 PM"


def test_frozen_instant_after_fall_back(clock):
    clock.use(UTC_POD)
    clock.frozen_utc(datetime(2026, 11, 2, 4, 30, tzinfo=timezone.utc))
    assert resolve_dynamic_value("{dynamic:current_date} {dynamic:current_time}") == "Sunday, November 01, 2026 11:30 PM"
