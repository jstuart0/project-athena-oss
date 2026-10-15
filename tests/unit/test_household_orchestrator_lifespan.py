"""P2: the orchestrator lifespan loads the household settings without letting an
unreachable admin hold startup, and closes the reader before background work drains.

Runs the real lifespan (RAG and mode services are unreachable, which the
lifespan already tolerates); only the household fetch is replaced."""
from __future__ import annotations

import asyncio
import time

import pytest
from unittest import mock

import shared.config
from shared import household
from shared import household_settings
from orchestrator.nodes import _runtime

STARTUP_BUDGET_S = 3.0


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "k" * 40)
    monkeypatch.setenv("DEFAULT_CITY", "Denver")
    monkeypatch.setenv("DEFAULT_STATE", "CO")
    monkeypatch.delenv("HA_BED_WARMER_ENTITIES", raising=False)
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    shared.config._clear_cache_for_tests()
    yield
    shared.config._clear_cache_for_tests()
    _runtime.reset_for_test()


def _main(monkeypatch):
    import orchestrator.main as main

    monkeypatch.setattr(main, "ensure_gateway_running", mock.AsyncMock(return_value=True))
    return main


def _hanging_admin(monkeypatch):
    async def never():
        await asyncio.sleep(3600)

    monkeypatch.setattr(household, "_admin_fetch", lambda sections: never)


def _unreachable_admin(monkeypatch):
    async def refused():
        raise ConnectionError("admin down")

    monkeypatch.setattr(household, "_admin_fetch", lambda sections: refused)


async def _start_and_stop(main):
    started = time.monotonic()
    async with main.lifespan(main.app):
        elapsed = time.monotonic() - started
        return elapsed, household.location(), household.room_light_exclude_patterns(), household.bed_warmer()


def test_the_lifespan_configures_exactly_the_orchestrator_sections(monkeypatch):
    main = _main(monkeypatch)
    _unreachable_admin(monkeypatch)
    seen = {}
    real = household.configure

    def spy(**kwargs):
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(household, "configure", spy)
    asyncio.run(_start_and_stop(main))
    assert main.HOUSEHOLD_SECTIONS == ("location", "lights", "bed_warmer")
    assert seen["sections"] == main.HOUSEHOLD_SECTIONS
    assert seen["spawn"] is _runtime.spawn_background
    assert set(main.HOUSEHOLD_SECTIONS) <= set(household_settings.SECTIONS)


def test_a_hanging_admin_does_not_hold_startup_and_the_resolvers_serve_env(monkeypatch):
    main = _main(monkeypatch)
    _hanging_admin(monkeypatch)
    elapsed, location, patterns, bed = asyncio.run(_start_and_stop(main))
    assert elapsed < STARTUP_BUDGET_S
    assert (location.city, location.text, location.source) == ("Denver", "Denver, CO", "env")
    assert patterns.source == "default" and bed is None


def test_a_refusing_admin_starts_at_once_on_env(monkeypatch):
    main = _main(monkeypatch)
    _unreachable_admin(monkeypatch)
    elapsed, location, _, _ = asyncio.run(_start_and_stop(main))
    assert elapsed < STARTUP_BUDGET_S and location.city == "Denver"


def test_a_reachable_admin_is_loaded_before_the_first_request(monkeypatch):
    main = _main(monkeypatch)

    async def answer():
        return {"location": {"value": {"city": "Springfield", "state": "IL", "text": "Springfield, IL"}, "revision": 3}}

    monkeypatch.setattr(household, "_admin_fetch", lambda sections: answer)
    _, location, _, _ = asyncio.run(_start_and_stop(main))
    assert (location.city, location.source) == ("Springfield", "admin")


def test_shutdown_closes_the_reader_before_background_work_drains(monkeypatch):
    main = _main(monkeypatch)
    _unreachable_admin(monkeypatch)
    order = []
    real_aclose = household_settings.HouseholdSettingsCache.aclose
    real_drain = _runtime.drain_background

    async def aclose(self):
        order.append("aclose")
        await real_aclose(self)

    async def drain():
        order.append("drain")
        await real_drain()

    monkeypatch.setattr(household_settings.HouseholdSettingsCache, "aclose", aclose)
    monkeypatch.setattr(_runtime, "drain_background", drain)
    asyncio.run(_start_and_stop(main))
    assert order[:2] == ["aclose", "drain"] and order.count("aclose") == 1
