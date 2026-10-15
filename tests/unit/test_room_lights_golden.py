"""Golden baseline for room lights and speaker rooms, taken before the admin move.

Inputs are the real environment variables (HA_LIGHT_GROUPS, HA_SATELLITE_ROOM_MAP)
read through shared.config, over a fictional house. The expected values are
hard-coded and must not change when the readers change: this file pins what
the env-only deployment does today.
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest.mock as mock
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

import os  # noqa: E402

os.environ.setdefault("SERVICE_API_KEY", "test-key-room-lights-golden")
os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

import gateway.main as gw  # noqa: E402
import orchestrator.smart_home_controller as shc  # noqa: E402
import shared.config  # noqa: E402
from gateway import speaker_room as sr  # noqa: E402
from orchestrator import ha_entity_manager as hem  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import room_directory as rd  # noqa: E402
from orchestrator import write_fanout  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402
from shared import ha_registry as hr  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ha_registry" / "house_a.json"
RAW_HOUSE = json.loads(FIXTURE.read_text(encoding="utf-8"))
STUDY_DEVICE = RAW_HOUSE["devices_by_name"]["study_sat"]
DEVICE_ONLY_ID = "c" * 32


def set_env(monkeypatch, *, groups=None, satellites=None):
    for name, value in (("HA_LIGHT_GROUPS", groups), ("HA_SATELLITE_ROOM_MAP", satellites)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()
    monkeypatch.setattr(shc, "_light_groups_cache", None, raising=False)
    monkeypatch.setattr(gw, "_ha_satellite_room_map_cache", None, raising=False)


@pytest.fixture(autouse=True)
def _isolated():
    _runtime.reset_for_test()
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()
    sr.reset_for_test()
    yield
    _runtime.reset_for_test()
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()
    sr.reset_for_test()


# ---------------------------------------------------------------------------
# A fictional house
# ---------------------------------------------------------------------------

def _state(entity_id, members=None, name=None):
    attrs = {"friendly_name": name or entity_id.split(".")[1].replace("_", " ").title()}
    if members is not None:
        attrs["entity_id"] = list(members)
    return {"entity_id": entity_id, "state": "on", "attributes": attrs}


def _reg(entity_id, area=None):
    return {"entity_id": entity_id, "area_id": area, "device_id": None,
            "entity_category": None, "hidden_by": None, "disabled_by": None}


def house_states():
    return {s["entity_id"]: s for s in [
        _state("light.lounge_a"), _state("light.lounge_b"),
        _state("light.lounge_group", members=["light.lounge_a", "light.lounge_b"], name="Lounge Group"),
        _state("light.den_lamp"), _state("light.den_strip"),
        _state("light.loft_lamp"),
        _state("light.attic_one", name="Attic One"), _state("light.attic_two", name="Attic Two"),
        _state("light.desk_group", members=["light.desk_lamp"], name="Desk Group"),
        _state("light.desk_lamp"),
        _state("light.loose_lamp"),
    ]}


def house_snapshot():
    raw = {
        "areas": [{"area_id": a, "name": n, "aliases": []} for a, n in
                  [("area_den", "Den"), ("area_loft", "Loft"), ("area_desk", "Desk")]],
        "devices": [],
        "entities": [_reg("light.den_lamp", "area_den"), _reg("light.den_strip", "area_den"),
                     _reg("light.loft_lamp", "area_loft"), _reg("light.desk_lamp", "area_desk")],
    }
    return hr.RegistrySnapshot.from_raw(raw, fetched_at=1.0)


class FakeRegistry:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.state = "ok" if snapshot else "unavailable"

    def current(self):
        return self._snapshot

    def read(self):
        return self._snapshot


class Rig:
    def __init__(self, monkeypatch, *, registry=True, room_groups=None):
        _runtime.set_ha_registry(FakeRegistry(house_snapshot() if registry else None))
        ha = MagicMock()
        self.manager = hem.HAEntityManager(ha)
        self.manager._entities_cache = house_states()
        self.manager._cache_time = datetime.now()
        self.manager._build_indexes()
        self.manager._cache_duration = hem.timedelta(days=1)
        admin = MagicMock()
        admin.get_room_groups = AsyncMock(return_value=room_groups or [])
        monkeypatch.setattr(shc, "get_admin_client", lambda: admin)
        self.controller = shc.SmartHomeController(entity_manager=self.manager, llm_router=MagicMock())
        self.gates = []
        real_gate = write_fanout.gate

        def recording_gate(domain, service=None, entity_ids=None, *a, **kw):
            self.gates.append((domain, service, tuple(entity_ids or ())))
            return real_gate(domain, service, entity_ids, *a, **kw)

        monkeypatch.setattr(write_fanout, "gate", recording_gate)

    def in_scope(self, coro_fn, *, perms=None, mode="owner", trusted_voice=True, query="turn on the lights"):
        async def go():
            with mp.ha_permission_scope(perms or {"mode": "owner"}, mode=mode,
                                        utterance=classify_utterance(query), trusted_voice=trusted_voice):
                return await coro_fn()

        return asyncio.run(go())


def ids_of(matches):
    return sorted(m["entity_id"] for m in matches)


# ---------------------------------------------------------------------------
# (a) _resolve_room_lights
# ---------------------------------------------------------------------------

GROUPS = '{"Lounge": "light.lounge_group", "den": "light.den_missing", "Desk": "light.desk_group"}'


def test_a_configured_group_with_members_is_the_one_target(monkeypatch):
    set_env(monkeypatch, groups=GROUPS)
    rig = Rig(monkeypatch)
    got = asyncio.run(rig.controller._resolve_room_lights("Lounge"))
    assert got == [{
        "entity_id": "light.lounge_group",
        "friendly_name": "Lounge Group",
        "members": ["light.lounge_a", "light.lounge_b"],
        "state": "on",
        "type": "group",
    }]


def test_a_configured_single_light_has_no_members_and_is_individual(monkeypatch):
    set_env(monkeypatch, groups='{"Living Room": "light.loft_lamp"}')
    rig = Rig(monkeypatch)
    for spoken in ("living room", "Living-Room", "LIVING_ROOM"):
        got = asyncio.run(rig.controller._resolve_room_lights(spoken))
        assert [(m["entity_id"], m["members"], m["type"]) for m in got] == [("light.loft_lamp", [], "individual")], spoken


def test_a_configured_id_missing_from_home_assistant_falls_to_the_area_tier(monkeypatch):
    set_env(monkeypatch, groups=GROUPS)
    rig = Rig(monkeypatch)
    got = asyncio.run(rig.controller._resolve_room_lights("den"))
    assert ids_of(got) == ["light.den_lamp", "light.den_strip"]


def test_a_configured_id_missing_with_no_registry_falls_to_the_name_tier(monkeypatch):
    set_env(monkeypatch, groups='{"attic": "light.attic_missing"}')
    rig = Rig(monkeypatch, registry=False)
    got = asyncio.run(rig.controller._resolve_room_lights("attic"))
    assert ids_of(got) == ["light.attic_one", "light.attic_two"]


def test_no_entry_uses_the_area_tier_then_the_name_tier(monkeypatch):
    set_env(monkeypatch)
    with_registry = Rig(monkeypatch)
    assert ids_of(asyncio.run(with_registry.controller._resolve_room_lights("loft"))) == ["light.loft_lamp"]
    _runtime.reset_for_test()
    without_registry = Rig(monkeypatch, registry=False)
    assert ids_of(asyncio.run(without_registry.controller._resolve_room_lights("attic"))) == [
        "light.attic_one", "light.attic_two"]


def test_invalid_json_and_non_object_values_configure_nothing_and_the_name_tier_answers(monkeypatch):
    for raw in ("{not json", '["light.lounge_group"]'):
        set_env(monkeypatch, groups=raw)
        rig = Rig(monkeypatch, registry=False)
        assert shc._get_light_groups() == {}, raw
        assert ids_of(asyncio.run(rig.controller._resolve_room_lights("lounge"))) == ["light.lounge_group"], raw


def test_light_group_keys_are_normalised_and_the_last_duplicate_wins(monkeypatch):
    set_env(monkeypatch, groups='{"Living Room": "light.a", "living_room": "light.b", "!!!": "light.c", "Den": 7}')
    assert shc._get_light_groups() == {"living_room": "light.b", "": "light.c", "den": "7"}


# ---------------------------------------------------------------------------
# (b) _whole_house_lamp_rooms by audience
# ---------------------------------------------------------------------------

WHOLE_GROUPS = '{"Lounge": "light.lounge_group", "Desk": "light.desk_group"}'
UPPER = [{"name": "upper", "display_name": "Upper", "aliases": [],
          "members": [{"room_name": "loft"}, {"room_name": "den"}]}]


def whole_house(monkeypatch, *, perms, mode, trusted, groups=WHOLE_GROUPS, registry=True, room_groups=None,
                excluded=()):
    set_env(monkeypatch, groups=groups)
    rig = Rig(monkeypatch, registry=registry, room_groups=room_groups)
    return rig.in_scope(lambda: rig.controller._whole_house_lamp_rooms(list(excluded)),
                        perms=perms, mode=mode, trusted_voice=trusted)


GUEST = {"mode": "guest"}
PUBLIC = {"mode": "owner", "audience": "public"}


def test_an_owner_the_gateway_vouched_for_gets_every_areas_lamps(monkeypatch):
    rooms, why = whole_house(monkeypatch, perms={"mode": "owner"}, mode="owner", trusted=True)
    assert (rooms, why) == ([["light.den_lamp", "light.den_strip"], ["light.desk_lamp"], ["light.loft_lamp"]], "none")


def test_an_owner_without_the_vouch_gets_the_leaves_of_the_home_assistant_light_groups(monkeypatch):
    rooms, why = whole_house(monkeypatch, perms={"mode": "owner"}, mode="owner", trusted=False)
    assert why == "none"
    assert sorted(map(tuple, rooms)) == [("light.desk_lamp",), ("light.lounge_a", "light.lounge_b")]


def test_a_guest_gets_only_the_configured_groups_trusted_mark_or_not(monkeypatch):
    for trusted in (True, False):
        rooms, why = whole_house(monkeypatch, perms=GUEST, mode="guest", trusted=trusted)
        assert why == "none"
        assert sorted(map(tuple, rooms)) == [("light.desk_lamp",), ("light.lounge_a", "light.lounge_b")]


def test_the_public_audience_is_treated_like_a_guest(monkeypatch):
    rooms, why = whole_house(monkeypatch, perms=PUBLIC, mode="owner", trusted=True)
    assert why == "none"
    assert sorted(map(tuple, rooms)) == [("light.desk_lamp",), ("light.lounge_a", "light.lounge_b")]


def test_a_guest_with_no_groups_and_no_room_groups_is_asked_which_room(monkeypatch):
    assert whole_house(monkeypatch, perms=GUEST, mode="guest", trusted=True, groups=None) == (None, "ask")


def test_a_guest_also_reaches_the_rooms_of_admin_room_groups(monkeypatch):
    rooms, why = whole_house(monkeypatch, perms=GUEST, mode="guest", trusted=True, groups=None, room_groups=UPPER)
    assert why == "none"
    assert sorted(map(tuple, rooms)) == [("light.den_lamp", "light.den_strip"), ("light.loft_lamp",)]


def test_env_groups_and_admin_room_groups_both_count_for_a_guest(monkeypatch):
    rooms, _ = whole_house(monkeypatch, perms=GUEST, mode="guest", trusted=True, room_groups=UPPER)
    flat = sorted(leaf for room in rooms for leaf in room)
    assert flat == ["light.den_lamp", "light.den_strip", "light.desk_lamp", "light.loft_lamp", "light.lounge_a",
                    "light.lounge_b"]


def test_an_excluded_room_is_subtracted_in_every_audience(monkeypatch):
    owner, _ = whole_house(monkeypatch, perms={"mode": "owner"}, mode="owner", trusted=True, excluded=["den"])
    assert owner == [["light.desk_lamp"], ["light.loft_lamp"]]
    guest, _ = whole_house(monkeypatch, perms=GUEST, mode="guest", trusted=True, excluded=["desk"])
    assert sorted(map(tuple, guest)) == [("light.lounge_a", "light.lounge_b")]


# ---------------------------------------------------------------------------
# (c) RoomDirectory vocabulary from env inputs
# ---------------------------------------------------------------------------

def directory(monkeypatch, *, groups, satellites, snapshot=None):
    set_env(monkeypatch, groups=groups, satellites=satellites)
    registry = FakeRegistry(snapshot)
    return rd.RoomDirectoryProvider(registry, None).current()


def labels(d):
    return {k: d.ref(k).label for k in d.keys()}


def test_env_group_keys_and_satellite_rooms_make_the_vocabulary(monkeypatch):
    d = directory(
        monkeypatch,
        groups='{"Living Room": "light.a", "living_room": "light.b", "!!!": "light.c", "Den": "light.d"}',
        satellites=json.dumps({"assist_satellite.kitchen_nook_voice": "Kitchen_Nook", DEVICE_ONLY_ID: "Garage"}),
    )
    assert d.keys() == ("den", "garage", "kitchen_nook", "living_room")
    assert labels(d) == {"den": "den", "garage": "garage", "kitchen_nook": "kitchen nook", "living_room": "living room"}


def test_a_punctuation_only_key_and_a_normalised_duplicate_add_no_extra_room(monkeypatch):
    d = directory(monkeypatch, groups='{"Den": "light.a", "den": "light.b", "?": "light.c"}', satellites=None)
    assert d.keys() == ("den",) and len(d) == 1


def test_registry_areas_join_the_env_vocabulary(monkeypatch):
    d = directory(monkeypatch, groups='{"Lounge": "light.lounge_group"}', satellites=None, snapshot=house_snapshot())
    assert d.keys() == ("den", "desk", "loft", "lounge")
    assert labels(d) == {"den": "den", "desk": "desk", "loft": "loft", "lounge": "lounge"}


def test_unset_env_and_no_registry_is_an_empty_directory(monkeypatch):
    assert len(directory(monkeypatch, groups=None, satellites=None)) == 0


# ---------------------------------------------------------------------------
# (d) gateway resolve_speaker_room from HA_SATELLITE_ROOM_MAP
# ---------------------------------------------------------------------------

class Fetcher:
    async def __call__(self):
        return RAW_HOUSE


class Clock:
    def __call__(self):
        return 1000.0


def speaker_room(monkeypatch, *, satellites, device_id, trusted=True, states=None):
    set_env(monkeypatch, satellites=satellites)
    registry = hr.HARegistry(Fetcher(), monotonic=Clock())
    asyncio.run(registry.ensure(1))

    async def read_states():
        return states

    return asyncio.run(sr.resolve_speaker_room(
        device_id=device_id, trusted=trusted, registry=registry,
        satellite_map=gw._get_satellite_room_map(),
        read_states=read_states if states is not None else None,
    ))


def test_an_env_device_key_gives_its_room_from_the_map(monkeypatch):
    got = speaker_room(monkeypatch, satellites=json.dumps({STUDY_DEVICE: "Kitchen Nook"}), device_id=STUDY_DEVICE)
    assert got == sr.SpeakerRoom("kitchen_nook", "map")


def test_an_env_entity_key_on_the_device_gives_its_room_from_the_map(monkeypatch):
    got = speaker_room(monkeypatch, satellites=json.dumps({"assist_satellite.study_voice": "Garage"}),
                       device_id=STUDY_DEVICE)
    assert got == sr.SpeakerRoom("garage", "map")


def test_with_no_map_the_registry_area_answers(monkeypatch):
    assert speaker_room(monkeypatch, satellites=None, device_id=STUDY_DEVICE) == sr.SpeakerRoom("study", "area")


def test_a_map_naming_another_device_leaves_the_area_answer(monkeypatch):
    got = speaker_room(monkeypatch, satellites=json.dumps({DEVICE_ONLY_ID: "Garage"}), device_id=STUDY_DEVICE)
    assert got == sr.SpeakerRoom("study", "area")


def test_one_active_satellite_with_no_device_id_is_the_guess(monkeypatch):
    states = [{"entity_id": "assist_satellite.study_voice", "state": "listening"},
              {"entity_id": "assist_satellite.galley_voice", "state": "idle"}]
    got = speaker_room(monkeypatch, satellites=None, device_id=None, states=states)
    assert got == sr.SpeakerRoom("study", "guess")


def test_an_untrusted_caller_or_no_signal_is_unknown(monkeypatch):
    assert speaker_room(monkeypatch, satellites=json.dumps({STUDY_DEVICE: "Garage"}),
                        device_id=STUDY_DEVICE, trusted=False) == sr.UNKNOWN_ROOM
    assert speaker_room(monkeypatch, satellites=None, device_id=None) == sr.UNKNOWN_ROOM


def test_the_gateway_map_splits_env_keys_and_drops_bad_ones(monkeypatch):
    set_env(monkeypatch, satellites=json.dumps(
        {"assist_satellite.a_voice": "Garage", STUDY_DEVICE: "Study", "not a key": "Nowhere"}))
    parsed = gw._get_satellite_room_map()
    assert parsed.entity_rooms == {"assist_satellite.a_voice": "Garage"}
    assert parsed.device_rooms == {STUDY_DEVICE: "Study"}


# ---------------------------------------------------------------------------
# (e) the owner scene fallback
# ---------------------------------------------------------------------------

FALLBACK_GROUPS = '{"Living Room": "light.lr_group", "Office": "light.office_group"}'


def run_fallback(monkeypatch, query, *, groups=FALLBACK_GROUPS):
    set_env(monkeypatch, groups=groups)
    rig = Rig(monkeypatch, registry=False)
    ha = MagicMock()
    ha.call_service = AsyncMock(return_value={"ok": True})
    answer = rig.in_scope(lambda: rig.controller._run_scene_fallback(query, ha, query), query=query)
    return answer, [c.args for c in ha.call_service.await_args_list], rig.gates


def test_movie_dims_the_configured_living_room_group(monkeypatch):
    answer, calls, gates = run_fallback(monkeypatch, "start movie mode")
    assert answer == "Movie mode ready! I've dimmed the living room lights."
    assert calls == [("light", "turn_on", {"entity_id": "light.lr_group", "brightness_pct": 20})]
    assert gates == [("light", "turn_on", ("light.lr_group",))]


def test_good_morning_turns_on_the_configured_office_group(monkeypatch):
    answer, calls, gates = run_fallback(monkeypatch, "good morning")
    assert answer == "Good morning! I've turned on the lights."
    assert calls == [("light", "turn_on", {"entity_id": "light.office_group", "brightness_pct": 100})]
    assert gates == [("light", "turn_on", ("light.office_group",))]


def test_home_turns_on_the_configured_living_room_group(monkeypatch):
    answer, calls, gates = run_fallback(monkeypatch, "i'm home")
    assert answer == "Welcome home! I've turned on the lights."
    assert calls == [("light", "turn_on", {"entity_id": "light.lr_group", "brightness_pct": 80})]
    assert gates == [("light", "turn_on", ("light.lr_group",))]


def test_with_no_groups_configured_no_branch_writes(monkeypatch):
    for query in ("start movie mode", "good morning", "i'm home"):
        answer, calls, gates = run_fallback(monkeypatch, query, groups=None)
        assert (answer, calls, gates) == (None, [], []), query


def test_a_missing_room_key_writes_nothing_for_that_branch(monkeypatch):
    answer, calls, gates = run_fallback(monkeypatch, "good morning", groups='{"Living Room": "light.lr_group"}')
    assert (answer, calls, gates) == (None, [], [])
