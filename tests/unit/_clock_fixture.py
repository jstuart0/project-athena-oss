"""Isolation for clock tests: the property zone, the process TZ, a frozen instant.

``clock.property_zone(name)`` sets ``DEFAULT_TIMEZONE`` and drops the cached
config and the ``local_time`` log latch. ``clock.process_tz(name)`` sets the
process ``TZ`` with ``time.tzset()``, so naive ``datetime.now()`` calls,
including function-local imports, see that zone. ``clock.frozen_utc(instant)``
replaces ``shared.local_time._utc_now``. Everything is restored on teardown.

Zone pairs used by the tests:

- UTC pod: process ``UTC``, property ``America/New_York``. Clock strings
  always differ by 4-5 hours.
- Date divergence: process ``Pacific/Kiritimati`` (UTC+14), property
  ``Pacific/Pago_Pago`` (UTC-11). They're 25 hours apart, so the process
  date always differs from the property date.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

SRC = str(Path(__file__).resolve().parents[2] / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

UTC_POD = ("UTC", "America/New_York")
DATE_DIVERGENCE = ("Pacific/Kiritimati", "Pacific/Pago_Pago")

_UNSET = object()


def _clear_config_caches() -> None:
    import shared.config

    shared.config._clear_cache_for_tests()
    local_time = sys.modules.get("shared.local_time")
    if local_time is not None:
        local_time._reset_for_tests()


class Clock:
    def __init__(self) -> None:
        self._env: dict[str, object] = {}
        self._frozen = _UNSET

    def _save_env(self, key: str) -> None:
        if key not in self._env:
            self._env[key] = os.environ.get(key, _UNSET)

    def property_zone(self, name: str) -> None:
        self._save_env("DEFAULT_TIMEZONE")
        os.environ["DEFAULT_TIMEZONE"] = name
        _clear_config_caches()

    def process_tz(self, name: str) -> None:
        if not hasattr(time, "tzset"):
            pytest.skip("time.tzset is unavailable on this platform")
        self._save_env("TZ")
        os.environ["TZ"] = name
        time.tzset()

    def frozen_utc(self, instant: datetime) -> None:
        from shared import local_time

        if self._frozen is _UNSET:
            self._frozen = local_time._utc_now
        local_time._utc_now = lambda: instant

    def use(self, pair: tuple[str, str]) -> None:
        process, prop = pair
        self.process_tz(process)
        self.property_zone(prop)

    def restore(self) -> None:
        if self._frozen is not _UNSET:
            from shared import local_time

            local_time._utc_now = self._frozen
        for key, value in self._env.items():
            if value is _UNSET:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if "TZ" in self._env and hasattr(time, "tzset"):
            time.tzset()
        _clear_config_caches()


def property_today(zone: str) -> date:
    return datetime.now(ZoneInfo(zone)).date()


@pytest.fixture
def clock():
    c = Clock()
    yield c
    c.restore()
