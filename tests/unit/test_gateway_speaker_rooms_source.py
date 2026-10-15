"""The gateway's speaker-room map comes from household.speaker_rooms(): the
admin setting, else HA_SATELLITE_ROOM_MAP, else none, with no I/O on the
request path and a 600 s cap on a last good admin value.

Runs in its own process: it imports gateway.main behind a stubbed
prometheus_client. Fictional house (tests/fixtures/ha_registry/house_a.json)."""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import socket
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

sys.modules.setdefault("prometheus_client", mock.MagicMock())
os.environ.setdefault("SERVICE_API_KEY", "test-key-speaker-rooms-source")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import gateway.main as gw  # noqa: E402
import shared.config  # noqa: E402
from gateway import speaker_room as sr  # noqa: E402
from shared import ha_registry as hr  # noqa: E402
from shared import household  # noqa: E402
from shared.household_settings import HouseholdSettingsCache  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json"
RAW_HOUSE = json.loads(FIXTURE.read_text(encoding="utf-8"))
STUDY_DEVICE = RAW_HOUSE["devices_by_name"]["study_sat"]
OTHER_DEVICE = "c" * 32
STUDY_ENTITY = "assist_satellite.study_voice"
ENV_MAP = json.dumps({STUDY_ENTITY: "Env Room", OTHER_DEVICE: "Env Device Room"})


def set_env(monkeypatch, satellites):
    if satellites is None:
        monkeypatch.delenv("HA_SATELLITE_ROOM_MAP", raising=False)
    else:
        monkeypatch.setenv("HA_SATELLITE_ROOM_MAP", satellites)
    shared.config._clear_cache_for_tests()


def admin(entries):
    household.install_for_test(sources={"speaker_rooms": {"entries": entries}})


class Registry:
    async def __call__(self):
        return RAW_HOUSE


def resolve(device_id):
    registry = hr.HARegistry(Registry(), monotonic=lambda: 1000.0)
    asyncio.run(registry.ensure(1))
    return asyncio.run(sr.resolve_speaker_room(
        device_id=device_id, trusted=True, registry=registry,
        satellite_map=gw._get_satellite_room_map(), read_states=None,
    ))


# -- precedence -----------------------------------------------------------------

def test_env_only_matches_the_parsed_env_map(monkeypatch):
    set_env(monkeypatch, ENV_MAP)
    got = gw._get_satellite_room_map()
    expected = hr.parse_satellite_room_map(ENV_MAP)
    assert (got.entity_rooms, got.device_rooms) == (expected.entity_rooms, expected.device_rooms)
    assert household.speaker_rooms().source == "env"


def test_admin_only(monkeypatch):
    set_env(monkeypatch, None)
    admin([{"key": STUDY_ENTITY, "room": "Garage"}])
    assert gw._get_satellite_room_map().entity_rooms == {STUDY_ENTITY: "Garage"}
    assert household.speaker_rooms().source == "admin"


def test_both_gives_admin(monkeypatch):
    set_env(monkeypatch, ENV_MAP)
    admin([{"key": STUDY_ENTITY, "room": "Garage"}])
    got = gw._get_satellite_room_map()
    assert got.entity_rooms == {STUDY_ENTITY: "Garage"} and got.device_rooms == {}


def test_neither_is_an_empty_map(monkeypatch):
    set_env(monkeypatch, None)
    got = gw._get_satellite_room_map()
    assert (got.entity_rooms, got.device_rooms) == ({}, {})
    assert household.speaker_rooms().source == "default"
    assert gw._get_room_to_satellite_map() == {}


def test_an_empty_admin_list_beats_env_and_leaves_the_area_answer(monkeypatch):
    set_env(monkeypatch, ENV_MAP)
    admin([])
    got = gw._get_satellite_room_map()
    assert (got.entity_rooms, got.device_rooms) == ({}, {})
    assert resolve(STUDY_DEVICE) == sr.SpeakerRoom("study", "area")


def test_an_admin_device_key_answers_before_the_area(monkeypatch):
    set_env(monkeypatch, None)
    admin([{"key": STUDY_DEVICE, "room": "Kitchen Nook"}])
    assert resolve(STUDY_DEVICE) == sr.SpeakerRoom("kitchen_nook", "map")


def test_an_admin_entity_key_on_the_device_wins_over_the_area(monkeypatch):
    set_env(monkeypatch, None)
    admin([{"key": STUDY_ENTITY, "room": "Garage"}])
    assert resolve(STUDY_DEVICE) == sr.SpeakerRoom("garage", "map")


def test_the_announcement_inverse_uses_admin_entity_keys_only(monkeypatch):
    set_env(monkeypatch, ENV_MAP)
    admin([{"key": STUDY_ENTITY, "room": "Study"}, {"key": OTHER_DEVICE, "room": "Galley"}])
    assert gw._get_room_to_satellite_map() == {"study": STUDY_ENTITY}


# -- the request path -------------------------------------------------------------

class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


class Admin:
    """A fetch with a script: a body, or an exception, per call."""

    def __init__(self):
        self.calls = 0
        self.body = None
        self.fail = False

    async def __call__(self):
        self.calls += 1
        if self.fail:
            raise ConnectionError("admin down")
        return self.body


def body(entries, revision=1):
    return {"speaker_rooms": {"value": {"entries": entries}, "revision": revision}}


@pytest.fixture
def rig(monkeypatch):
    set_env(monkeypatch, ENV_MAP)
    clock, fetch = Clock(), Admin()
    cache = HouseholdSettingsCache(fetch, clock=clock, spawn=gw._spawn_household_refresh)
    monkeypatch.setattr(household, "_reader", cache)
    monkeypatch.setattr(household, "_monotonic", clock)
    fetch.body = body([{"key": STUDY_ENTITY, "room": "Garage"}])
    return clock, fetch, cache


def test_the_lookup_is_synchronous():
    assert not inspect.iscoroutinefunction(gw._get_satellite_room_map)
    assert not inspect.iscoroutinefunction(gw._get_room_to_satellite_map)


def test_a_fresh_snapshot_makes_no_fetch_and_no_socket(rig, monkeypatch):
    clock, fetch, cache = rig

    def no_network(*a, **k):
        raise AssertionError("network on the request path")

    async def go():
        await cache.current()
        fetch.calls = 0
        monkeypatch.setattr(socket.socket, "connect", no_network)
        for _ in range(100):
            assert gw._get_satellite_room_map().entity_rooms == {STUDY_ENTITY: "Garage"}
            gw._get_room_to_satellite_map()
        return len(gw._household_refresh_tasks)

    assert asyncio.run(go()) == 0
    assert fetch.calls == 0


def test_a_stale_snapshot_spawns_one_tracked_refresh_and_returns_the_old_value(rig):
    clock, fetch, cache = rig

    async def go():
        await cache.current()
        fetch.calls = 0
        fetch.body = body([{"key": STUDY_ENTITY, "room": "Office"}], revision=2)
        clock.now += 61
        first = gw._get_satellite_room_map().entity_rooms
        tracked = len(gw._household_refresh_tasks)
        for _ in range(20):
            gw._get_satellite_room_map()
        still_one = len(gw._household_refresh_tasks)
        await asyncio.gather(*list(gw._household_refresh_tasks))
        return first, tracked, still_one, gw._get_satellite_room_map().entity_rooms

    first, tracked, still_one, after = asyncio.run(go())
    assert first == {STUDY_ENTITY: "Garage"}
    assert (tracked, still_one) == (1, 1)
    assert fetch.calls == 1
    assert after == {STUDY_ENTITY: "Office"}
    assert not gw._household_refresh_tasks


def test_the_built_map_is_memoized_until_the_revision_changes(rig):
    clock, fetch, cache = rig

    async def go():
        await cache.current()
        a, b = gw._get_satellite_room_map(), gw._get_satellite_room_map()
        fetch.body = body([{"key": STUDY_ENTITY, "room": "Office"}], revision=2)
        clock.now += 61
        await cache.current()
        return a, b, gw._get_satellite_room_map()

    a, b, c = asyncio.run(go())
    assert a is b
    assert c is not a and c.entity_rooms == {STUDY_ENTITY: "Office"}


# -- outage and the 600 s cap -----------------------------------------------------
# The cap counts from the FIRST failed refresh after the last success. Idle time
# alone is never an outage: nothing refreshes while nobody asks.

def room_now():
    return dict(gw._get_satellite_room_map().entity_rooms)


def test_a_healthy_admin_is_never_stale_however_long_the_process_sat_idle(rig, caplog):
    clock, fetch, cache = rig

    async def go():
        await cache.current()
        before = room_now()
        clock.now += 700                      # idle: no request, no refresh
        first_after_idle = room_now()         # served from the held snapshot, before the refresh lands
        await cache.current()
        return before, first_after_idle, room_now()

    with caplog.at_level(logging.WARNING, logger="shared.household"):
        before, first_after_idle, after = asyncio.run(go())
    assert before == first_after_idle == after == {STUDY_ENTITY: "Garage"}
    assert household.speaker_rooms().source == "admin"
    assert "speaker_rooms_admin_stale" not in [r.getMessage() for r in caplog.records]


def test_an_outage_is_trusted_for_600_seconds_from_its_first_failure_then_falls_back_to_env_once(rig, caplog):
    clock, fetch, cache = rig

    async def go():
        await cache.current()
        clock.now += 700                      # a long quiet period first: not an outage
        fetch.fail = True
        await cache.current()                 # the first failure, 700 s after the last success
        seen = [room_now()]
        for step in (61, 200, 338):           # 599 s after the first failure: still trusted
            clock.now += step
            await cache.current()
            seen.append(room_now())
        clock.now += 2                        # 601 s
        await cache.current()
        seen.append(room_now())
        return seen

    with caplog.at_level(logging.WARNING, logger="shared.household"):
        seen = asyncio.run(go())
        for _ in range(5):
            gw._get_satellite_room_map()
    assert seen[:4] == [{STUDY_ENTITY: "Garage"}] * 4
    assert seen[4] == {STUDY_ENTITY: "Env Room"}
    assert household.speaker_rooms().source == "env"
    assert [r.getMessage() for r in caplog.records].count("speaker_rooms_admin_stale") == 1


def test_recovery_after_the_cap_returns_to_admin_and_a_later_outage_starts_a_new_clock(rig):
    clock, fetch, cache = rig

    async def go():
        await cache.current()
        fetch.fail = True
        clock.now += 61
        await cache.current()                 # the first failure
        clock.now += 700
        await cache.current()
        assert household.speaker_rooms().source == "env"
        fetch.fail = False
        clock.now += 61
        await cache.current()
        recovered = household.speaker_rooms().source
        fetch.fail = True
        clock.now += 61
        await cache.current()                 # a new outage begins now
        clock.now += 300
        await cache.current()
        return recovered, household.speaker_rooms().source

    assert asyncio.run(go()) == ("admin", "admin")


def test_the_gateway_configures_the_speaker_rooms_section():
    assert "speaker_rooms" in gw.HOUSEHOLD_SECTIONS
    assert household.SECTIONS["speaker_rooms"].required_permission == "manage_infrastructure"


def test_the_version_is_an_exact_counter_that_moves_only_when_the_map_does(rig):
    clock, fetch, cache = rig

    async def go():
        await cache.current()
        first = household.speaker_rooms().version
        fetch.body = body([{"key": STUDY_ENTITY, "room": "Garage"}], revision=2)   # same content, new revision
        clock.now += 61
        await cache.current()
        same = household.speaker_rooms().version
        fetch.body = body([{"key": STUDY_ENTITY, "room": "Attic"}], revision=3)
        clock.now += 61
        await cache.current()
        return first, same, household.speaker_rooms().version

    first, same, changed = asyncio.run(go())
    assert same == first and changed > first
