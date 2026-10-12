"""shared.ha_registry: the room-name tokenizer, the satellite map parser, the
registry snapshot (area inheritance, lamps, collisions) and the cache.

Fictional house only (tests/fixtures/ha_registry/house_a.json). The fetcher is
an in-memory fake; the real WebSocket adapter is test_ha_registry_ws_protocol.py.
Time is the registry's injected monotonic clock; nothing sleeps.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from shared import ha_registry as hr  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json"
RAW = json.loads(FIXTURE.read_text(encoding="utf-8"))
DEV = RAW["devices_by_name"]


def snapshot(raw=None, fetched_at=0.0) -> hr.RegistrySnapshot:
    return hr.RegistrySnapshot.from_raw(raw if raw is not None else RAW, fetched_at=fetched_at)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class FakeFetcher:
    """Counts calls; returns `raw`, raises `error`, or hangs on `gate`."""

    def __init__(self, raw=None, error=None, gate=None):
        self.raw = RAW if raw is None else raw
        self.error = error
        self.gate = gate
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return copy.deepcopy(self.raw)


def registry(fetcher, clock=None, **kwargs):
    return hr.HARegistry(fetcher, monotonic=clock or Clock(), **kwargs)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fixture floors
# ---------------------------------------------------------------------------

def test_fixture_has_enough_areas_and_a_satellite_device():
    snap = snapshot()
    assert len(snap.areas) >= 4
    assert snap.satellite_entities(DEV["study_sat"]), "the fixture needs a satellite device"


# ---------------------------------------------------------------------------
# tokens / room_key
# ---------------------------------------------------------------------------

def test_tokens_is_the_same_function_the_orchestrator_exports():
    from orchestrator import ha_entity_manager

    assert ha_entity_manager._tokens is hr.tokens


@pytest.mark.parametrize("name,expected", [
    ("Living Room", "living_room"),
    ("Living Room (2)", "living_room_2"),
    ("Living-Room", "living_room"),
    ("2", "2"),
    ("Café", "café"),
    ("", ""),
])
def test_room_key_normalizes_case_punctuation_and_digits(name, expected):
    assert hr.room_key(name) == expected


def test_tokens_agree_between_composed_and_decomposed_accents():
    assert hr.tokens("Café") == hr.tokens("Café")


# ---------------------------------------------------------------------------
# parse_satellite_room_map
# ---------------------------------------------------------------------------

def test_satellite_map_splits_entity_keys_from_device_keys():
    device = "ab" * 16
    parsed = hr.parse_satellite_room_map(json.dumps({
        "assist_satellite.galley_voice": "galley",
        device: "study",
    }))
    assert dict(parsed.entity_rooms) == {"assist_satellite.galley_voice": "galley"}
    assert dict(parsed.device_rooms) == {device: "study"}


def test_satellite_map_drops_other_keys_with_one_error_that_names_none():
    with structlog.testing.capture_logs() as logs:
        parsed = hr.parse_satellite_room_map({
            "assist_satellite.galley_voice": "galley",
            "light.galley_all": "galley",
            "Not A Key": "study",
            "ABCDEF" * 5 + "ab": "study",
        })
    assert dict(parsed.entity_rooms) == {"assist_satellite.galley_voice": "galley"}
    assert not parsed.device_rooms
    errors = [e for e in logs if e["log_level"] == "error"]
    assert len(errors) == 1 and errors[0]["count"] == 3
    assert "galley" not in json.dumps(errors, default=str)


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", "3"])
def test_satellite_map_unset_or_malformed_is_empty(raw):
    parsed = hr.parse_satellite_room_map(raw)
    assert not parsed.entity_rooms and not parsed.device_rooms


# ---------------------------------------------------------------------------
# Snapshot: area resolution
# ---------------------------------------------------------------------------

def area_of(ref, snap=None):
    area = (snap or snapshot()).area_for(ref)
    return area.area_id if area else None


def test_an_entity_area_wins_over_its_device_area():
    assert area_of("light.override_lamp") == "area_north_bath"
    assert area_of("light.lamp") == "area_lower_bath", "no entity area: the device's"


def test_an_entity_with_no_device_and_no_area_has_no_area():
    assert area_of("light.no_device_no_area") is None


def test_a_child_device_with_no_area_inherits_its_parents():
    assert area_of(DEV["reduced_child"]) == "area_galley"
    assert area_of("light.reduced_child_lamp") == "area_galley"


def test_a_child_device_with_its_own_area_keeps_it():
    assert area_of(DEV["child_own"]) == "area_south_bath"
    assert area_of("light.child_own_lamp") == "area_south_bath"


def test_a_two_level_parent_chain_reaches_the_first_area():
    assert area_of("light.chain_lamp") == "area_north_bath"


def test_a_parent_cycle_terminates_with_no_area():
    assert area_of("light.cycle_lamp") is None
    assert area_of(DEV["cycle_x"]) is None


def test_a_chain_longer_than_four_parents_is_not_followed():
    devices = [{"id": f"{i:032x}", "area_id": None, "parent_device_id": f"{i + 1:032x}"} for i in range(1, 7)]
    devices.append({"id": f"{7:032x}", "area_id": "area_study", "parent_device_id": None})
    raw = {"areas": [{"area_id": "area_study", "name": "Study"}], "devices": devices, "entities": []}
    snap = snapshot(raw)
    assert snap.area_for(f"{3:032x}").area_id == "area_study", "four parents up"
    assert snap.area_for(f"{2:032x}") is None, "five parents up is out of range"


def test_a_reduced_child_entry_and_the_new_config_entry_fields_parse():
    assert area_of(DEV["new_style"]) == "area_lower_bath"
    assert area_of(DEV["reduced_child"]) == "area_galley"


def test_an_unknown_reference_has_no_area():
    assert snapshot().area_for("light.not_in_the_registry") is None


def test_the_digit_only_and_accented_areas_resolve_by_key():
    snap = snapshot()
    assert snap.area_by_key("2").area_id == "area_digits"
    assert snap.area_by_key("café").area_id == "area_cafe"
    assert area_of("light.digits_lamp", snap) == "area_digits"


def test_area_aliases_are_looked_up_by_room_key():
    snap = snapshot()
    assert [a.area_id for a in snap.areas_by_alias("Den")] == ["area_study"]
    assert [a.area_id for a in snap.areas_by_alias("cafe nook")] == ["area_cafe"]
    assert snap.areas_by_alias("nowhere") == ()


def test_two_areas_with_one_key_are_a_collision_reported_by_area_id():
    snap = snapshot()
    assert snap.collisions == (("living_room", ("area_living_dash", "area_living_space")),)
    assert snap.area_by_key("living_room") is None, "a collided key matches nothing"
    assert area_of("light.living_dash_lamp", snap) == "area_living_dash", "but its lights still resolve by id"


# ---------------------------------------------------------------------------
# Snapshot: membership and lamps
# ---------------------------------------------------------------------------

def test_entities_in_an_area_exclude_hidden_and_disabled_entries():
    lights = snapshot().entities_in("area_galley", "light")
    assert "light.galley_bulb_1" in lights and "light.galley_all" in lights
    assert "light.hidden_lamp" not in lights and "light.disabled_lamp" not in lights
    assert "light.child_own_lamp" not in lights, "that lamp is in the south bathroom"
    assert snapshot().entities_in("area_galley", "fan") == ("fan.galley_fan",)


def test_a_satellite_led_on_the_satellite_device_is_not_a_lamp():
    assert "light.study_voice_led" in snapshot().non_lamp_lights


def test_a_led_ring_on_a_child_of_the_satellite_device_is_not_a_lamp():
    assert "light.study_ring_child" in snapshot().non_lamp_lights


def test_disabled_hidden_and_config_category_lights_are_not_lamps():
    non_lamps = snapshot().non_lamp_lights
    assert {"light.disabled_lamp", "light.hidden_lamp", "light.config_light"} <= non_lamps


def test_ordinary_lights_are_lamps_including_ones_named_like_relays():
    non_lamps = snapshot().non_lamp_lights
    for lamp in ("light.galley_bulb_1", "light.study_lamp", "light.galley_relay_1", "light.light_1"):
        assert lamp not in non_lamps, lamp


def test_satellite_entities_of_a_device_include_those_on_its_children():
    assert snapshot().satellite_entities(DEV["study_sat"]) == (
        "assist_satellite.study_child_mic", "assist_satellite.study_voice",
    )
    assert snapshot().satellite_entities(DEV["study_sat_child"]) == ("assist_satellite.study_child_mic",)
    assert snapshot().satellite_entities(DEV["galley_hub"]) == ()


# ---------------------------------------------------------------------------
# Snapshot: unknown shapes
# ---------------------------------------------------------------------------

def broken(**changes):
    raw = copy.deepcopy(RAW)
    raw.update(changes)
    return raw


@pytest.mark.parametrize("raw", [
    broken(areas={"areas": []}),
    broken(areas=None),
    broken(devices="devices"),
    broken(entities=[["light.x"]]),
    broken(areas=[{"name": "No Id"}]),
    broken(areas=[{"area_id": "a", "name": "A", "aliases": "den"}]),
    broken(devices=[{"area_id": None}]),
    broken(entities=[{"device_id": None}]),
    broken(entities=[{"entity_id": "light.x", "area_id": 4}]),
    {"areas": [], "devices": []},
], ids=["areas-dict", "areas-none", "devices-str", "entity-row-list", "area-no-id",
        "aliases-str", "device-no-id", "entity-no-id", "area-id-int", "missing-entities"])
def test_an_unknown_registry_shape_raises_rather_than_reading_as_empty(raw):
    with pytest.raises(hr.RegistryShapeError):
        snapshot(raw)


def test_an_empty_but_well_formed_registry_is_a_valid_snapshot():
    snap = snapshot({"areas": [], "devices": [], "entities": []})
    assert snap.areas == () and not snap.non_lamp_lights


# ---------------------------------------------------------------------------
# HARegistry
# ---------------------------------------------------------------------------

def test_a_disabled_registry_never_fetches_and_says_so():
    reg = hr.HARegistry(None)

    async def go():
        await reg.start()
        reg.request_refresh(force=True)
        return await reg.ensure(0.1, refresh=True)

    assert run(go()) is None
    assert reg.state == hr.STATE_DISABLED and reg.current() is None


def test_current_makes_no_fetcher_calls_even_when_stale():
    fetcher = FakeFetcher()
    clock = Clock()
    reg = registry(fetcher, clock)

    async def go():
        await reg.ensure(1)
        calls = fetcher.calls
        clock.now += 10_000
        for _ in range(20):
            assert reg.current() is not None
        return calls

    calls = run(go())
    assert calls == 1 and fetcher.calls == 1


def test_ensure_fetches_once_and_reports_ok():
    fetcher = FakeFetcher()
    reg = registry(fetcher)
    assert reg.state == hr.STATE_UNAVAILABLE and reg.current() is None
    snap = run(reg.ensure(1))
    assert snap is reg.current() and reg.state == hr.STATE_OK and fetcher.calls == 1
    assert len(snap.areas) == 9


def test_ten_concurrent_ensures_make_one_fetch():
    fetcher = FakeFetcher()
    reg = registry(fetcher)

    async def go():
        return await asyncio.gather(*(reg.ensure(1) for _ in range(10)))

    snaps = run(go())
    assert fetcher.calls == 1 and all(s is snaps[0] for s in snaps)


def test_a_fresh_snapshot_is_served_without_a_fetch_and_a_stale_one_refreshes():
    fetcher = FakeFetcher()
    clock = Clock()
    reg = registry(fetcher, clock, ttl=300, min_refresh_interval=60)

    async def go():
        await reg.ensure(1)
        await reg.ensure(1)
        assert fetcher.calls == 1, "fresh: served from the cache"
        clock.now += 301
        await reg.ensure(1)
        assert fetcher.calls == 2, "stale: refreshed"

    run(go())


def test_request_refresh_is_rate_limited_by_the_minimum_interval():
    fetcher = FakeFetcher()
    clock = Clock()
    reg = registry(fetcher, clock, ttl=300, min_refresh_interval=60)

    async def go():
        await reg.ensure(1)
        for _ in range(5):
            reg.request_refresh(force=True)
            await asyncio.sleep(0)
        assert fetcher.calls == 1, "inside the minimum interval"
        clock.now += 61
        reg.request_refresh(force=True)
        await reg.ensure(1, refresh=True)
        assert fetcher.calls == 2
        reg.request_refresh()
        await asyncio.sleep(0)
        assert fetcher.calls == 2, "fresh snapshot: a plain request is a no-op"

    run(go())


def test_a_forced_ensure_bypasses_the_minimum_interval_but_is_itself_rate_limited():
    fetcher = FakeFetcher()
    clock = Clock()
    reg = registry(fetcher, clock, ttl=300, min_refresh_interval=60)

    async def go():
        await reg.ensure(1)
        assert fetcher.calls == 1
        clock.now += 1
        await reg.ensure(1, refresh=True)
        assert fetcher.calls == 2, "an unknown device id gets a real refresh inside the 60 s interval"
        clock.now += 1
        await reg.ensure(1, refresh=True)
        assert fetcher.calls == 2, "but not more than once per forced-refresh interval"
        clock.now += hr.FORCED_REFRESH_MIN_INTERVAL + 1
        await reg.ensure(1, refresh=True)
        assert fetcher.calls == 3

    run(go())


def test_a_failed_refresh_keeps_the_last_snapshot_and_reports_unavailable():
    fetcher = FakeFetcher()
    clock = Clock()
    reg = registry(fetcher, clock, ttl=300, min_refresh_interval=60)

    async def go():
        first = await reg.ensure(1)
        fetcher.error = ConnectionError("down")
        clock.now += 400
        after = await reg.ensure(1)
        return first, after

    first, after = run(go())
    assert after is first and reg.state == hr.STATE_UNAVAILABLE


def test_a_registry_that_never_fetched_stays_unavailable_with_no_snapshot():
    reg = registry(FakeFetcher(error=OSError("down")))
    assert run(reg.ensure(1)) is None and reg.state == hr.STATE_UNAVAILABLE


def test_an_unknown_shape_reads_unavailable_not_ok_and_empty():
    reg = registry(FakeFetcher(raw={"areas": {"x": 1}, "devices": [], "entities": []}))
    assert run(reg.ensure(1)) is None and reg.state == hr.STATE_UNAVAILABLE


def test_a_slow_fetch_does_not_hold_ensure_past_its_timeout_and_keeps_running():
    async def go():
        gate = asyncio.Event()
        fetcher = FakeFetcher(gate=gate)
        reg = registry(fetcher)
        got = await asyncio.wait_for(reg.ensure(0.05), timeout=5)
        assert got is None and fetcher.calls == 1
        gate.set()
        again = await reg.ensure(1)
        assert again is not None and fetcher.calls == 1, "the same fetch finished"
        await reg.close()

    run(go())


def test_a_stale_snapshot_with_a_hanging_fetcher_is_still_served_with_one_fetch_in_flight():
    async def go():
        clock = Clock()
        fetcher = FakeFetcher()
        reg = registry(fetcher, clock, ttl=300, min_refresh_interval=60)
        first = await reg.ensure(1)
        fetcher.gate = asyncio.Event()
        clock.now += 400
        served = [await reg.ensure(0.02) for _ in range(5)]
        assert all(s is first for s in served)
        assert fetcher.calls == 2, "one refresh, not one per turn"
        await reg.close()

    run(go())


def test_a_fetch_over_its_deadline_reads_unavailable():
    async def go():
        fetcher = FakeFetcher(gate=asyncio.Event())
        reg = registry(fetcher, fetch_timeout=0.05)
        await reg.ensure(2)
        return reg

    assert run(go()).state == hr.STATE_UNAVAILABLE


# -- auth refusal -----------------------------------------------------------

TOKEN = "tok-" + "x" * 40


def test_auth_invalid_reads_refused_and_notes_a_failure_of_class_auth():
    reg = registry(FakeFetcher(error=hr.AuthInvalid()))
    run(reg.ensure(1))
    assert reg.state == hr.STATE_REFUSED
    assert reg.link_summary()["last_failure_class"] == "auth"


def test_a_registry_keeps_no_refusal_marker_of_its_own():
    text = Path(hr.__file__).read_text(encoding="utf-8")
    assert "athena:ha_registry" not in text and "_redis" not in text, "the shared latch owns the stop"


@pytest.mark.parametrize("reason", ["latched", "admin_only", "budget"])
def test_a_fetch_the_latch_withheld_reads_refused_with_class_auth(reason):
    reg = registry(FakeFetcher(error=hr.HAWithheld(reason)))
    run(reg.ensure(1))
    assert reg.state == hr.STATE_REFUSED and reg.link_summary()["last_failure_class"] == "auth"


def test_a_gated_fetch_reads_unavailable_not_refused():
    reg = registry(FakeFetcher(error=hr.HAWithheld("gated")))
    run(reg.ensure(1))
    assert reg.state == hr.STATE_UNAVAILABLE and reg.link_summary()["last_failure_class"] == "other"


def test_a_refused_registry_tries_again_at_the_normal_interval_not_on_every_turn():
    fetcher = FakeFetcher(error=hr.AuthInvalid())
    clock = Clock()
    reg = registry(fetcher, clock)

    async def go():
        await reg.ensure(1)
        assert fetcher.calls == 1
        for _ in range(50):
            clock.now += 1
            reg.current()
            reg.request_refresh(force=True)
            await reg.ensure(0.01, refresh=False)
        assert fetcher.calls == 1, "inside the minimum interval nothing is tried"
        clock.now += 61
        reg.request_refresh(force=True)
        await reg.ensure(1)

    run(go())
    assert fetcher.calls == 2


def test_a_refused_registry_recovers_by_itself_once_the_latch_lets_a_fetch_through():
    fetcher = FakeFetcher(error=hr.HAWithheld("latched"))
    clock = Clock()
    reg = registry(fetcher, clock)

    async def go():
        await reg.ensure(1)
        assert reg.state == hr.STATE_REFUSED
        fetcher.error = None
        clock.now += 61
        await reg.ensure(1, refresh=True)

    run(go())
    assert reg.state == hr.STATE_OK and reg.current() is not None


def test_replaced_credentials_give_a_refused_registry_a_fresh_try_now():
    fetcher = FakeFetcher(error=hr.AuthInvalid())
    clock = Clock()
    reg = registry(fetcher, clock)

    async def go():
        await reg.ensure(1)
        fetcher.error = None
        clock.now += 1  # well inside the minimum interval
        reg.credentials_changed()
        await reg.ensure(1)

    run(go())
    assert fetcher.calls == 2 and reg.state == hr.STATE_OK


def test_a_blocked_address_reads_unavailable_class_other_with_the_ban_flag_until_a_fetch_works():
    fetcher = FakeFetcher(error=hr.HABanned())
    clock = Clock()
    reg = registry(fetcher, clock)

    async def go():
        await reg.ensure(1)
        banned = reg.link_summary()
        fetcher.error = None
        clock.now += 61
        await reg.ensure(1, refresh=True)
        return banned, reg.link_summary()

    banned, recovered = run(go())
    assert (banned["state"], banned["last_failure_class"], banned["ban_suspected"]) == ("unavailable", "other", True)
    assert (recovered["state"], recovered["ban_suspected"]) == ("ok", False)


def test_the_registry_is_enabled_exactly_while_home_assistant_is_configured():
    flag = {"on": False}
    fetcher = FakeFetcher()
    reg = registry(fetcher, configured=lambda: flag["on"])

    async def go():
        await reg.ensure(1)
        assert (fetcher.calls, reg.state) == (0, hr.STATE_DISABLED)
        flag["on"] = True
        await reg.ensure(1)
        enabled = (fetcher.calls, reg.state)
        flag["on"] = False
        return enabled, reg.link_summary()["state"]

    enabled, after = run(go())
    assert enabled == (1, hr.STATE_OK) and after == hr.STATE_DISABLED


def test_a_credentials_not_configured_error_from_the_fetch_reads_disabled():
    flag = {"on": True}
    reg = registry(FakeFetcher(error=hr.HomeAssistantNotConfiguredError("x")), configured=lambda: flag["on"])

    async def go():
        flag["on"] = False
        await reg.ensure(1)

    run(go())
    assert reg.state == hr.STATE_DISABLED


def test_read_is_current_that_asks_for_a_refresh_only_when_stale():
    fetcher = FakeFetcher()
    clock = Clock()
    reg = registry(fetcher, clock)

    async def go():
        assert reg.read() is None  # nothing yet: a refresh starts
        await asyncio.sleep(0.01)
        first = fetcher.calls
        got = reg.read()  # fresh now
        await asyncio.sleep(0.01)
        clock.now += 400  # past the ttl and the minimum interval
        reg.read()
        await asyncio.sleep(0.01)
        return first, got, fetcher.calls

    first, got, calls = run(go())
    assert first == 1 and got is not None and calls == 2


# -- the send allowlist -----------------------------------------------------

def test_only_auth_and_the_three_list_commands_may_be_sent():
    from shared import ha_ws

    assert hr._ALLOWED_TYPES == frozenset({
        "auth", "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list",
    })
    assert hr._ALLOWED_TYPES == ha_ws.REGISTRY_READ.types | {"auth"}


def test_the_real_fetcher_reads_through_a_session_limited_to_the_registry_read_profile():
    from shared import ha_auth, ha_registry, ha_ws
    from shared.ha_credentials import KeyLookup, StaticResolver, resolve

    from .ha_auth_fakes import FakeHA

    fake = FakeHA()
    creds = resolve(KeyLookup.found("https://ha-fixture.invalid", "sekret-token-123"), {}, previous=None, service_api_key="k")
    latch = ha_auth.HAAuthLatch(None, instance="t", service_api_key="k")
    sessions = []
    original = ha_ws.ha_ws_session

    def spy(*args, **kwargs):
        sessions.append(kwargs["profile"])
        return original(*args, **kwargs)

    ha_registry.ha_ws_session = spy
    try:
        raw = run(ha_registry.snapshot_fetcher(StaticResolver(creds), latch, caller="orchestrator_registry", connect=fake.ws_connect())())
    finally:
        ha_registry.ha_ws_session = original
    assert sessions == [ha_ws.REGISTRY_READ] and set(raw) == {"areas", "devices", "entities"}


def test_the_module_has_no_service_call_frame_type_anywhere():
    text = Path(hr.__file__).read_text(encoding="utf-8")
    assert ("call" + "_service") not in text


def test_the_websockets_package_is_imported_only_inside_the_adapter():
    import ast

    tree = ast.parse(Path(hr.__file__).read_text(encoding="utf-8"))
    top_level = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    names = {a.name for n in top_level for a in n.names} | {getattr(n, "module", "") for n in top_level}
    assert "websockets" not in names


# -- logging ----------------------------------------------------------------

def test_a_failure_logs_the_exception_class_and_never_its_text(caplog):
    secret = "http://ha.invalid:8123 token=" + TOKEN
    reg = registry(FakeFetcher(error=RuntimeError(secret)))
    with structlog.testing.capture_logs() as logs, caplog.at_level(logging.DEBUG):
        run(reg.ensure(1))
    blob = json.dumps(logs, default=str) + caplog.text
    assert TOKEN not in blob and "ha.invalid" not in blob
    assert any(e.get("error_class") == "RuntimeError" for e in logs)


def test_create_registry_is_disabled_when_off_and_idle_until_home_assistant_is_configured():
    from shared import ha_auth
    from shared.ha_credentials import HACredentials, KeyLookup, StaticResolver, resolve

    latch = ha_auth.HAAuthLatch(None, instance="t", service_api_key="k")
    configured = StaticResolver(resolve(KeyLookup.found("http://ha.example", "t"), {}, previous=None, service_api_key="k"))
    empty = StaticResolver(HACredentials())
    kwargs = dict(latch=latch, caller="orchestrator_registry")
    assert hr.create_registry(credentials=configured, mode="off", **kwargs).state == hr.STATE_DISABLED
    assert hr.create_registry(credentials=empty, mode="auto", **kwargs).state == hr.STATE_DISABLED
    assert hr.create_registry(credentials=configured, mode="auto", **kwargs).state == hr.STATE_UNAVAILABLE


# ---------------------------------------------------------------------------
# Config fields
# ---------------------------------------------------------------------------

def _config(monkeypatch, **env):
    from shared import config

    for name in ("HA_AREAS", "HA_DEVICE_CLIENT_NETWORKS", "HA_WHOLE_HOUSE_UNASSIGNED_LIGHTS"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.get_config.cache_clear()
    try:
        return config.AthenaConfig()
    finally:
        config.get_config.cache_clear()


def test_config_defaults_are_areas_auto_no_device_networks_and_no_unassigned_lights(monkeypatch):
    cfg = _config(monkeypatch)
    assert cfg.ha_areas == "auto"
    assert cfg.ha_device_client_networks == ""
    assert cfg.ha_whole_house_unassigned_lights is False


def test_ha_areas_off_is_honoured_case_insensitively(monkeypatch):
    assert _config(monkeypatch, HA_AREAS="OFF").ha_areas == "off"


def test_an_invalid_ha_areas_logs_one_error_and_reads_off(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="shared.config"):
        cfg = _config(monkeypatch, HA_AREAS="sometimes")
    assert cfg.ha_areas == "off"
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "sometimes" not in errors[0].getMessage()


def test_the_unassigned_lights_flag_and_networks_read_their_variables(monkeypatch):
    cfg = _config(monkeypatch, HA_WHOLE_HOUSE_UNASSIGNED_LIGHTS="true", HA_DEVICE_CLIENT_NETWORKS="192.0.2.5/32")
    assert cfg.ha_whole_house_unassigned_lights is True
    assert cfg.ha_device_client_networks == "192.0.2.5/32"


def test_a_forced_refresh_outside_the_interval_still_counts_against_the_forced_limit():
    fetcher = FakeFetcher()
    clock = Clock()
    reg = registry(fetcher, clock, ttl=300, min_refresh_interval=60)

    async def go():
        await reg.ensure(1)
        assert fetcher.calls == 1
        clock.now += 61
        await reg.ensure(1, refresh=True)
        assert fetcher.calls == 2, "a forced refresh past the interval runs"
        clock.now += 1
        await reg.ensure(1, refresh=True)
        assert fetcher.calls == 2, "a second forced refresh 1 s later is rate-limited"
        clock.now += hr.FORCED_REFRESH_MIN_INTERVAL + 1
        await reg.ensure(1, refresh=True)
        assert fetcher.calls == 3

    run(go())


# -- link summary: wall-clock facts for the Home Assistant link status ------

WALL = [datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)]


def wall_registry(fetcher, **kwargs):
    WALL[0] = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
    return hr.HARegistry(fetcher, monotonic=Clock(), wall=lambda: WALL[0], **kwargs)


LINK_KEYS = {"state", "areas", "last_success_at", "last_failure_at", "last_failure_class", "ban_suspected"}


def test_link_summary_has_exactly_six_keys_and_no_names():
    reg = wall_registry(FakeFetcher())
    run(reg.ensure(1))
    summary = reg.link_summary()
    assert set(summary) == LINK_KEYS
    assert summary["state"] == "ok" and summary["areas"] == snapshot().counts()["areas"]
    assert summary["last_success_at"] == WALL[0].isoformat()
    assert summary["last_failure_at"] is None and summary["last_failure_class"] is None
    assert not any(name in json.dumps(summary) for name in RAW["areas"][0].values() if isinstance(name, str))


def test_a_disabled_registry_summarises_as_disabled_with_nothing_else():
    assert hr.HARegistry(None).link_summary() == {
        "state": "disabled", "areas": None, "last_success_at": None,
        "last_failure_at": None, "last_failure_class": None, "ban_suspected": False,
    }


@pytest.mark.parametrize(
    "error,expected_class",
    [
        (asyncio.TimeoutError(), "timeout"),
        (TimeoutError(), "timeout"),
        (ConnectionRefusedError("down"), "unreachable"),
        (OSError("net"), "unreachable"),
        (ValueError("odd"), "other"),
    ],
)
def test_a_failed_fetch_records_when_and_what_class_of_failure(error, expected_class):
    reg = wall_registry(FakeFetcher(error=error))
    run(reg.ensure(1))
    summary = reg.link_summary()
    assert summary["state"] == "unavailable"
    assert summary["last_failure_at"] == WALL[0].isoformat()
    assert summary["last_failure_class"] == expected_class
    assert summary["last_success_at"] is None


def test_a_fetch_over_its_deadline_is_a_timeout_failure():
    reg = wall_registry(FakeFetcher(gate=asyncio.Event()), fetch_timeout=0.05)
    run(reg.ensure(2))
    assert reg.link_summary()["last_failure_class"] == "timeout"


def test_a_success_after_a_failure_keeps_both_times():
    fetcher = FakeFetcher(error=OSError("down"))
    clock = Clock()
    reg = hr.HARegistry(fetcher, monotonic=clock, wall=lambda: WALL[0], ttl=300, min_refresh_interval=60)

    async def go():
        WALL[0] = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)
        await reg.ensure(1)
        fetcher.error = None
        clock.now += 400
        WALL[0] = datetime(2026, 10, 11, 12, 10, 0, tzinfo=timezone.utc)
        await reg.ensure(1)

    run(go())
    summary = reg.link_summary()
    assert summary["state"] == "ok"
    assert summary["last_failure_at"] == "2026-10-11T12:00:00+00:00"
    assert summary["last_success_at"] == "2026-10-11T12:10:00+00:00"


def test_an_auth_refusal_records_a_failure_of_class_auth():
    reg = wall_registry(FakeFetcher(error=hr.AuthInvalid()))
    run(reg.ensure(1))
    summary = reg.link_summary()
    assert summary["state"] == "refused"
    assert (summary["last_failure_at"], summary["last_failure_class"]) == (WALL[0].isoformat(), "auth")


def test_a_start_that_the_latch_withholds_records_class_auth_now():
    fetcher = FakeFetcher(error=hr.HAWithheld("latched"))
    reg = wall_registry(fetcher)

    async def go():
        await reg.start()
        await asyncio.sleep(0.01)

    run(go())
    summary = reg.link_summary()
    assert (summary["state"], summary["last_failure_class"]) == ("refused", "auth")
    assert summary["last_failure_at"] == WALL[0].isoformat()
