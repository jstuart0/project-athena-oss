"""The household settings reader: cache invariants, the precedence matrix over
every section, the test seam and an old admin-backend.

Pure-module rules (validators, masking) are in test_household_settings_model.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import httpx
import pytest

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

# Import the services from src/ whatever else runs in this process (CI sets no PYTHONPATH).
_SRC = str(_Path(__file__).resolve().parents[2] / "src")
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from shared import household  # noqa: E402
from shared import household_settings as hs  # noqa: E402
from shared.admin_config import AdminConfigClient  # noqa: E402
from shared.config import _clear_cache_for_tests  # noqa: E402

# -- fakes ------------------------------------------------------------------------------------------


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def entry(value, revision=1):
    return {"value": value, "stored": {}, "revision": revision}


BODY = {"sms": entry({"default_country_code": "44"}, 3)}


def counting_fetch(body=BODY, *, fail=None):
    calls = []

    async def fetch():
        calls.append(1)
        if fail is not None and fail():
            raise RuntimeError("down")
        return body() if callable(body) else body

    fetch.calls = calls
    return fetch


# -- cache invariants -----------------------------------------------------------------------------------


def test_fifty_concurrent_calls_make_one_fetch():
    fetch = counting_fetch()

    async def run():
        gate = asyncio.Event()
        inner = fetch

        async def slow():
            await gate.wait()
            return await inner()

        cache = hs.HouseholdSettingsCache(slow, clock=Clock())
        tasks = [asyncio.ensure_future(cache.current()) for _ in range(50)]
        await asyncio.sleep(0)
        gate.set()
        return await asyncio.gather(*tasks), cache

    results, cache = asyncio.run(run())
    assert len(fetch.calls) == 1
    assert cache.peek().sections["sms"].default_country_code == "44"
    assert results[0].loaded and results[0].revisions["sms"] == 3


def test_the_ttl_boundary():
    clock, fetch = Clock(), counting_fetch()
    cache = hs.HouseholdSettingsCache(fetch, ttl=60.0, clock=clock)

    async def run():
        await cache.current()
        clock.now += 59.0
        await cache.current()
        assert len(fetch.calls) == 1
        clock.now += 1.0
        await cache.current()
        assert len(fetch.calls) == 2

    asyncio.run(run())


def test_a_failure_stamps_the_attempt_so_callers_do_not_retry_inside_the_ttl():
    clock = Clock()
    fetch = counting_fetch(fail=lambda: True)
    cache = hs.HouseholdSettingsCache(fetch, ttl=60.0, clock=clock)

    async def run():
        for _ in range(5):
            snap = await cache.current()
        assert len(fetch.calls) == 1 and snap.loaded is False and snap.loaded_at == 0.0
        clock.now += 60.0
        await cache.current()
        assert len(fetch.calls) == 2

    asyncio.run(run())


def test_a_hanging_fetch_does_not_queue_other_callers():
    async def run():
        never = asyncio.Event()

        async def hang():
            await never.wait()

        cache = hs.HouseholdSettingsCache(hang, clock=Clock())
        first = asyncio.ensure_future(cache.current())
        await asyncio.sleep(0)
        snaps = await asyncio.wait_for(asyncio.gather(*(cache.current() for _ in range(10))), timeout=1.0)
        assert all(s.loaded is False for s in snaps)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        # the lock and the in-flight flag are released: the next call fetches
        cache2_calls = counting_fetch()
        cache._fetch = cache2_calls
        assert (await cache.current()).loaded and len(cache2_calls.calls) == 1

    asyncio.run(run())


def test_a_failed_refresh_keeps_the_last_good_snapshot_and_its_loaded_at():
    clock = Clock(1000.0)
    state = {"down": False, "body": BODY}
    fetch = counting_fetch(lambda: state["body"], fail=lambda: state["down"])
    cache = hs.HouseholdSettingsCache(fetch, ttl=60.0, clock=clock)

    async def run():
        good = await cache.current()
        assert good.loaded and good.loaded_at == 1000.0
        clock.now, state["down"] = 1100.0, True
        kept = await cache.current()
        assert kept is good and kept.loaded_at == 1000.0 and kept.sections["sms"].default_country_code == "44"
        clock.now, state["down"] = 1200.0, False
        state["body"] = {"sms": entry({"default_country_code": "33"}, 4)}
        fresh = await cache.current()
        assert fresh.loaded_at == 1200.0 and fresh.sections["sms"].default_country_code == "33"
        assert fresh.loaded_at > kept.loaded_at

    asyncio.run(run())


def test_a_non_dict_answer_is_a_failure():
    state = {"body": BODY}
    clock = Clock()
    cache = hs.HouseholdSettingsCache(counting_fetch(lambda: state["body"]), ttl=1.0, clock=clock)

    async def run():
        good = await cache.current()
        for junk in (["x"], "x", 5):
            clock.now += 2
            state["body"] = junk
            assert await cache.current() is good

    asyncio.run(run())


def test_a_section_missing_from_a_later_answer_means_admin_has_no_value():
    state = {"body": {**BODY, "amtrak": entry({"default_station": "BOS"})}}
    clock = Clock()
    cache = hs.HouseholdSettingsCache(counting_fetch(lambda: state["body"]), ttl=1.0, clock=clock)

    async def run():
        first = await cache.current()
        assert set(first.sections) == {"sms", "amtrak"}
        clock.now += 2
        state["body"] = {"sms": BODY["sms"]}
        second = await cache.current()
        assert set(second.sections) == {"sms"} and second.loaded

    asyncio.run(run())


def test_an_empty_list_is_a_stored_value_and_null_is_not():
    body = {
        "lights": entry({"room_light_exclude": []}),
        "transit": entry({"gtfs_feeds": None, "region_name": None}),
        "gateway": entry(None, 0),
    }
    snap = hs.snapshot_from_body(body, 1.0)
    assert snap.sections["lights"].room_light_exclude == []
    assert snap.sections["transit"].gtfs_feeds is None
    assert "gateway" not in snap.sections and snap.revisions["gateway"] == 0


def test_a_stale_peek_schedules_exactly_one_task_and_enters_the_fetch_after_the_first_await():
    fetch = counting_fetch()
    spawned = []

    def spawn(coro):
        task = asyncio.ensure_future(coro)
        spawned.append(task)
        return task

    cache = hs.HouseholdSettingsCache(fetch, spawn=spawn, clock=Clock())

    async def run():
        for _ in range(5):
            cache.peek()
        assert len(spawned) == 1 and fetch.calls == []
        await asyncio.sleep(0)
        assert fetch.calls == [1]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert cache.peek().loaded and len(spawned) == 1
        cache.peek()
        assert len(spawned) == 1  # fresh: nothing more

    asyncio.run(run())


def test_peek_outside_a_loop_does_io_nothing_and_schedules_nothing():
    spawned, fetch = [], counting_fetch()
    cache = hs.HouseholdSettingsCache(fetch, spawn=lambda coro: spawned.append(coro), clock=Clock())
    assert cache.peek().loaded is False
    assert spawned == [] and fetch.calls == []


def test_peek_after_aclose_schedules_nothing_and_aclose_cancels_the_refresh():
    spawned = []

    def spawn(coro):
        task = asyncio.ensure_future(coro)
        spawned.append(task)
        return task

    async def run():
        never = asyncio.Event()

        async def hang():
            await never.wait()

        cache = hs.HouseholdSettingsCache(hang, spawn=spawn, clock=Clock())
        cache.peek()
        await asyncio.sleep(0)
        assert len(spawned) == 1 and not spawned[0].done()
        await cache.aclose()
        assert spawned[0].cancelled()
        cache.peek()
        assert len(spawned) == 1
        assert (await cache.current()).loaded is False  # closed: no fetch

    asyncio.run(run())


def test_a_spawner_that_raises_leaves_peek_working():
    def spawn(_coro):
        raise RuntimeError("no")

    cache = hs.HouseholdSettingsCache(counting_fetch(), spawn=spawn, clock=Clock())

    async def run():
        cache.peek()
        cache.peek()

    asyncio.run(run())


def test_the_lock_is_per_running_loop():
    clock, fetch = Clock(), counting_fetch()
    cache = hs.HouseholdSettingsCache(fetch, ttl=0.0, clock=clock)
    for _ in range(3):
        assert asyncio.run(cache.current()).loaded
    assert len(fetch.calls) == 3


def test_a_refresh_scheduled_in_a_closed_loop_does_not_block_the_next_loop():
    cache = hs.HouseholdSettingsCache(counting_fetch(), clock=Clock())
    loop = asyncio.new_event_loop()

    async def schedule_and_leave():
        cache.peek()

    loop.run_until_complete(schedule_and_leave())
    loop.close()  # the scheduled task never ran

    async def again():
        cache.peek()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return cache.peek()

    assert asyncio.run(again()).loaded


# -- the process reader, the seam and configure -----------------------------------------------------------------


def test_the_default_reader_does_no_io_without_an_admin_url(monkeypatch):
    household.reset_for_test()
    monkeypatch.setattr("shared.admin_url.get_admin_url", lambda: "")

    def forbidden():
        raise AssertionError("an admin client was built")

    monkeypatch.setattr("shared.admin_config.get_admin_client", forbidden)
    snap = asyncio.run(household.reader().current())
    assert snap.loaded is False
    assert household.amtrak_station().source in ("env", "default")


def test_configure_returns_one_reader_and_logs_a_reconfigure_once(caplog):
    household.reset_for_test()
    with caplog.at_level(logging.WARNING, logger=household.logger.name):
        first = household.configure(sections=("sms",), fetch=counting_fetch())
        assert household.reader() is first
        assert household.configure(sections=("sms",), fetch=first._fetch) is first
        assert "household_reader_reconfigured" not in caplog.text
        second = household.configure(sections=("sms", "amtrak"), fetch=counting_fetch())
    assert second is not first and household.reader() is second
    assert caplog.text.count("household_reader_reconfigured") == 1
    with pytest.raises(ValueError):
        household.configure(sections=("nope",))


def test_a_resolver_outside_the_configured_sections_logs_once(caplog):
    household.reset_for_test()
    household.configure(sections=("sms",), fetch=counting_fetch())
    with caplog.at_level(logging.WARNING, logger=household.logger.name):
        household.amtrak_station()
        household.amtrak_station()
        household.sms_country_code()
    assert caplog.text.count("household_section_not_configured section=amtrak") == 1
    assert "section=sms" not in caplog.text


def test_install_for_test_validates_sources_through_the_models():
    household.install_for_test(sources={"sms": {"default_country_code": "+44"}, "amtrak": None})
    assert household.sms_country_code() == "44" and household.sms_country_code().source == "admin"
    assert household.amtrak_station().source != "admin"
    with pytest.raises(hs.SettingsValidationError):
        household.install_for_test(sources={"sms": {"default_country_code": "0"}})


def test_install_for_test_takes_a_snapshot_and_serves_it_forever():
    snap = hs.snapshot_from_body({"gateway": entry({"fallback_model": "tiny"})}, 5.0)
    cache = household.install_for_test(snapshot=snap)
    assert asyncio.run(cache.current()) is snap and household.gateway_fallback_model().source == "admin"


def test_the_seam_leak_probe_part_one_installs_a_value():
    household.install_for_test(sources={"sms": {"default_country_code": "99"}})
    assert household.sms_country_code() == "99"


def test_the_seam_leak_probe_part_two_sees_nothing_of_part_one():
    """Fails if the autouse fixture stops resetting the seam."""
    assert household.sms_country_code().source != "admin"
    assert household.sms_country_code() != "99"


def test_reset_for_test_clears_the_singleton_and_the_seam():
    household.install_for_test(sources={"sms": {"default_country_code": "99"}})
    household.configure(sections=("sms",), fetch=counting_fetch())
    household.reset_for_test()
    assert household._reader is None and household._configured is None and household._seam is False


def test_runtime_reset_for_test_resets_the_household_reader():
    from orchestrator.nodes import _runtime

    household.install_for_test(sources={"sms": {"default_country_code": "99"}})
    _runtime.reset_for_test()
    assert household._reader is None


# -- the precedence matrix -------------------------------------------------------------------------------------


ENV_NAMES = (
    "DEFAULT_CITY", "DEFAULT_STATE", "DEFAULT_AMTRAK_STATION", "TRANSIT_REGION_NAME", "TRANSIT_GTFS_FEEDS",
    "TRANSIT_STATIC_SERVICES", "COMMUNITY_EVENTS_SOURCES", "GATEWAY_FALLBACK_MODEL", "ATHENA_DEFAULT_MODEL",
    "SMS_DEFAULT_COUNTRY_CODE", "HA_ROOM_LIGHT_EXCLUDE_ENTITIES", "HA_BED_WARMER_ENTITIES", "DEFAULT_ROOM",
    "CLIMATE_ENTITY", "MIN_TEMP", "MAX_TEMP", "OVEN_ENTITY_ID", "FRIDGE_ENTITY_ID", "FREEZER_ENTITY_ID",
    "STOVE_COOK_MODE_SENSOR_ID", "STOVE_DISPLAY_TEMP_SENSOR_ID", "STOVE_TIMER_SENSOR_ID", "FRIDGE_DOOR_SENSOR_ID",
    "HA_LIGHT_GROUPS", "HA_SATELLITE_ROOM_MAP",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def set_env(monkeypatch, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _clear_cache_for_tests()


BED = {"level_left": "select.l", "level_right": "select.r", "power_main": "switch.m", "power_side_a": "switch.a", "power_side_b": "switch.b"}
BED_ENV = {"level_left": "select.el", "level_right": "select.er", "power_main": "switch.em", "power_side_a": "switch.ea", "power_side_b": "switch.eb"}
FEED_A = {"id": "a", "name": "Admin Feed", "url": "https://admin.example.org/f.zip"}
FEED_ENV = {"e": {"name": "Env Feed", "url": "https://env.example.org/f.zip"}}
SOURCE_A = {"id": "a", "name": "Admin Source", "type": "link_scan", "url": "https://admin.example.org/events"}
SOURCE_ENV = [{"name": "Env Source", "type": "link_scan", "url": "https://env.example.org/events"}]
BUILTIN = household.BUILTIN_ROOM_LIGHT_EXCLUDE


def read_transit(r):
    return (r.region_name, tuple(f["id"] for f in r.feeds), tuple(s["id"] for s in r.static_services))


def read_jarvis(r):
    return (r.default_room, r.climate_entity, r.min_temp, r.max_temp, r.oven_entity)


CASES = {
    "location": dict(
        read=lambda r: (r.city, r.state, r.text),
        admin={"city": "Springfield", "state": "IL", "text": "Springfield, IL"},
        admin_expect=("Springfield", "IL", "Springfield, IL"),
        null={"city": None, "state": None, "text": None},
        env={"DEFAULT_CITY": "Denver", "DEFAULT_STATE": "CO"}, env_expect=("Denver", "CO", "Denver, CO"),
        default_expect=("", "", ""),
        extra=[("text only keeps the env city", {"text": "123 Main St, Springfield, IL"},
                ("Denver", "CO", "123 Main St, Springfield, IL"), "admin")],
    ),
    "amtrak": dict(
        read=str, admin={"default_station": "BOS"}, admin_expect="BOS", null={"default_station": None},
        env={"DEFAULT_AMTRAK_STATION": "nyp"}, env_expect="NYP", default_expect="",
    ),
    "transit": dict(
        read=read_transit,
        admin={"region_name": "Admin Region", "gtfs_feeds": [FEED_A], "static_services": None},
        admin_expect=("Admin Region", ("a",), ()),
        null={"region_name": None, "gtfs_feeds": None, "static_services": None},
        env={"TRANSIT_REGION_NAME": "Env Region", "TRANSIT_GTFS_FEEDS": json.dumps(FEED_ENV)},
        env_expect=("Env Region", ("e",), ()), default_expect=("", (), ()),
        empty={"gtfs_feeds": []}, empty_expect=("Env Region", (), ()),
    ),
    "community_events": dict(
        read=lambda r: tuple(s["name"] for s in r.sources),
        admin={"sources": [SOURCE_A]}, admin_expect=("Admin Source",), null={"sources": None},
        env={"COMMUNITY_EVENTS_SOURCES": json.dumps(SOURCE_ENV)}, env_expect=("Env Source",), default_expect=(),
        empty={"sources": []}, empty_expect=(),
    ),
    "gateway": dict(
        read=str, admin={"fallback_model": "admin-model"}, admin_expect="admin-model", null={"fallback_model": None},
        env={"GATEWAY_FALLBACK_MODEL": "env-model"}, env_expect="env-model",
        default_expect="qwen3:4b-instruct-2507-q4_K_M",
    ),
    "sms": dict(
        read=str, admin={"default_country_code": "44"}, admin_expect="44", null={"default_country_code": None},
        env={"SMS_DEFAULT_COUNTRY_CODE": "33"}, env_expect="33", default_expect="1",
    ),
    "lights": dict(
        read=lambda r: r.patterns,
        admin={"room_light_exclude": ["light.porch_*"]},
        admin_expect=BUILTIN + (hs.glob_to_regex("light.porch_*"),),
        null={"room_light_exclude": None},
        env={"HA_ROOM_LIGHT_EXCLUDE_ENTITIES": json.dumps(["^light\\.x$"])}, env_expect=("^light\\.x$",),
        default_expect=BUILTIN, empty={"room_light_exclude": []}, empty_expect=BUILTIN,
    ),
    "bed_warmer": dict(
        read=lambda r: r.entities, admin=BED, admin_expect=BED, null={k: None for k in BED},
        env={"HA_BED_WARMER_ENTITIES": json.dumps(BED_ENV)}, env_expect=BED_ENV, default_expect=None,
        partial={"power_main": "switch.only"}, partial_expect=BED_ENV,
    ),
    "jarvis_web": dict(
        read=read_jarvis,
        admin={"default_room": "Den", "climate_entity": "climate.admin", "min_temp": 62, "max_temp": 80,
               "oven_entity": "water_heater.admin_oven"},
        admin_expect=("Den", "climate.admin", 62, 80, "water_heater.admin_oven"),
        null={f: None for f in hs.JarvisWebSection.model_fields},
        env={"DEFAULT_ROOM": "Kitchen", "CLIMATE_ENTITY": "climate.env", "MIN_TEMP": "60", "MAX_TEMP": "78",
             "OVEN_ENTITY_ID": "water_heater.env_oven"},
        env_expect=("Kitchen", "climate.env", 60, 78, "water_heater.env_oven"),
        default_expect=("guest", "climate.thermostat", 65, 75, ""),
        partial={"min_temp": 62, "climate_entity": "climate.admin"},
        partial_expect=("Kitchen", "climate.admin", 60, 78, "water_heater.env_oven"),
    ),
    "room_lights": dict(
        read=lambda r: dict(r.mapping),
        admin={"rooms": [{"room": "Den", "lights": ["light.den_a", "light.den_b"]}]},
        admin_expect={"den": ("light.den_a", "light.den_b")}, null={"rooms": None},
        env={"HA_LIGHT_GROUPS": json.dumps({"Kitchen": "light.kitchen"})}, env_expect={"kitchen": ("light.kitchen",)},
        default_expect={}, empty={"rooms": []}, empty_expect={},
    ),
    "speaker_rooms": dict(
        read=lambda r: (dict(r.map.device_rooms), dict(r.map.entity_rooms)),
        admin={"entries": [{"key": "a" * 32, "room": "Den"}, {"key": "assist_satellite.den", "room": "Den"}]},
        admin_expect=({"a" * 32: "Den"}, {"assist_satellite.den": "Den"}), null={"entries": None},
        env={"HA_SATELLITE_ROOM_MAP": json.dumps({"b" * 32: "Kitchen"})}, env_expect=({"b" * 32: "Kitchen"}, {}),
        default_expect=({}, {}), empty={"entries": []}, empty_expect=({}, {}),
    ),
}


def test_the_matrix_covers_every_section_and_every_section_has_a_resolver():
    assert set(CASES) == set(hs.SECTIONS)
    assert set(hs.SECTIONS) == set(household.RESOLVERS)


def resolve(section):
    result = household.RESOLVERS[section]()
    assert result.source in ("admin", "env", "default")
    return result


@pytest.mark.parametrize("section", sorted(CASES))
def test_admin_wins_over_env(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    household.install_for_test(sources={section: c["admin"]})
    result = resolve(section)
    assert c["read"](result) == c["admin_expect"]
    assert result.source == "admin"


@pytest.mark.parametrize("section", sorted(CASES))
def test_admin_null_falls_to_env(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    household.install_for_test(sources={section: c["null"]})
    result = resolve(section)
    assert c["read"](result) == c["env_expect"] and result.source == "env"


@pytest.mark.parametrize("section", sorted(CASES))
def test_env_only(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    result = resolve(section)
    assert c["read"](result) == c["env_expect"] and result.source == "env"


@pytest.mark.parametrize("section", sorted(CASES))
def test_default_only(section):
    c = CASES[section]
    result = resolve(section)
    assert c["read"](result) == c["default_expect"] and result.source == "default"


@pytest.mark.parametrize("section", sorted(CASES))
def test_admin_never_reachable_gives_env(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    fetch = counting_fetch(fail=lambda: True)
    household.reset_for_test()
    cache = household.configure(sections=tuple(hs.SECTIONS), fetch=fetch)
    asyncio.run(cache.current())
    result = resolve(section)
    assert fetch.calls and c["read"](result) == c["env_expect"] and result.source == "env"


@pytest.mark.parametrize("section", sorted(CASES))
def test_admin_down_after_a_good_fetch_keeps_the_last_good_value(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    state = {"down": False}
    fetch = counting_fetch({section: entry(c["admin"])}, fail=lambda: state["down"])
    household.reset_for_test()
    cache = household.configure(sections=tuple(hs.SECTIONS), fetch=fetch)
    cache._ttl = 0.0

    async def run():
        await cache.current()
        state["down"] = True
        await cache.current()

    asyncio.run(run())
    assert len(fetch.calls) == 2
    result = resolve(section)
    assert c["read"](result) == c["admin_expect"] and result.source == "admin"


@pytest.mark.parametrize("section", sorted(CASES))
def test_an_old_admin_without_the_section_gives_env(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    other = next(n for n in hs.SECTIONS if n != section)
    household.reset_for_test()
    cache = household.configure(
        sections=tuple(hs.SECTIONS), fetch=counting_fetch({other: entry(CASES[other]["admin"]), "future_section": entry({"x": 1})}),
    )
    asyncio.run(cache.current())
    result = resolve(section)
    assert c["read"](result) == c["env_expect"] and result.source == "env"


@pytest.mark.parametrize("section", sorted(s for s, c in CASES.items() if "empty" in c))
def test_an_empty_admin_list_is_an_answer(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    household.install_for_test(sources={section: c["empty"]})
    result = resolve(section)
    assert c["read"](result) == c["empty_expect"] and result.source == "admin"


@pytest.mark.parametrize("section", sorted(s for s, c in CASES.items() if "partial" in c))
def test_a_partial_unit_is_resolved_whole_from_env(section, monkeypatch):
    c = CASES[section]
    set_env(monkeypatch, c["env"])
    body = {section: entry(c["partial"])}
    household.install_for_test(snapshot=hs.snapshot_from_body(body, 1.0))
    result = resolve(section)
    assert c["read"](result) == c["partial_expect"]
    if section == "bed_warmer":
        assert result.source == "env"


def test_a_partial_bed_warmer_without_env_is_unconfigured():
    household.install_for_test(snapshot=hs.snapshot_from_body({"bed_warmer": entry({"power_main": "switch.only"})}, 1.0))
    assert household.bed_warmer() is None


def test_bed_warmer_returns_a_fresh_dict_of_the_unit():
    household.install_for_test(sources={"bed_warmer": BED})
    got = household.bed_warmer()
    assert got == BED
    got["power_main"] = "switch.changed"
    assert household.bed_warmer() == BED


def test_bed_warmer_env_with_four_of_five_keys_is_unconfigured(monkeypatch):
    partial = {k: v for k, v in BED_ENV.items() if k != "level_left"}
    set_env(monkeypatch, {"HA_BED_WARMER_ENTITIES": json.dumps(partial)})
    assert household.bed_warmer() is None
    set_env(monkeypatch, {"HA_BED_WARMER_ENTITIES": "not json"})
    assert household.bed_warmer() is None


def test_jarvis_bounds_resolve_as_a_unit_even_with_one_env_bound(monkeypatch):
    set_env(monkeypatch, {"MIN_TEMP": "60"})
    household.install_for_test(snapshot=hs.snapshot_from_body({"jarvis_web": entry({"max_temp": 90})}, 1.0))
    result = household.jarvis_web()
    assert (result.min_temp, result.max_temp) == (60, 75)


def test_the_text_only_location_keeps_the_env_city_for_keys(monkeypatch):
    set_env(monkeypatch, {"DEFAULT_CITY": "Denver"})
    household.install_for_test(sources={"location": {"text": "123 Main St, Springfield, IL"}})
    loc = household.location()
    assert (loc.city, loc.text, loc.source) == ("Denver", "123 Main St, Springfield, IL", "admin")
    household.install_for_test(sources={"location": {"city": "Springfield", "state": "IL", "text": "Springfield, IL"}})
    assert household.location().city == "Springfield"


def test_the_env_regexes_replace_the_builtin_exactly_as_before(monkeypatch):
    set_env(monkeypatch, {"HA_ROOM_LIGHT_EXCLUDE_ENTITIES": "[]"})
    assert household.room_light_exclude_patterns().patterns == ()
    set_env(monkeypatch, {"HA_ROOM_LIGHT_EXCLUDE_ENTITIES": "{broken"})
    assert household.room_light_exclude_patterns().patterns == BUILTIN


def test_resolvers_read_get_config_at_call_time(monkeypatch):
    set_env(monkeypatch, {"DEFAULT_AMTRAK_STATION": "bos"})
    assert household.amtrak_station() == "BOS"
    set_env(monkeypatch, {"DEFAULT_AMTRAK_STATION": "den"})
    assert household.amtrak_station() == "DEN"


def test_transit_env_errors_are_reported_and_clear_the_data(monkeypatch):
    set_env(monkeypatch, {"TRANSIT_GTFS_FEEDS": "{nope", "TRANSIT_REGION_NAME": "R"})
    t = household.transit()
    assert t.error.startswith("TRANSIT_GTFS_FEEDS: invalid JSON") and t.feeds == () and t.region_name == "R"
    set_env(monkeypatch, {"TRANSIT_GTFS_FEEDS": "[1]"})
    assert household.transit().error == "TRANSIT_GTFS_FEEDS: must be a JSON object"
    set_env(monkeypatch, {"TRANSIT_GTFS_FEEDS": json.dumps({"x": "not a dict"}), "TRANSIT_STATIC_SERVICES": "{bad"})
    assert household.transit().error.startswith("TRANSIT_STATIC_SERVICES: invalid JSON")


def test_community_env_errors_use_todays_messages(monkeypatch):
    set_env(monkeypatch, {"COMMUNITY_EVENTS_SOURCES": "{nope"})
    assert household.community_sources().error.startswith("COMMUNITY_EVENTS_SOURCES: invalid JSON")
    set_env(monkeypatch, {"COMMUNITY_EVENTS_SOURCES": "{}"})
    assert household.community_sources().error == "COMMUNITY_EVENTS_SOURCES: must be a JSON array"
    set_env(monkeypatch, {"COMMUNITY_EVENTS_SOURCES": json.dumps([{"name": "a"}, 3])})
    assert household.community_sources().error == "COMMUNITY_EVENTS_SOURCES: source at index 1 must be an object"


def test_items_carry_their_origin_and_admin_items_drop_null_keys(monkeypatch):
    set_env(monkeypatch, {"TRANSIT_GTFS_FEEDS": json.dumps({"e": {"name": "E", "url": "https://e.example.org/f", "allow_private": True}})})
    (env_feed,) = household.transit().feeds
    assert env_feed["origin"] == "env" and env_feed["id"] == "e" and env_feed["allow_private"] is True
    household.install_for_test(sources={"transit": {"gtfs_feeds": [FEED_A]}})
    (admin_feed,) = household.transit().feeds
    assert admin_feed == {**FEED_A, "origin": "admin"}  # no max_bytes: None, so the loader's default applies
    assert "allow_private" not in admin_feed


# -- an old admin-backend, through the real client ----------------------------------------------------------------------------


def _client(handler):
    admin = AdminConfigClient(admin_url="http://admin.test", api_key="k")
    admin.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return admin


def test_a_404_from_an_old_admin_is_none_and_resolution_uses_env(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(404, json={"detail": "Not Found"})

    admin = _client(handler)
    assert asyncio.run(admin.fetch_household_settings(("sms", "amtrak"))) is None
    (request,) = seen
    assert request.url.path == "/api/settings/household" and request.url.params.get_list("sections") == ["sms", "amtrak"]
    assert request.headers["X-Service-Key"] == "k"
    set_env(monkeypatch, {"DEFAULT_AMTRAK_STATION": "NYP"})
    household.reset_for_test()
    cache = household.configure(sections=("amtrak",), fetch=lambda: admin.fetch_household_settings(("amtrak",)))
    asyncio.run(cache.current())
    assert household.amtrak_station() == "NYP" and household.amtrak_station().source == "env"


def test_the_client_returns_none_for_errors_and_junk_and_the_body_otherwise():
    for response in (httpx.Response(500), httpx.Response(401), httpx.Response(200, json=["x"])):
        assert asyncio.run(_client(lambda _r, resp=response: resp).fetch_household_settings(("sms",))) is None

    def boom(_request):
        raise httpx.ConnectError("SECRET-HOST unreachable")

    assert asyncio.run(_client(boom).fetch_household_settings(("sms",))) is None
    assert asyncio.run(_client(lambda _r: httpx.Response(200, json=BODY)).fetch_household_settings(("sms",))) == BODY
    no_url = AdminConfigClient(admin_url="", api_key="k")
    no_url.admin_url = ""
    assert asyncio.run(no_url.fetch_household_settings(("sms",))) is None


def test_the_fetch_logs_the_error_type_only(captured_logs):
    def boom(_request):
        raise httpx.ConnectError("SECRET-HOST unreachable")

    asyncio.run(_client(boom).fetch_household_settings(("sms",)))
    assert "SECRET-HOST" not in repr(captured_logs)


# -- pins to other modules -------------------------------------------------------------------------------------------


def test_the_builtin_lamp_pattern_equals_the_orchestrators():
    from orchestrator.ha_entity_manager import DEFAULT_ROOM_LIGHT_EXCLUDE_PATTERNS

    assert tuple(DEFAULT_ROOM_LIGHT_EXCLUDE_PATTERNS) == household.BUILTIN_ROOM_LIGHT_EXCLUDE


ROOM_VECTORS = ("Living Room", "living_room", "Kid's room & den", "  Kitchen  ", "Café", "Rec-Room", "ROOM 2", "", "a  b", "Sun Room!!")


def test_room_key_equals_the_registrys_on_the_vector_set():
    from shared.ha_registry import room_key

    for name in ROOM_VECTORS:
        assert hs.room_key(name) == room_key(name), name


WHOLE_HOUSE_VECTORS = (
    "whole house", "the whole house", "entire house", "every room", "all rooms", "all the rooms", "everywhere",
    "throughout the house", "all over the house", "here", "this room", "my room", "the room", "in here",
    "my current room",
)


def test_every_whole_house_or_speaker_relative_wording_is_a_reserved_room():
    from orchestrator.room_context import WHOLE_HOUSE_ROOM_RE, is_speaker_relative

    matched = [p for p in WHOLE_HOUSE_VECTORS if WHOLE_HOUSE_ROOM_RE.search(p) or is_speaker_relative(p)]
    assert len(matched) >= 12
    for phrase in matched:
        assert hs.is_reserved_room(phrase), phrase


def test_log_lines_of_the_reader_carry_no_values(caplog):
    household.reset_for_test()
    secret_body = {"sms": entry({"default_country_code": "44"}), "lights": entry({"room_light_exclude": ["light.hidden_value_*"]})}
    cache = household.configure(sections=tuple(hs.SECTIONS), fetch=counting_fetch(secret_body))
    with caplog.at_level(logging.DEBUG):
        asyncio.run(cache.current())
        household.sms_country_code()
        household.room_light_exclude_patterns()
    assert "hidden_value" not in caplog.text and "44" not in re.sub(r"\d+ms|sections=\d", "", caplog.text)


# -- startup bound, reconfigure after close, scheduling release, backoff -----------------------------

def test_warm_returns_within_its_bound_when_admin_hangs(caplog):
    household.reset_for_test()
    never = asyncio.Event()

    async def hang():
        await never.wait()

    household.configure(sections=("sms",), fetch=hang)

    async def run():
        loop = asyncio.get_running_loop()
        started = loop.time()
        await household.warm(timeout=0.05)
        return loop.time() - started

    with caplog.at_level(logging.WARNING, logger=household.logger.name):
        elapsed = asyncio.run(run())
    assert elapsed < 1.0
    assert "household_settings_startup_timeout" in caplog.text


def test_warm_loads_the_snapshot_when_admin_answers():
    household.reset_for_test()
    fetch = counting_fetch()
    household.configure(sections=("sms",), fetch=fetch)
    asyncio.run(household.warm())
    assert fetch.calls == [1] and household.reader().peek().loaded


def test_configure_after_aclose_builds_a_working_reader():
    household.reset_for_test()
    fetch = counting_fetch()
    first = household.configure(sections=("sms",), fetch=fetch)
    asyncio.run(first.aclose())
    assert first.closed
    second = household.configure(sections=("sms",), fetch=fetch)
    assert second is not first and not second.closed
    asyncio.run(second.current())
    assert fetch.calls == [1], "the second lifespan fetches instead of serving a frozen snapshot"


def test_a_refresh_cancelled_before_its_first_step_does_not_stop_scheduling_for_good():
    spawned = []

    def spawn(coro):
        task = asyncio.ensure_future(coro)
        task.cancel()  # before the coroutine's first step: its finally never runs
        spawned.append(task)
        return task

    cache = hs.HouseholdSettingsCache(counting_fetch(), spawn=spawn, clock=Clock())

    async def run():
        cache.peek()
        cache.peek()
        assert len(spawned) == 1, "floor: one task while it is pending"
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert spawned[0].cancelled()
        cache.peek()
        assert len(spawned) == 2, "the loop can schedule again once the cancelled task is done"

    asyncio.run(run())


def test_reload_backoff_doubles_up_to_its_cap_and_resets_on_success():
    now = [0.0]
    backoff = household.ReloadBackoff(base=60.0, cap=300.0, clock=lambda: now[0])
    assert not backoff.held_back("a")
    assert backoff.failed("a") == 60.0 and backoff.held_back("a")
    now[0] += 61
    assert not backoff.held_back("a")
    assert backoff.failed("a") == 120.0
    assert backoff.failed("a") == 240.0
    assert backoff.failed("a") == 300.0 and backoff.failed("a") == 300.0
    assert backoff.held_back("a") and not backoff.held_back("b"), "only the failing key is held back"
    backoff.succeeded()
    assert not backoff.held_back("a")
    assert backoff.failed("a") == 60.0


def test_reload_backoff_restarts_the_count_for_a_different_key():
    backoff = household.ReloadBackoff(base=60.0, cap=1800.0, clock=lambda: 0.0)
    backoff.failed("a")
    backoff.failed("a")
    assert backoff.failed("b") == 60.0


# -- env thermostat bounds follow the admin rules --------------------------------------------------------

@pytest.mark.parametrize("env", [
    {"MIN_TEMP": "90", "MAX_TEMP": "50"},   # min above max
    {"MIN_TEMP": "0", "MAX_TEMP": "200"},   # outside 40-95
    {"MIN_TEMP": "30"},                      # a lone bound below the range
    {"MAX_TEMP": "99"},                      # a lone bound above the range
    {"MIN_TEMP": "80"},                      # lone min above the default max (75)
])
def test_invalid_env_thermostat_bounds_fall_back_to_the_defaults_with_one_warning(env, monkeypatch, caplog):
    monkeypatch.setattr(hs, "_warned_env_temps", False)
    set_env(monkeypatch, env)
    with caplog.at_level(logging.WARNING, logger=hs.logger.name):
        first = household.jarvis_web()
        second = household.jarvis_web()
    assert (first.min_temp, first.max_temp) == (65, 75) == (second.min_temp, second.max_temp)
    assert first.sources["min_temp"] == "default"
    assert caplog.text.count("household_env_invalid section=jarvis_web") == 1
    assert "90" not in caplog.text and "200" not in caplog.text, "the warning names no value"


@pytest.mark.parametrize("env,expect", [
    ({"MIN_TEMP": "60", "MAX_TEMP": "80"}, (60, 80)),
    ({"MIN_TEMP": "40", "MAX_TEMP": "95"}, (40, 95)),
    ({"MIN_TEMP": "70", "MAX_TEMP": "70"}, (70, 70)),
    ({"MIN_TEMP": "60"}, (60, 75)),
    ({"MAX_TEMP": "70"}, (65, 70)),
])
def test_valid_env_thermostat_bounds_are_kept(env, expect, monkeypatch):
    set_env(monkeypatch, env)
    result = household.jarvis_web()
    assert (result.min_temp, result.max_temp) == expect
    assert result.sources["min_temp"] == "env"
