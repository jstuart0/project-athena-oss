"""shared.local_time: the property clock never depends on the process TZ."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from ._clock_fixture import clock  # noqa: F401  (fixture)

NY = "America/New_York"
UTC = timezone.utc


def _utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def test_frozen_instant_in_new_york(clock):
    from shared.local_time import local_now, local_today

    clock.use(("UTC", NY))
    clock.frozen_utc(_utc(2026, 9, 30, 3, 17))
    now = local_now()
    assert now.tzinfo is not None
    assert (now.year, now.month, now.day, now.hour, now.minute) == (2026, 9, 29, 23, 17)
    assert local_today() == date(2026, 9, 29)


def test_dst_fold_on_fall_back(clock):
    from shared.local_time import local_now

    clock.property_zone(NY)
    clock.frozen_utc(_utc(2026, 11, 1, 5, 30))
    first = local_now()
    clock.frozen_utc(_utc(2026, 11, 1, 6, 30))
    second = local_now()
    assert (first.hour, first.minute, first.fold) == (1, 30, 0)
    assert first.utcoffset() == timedelta(hours=-4)
    assert (second.hour, second.minute, second.fold) == (1, 30, 1)
    assert second.utcoffset() == timedelta(hours=-5)


def test_date_after_fall_back_is_not_a_fixed_offset(clock):
    from shared.local_time import local_now

    clock.property_zone(NY)
    clock.frozen_utc(_utc(2026, 11, 2, 4, 30))
    now = local_now()
    assert (now.month, now.day, now.hour, now.minute) == (11, 1, 23, 30)
    assert now.strftime("%A") == "Sunday"


def test_spring_forward(clock):
    from shared.local_time import local_now

    clock.property_zone(NY)
    clock.frozen_utc(_utc(2026, 3, 8, 7, 30))
    now = local_now()
    assert (now.hour, now.minute) == (3, 30)
    assert now.utcoffset() == timedelta(hours=-4)


def test_zone_ahead_of_utc(clock):
    from shared.local_time import local_today

    clock.property_zone("Pacific/Auckland")
    clock.frozen_utc(_utc(2026, 9, 29, 12, 30))
    assert local_today() == date(2026, 9, 30)


def test_day_bounds_utc(clock):
    from shared.local_time import local_day_bounds_utc

    clock.property_zone(NY)
    start, end = local_day_bounds_utc(date(2026, 11, 1))
    assert start == _utc(2026, 11, 1, 4, 0)
    assert end == _utc(2026, 11, 2, 5, 0)
    assert end - start == timedelta(hours=25)
    start, end = local_day_bounds_utc(date(2026, 9, 29))
    assert (start, end) == (_utc(2026, 9, 29, 4, 0), _utc(2026, 9, 30, 4, 0))


def test_day_bounds_default_to_local_today(clock):
    from shared.local_time import local_day_bounds_utc

    clock.property_zone(NY)
    clock.frozen_utc(_utc(2026, 9, 30, 3, 17))
    assert local_day_bounds_utc() == (_utc(2026, 9, 29, 4, 0), _utc(2026, 9, 30, 4, 0))
    assert local_day_bounds_utc(days=7)[1] == _utc(2026, 10, 6, 4, 0)


def test_process_tz_is_never_read(clock):
    from shared.local_time import local_now

    clock.use(("Asia/Tokyo", NY))
    before = datetime.now(ZoneInfo(NY))
    now = local_now()
    after = datetime.now(ZoneInfo(NY))
    assert now.utcoffset() == before.utcoffset()
    assert before - timedelta(seconds=5) <= now <= after + timedelta(seconds=5)


def test_invalid_zone_falls_back_to_utc_and_logs_once(clock, captured_logs):
    from shared.local_time import local_tz

    clock.property_zone("Not/AZone")
    assert local_tz() is UTC
    first = [e for e in captured_logs if e.get("event") == "local_timezone_invalid"]
    assert len(first) == 1
    assert first[0]["log_level"] == "error"
    assert local_tz() is UTC
    second = [e for e in captured_logs if e.get("event") == "local_timezone_invalid"]
    assert len(second) == 1


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_zone_falls_back_to_utc(clock, name):
    from shared.local_time import local_tz

    clock.property_zone(name)
    assert local_tz() is UTC
