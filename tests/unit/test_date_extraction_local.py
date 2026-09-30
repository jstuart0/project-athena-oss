"""extract_date_from_query: the property's "today" decides the year (both copies)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from . import _public_audience_harness as h  # noqa: F401  (orchestrator import discipline)
from ._clock_fixture import DATE_DIVERGENCE, clock, property_today  # noqa: F401  (fixture)

import orchestrator.main as orchestrator_main
from orchestrator.utils import helpers as utils_helpers

COPIES = [
    pytest.param(orchestrator_main.extract_date_from_query, id="main"),
    pytest.param(utils_helpers.extract_date_from_query, id="utils"),
]
NY = "America/New_York"


def _phrasings(d: date) -> list[str]:
    month = d.strftime("%B").lower()
    return [f"what's on {month} {d.day}", f"events on the {d.day} of {month}", f"plans for {d.month}/{d.day}"]


def _expected_year(target_md: date, today: date) -> int:
    candidate = date(today.year, target_md.month, target_md.day)
    return today.year if candidate >= today else today.year + 1


def _check(extract, zone: str, offset_days: int):
    for _ in range(2):
        today = property_today(zone)
        target = today + timedelta(days=offset_days)
        results = [extract(q) for q in _phrasings(target)]
        if property_today(zone) == today:
            break
    expected = _expected_year(target, today)
    for query, result in zip(_phrasings(target), results):
        assert result is not None, query
        assert result[1] == f"{expected:04d}-{target.month:02d}-{target.day:02d}", query


@pytest.mark.parametrize("extract", COPIES)
def test_today_is_this_year(clock, extract):
    clock.process_tz(NY)
    clock.property_zone(NY)
    _check(extract, NY, 0)


@pytest.mark.parametrize("extract", COPIES)
def test_property_tomorrow_when_the_process_date_is_ahead(clock, extract):
    clock.use(DATE_DIVERGENCE)
    _check(extract, DATE_DIVERGENCE[1], 1)


@pytest.mark.parametrize("extract", COPIES)
def test_yesterday_rolls_to_next_year(clock, extract):
    clock.process_tz(NY)
    clock.property_zone(NY)
    _check(extract, NY, -1)


@pytest.mark.parametrize("extract", COPIES)
@pytest.mark.parametrize("query", ["what's on feb 30", "plans for 4/31"])
def test_impossible_date_is_none(clock, extract, query):
    clock.property_zone(NY)
    assert extract(query) is None
