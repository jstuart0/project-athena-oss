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


class FakeRedis:
    def __init__(self, present=()):
        self.keys = set(present)
        self.sets = []

    async def exists(self, key):
        return 1 if key in self.keys else 0

    async def set(self, key, value, ex=None):
        self.keys.add(key)
        self.sets.append((key, value, ex))


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


def test_auth_invalid_latches_refused_and_writes_a_24_hour_hmac_marker():
    redis = FakeRedis()
    marker = hr.token_marker_key(TOKEN, "service-key-for-tests")
    reg = registry(FakeFetcher(error=hr.AuthInvalid()), token_marker_key=marker, redis=redis)
    run(reg.ensure(1))
    assert reg.state == hr.STATE_REFUSED
    [(key, value, ttl)] = redis.sets
    assert key == f"athena:ha_registry:refused:{marker}" and ttl == 86400
    assert TOKEN not in key and len(marker) == 16


def test_the_marker_key_is_keyed_on_the_service_key_and_absent_without_one():
    assert hr.token_marker_key(TOKEN, "k1") != hr.token_marker_key(TOKEN, "k2")
    assert hr.token_marker_key(TOKEN, "k1") == hr.token_marker_key(TOKEN, "k1")
    assert hr.token_marker_key(TOKEN, "") is None and hr.token_marker_key(TOKEN, None) is None


def test_without_a_service_key_the_latch_is_in_process_only():
    redis = FakeRedis()
    reg = registry(FakeFetcher(error=hr.AuthInvalid()), token_marker_key=None, redis=redis)
    run(reg.ensure(1))
    assert reg.state == hr.STATE_REFUSED and redis.sets == []


def test_a_new_client_with_the_marker_makes_no_fetches_and_logs_an_error_every_start():
    marker = hr.token_marker_key(TOKEN, "service-key-for-tests")
    redis = FakeRedis(present={f"athena:ha_registry:refused:{marker}"})
    for _ in range(2):
        fetcher = FakeFetcher()
        reg = registry(fetcher, token_marker_key=marker, redis=redis)
        with structlog.testing.capture_logs() as logs:
            run(reg.start())
            run(reg.ensure(1, refresh=True))
        assert fetcher.calls == 0 and reg.state == hr.STATE_REFUSED
        assert any(
            e["event"] == "ha_registry_auth_refused_marker_present" and e["log_level"] == "error"
            for e in logs
        )


def test_a_refused_registry_makes_no_fetcher_calls_over_fifty_turns():
    fetcher = FakeFetcher(error=hr.AuthInvalid())
    clock = Clock()
    reg = registry(fetcher, clock)

    async def go():
        await reg.ensure(1)
        assert fetcher.calls == 1
        for _ in range(50):
            clock.now += 1000
            reg.current()
            reg.request_refresh(force=True)
            await reg.ensure(0.01, refresh=True)

    run(go())
    assert fetcher.calls == 1


# -- the send allowlist -----------------------------------------------------

def test_only_auth_and_the_three_list_commands_may_be_sent():
    assert hr._ALLOWED_TYPES == frozenset({
        "auth", "config/area_registry/list", "config/device_registry/list", "config/entity_registry/list",
    })


class _Socket:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


@pytest.mark.parametrize("frame_type", ["call" + "_service", "subscribe_events", "config/area_registry/create", "", None])
def test_sending_any_other_frame_type_raises_before_it_reaches_the_socket(frame_type):
    ws = _Socket()
    with pytest.raises(hr.ForbiddenFrameType):
        run(hr._send(ws, {"id": 9, "type": frame_type}))
    assert ws.sent == []


def test_an_allowed_frame_is_sent():
    ws = _Socket()
    run(hr._send(ws, {"id": 1, "type": "config/area_registry/list"}))
    assert ws.sent == [{"id": 1, "type": "config/area_registry/list"}]


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


def test_websocket_url_follows_the_scheme():
    assert hr.websocket_url("http://ha.example:8123") == "ws://ha.example:8123/api/websocket"
    assert hr.websocket_url("https://ha.example/") == "wss://ha.example/api/websocket"


def test_create_registry_is_disabled_when_off_or_missing_credentials():
    assert hr.create_registry(ha_url="http://ha.example", ha_token="t", mode="off").state == hr.STATE_DISABLED
    assert hr.create_registry(ha_url="", ha_token="t", mode="auto").state == hr.STATE_DISABLED
    assert hr.create_registry(ha_url="http://ha.example", ha_token=None, mode="auto").state == hr.STATE_DISABLED
    assert hr.create_registry(ha_url="http://ha.example", ha_token="t", mode="auto").state == hr.STATE_UNAVAILABLE


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
