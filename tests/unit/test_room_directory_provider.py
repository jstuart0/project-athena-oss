"""RoomDirectoryProvider: one directory, built from the registry snapshot, the
room lights and speaker rooms values (injected resolvers; the environment
strings here stand in for ``HA_LIGHT_GROUPS`` / ``HA_SATELLITE_ROOM_MAP``)
and the admin room groups, rebuilt only when one of them changes. Fictional
rooms; injected clock."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import structlog

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import room_directory as rd  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from shared import ha_registry as hr  # noqa: E402
from shared import household  # noqa: E402

RAW = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json").read_text(encoding="utf-8")
)
STUDY_DEVICE = "ab" * 16


class Registry:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot
        self.reads = 0

    def current(self):
        return self.snapshot

    def read(self):
        self.reads += 1
        return self.snapshot


def snapshot(fetched_at=1.0):
    return hr.RegistrySnapshot.from_raw(RAW, fetched_at=fetched_at)


class Config:
    def __init__(self, groups="", satellites=""):
        self.ha_light_groups = groups
        self.ha_satellite_room_map = satellites


class Admin:
    def __init__(self, groups=None, error=None):
        self.groups = groups or []
        self.error = error
        self.calls = 0

    async def get_room_groups(self, enabled_only=True):
        self.calls += 1
        if self.error:
            raise self.error
        return self.groups


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def provider(registry=None, admin=None, config=None, names=None, clock=None):
    cfg = config or Config()
    return rd.RoomDirectoryProvider(
        registry, admin, light_group_names=names, monotonic=clock or Clock(),
        room_lights=lambda: household.room_lights_from(None, cfg.ha_light_groups),
        speaker_rooms=lambda: household.speaker_rooms_from(None, cfg.ha_satellite_room_map, outage_age=None),
    )


def test_the_directory_holds_the_registry_areas_and_their_aliases():
    directory = provider(Registry(snapshot())).current()
    assert directory.resolves("galley") is not None
    assert directory.resolves("den").key == "study"
    assert directory.resolves("not_a_room") is None


def test_light_group_keys_and_satellite_map_values_are_rooms_too():
    cfg = Config(
        groups=json.dumps({"Sunroom": "light.sunroom_all"}),
        satellites=json.dumps({"assist_satellite.loft_voice": "loft", STUDY_DEVICE: "study"}),
    )
    directory = provider(Registry(snapshot()), config=cfg).current()
    assert directory.resolves("sunroom") is not None and directory.resolves("loft") is not None


def test_the_directory_is_memoized_until_an_input_changes():
    registry = Registry(snapshot(1.0))
    cfg = Config()
    p = provider(registry, config=cfg)
    first = p.current()
    assert p.current() is first
    registry.snapshot = snapshot(2.0)
    second = p.current()
    assert second is not first and p.current() is second
    cfg.ha_light_groups = json.dumps({"Sunroom": "light.sunroom_all"})
    third = p.current()
    assert third is not second and third.resolves("sunroom") is not None
    cfg.ha_satellite_room_map = json.dumps({"assist_satellite.loft_voice": "loft"})
    assert p.current().resolves("loft") is not None


def test_a_malformed_light_group_value_adds_no_rooms_and_does_not_raise():
    directory = provider(Registry(snapshot()), config=Config(groups="{not json")).current()
    assert directory.resolves("galley") is not None


def test_group_friendly_names_are_used_only_while_the_registry_has_no_snapshot():
    names = lambda: ["Master Bathroom Lights", "Master Bathroom All"]  # noqa: E731
    down = provider(Registry(None), names=names).current()
    assert down.keys() == ("master_bathroom",)
    up = provider(Registry(snapshot()), names=names).current()
    assert up.resolves("master_bathroom") is None


def test_group_names_that_change_rebuild_the_directory_while_the_registry_is_down():
    current = ["Galley Lights"]
    p = provider(Registry(None), names=lambda: list(current))
    first = p.current()
    current.append("Study Lights")
    assert p.current().keys() == ("galley", "study") and p.current() is not first


def test_without_a_registry_the_directory_still_has_the_configured_rooms():
    directory = provider(None, config=Config(groups=json.dumps({"Sunroom": "light.sunroom_all"}))).current()
    assert directory.keys() == ("sunroom",)


GROUP = {"name": "upper", "display_name": "Upper", "aliases": ["upstairs"],
         "members": [{"room_name": "study", "display_name": "Study"}]}


def test_start_fetches_the_room_groups_once_and_the_directory_has_them():
    admin = Admin([GROUP])
    p = provider(Registry(snapshot()), admin)
    asyncio.run(p.start())
    assert admin.calls == 1
    directory = p.current()
    assert directory.resolves("upstairs").key == "upper" and directory.members_of("upper") == ("study",)


def test_room_groups_refresh_in_the_background_after_the_ttl_and_not_before():
    clock = Clock()
    admin = Admin([GROUP])
    p = provider(Registry(snapshot()), admin, clock=clock)

    async def go():
        await p.start()
        p.current()
        await asyncio.sleep(0)
        assert admin.calls == 1, "inside the TTL"
        clock.now += rd.GROUP_TTL_SECONDS + 1
        admin.groups = []
        p.current()
        await p._task
        return p.current()

    directory = asyncio.run(go())
    assert admin.calls == 2 and directory.resolves("upstairs") is None


def test_a_failed_group_refresh_keeps_the_previous_groups():
    clock = Clock()
    admin = Admin([GROUP])
    p = provider(Registry(snapshot()), admin, clock=clock)

    async def go():
        await p.start()
        admin.error = ConnectionError("down")
        clock.now += rd.GROUP_TTL_SECONDS + 1
        p.current()
        await p._task

    asyncio.run(go())
    assert p.current().resolves("upstairs") is not None


def test_building_logs_counts_and_no_room_names():
    p = provider(Registry(snapshot()), config=Config(groups=json.dumps({"Sunroom": "light.sunroom_all"})))
    with structlog.testing.capture_logs() as logs:
        p.current()
    built = [e for e in logs if e["event"] == "room_directory_built"]
    assert len(built) == 1 and built[0]["rooms"] >= 5 and "collisions" in built[0]
    blob = json.dumps(logs, default=str).lower()
    assert "galley" not in blob and "sunroom" not in blob and "study" not in blob


def test_current_directory_is_none_until_a_provider_is_registered():
    _runtime.reset_for_test()
    assert rd.current_directory() is None
    p = provider(Registry(snapshot()))
    _runtime.set_room_directory(p)
    try:
        assert rd.current_directory() is p.current()
    finally:
        _runtime.reset_for_test()


def test_a_changed_room_lights_or_speaker_rooms_version_rebuilds_the_directory():
    lights = {"value": household.room_lights_from(None, "")}
    speakers = {"value": household.speaker_rooms_from(None, "", outage_age=None)}
    p = rd.RoomDirectoryProvider(
        Registry(snapshot()), None, room_lights=lambda: lights["value"], speaker_rooms=lambda: speakers["value"],
    )
    first = p.current()
    assert p.current() is first
    lights["value"] = household.room_lights_from(None, json.dumps({"Sunroom": "light.sunroom_all"}))
    second = p.current()
    assert second is not first and second.resolves("sunroom") is not None
    speakers["value"] = household.speaker_rooms_from(
        None, json.dumps({"assist_satellite.loft_voice": "loft"}), outage_age=None)
    third = p.current()
    assert third is not second and third.resolves("loft") is not None


def test_a_room_with_no_key_is_kept_out_of_the_directory_and_keys_are_sorted():
    cfg = Config(groups=json.dumps({"!!!": "light.x", "Zed Room": "light.z", "Attic": "light.a"}))
    assert provider(None, config=cfg).current().keys() == ("attic", "zed_room")


def test_the_provider_serves_the_resolver_values_it_was_given():
    cfg = Config(groups=json.dumps({"Sunroom": "light.sunroom_all"}))
    p = provider(Registry(snapshot()), config=cfg)
    assert dict(p.room_lights().mapping) == {"sunroom": ("light.sunroom_all",)}
    assert p.speaker_rooms().source == "default"


def test_sources_reports_counts_and_words_only():
    cfg = Config(
        groups=json.dumps({"Sunroom": "light.sunroom_all", "Galley": "light.galley_all"}),
        satellites=json.dumps({"assist_satellite.loft_voice": "loft", STUDY_DEVICE: "study"}),
    )
    report = provider(Registry(snapshot()), config=cfg).sources()
    assert report["room_lights"] == {"source": "env", "rooms": 2, "lights": 2, "unmatched": 1}
    assert report["speaker_rooms"] == {"source": "env", "device_keys": 1, "entity_keys": 1, "unmatched": 1}
    assert provider(None, config=cfg).sources()["room_lights"]["unmatched"] is None


def test_unmatched_override_rooms_are_logged_once_per_change_with_a_snapshot_only():
    cfg = Config(groups=json.dumps({"Sunroom": "light.sunroom_all", "Galley": "light.galley_all"}))
    p = provider(Registry(snapshot(1.0)), config=cfg)
    with structlog.testing.capture_logs() as logs:
        p.current()
        p.current()
    unmatched = [e for e in logs if e["event"] == "room_overrides_unmatched"]
    assert [e["count"] for e in unmatched] == [1]
    assert "sunroom" not in json.dumps(logs, default=str).lower()
    with structlog.testing.capture_logs() as logs:
        provider(None, config=cfg).current()
    assert not [e for e in logs if e["event"] == "room_overrides_unmatched"]


def test_sources_counts_only_rooms_with_a_key():
    cfg = Config(groups=json.dumps({"!!!": "light.x", "Attic": "light.a"}))
    assert provider(None, config=cfg).sources()["room_lights"]["rooms"] == 1
