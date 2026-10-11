"""Room lights by Home Assistant area, the audience-scoped whole house, the
unavailable-light replies and the gate's leaf count.

Real HAEntityManager over a fictional house, a real area-registry snapshot, a
recording raw HA client behind the controller's own permission guard. Time is
the controller's injected monotonic; HA's states fetch is a counted fake.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import unittest.mock as mock
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

import orchestrator.smart_home_controller as shc  # noqa: E402
import shared.config  # noqa: E402
from orchestrator import ha_entity_manager as hem  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import room_directory as rd  # noqa: E402
from orchestrator import write_fanout  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402
from shared import ha_registry as hr  # noqa: E402

SATELLITE_DEVICE = "a" * 32


def _device(device_id, area=None, parent=None):
    return {"id": device_id, "area_id": area, "parent_device_id": parent}


def _reg(entity_id, *, area=None, device=None, **extra):
    row = {"entity_id": entity_id, "area_id": area, "device_id": device,
           "entity_category": None, "hidden_by": None, "disabled_by": None}
    row.update(extra)
    return row


def _state(entity_id, state="on", members=None, name=None):
    attrs = {"friendly_name": name or entity_id.split(".")[1].replace("_", " ").title()}
    if members is not None:
        attrs["entity_id"] = list(members)
    return {"entity_id": entity_id, "state": state, "attributes": attrs}


def house(*, attic_state="unavailable"):
    """(registry raw lists, states) for the fictional house."""
    areas = [{"area_id": a, "name": n, "aliases": []} for a, n in [
        ("area_galley", "Galley"), ("area_study", "Study"), ("area_hall", "Hall"), ("area_attic", "Attic"),
        ("area_north", "North Bathroom"), ("area_south", "South Bathroom"),
    ]]
    devices = [_device(SATELLITE_DEVICE, "area_study")]
    reg = [
        _reg("light.galley_bulb_1", area="area_galley"),
        _reg("light.galley_bulb_2", area="area_galley"),
        _reg("light.galley_relay_1", area="area_galley"),
        _reg("light.galley_led_ring", area="area_galley"),
        _reg("light.galley_hidden", area="area_galley", hidden_by="integration"),
        _reg("light.galley_config", area="area_galley", entity_category="config"),
        _reg("light.galley_pair", area="area_galley"),
        _reg("light.galley_all", area="area_galley"),
        _reg("fan.galley_fan", area="area_galley"),
        _reg("light.study_lamp", area="area_study"),
        _reg("light.study_voice_led", device=SATELLITE_DEVICE),
        _reg("assist_satellite.study_voice", device=SATELLITE_DEVICE),
        _reg("light.hall_lamp", area="area_hall"),
        _reg("light.hall_pair", area="area_hall"),
        _reg("light.attic_a", area="area_attic"),
        _reg("light.attic_b", area="area_attic"),
        _reg("light.north_lamp", area="area_north"),
        _reg("light.south_lamp", area="area_south"),
    ]
    states = {}
    for state in [
        _state("light.galley_bulb_1"), _state("light.galley_bulb_2"), _state("light.galley_relay_1"),
        _state("light.galley_led_ring"), _state("light.galley_hidden"), _state("light.galley_config"),
        _state("light.galley_pair", members=["light.galley_bulb_1", "light.galley_bulb_2"]),
        _state("light.galley_all", members=["light.galley_pair", "light.galley_relay_1"]),
        _state("light.study_lamp"), _state("light.study_voice_led"),
        _state("light.hall_lamp"),
        _state("light.hall_pair", members=["light.hall_lamp", "light.galley_bulb_2"]),
        _state("light.attic_a", attic_state), _state("light.attic_b", attic_state),
        _state("light.north_lamp"), _state("light.south_lamp"),
        _state("light.loose_lamp"),
    ]:
        states[state["entity_id"]] = state
    return {"areas": areas, "devices": devices, "entities": reg}, states


RELAY_AND_LED = '["(?:^|[._])(?:led_ring|status_led)(?:_|$)", "^light\\\\.galley_relay_1$"]'
GALLEY_LEAVES = {"light.galley_bulb_1", "light.galley_bulb_2", "light.galley_relay_1"}
NEVER_WRITTEN = {"light.galley_led_ring", "light.galley_hidden", "light.galley_config", "light.study_voice_led"}


class FakeRegistry:
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.state = "ok" if snapshot else "unavailable"

    def current(self):
        return self._snapshot


class Clock:
    now = 10_000.0

    def __call__(self):
        return self.now


class Cfg:
    ha_light_groups = ""
    ha_satellite_room_map = ""
    ha_whole_house_unassigned_lights = False
    ha_room_light_exclude_entities = ""


def fanout_cfg(threshold=6, hard_limit=18):
    cfg = MagicMock()
    cfg.ha_write_fanout_confirm_threshold = threshold
    cfg.ha_write_fanout_hard_limit = hard_limit
    return cfg


class Rig:
    def __init__(self, monkeypatch, *, registry=True, groups="", unassigned=False, exclude=None,
                 attic_state="unavailable", threshold=6, hard_limit=18, room_groups=None, states=None,
                 resolve_group=None):
        raw, state_map = house(attic_state=attic_state)
        if states is not None:
            state_map = states
        snapshot = hr.RegistrySnapshot.from_raw(raw, fetched_at=1.0) if registry else None
        self.registry = FakeRegistry(snapshot)
        _runtime.set_ha_registry(self.registry)
        self.cfg = Cfg()
        self.cfg.ha_light_groups = groups
        self.cfg.ha_whole_house_unassigned_lights = unassigned
        provider = rd.RoomDirectoryProvider(self.registry, None, config=lambda: self.cfg)
        _runtime.set_room_directory(provider)
        if exclude is not None:
            monkeypatch.setenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", exclude)
        shared.config._clear_cache_for_tests()
        hem._clear_room_light_exclude_cache()

        self.manager = hem.HAEntityManager("http://ha.invalid:8123", "token")
        self.manager._entities_cache = state_map
        self.manager._cache_time = datetime.now()
        self.manager._build_indexes()
        self.refreshes = 0
        self.refresh_error = None
        self.refresh_to = None

        async def refresh():
            self.refreshes += 1
            if self.refresh_error:
                raise self.refresh_error
            if self.refresh_to is not None:
                self.manager._entities_cache = self.refresh_to
                self.manager._build_indexes()
            self.manager._cache_time = datetime.now()
            return self.manager._entities_cache

        self.manager.refresh_entities = refresh
        self.manager._cache_duration = hem.timedelta(days=1)

        self.clock = Clock()
        monkeypatch.setattr(shc, "_monotonic", self.clock)
        monkeypatch.setattr(shc, "_light_groups_cache", None)
        monkeypatch.setattr(shc, "get_config", lambda: self.cfg)
        monkeypatch.setattr(write_fanout, "get_config", lambda: fanout_cfg(threshold, hard_limit))
        admin = MagicMock()
        admin.resolve_room_group = AsyncMock(return_value=resolve_group)
        admin.get_room_groups = AsyncMock(return_value=room_groups or [])
        monkeypatch.setattr(shc, "get_admin_client", lambda: admin)
        self.controller = shc.SmartHomeController(entity_manager=self.manager, llm_router=MagicMock())
        self.raw = MagicMock()
        self.raw.call_service = AsyncMock(return_value={"ok": True})
        self.gate_calls = []
        real_gate = write_fanout.gate

        def recording_gate(domain, service=None, entity_ids=None, *a, **kw):
            self.gate_calls.append(tuple(entity_ids or ()))
            return real_gate(domain, service, entity_ids, *a, **kw)

        monkeypatch.setattr(write_fanout, "gate", recording_gate)
        self.finalize_calls = []
        real_finalize = self.controller._finalize_light_targets

        async def spy_finalize(*a, **kw):
            self.finalize_calls.append(kw.get("ids"))
            return await real_finalize(*a, **kw)

        self.controller._finalize_light_targets = spy_finalize

    def run(self, intent, query, perms=None, mode="owner", trusted_voice=True):
        perms = perms or {"mode": "owner"}

        async def go():
            with mp.ha_permission_scope(perms, mode=mode, utterance=classify_utterance(query), trusted_voice=trusted_voice):
                return await self.controller.execute_intent(intent, self.raw, query)

        return asyncio.run(go())

    @property
    def written(self):
        out = []
        for call in self.raw.call_service.await_args_list:
            data = call.args[2] if len(call.args) > 2 else {}
            eid = (data or {}).get("entity_id")
            out.extend(eid if isinstance(eid, (list, tuple)) else [eid])
        return out


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    _runtime.reset_for_test()
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()
    yield
    _runtime.reset_for_test()
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()


def light(room, action="turn_on", **extra):
    intent = {"device_type": "light", "room": room, "action": action, "target_scope": "group", "parameters": {}}
    intent.update(extra)
    return intent


def guest(**overrides):
    perms = mp.apply_guest_baseline({"mode": "guest"})
    perms.update(overrides)
    return perms


# ---------------------------------------------------------------------------
# The area tier
# ---------------------------------------------------------------------------

def test_galley_writes_the_top_group_once_and_the_gate_counts_its_leaves(monkeypatch):
    rig = Rig(monkeypatch)
    answer = rig.run(light("galley"), "turn on the galley lights")
    assert rig.written == ["light.galley_all"]
    assert rig.gate_calls == [tuple(sorted(GALLEY_LEAVES))]
    assert answer == "Turned on the galley lights."


def test_leds_hidden_and_config_lights_are_never_written(monkeypatch):
    rig = Rig(monkeypatch)
    rig.run(light("galley", "set_color", parameters={"hs_colors": [[10, 10]]}, color_description="blue"),
            "make the galley lights blue")
    assert set(rig.written) == GALLEY_LEAVES
    assert not set(rig.written) & NEVER_WRITTEN


def test_a_satellites_led_on_its_device_is_not_written_by_its_rooms_command(monkeypatch):
    rig = Rig(monkeypatch)
    rig.run(light("study", "set_color", parameters={"hs_colors": [[10, 10]]}, color_description="blue"),
            "make the study lights blue")
    assert rig.written == ["light.study_lamp"]


def test_a_relay_excluded_by_pattern_inside_a_group_means_the_group_is_never_written_whole(monkeypatch):
    rig = Rig(monkeypatch, exclude=RELAY_AND_LED)
    rig.run(light("galley"), "turn on the galley lights")
    assert rig.written == ["light.galley_pair"], "the smaller group that holds only lamps"
    assert "light.galley_all" not in rig.written and "light.galley_relay_1" not in rig.written


def test_the_documented_exclusion_examples_match_fictional_ids():
    patterns = [
        r"(?:^|[._])(?:led_ring|status_led)(?:_|$)",
        r"^light\.[a-z_]+_relay_[0-9]+$",
        r"^light\.[a-z_]+_fan$",
    ]
    import re

    compiled = [re.compile(p) for p in patterns]
    for entity_id in ("light.galley_led_ring", "light.study_relay_2", "light.north_bathroom_fan"):
        assert any(p.search(entity_id) for p in compiled), entity_id
    for entity_id in ("light.galley_bulb_1", "light.study_lamp"):
        assert not any(p.search(entity_id) for p in compiled), entity_id


def test_a_configured_group_beats_the_area(monkeypatch):
    rig = Rig(monkeypatch, groups='{"galley": "light.hall_pair"}')
    rig.run(light("galley"), "turn on the galley lights")
    assert rig.written == ["light.hall_pair"]


def test_with_no_registry_the_anchored_name_tier_answers(monkeypatch):
    rig = Rig(monkeypatch, registry=False)
    rig.run(light("galley"), "turn on the galley lights")
    assert rig.written and all(w.startswith("light.galley") for w in rig.written)


def test_a_name_that_is_part_of_two_rooms_asks_which_and_writes_nothing(monkeypatch):
    states = {s["entity_id"]: s for s in [
        _state("light.north_bathroom_lamp"), _state("light.south_bathroom_lamp"),
    ]}
    rig = Rig(monkeypatch, registry=False, states=states)
    answer = rig.run(light("bathroom"), "turn on the bathroom lights")
    assert rig.written == []
    assert answer == "Which bathroom: north or south?"


def test_a_partial_name_asks_only_about_rooms_the_caller_may_use(monkeypatch):
    states = {s["entity_id"]: s for s in [
        _state("light.north_bathroom_lamp"), _state("light.south_bathroom_lamp"),
    ]}
    rig = Rig(monkeypatch, registry=False, states=states)
    perms = guest(restricted_entities=[r"^light\.south_bathroom"])
    answer = rig.run(light("bathroom"), "turn on the bathroom lights", perms, "guest")
    assert "south" not in answer.lower(), "a room the caller can't use is never offered"
    assert set(rig.written) == {"light.north_bathroom_lamp"}, "one permitted room: no question"


def test_a_partial_name_with_every_candidate_denied_gets_the_refusal(monkeypatch):
    states = {s["entity_id"]: s for s in [
        _state("light.north_bathroom_lamp"), _state("light.south_bathroom_lamp"),
    ]}
    rig = Rig(monkeypatch, registry=False, states=states)
    perms = guest(restricted_entities=[r"^light\.(north|south)_bathroom"])
    answer = rig.run(light("bathroom"), "turn on the bathroom lights", perms, "guest")
    assert rig.written == [] and "north" not in answer.lower() and "south" not in answer.lower()
    assert "guest mode" in answer.lower()


# ---------------------------------------------------------------------------
# The whole house, by audience
# ---------------------------------------------------------------------------

def test_the_whole_house_goes_through_the_finalizer(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    rig.run(light("whole_house"), "turn on all the lights")
    assert len(rig.finalize_calls) == 1 and rig.finalize_calls[0]


def test_the_owners_whole_house_is_every_areas_lamps_each_once_and_no_indicator(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on", exclude=RELAY_AND_LED)
    answer = rig.run(light("whole_house"), "turn on all the lights")
    written = rig.written
    assert len(written) == len(set(written)), "no bulb twice"
    expected = {"light.galley_bulb_1", "light.galley_bulb_2", "light.study_lamp", "light.hall_lamp",
                "light.attic_a", "light.attic_b", "light.north_lamp", "light.south_lamp"}
    assert set(written) == expected
    assert not set(written) & (NEVER_WRITTEN | {"light.galley_relay_1", "light.loose_lamp"})
    assert answer == "Turned on all the lights."


def test_all_the_lights_except_a_room_leaves_that_room_alone_and_says_so(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    answer = rig.run(light("whole_house", "turn_off", excluded_rooms=["galley"]), "turn off all the lights except the galley")
    assert not set(rig.written) & GALLEY_LEAVES
    assert "light.study_lamp" in rig.written
    assert answer == "Turned off all the lights except the galley."


def test_an_owner_the_gateway_did_not_vouch_for_gets_the_light_groups_only(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on", groups='{"study": "light.study_lamp"}',
              exclude='["(?:^|[._])(?:led_ring|status_led)(?:_|$)"]')
    rig.run(light("whole_house"), "turn off all the lights", trusted_voice=False)
    assert "light.north_lamp" not in rig.written and "light.attic_a" not in rig.written
    assert rig.written, "the groups' leaves still go"


def test_the_unassigned_flag_is_owner_only(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on", unassigned=True, groups='{"study": "light.study_lamp"}')
    rig.run(light("whole_house"), "turn on all the lights", guest(allowed_intents=["control"]), "guest")
    assert "light.loose_lamp" not in rig.written


def test_an_unassigned_light_is_left_out_unless_the_flag_is_set(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    rig.run(light("whole_house"), "turn on all the lights")
    assert "light.loose_lamp" not in rig.written
    flagged = Rig(monkeypatch, attic_state="on", unassigned=True)
    flagged.run(light("whole_house"), "turn on all the lights")
    assert "light.loose_lamp" in flagged.written


def test_with_no_registry_the_owners_whole_house_is_the_leaves_of_the_light_groups(monkeypatch):
    rig = Rig(monkeypatch, registry=False, exclude='["^light\\\\.galley_relay_1$", "led_ring"]')
    rig.run(light("whole_house"), "turn on all the lights")
    assert set(rig.written) == {"light.galley_bulb_1", "light.galley_bulb_2", "light.hall_lamp"}


def test_a_guests_all_the_lights_writes_nothing_outside_the_configured_groups(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on", groups='{"study": "light.study_lamp"}')
    perms = guest(allowed_intents=["control"])
    rig.run(light("whole_house"), "turn on all the lights", perms, "guest")
    assert rig.written == ["light.study_lamp"], "not the galley, hall, bathrooms or the loose lamp"


def test_a_guests_all_the_lights_with_nothing_configured_asks_which_room(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    answer = rig.run(light("whole_house"), "turn on all the lights", guest(allowed_intents=["control"]), "guest")
    assert rig.written == [] and answer == "Which room?"


def test_a_guests_whole_house_reaches_admin_room_group_member_rooms(monkeypatch):
    groups = [{"name": "upper", "display_name": "Upper", "aliases": [], "members": [{"room_name": "study"}]}]
    rig = Rig(monkeypatch, attic_state="on", room_groups=groups)
    rig.run(light("whole_house"), "turn on all the lights", guest(allowed_intents=["control"]), "guest")
    assert rig.written == ["light.study_lamp"]


def test_a_restricted_bulb_inside_a_guests_configured_group_degrades_the_group(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on", groups='{"galley": "light.galley_all"}', exclude="[]")
    perms = guest(allowed_intents=["control"], restricted_entities=[r"^light\.galley_relay_1$"])
    answer = rig.run(light("whole_house"), "turn on all the lights", perms, "guest")
    assert set(rig.written) == {"light.galley_bulb_1", "light.galley_bulb_2"}
    assert "light.galley_all" not in rig.written and "light.galley_relay_1" not in rig.written
    assert "did part of that" in answer.lower()


@pytest.mark.parametrize("leaves", [0, 1, 25])
def test_an_explicit_all_the_lights_asks_for_no_confirmation_at_any_count(monkeypatch, leaves):
    states = {}
    areas = [{"area_id": "area_big", "name": "Big", "aliases": []}]
    reg = [_reg(f"light.big_{i}", area="area_big") for i in range(leaves)]
    for i in range(leaves):
        states[f"light.big_{i}"] = _state(f"light.big_{i}")
    rig = Rig(monkeypatch, states=states)
    rig.registry._snapshot = hr.RegistrySnapshot.from_raw(
        {"areas": areas, "devices": [], "entities": reg}, fetched_at=1.0,
    )
    answer = rig.run(light("whole_house"), "turn on all the lights")
    if leaves:
        assert len(rig.written) == leaves, answer
    else:
        assert rig.written == [] and "couldn't find any lights" in answer


def test_a_guests_cue_does_not_bypass_the_permission_degrade(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on", groups='{"galley": "light.galley_all"}', exclude="[]")
    perms = guest(allowed_intents=["control"], restricted_entities=[r"^light\.galley_bulb_1$"])
    rig.run(light("whole_house"), "turn on every light in the house", perms, "guest")
    assert "light.galley_bulb_1" not in rig.written


# ---------------------------------------------------------------------------
# The gate counts leaves
# ---------------------------------------------------------------------------

def _nested_states():
    states = {}
    for i in range(1, 8):
        states[f"light.n_bulb_{i}"] = _state(f"light.n_bulb_{i}")
    states["light.n_a"] = _state("light.n_a", members=["light.n_bulb_1", "light.n_bulb_2", "light.n_bulb_3"])
    states["light.n_b"] = _state("light.n_b", members=["light.n_bulb_4", "light.n_bulb_5"])
    states["light.n_c"] = _state("light.n_c", members=["light.n_bulb_6", "light.n_bulb_7"])
    states["light.n_top"] = _state("light.n_top", members=["light.n_a", "light.n_b", "light.n_c"])
    return states


def test_whole_house_gate_counts_leaves_not_direct_members(monkeypatch):
    rig = Rig(monkeypatch, registry=False, states=_nested_states(), exclude="[]")
    rig.run(light("whole_house"), "turn on all the lights")
    assert rig.gate_calls == [tuple(f"light.n_bulb_{i}" for i in range(1, 8))], "7 leaves, not 3 direct members"


def test_the_room_path_over_the_threshold_confirms_on_the_leaf_count(monkeypatch):
    rig = Rig(monkeypatch, registry=False, states=_nested_states(), exclude="[]",
              threshold=5, groups='{"nested": "light.n_top"}')
    answer = rig.run(light("nested"), "please")
    assert rig.written == [], "7 leaves is over a threshold of 5; 3 direct members would not be"
    assert "say:" in answer or "?" in answer


# ---------------------------------------------------------------------------
# Unavailable lights
# ---------------------------------------------------------------------------

def test_all_unavailable_lights_write_nothing_force_one_refresh_and_say_so(monkeypatch):
    rig = Rig(monkeypatch)
    answer = rig.run(light("attic"), "turn on the attic lights")
    assert rig.written == [] and rig.refreshes == 1
    assert answer == "The attic lights aren't responding right now."


def test_a_second_command_within_a_minute_does_not_force_another_refresh(monkeypatch):
    rig = Rig(monkeypatch)
    rig.run(light("attic"), "turn on the attic lights")
    rig.clock.now += 30
    answer = rig.run(light("attic"), "turn on the attic lights")
    assert rig.refreshes == 1 and answer == "The attic lights aren't responding right now."
    rig.clock.now += 31
    rig.run(light("attic"), "turn on the attic lights")
    assert rig.refreshes == 2


def test_a_refresh_that_brings_the_lights_back_lets_the_command_through(monkeypatch):
    rig = Rig(monkeypatch)
    raw, states = house(attic_state="on")
    rig.refresh_to = states
    answer = rig.run(light("attic"), "turn on the attic lights")
    assert rig.refreshes == 1 and sorted(rig.written) == ["light.attic_a", "light.attic_b"]
    assert answer == "Turned on the attic lights."


def test_a_refresh_that_raises_gives_the_same_honest_reply_and_no_exception(monkeypatch):
    rig = Rig(monkeypatch)
    rig.refresh_error = ConnectionError("down")
    answer = rig.run(light("attic"), "turn on the attic lights")
    assert rig.written == [] and answer == "The attic lights aren't responding right now."


def test_some_unavailable_lights_are_written_and_counted(monkeypatch):
    rig = Rig(monkeypatch)
    rig.manager._entities_cache["light.north_lamp"]["state"] = "unavailable"
    rig.manager._build_indexes()
    answer = rig.run(light("multi_room", rooms=["north_bathroom", "study"]), "turn on the north bathroom and the study lights")
    assert sorted(rig.written) == ["light.north_lamp", "light.study_lamp"]
    assert answer == "Turned on the north bathroom and study lights. 1 isn't responding."


def test_two_of_three_unavailable_says_how_many(monkeypatch):
    states = {s["entity_id"]: s for s in [
        _state("light.den_a", "unavailable"), _state("light.den_b", "unknown"), _state("light.den_c", "on"),
    ]}
    rig = Rig(monkeypatch, registry=False, states=states)
    answer = rig.run(light("den", "set_color", parameters={"hs_colors": [[10, 10]]}, color_description="blue"),
                     "make the den lights blue")
    assert answer == "Set the den lights to blue. 2 aren't responding."
    assert len(rig.written) == 3


def test_a_light_absent_from_the_states_counts_as_unavailable(monkeypatch):
    states = {"light.den_a": _state("light.den_a")}
    rig = Rig(monkeypatch, registry=False, states=states)
    rig.manager._entities_cache.pop("light.den_a")
    rig.manager._build_indexes()
    answer = rig.run(light("den"), "turn on the den lights")
    assert rig.written == [] and "couldn't find any lights" in answer


def test_the_whole_house_all_unavailable_is_reported(monkeypatch):
    states = {s["entity_id"]: s for s in [_state("light.den_a", "unavailable"), _state("light.den_b", "unavailable")]}
    rig = Rig(monkeypatch, registry=False, states=states, exclude="[]")
    rig.manager._entities_cache["light.den_all"] = _state("light.den_all", "unavailable", ["light.den_a", "light.den_b"])
    rig.manager._build_indexes()
    answer = rig.run(light("whole_house"), "turn on all the lights")
    assert rig.written == [] and answer == "None of the lights are responding right now."


def test_multi_room_with_one_dead_room_writes_the_live_one_and_counts_the_dead(monkeypatch):
    rig = Rig(monkeypatch)
    answer = rig.run(light("multi_room", rooms=["attic", "study"]), "turn on the attic and the study lights")
    assert "light.study_lamp" in rig.written
    assert answer == "Turned on the attic and study lights. 2 aren't responding."


def test_a_room_with_no_lamps_says_so(monkeypatch):
    rig = Rig(monkeypatch)
    answer = rig.run(light("porch"), "turn on the porch lights")
    assert answer == "I couldn't find any lights in the porch."


# ---------------------------------------------------------------------------
# Every writer finalizes
# ---------------------------------------------------------------------------

WRITERS = ("_dispatch_light_or_room_command", "_execute_multi_room_command",
           "_execute_room_group_command", "_execute_whole_house_command")


def test_every_writer_calls_finalizer(monkeypatch):
    group = {"display_name": "Upper", "name": "upper", "members": [{"room_name": "study"}, {"room_name": "hall"}]}
    rig = Rig(monkeypatch, attic_state="on", resolve_group=group)
    gates = []
    for intent, query in [
        (light("study"), "turn on the study lights"),
        (light("multi_room", rooms=["study", "hall"]), "turn on the study and hall lights"),
        (light("whole_house"), "turn on all the lights"),
        (light("upper"), "turn on the upper lights"),
    ]:
        before = len(rig.gate_calls)
        rig.run(intent, query)
        gates.append(len(rig.gate_calls) - before)
    assert gates == [1, 1, 1, 1], "each of the four writers finalized and reached the gate once"
    assert len(rig.finalize_calls) >= 4


def test_every_writer_has_a_finalizer_call_before_its_service_calls():
    tree = ast.parse((SRC / "orchestrator" / "smart_home_controller.py").read_text())
    methods = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for name in WRITERS:
        method = methods[name]
        finalize_lines = [n.lineno for n in ast.walk(method)
                          if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_finalize_light_targets"]
        write_lines = [n.lineno for n in ast.walk(method)
                       if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "call_service"]
        assert finalize_lines, f"{name} never finalizes"
        assert min(finalize_lines) < min(write_lines), f"{name} writes before it finalizes"


def test_a_planted_writer_without_a_finalizer_is_caught_by_the_same_check():
    planted = ast.parse("async def w(self, c):\n    await c.call_service('light', 'turn_on', {})\n")
    method = planted.body[0]
    finalize_lines = [n.lineno for n in ast.walk(method)
                      if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "_finalize_light_targets"]
    assert not finalize_lines


# ---------------------------------------------------------------------------
# The gate's scope cue reads room keys as they are spoken
# ---------------------------------------------------------------------------

def test_a_multi_room_command_that_names_its_rooms_is_an_explicit_scope(monkeypatch):
    states = {s["entity_id"]: s for s in [_state(f"light.den_{i}") for i in range(4)] + [_state(f"light.living_room_{i}") for i in range(4)]}
    rig = Rig(monkeypatch, registry=False, states=states, threshold=3, exclude="[]")
    answer = rig.run(light("multi_room", rooms=["den", "living_room"]), "the den and living room lights please")
    assert len(rig.written) == 8, answer


def test_a_multi_room_command_that_does_not_name_two_rooms_is_gated(monkeypatch):
    states = {s["entity_id"]: s for s in [_state(f"light.den_{i}") for i in range(4)] + [_state(f"light.living_room_{i}") for i in range(4)]}
    rig = Rig(monkeypatch, registry=False, states=states, threshold=3, exclude="[]")
    rig.run(light("multi_room", rooms=["den", "living_room"]), "please")
    assert rig.written == []


# "All the lights" writes, pinned per caller (fictional house, attic lights on)
UNTRUSTED_OWNER_WRITES = {"light.galley_bulb_1", "light.galley_bulb_2", "light.galley_relay_1", "light.hall_lamp"}
TRUSTED_OWNER_WRITES = UNTRUSTED_OWNER_WRITES | {
    "light.attic_a", "light.attic_b", "light.north_lamp", "light.south_lamp", "light.study_lamp",
}


def _all_the_lights(monkeypatch, *, perms=None, mode="owner", trusted=True, **rig_kw):
    rig = Rig(monkeypatch, attic_state="on", **rig_kw)
    rig.run(light("whole_house"), "turn on all the lights", perms, mode, trusted)
    return set(rig.written)


def test_all_the_lights_writes_exactly_these_ids_for_each_kind_of_caller(monkeypatch):
    assert _all_the_lights(monkeypatch, trusted=False) == UNTRUSTED_OWNER_WRITES, \
        "owner mode the gateway didn't vouch for: the leaves of every Home Assistant light group"
    assert _all_the_lights(monkeypatch, trusted=True) == TRUSTED_OWNER_WRITES, "trusted: every area's lamps"
    assert _all_the_lights(
        monkeypatch, perms=guest(allowed_intents=["control"]), mode="guest", trusted=True,
        groups='{"study": "light.study_lamp"}',
    ) == {"light.study_lamp"}, "guest: only the configured groups, trusted mark or not"


def test_unassigned_lights_join_only_a_trusted_owner(monkeypatch):
    assert _all_the_lights(monkeypatch, trusted=False, unassigned=True) == UNTRUSTED_OWNER_WRITES
    assert _all_the_lights(monkeypatch, trusted=True, unassigned=True) == TRUSTED_OWNER_WRITES | {"light.loose_lamp"}
    assert "light.loose_lamp" not in _all_the_lights(
        monkeypatch, perms=guest(allowed_intents=["control"]), mode="guest", trusted=True, unassigned=True,
        groups='{"study": "light.study_lamp"}')
