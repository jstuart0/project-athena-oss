"""RAG date windows follow DEFAULT_TIMEZONE on a UTC pod.

Frozen at 2026-09-30T03:17Z, which is 2026-09-29 23:17 in New York: the
property's "today" is still the 29th while the pod's UTC date is the 30th.
The instant is frozen twice: in shared.local_time, and in the service
module's own ``datetime`` (answering naive calls in the process zone, as a
real UTC pod would), so a process-TZ read can't pass on the real date.

The sports case imports feedparser, which the orchestrator lock doesn't
carry, so the CI behaviour job deselects it (-k "not sports"); it runs in a
venv built from the sports image's lock.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import unittest.mock as mock
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ._clock_fixture import UTC_POD, clock  # noqa: F401  (fixture)

_SRC = Path(__file__).resolve().parents[2] / "src"
FROZEN = datetime(2026, 9, 30, 3, 17, tzinfo=timezone.utc)

for _mod in ("prometheus_client",):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()


def _load(service_dir: str):
    name = f"_test_local_dates_{service_dir}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SRC / "rag" / service_dir / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _frozen_datetime(instant: datetime) -> type:
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return datetime.fromtimestamp(instant.timestamp())
            return instant.astimezone(tz)

        @classmethod
        def today(cls):
            return cls.now()

        @classmethod
        def utcnow(cls):
            return instant.astimezone(timezone.utc).replace(tzinfo=None)

    return FrozenDatetime


@pytest.fixture
def at(clock, monkeypatch):
    clock.use(UTC_POD)

    def freeze(module, instant: datetime = FROZEN):
        clock.frozen_utc(instant)
        monkeypatch.setattr(module, "datetime", _frozen_datetime(instant))
        return module

    return freeze


def test_community_search_keeps_tonights_event(at, monkeypatch):
    community = at(_load("community_events"))
    event = {"title": "Late Show", "start_date": "2026-09-29"}
    monkeypatch.setattr(community, "get_all_cached_events", mock.AsyncMock(return_value=[event]))
    result = asyncio.run(community.search_events())
    assert [e["title"] for e in result["events"]] == ["Late Show"]


def test_community_timestamp_to_date_is_local(at):
    community = at(_load("community_events"))
    assert community.timestamp_to_date(1790738220) == "2026-09-29"


def test_seatgeek_today_is_the_local_day_in_utc(at):
    seatgeek = at(_load("seatgeek_events"))
    assert seatgeek.get_date_range("today") == ("2026-09-29T04:00:00", "2026-09-30T04:00:00")


def test_seatgeek_explicit_date_spans_the_dst_change(at):
    seatgeek = at(_load("seatgeek_events"))
    assert seatgeek.get_date_range("2026-11-01") == ("2026-11-01T04:00:00", "2026-11-02T05:00:00")


def test_seatgeek_tomorrow_across_the_dst_change(at):
    seatgeek = at(_load("seatgeek_events"), datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc))
    assert seatgeek.get_date_range("tomorrow") == ("2026-11-01T04:00:00", "2026-11-02T05:00:00")


def test_transportation_friday_night_is_a_weekday(at, monkeypatch):
    transportation = at(_load("transportation"), datetime(2026, 10, 3, 2, 0, tzinfo=timezone.utc))
    service = {
        "name": "Harbor Ferry",
        "type": "ferry",
        "free": True,
        "hours": {"weekday": {"start": "06:00", "end": "23:00"}, "weekend": {"start": "09:00", "end": "20:00"}},
        "frequency_minutes": 30,
        "stops": [],
    }
    config = mock.MagicMock()
    config.static_services = {"harbor-ferry": service}
    monkeypatch.setattr(transportation, "transit_config", config)
    result = asyncio.run(transportation.get_water_transit())
    assert result["day_type"] == "weekday"
    assert result["services"][0]["hours"] == service["hours"]["weekday"]


def test_sports_window_keeps_tonights_event(at):
    sports = at(_load("sports"))
    events = [{"strEvent": "Tonight", "dateEvent": "2026-09-29"}, {"strEvent": "Past", "dateEvent": "2026-09-28"}]
    assert [e["strEvent"] for e in sports._filter_events_window(events)] == ["Tonight"]
