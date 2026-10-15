"""The room_lights and speaker_rooms sections: write rules, tolerant reads, the
resolvers' precedence, env parity with the old readers, the staleness cap."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from shared import household
from shared import household_settings as hs
from shared.config import _clear_cache_for_tests
from shared.ha_registry import _DEVICE_KEY, _ENTITY_KEY, room_key as registry_room_key

DEV = "a" * 32
DEV2 = "b" * 32


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for name in ("HA_LIGHT_GROUPS", "HA_SATELLITE_ROOM_MAP"):
        monkeypatch.delenv(name, raising=False)
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


def set_env(monkeypatch, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    _clear_cache_for_tests()


def bad(section, value, kind):
    with pytest.raises(hs.SettingsValidationError) as caught:
        hs.validate_update(section, value)
    errors = caught.value.errors
    assert all(set(e) == {"loc", "type"} for e in errors)
    assert kind in {e["type"] for e in errors}, errors
    return errors


def rooms(*pairs):
    return {"rooms": [{"room": r, "lights": list(l)} for r, l in pairs]}


def entries(*pairs):
    return {"entries": [{"key": k, "room": r} for k, r in pairs]}


# -- write rules ---------------------------------------------------------------------------------------------


def test_a_non_light_entity_is_refused_on_write():
    bad("room_lights", rooms(("Den", ["switch.x"])), "not_a_light")
    bad("room_lights", rooms(("Den", ["lock.front_door"])), "not_a_light")


@pytest.mark.parametrize("key", ["ABC", "a" * 31, "a" * 33, "assist_satellite.", "light.x", "g" * 32, ""])
def test_a_bad_satellite_key_is_refused(key):
    bad("speaker_rooms", entries((key, "Den")), "bad_satellite_key")


def test_an_uppercase_device_key_is_normalized_not_refused():
    out = hs.validate_update("speaker_rooms", entries((" " + DEV.upper() + " ", "Den")))
    assert out.entries[0].key == DEV


def test_a_duplicate_room_by_room_key_is_refused_with_its_row():
    errors = bad("room_lights", rooms(("Living Room", ["light.a"]), ("living_room", ["light.b"])), "duplicate_room")
    assert errors[0]["loc"] == ("rooms", 1, "room")


def test_size_limits():
    bad("room_lights", rooms(("Den", [f"light.l{i}" for i in range(33)])), "too_many")
    assert hs.validate_update("room_lights", rooms(("Den", [f"light.l{i}" for i in range(32)])))
    bad("room_lights", rooms(*[(f"Room {i}", ["light.a"]) for i in range(65)]), "too_many")
    assert hs.validate_update("room_lights", rooms(*[(f"Room {i}", ["light.a"]) for i in range(64)]))
    bad("speaker_rooms", entries(*[(f"assist_satellite.s{i}", f"Room {i}") for i in range(65)]), "too_many")
    bad("room_lights", rooms(("Den", [])), "no_lights")


@pytest.mark.parametrize("name", ["", "   ", "!!!", "x" * 65, "De\nn", "Den\x00"])
def test_an_unusable_room_name_is_refused(name):
    bad("room_lights", rooms((name, ["light.a"])), "bad_room")
    bad("speaker_rooms", entries((DEV, name)), "bad_room")


def test_reserved_room_rejected_on_write_and_dropped_on_read():
    for reserved in sorted(hs.RESERVED_ROOM_KEYS):
        name = reserved.replace("_", " ").title()
        bad("room_lights", rooms((name, ["light.a"])), "reserved_room")
        bad("speaker_rooms", entries((DEV, name)), "reserved_room")
    stored = hs.parse_value("room_lights", rooms(("Whole House", ["light.a"]), ("Den", ["light.b"])))
    assert [e.room for e in stored.rooms] == ["Den"]
    stored = hs.parse_value("speaker_rooms", entries((DEV, "Everywhere"), (DEV2, "Den")))
    assert [e.room for e in stored.entries] == ["Den"]


def test_two_satellite_entities_on_one_room_are_refused_but_devices_may_share():
    bad("speaker_rooms", entries(("assist_satellite.a", "Den"), ("assist_satellite.b", "den")), "duplicate_satellite_room")
    assert hs.validate_update("speaker_rooms", entries((DEV, "Den"), (DEV2, "Den"), ("assist_satellite.a", "Den")))
    bad("speaker_rooms", entries((DEV, "Den"), (DEV, "Kitchen")), "duplicate_key")


def test_names_with_punctuation_are_accepted():
    out = hs.validate_update("room_lights", rooms(("Bedroom #2", ["light.a"]), ("Living/Dining", ["light.b"])))
    assert [e.room for e in out.rooms] == ["Bedroom #2", "Living/Dining"]


# -- tolerant reads ------------------------------------------------------------------------------------------


def test_stored_lock_entity_dropped_on_read():
    stored = hs.parse_value("room_lights", rooms(("Den", ["light.a", "lock.front_door", "light.b"])))
    assert stored.rooms[0].lights == ["light.a", "light.b"]


def test_an_entry_left_empty_a_later_duplicate_and_a_repeated_light_are_dropped():
    stored = hs.parse_value("room_lights", rooms(
        ("Empty", ["lock.x"]), ("Den", ["light.a", "light.a"]), ("DEN", ["light.z"]), ("Hall", ["light.h"]),
    ))
    assert [(e.room, e.lights) for e in stored.rooms] == [("Den", ["light.a"]), ("Hall", ["light.h"])]


def test_dropped_items_are_counted_without_values(caplog):
    with caplog.at_level(logging.ERROR):
        hs.parse_value("room_lights", rooms(("Den", ["lock.secret_front_door"]), ("Hall", ["light.h"])))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "household_section_invalid_items section=room_lights count=2" in text
    assert "secret" not in text and "Den" not in text


def test_the_key_regexes_equal_the_registrys():
    for text in (DEV, DEV.upper(), "a" * 31, "assist_satellite.x_1", "assist_satellite.", "Assist_satellite.x"):  # ours is stricter on a trailing newline (fullmatch)
        assert bool(hs.DEVICE_KEY_RE.fullmatch(text)) == bool(_DEVICE_KEY.match(text)), text
        assert bool(hs.SATELLITE_ENTITY_RE.fullmatch(text)) == bool(_ENTITY_KEY.match(text)), text
    for name in ("Living Room", "Bedroom #2", "Café", "a  b"):
        assert hs.room_key(name) == registry_room_key(name)


# -- the resolvers ---------------------------------------------------------------------------------------------

ADMIN_LIGHTS = rooms(("Den", ["light.den_a"]))
ENV_LIGHTS = json.dumps({"Kitchen": "light.kitchen"})
ADMIN_SPEAKERS = entries((DEV, "Den"))
ENV_SPEAKERS = json.dumps({DEV2: "Kitchen"})


def lights():
    return household.room_lights()


def speakers():
    return household.speaker_rooms()


def test_room_lights_precedence(monkeypatch):
    assert (dict(lights().mapping), lights().source) == ({}, "default")
    set_env(monkeypatch, HA_LIGHT_GROUPS=ENV_LIGHTS)
    assert (dict(lights().mapping), lights().source) == ({"kitchen": ("light.kitchen",)}, "env")
    household.install_for_test(sources={"room_lights": ADMIN_LIGHTS})
    assert (dict(lights().mapping), lights().source) == ({"den": ("light.den_a",)}, "admin")
    household.install_for_test(sources={"room_lights": {"rooms": None}})
    assert lights().source == "env"


def test_admin_empty_list_beats_server_setting(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS=ENV_LIGHTS, HA_SATELLITE_ROOM_MAP=ENV_SPEAKERS)
    household.install_for_test(sources={"room_lights": {"rooms": []}, "speaker_rooms": {"entries": []}})
    assert (dict(lights().mapping), lights().source) == ({}, "admin")
    assert speakers().source == "admin" and not speakers().map.device_rooms and not speakers().map.entity_rooms


def test_speaker_rooms_precedence(monkeypatch):
    assert speakers().source == "default"
    set_env(monkeypatch, HA_SATELLITE_ROOM_MAP=ENV_SPEAKERS)
    assert speakers().source == "env" and dict(speakers().map.device_rooms) == {DEV2: "Kitchen"}
    household.install_for_test(sources={"speaker_rooms": ADMIN_SPEAKERS})
    assert speakers().source == "admin" and dict(speakers().map.device_rooms) == {DEV: "Den"}


def test_loaded_false_gives_env(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS=ENV_LIGHTS, HA_SATELLITE_ROOM_MAP=ENV_SPEAKERS)
    household.install_for_test(snapshot=hs.Snapshot())
    assert lights().source == "env" and speakers().source == "env"


def test_unknown_section_falls_back_to_env(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS=ENV_LIGHTS, HA_SATELLITE_ROOM_MAP=ENV_SPEAKERS)

    async def old_admin():
        return {"amtrak": {"value": {"default_station": "BOS"}, "revision": 1}}

    household.reset_for_test()
    cache = household.configure(sections=tuple(hs.SECTIONS), fetch=old_admin)
    asyncio.run(cache.current())
    assert lights().source == "env" and speakers().source == "env"


def test_env_parity_with_the_old_reader(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS=json.dumps({"Living Room": "light.a"}))
    assert dict(lights().mapping) == {"living_room": ("light.a",)}
    set_env(monkeypatch, HA_LIGHT_GROUPS=json.dumps({"!!!": "light.p"}))
    assert dict(lights().mapping) == {"": ("light.p",)}


def test_env_duplicate_after_normalization_last_wins(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS='{"Living Room":"a","living_room":"b"}')
    assert dict(lights().mapping) == {"living_room": ("b",)}


def test_env_list_value_is_stringified_as_before(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS='{"Den":["x"]}')
    assert dict(lights().mapping) == {"den": ("['x']",)}


@pytest.mark.parametrize("raw", ["not json", "[1,2]", '"str"'])
def test_env_invalid_json_gives_nothing_and_logs_once(monkeypatch, caplog, raw):
    set_env(monkeypatch, HA_LIGHT_GROUPS=raw)
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            assert dict(lights().mapping) == {}
    assert sum("ha_light_groups_invalid_json" in r.getMessage() for r in caplog.records) == 1


# -- last good and the staleness cap --------------------------------------------------------------------------------


def body(**sections):
    return {n: {"value": v, "revision": 3} for n, v in sections.items()}


def test_last_good_admin_value_survives_a_failed_fetch_and_a_null_goes_to_env(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS=ENV_LIGHTS)
    state = {"down": False}

    async def fetch():
        if state["down"]:
            raise RuntimeError("down")
        return body(room_lights=ADMIN_LIGHTS)

    household.reset_for_test()
    cache = household.configure(sections=tuple(hs.SECTIONS), fetch=fetch)
    cache._ttl = 0.0

    async def run():
        await cache.current()
        state["down"] = True
        await cache.current()

    asyncio.run(run())
    assert lights().source == "admin"
    household.install_for_test(snapshot=hs.snapshot_from_body(body(room_lights={"rooms": None}), time.monotonic()))
    assert lights().source == "env"


def test_never_loaded_gives_env(monkeypatch):
    set_env(monkeypatch, HA_SATELLITE_ROOM_MAP=ENV_SPEAKERS)
    assert speakers().source == "env"


def aged(seconds, **sections):
    return hs.snapshot_from_body(body(**sections), time.monotonic() - seconds)


def test_stale_speaker_rooms_fall_back_after_600s_logged_once(monkeypatch, caplog):
    set_env(monkeypatch, HA_SATELLITE_ROOM_MAP=ENV_SPEAKERS)
    household.install_for_test(snapshot=aged(601, speaker_rooms=ADMIN_SPEAKERS))
    with caplog.at_level(logging.WARNING):
        for _ in range(50):
            assert speakers().source == "env"
    assert sum("speaker_rooms_admin_stale" in r.getMessage() for r in caplog.records) == 1


def test_a_fresh_speaker_rooms_snapshot_is_admin(monkeypatch):
    set_env(monkeypatch, HA_SATELLITE_ROOM_MAP=ENV_SPEAKERS)
    household.install_for_test(snapshot=aged(599, speaker_rooms=ADMIN_SPEAKERS))
    assert speakers().source == "admin"


def test_room_lights_is_not_capped(monkeypatch):
    set_env(monkeypatch, HA_LIGHT_GROUPS=ENV_LIGHTS)
    household.install_for_test(snapshot=aged(601, room_lights=ADMIN_LIGHTS))
    assert lights().source == "admin"


# -- the memo ----------------------------------------------------------------------------------------------------


def test_a_thousand_calls_with_an_unchanged_snapshot_build_once(monkeypatch):
    built = []
    real = household.room_lights_from
    monkeypatch.setattr(household, "room_lights_from", lambda *a, **k: built.append(1) or real(*a, **k))
    household.install_for_test(sources={"room_lights": ADMIN_LIGHTS})
    first = lights()
    for _ in range(1000):
        assert lights() is first
    assert len(built) == 1


def test_the_version_changes_with_the_content_only():
    a = household.room_lights_from(None, '{"Den":"light.a"}')
    assert a.version == household.room_lights_from(None, '{"Den":"light.a"}').version
    assert a.version != household.room_lights_from(None, '{"Den":"light.b"}').version


def test_resolver_logs_carry_counts_only(monkeypatch, caplog):
    household.install_for_test(sources={"room_lights": rooms(("Secret Den", ["light.secret_lamp"]))})
    with caplog.at_level(logging.INFO):
        lights()
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "room_lights_source source=admin rooms=1 lights=1" in text
    assert "Secret" not in text and "secret" not in text
