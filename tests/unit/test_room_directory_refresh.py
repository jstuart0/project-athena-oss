"""The orchestrator's readers of the area registry ask for a refresh when it is stale.

``HARegistry.read()`` is ``current()`` that also starts a background refresh once
the snapshot is older than its TTL. The two readers (the room directory provider
and the smart-home controller's registry lookup) call ``read()``, so a registry
that fell out of date, or never loaded because Home Assistant was down at start,
heals on use without a timer. The registry still fetches at most once per minimum
interval, and a fresh snapshot costs nothing.
"""
from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import room_directory as rd  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from shared import ha_registry as hr  # noqa: E402

RAW = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json").read_text(encoding="utf-8")
)


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Fetcher:
    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        return copy.deepcopy(RAW)


def config():
    return SimpleNamespace(ha_light_groups="", ha_satellite_room_map="")


def make(clock=None):
    clock = clock or Clock()
    fetcher = Fetcher()
    registry = hr.HARegistry(fetcher, monotonic=clock, ttl=300, min_refresh_interval=60)
    return clock, fetcher, registry


async def settle():
    await asyncio.sleep(0.01)


def test_a_directory_read_of_a_registry_with_no_snapshot_starts_one_refresh_and_then_has_the_areas():
    async def go():
        clock, fetcher, registry = make()
        provider = rd.RoomDirectoryProvider(registry, None, monotonic=clock, config=config)
        assert provider.current().resolves("galley") is None
        await settle()
        calls_after_first = fetcher.calls
        directory = provider.current()
        await settle()
        return calls_after_first, fetcher.calls, directory.resolves("galley")

    first, calls, galley = asyncio.run(go())
    assert (first, calls) == (1, 1) and galley is not None


def test_a_fresh_snapshot_costs_no_refresh():
    async def go():
        clock, fetcher, registry = make()
        provider = rd.RoomDirectoryProvider(registry, None, monotonic=clock, config=config)
        provider.current()
        await settle()
        for _ in range(20):
            provider.current()
        await settle()
        return fetcher.calls

    assert asyncio.run(go()) == 1


def test_a_stale_snapshot_refreshes_once_and_two_reads_inside_the_interval_share_it():
    async def go():
        clock, fetcher, registry = make()
        provider = rd.RoomDirectoryProvider(registry, None, monotonic=clock, config=config)
        provider.current()
        await settle()
        clock.now += 400  # past the TTL and the minimum interval
        provider.current()
        provider.current()
        await settle()
        clock.now += 10  # inside the interval, though the snapshot is stale again only after the TTL
        provider.current()
        await settle()
        return fetcher.calls

    assert asyncio.run(go()) == 2


def test_the_smart_home_controllers_lookup_reads_through_the_same_door():
    from orchestrator.smart_home_controller import SmartHomeController

    async def go():
        clock, fetcher, registry = make()
        _runtime.set_ha_registry(registry)
        try:
            assert SmartHomeController._registry_snapshot() is None  # nothing loaded: a refresh starts
            await settle()
            snapshot = SmartHomeController._registry_snapshot()
            await settle()
            clock.now += 400
            SmartHomeController._registry_snapshot()
            await settle()
            return fetcher.calls, snapshot
        finally:
            _runtime.set_ha_registry(None)

    calls, snapshot = asyncio.run(go())
    assert calls == 2 and snapshot is not None


def test_a_registry_that_is_off_is_never_asked_for_a_fetch():
    registry = hr.HARegistry(None)
    provider = rd.RoomDirectoryProvider(registry, None, config=config)
    provider.current()
    assert registry.state == hr.STATE_DISABLED and registry.read() is None
