"""P2: the orchestrator reads location, lamp exclusions and the bed warmer
through shared.household (admin value, then env, then today's default)."""
from __future__ import annotations

import asyncio
import io
import re

import pytest
from unittest import mock

import shared.config
from shared import household
from orchestrator import ha_entity_manager as hem
from orchestrator import mode_permission as mp
from orchestrator import room_probe
from orchestrator import semantic_cache
from orchestrator.utils import constants
from shared.base_knowledge_utils import extract_home_address
from shared.knowledge_tiers import KnowledgeAudience

from .test_smart_home_controller_permissions import _controller, _guest_perms, _raw_ha_client
from . import _room_chat_harness as rch

OWNER = KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=False, owner_proven=False)
LED_RING = "light.kitchen_satellite_led_ring"
BED = {
    "level_left": "select.bed_left", "level_right": "select.bed_right", "power_main": "switch.bed_main",
    "power_side_a": "switch.bed_a", "power_side_b": "switch.bed_b",
}


@pytest.fixture(autouse=True)
def _house(monkeypatch):
    monkeypatch.setenv("DEFAULT_CITY", "Denver")
    monkeypatch.setenv("DEFAULT_STATE", "CO")
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    monkeypatch.delenv("HA_BED_WARMER_ENTITIES", raising=False)
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()
    yield
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()


def _matches(entity_id):
    return any(p.search(entity_id) for p in hem._room_light_exclude_patterns())


# -- location ---------------------------------------------------------------------------------

def test_a_matching_city_blob_keys_the_cache_by_the_admin_city():
    household.install_for_test(sources={"location": {"city": "Springfield", "state": "IL", "text": "Springfield, IL"}})
    assert constants.default_city() == "Springfield"
    assert constants.default_location(OWNER) == "Springfield, IL"
    assert semantic_cache.normalize_location("what's the weather") == "default_springfield"
    assert semantic_cache.normalize_location("weather in springfield") == "default_springfield"


def test_a_text_only_row_keeps_the_env_city_keyword_and_uses_the_text_as_location():
    household.install_for_test(sources={"location": {"text": "123 Main St, Springfield, IL"}})
    assert constants.default_city() == "Denver"
    assert constants.default_location(OWNER) == "123 Main St, Springfield, IL"
    assert semantic_cache.normalize_location("what's the weather") == "default_denver"


def test_admin_unreachable_since_boot_serves_the_env_city():
    async def down():
        raise ConnectionError("admin down")

    household.install_for_test(fetch=down)
    asyncio.run(household.reader().current())
    assert household.reader().peek().loaded is False
    assert semantic_cache.normalize_location("what's the weather") == "default_denver"
    assert constants.default_location(OWNER) == "Denver, CO"
    assert household.location().source == "env"


def test_nothing_set_anywhere_gives_the_generic_key(monkeypatch):
    monkeypatch.delenv("DEFAULT_CITY")
    monkeypatch.delenv("DEFAULT_STATE")
    shared.config._clear_cache_for_tests()
    assert constants.default_city() == ""
    assert constants.default_location(OWNER) == ""
    assert semantic_cache.normalize_location("what's the weather") == "default_location"


def test_a_location_change_is_picked_up_without_a_restart():
    household.install_for_test(sources={"location": {"city": "Springfield", "state": "IL", "text": "Springfield, IL"}})
    assert semantic_cache.normalize_location("what's the weather") == "default_springfield"
    household.install_for_test(sources={"location": {"city": "Boulder", "state": "CO", "text": "Boulder, CO"}})
    assert semantic_cache.normalize_location("what's the weather") == "default_boulder"


def test_base_knowledge_home_address_falls_back_to_the_city_level_household_location():
    household.install_for_test(sources={"location": {"text": "12 Elm Rd, Springfield, IL"}})
    assert extract_home_address([]) == "Denver, CO"
    assert extract_home_address([{"category": "misc", "key": "x", "value": "y"}]) == "Denver, CO"
    assert extract_home_address([{"category": "property", "key": "address", "value": "1 Row"}]) == "1 Row"


def test_the_location_constants_are_gone():
    for name in ("DEFAULT_CITY", "DEFAULT_STATE", "DEFAULT_LOCATION"):
        assert not hasattr(constants, name)


# -- lamp exclusions --------------------------------------------------------------------------

def test_the_built_in_led_rule_applies_when_nothing_is_stored():
    assert _matches(LED_RING) and not _matches("light.kitchen")


def test_admin_globs_add_to_the_built_in_rule():
    household.install_for_test(sources={"lights": {"room_light_exclude": ["light.porch_*"]}})
    assert _matches(LED_RING)
    assert _matches("light.porch_main")
    assert not _matches("light.kitchen")


def test_an_explicitly_empty_admin_list_keeps_the_built_in_rule():
    household.install_for_test(sources={"lights": {"room_light_exclude": []}})
    assert _matches(LED_RING)


def test_env_empty_list_still_opts_out_while_admin_is_unset(monkeypatch):
    monkeypatch.setenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", "[]")
    shared.config._clear_cache_for_tests()
    assert hem._room_light_exclude_patterns() == []
    household.install_for_test(sources={"lights": {"room_light_exclude": ["light.porch_*"]}})
    assert _matches(LED_RING) and _matches("light.porch_main")


def test_compiled_patterns_are_cached_on_the_sources_and_rotate_on_change():
    household.install_for_test(sources={"lights": {"room_light_exclude": ["light.porch_*"]}})
    first = hem._room_light_exclude_patterns()
    assert hem._room_light_exclude_patterns() is first
    household.install_for_test(sources={"lights": {"room_light_exclude": ["light.porch_*", "light.den_lamp"]}})
    second = hem._room_light_exclude_patterns()
    assert second is not first and any(p.search("light.den_lamp") for p in second)


def _probe_study_line(globs):
    calls = []

    async def fetch_household():
        calls.append(1)
        value = {"room_light_exclude": globs} if globs is not None else {"room_light_exclude": None}
        return {"lights": {"value": value, "revision": 1}}

    household.install_for_test(fetch=fetch_household)
    raw, states = rch.house_raw_and_states()

    async def fetch():
        return raw

    async def read_states():
        return states

    out = io.StringIO()
    code = asyncio.run(room_probe._run_with_household(
        fetch, read_states, runs=1, shape=False, show_names=True, utterances=[], out=out))
    assert code == 0 and calls == [1]
    return next(line for line in out.getvalue().splitlines() if line.startswith("  study:"))


def test_the_probe_loads_the_household_settings_before_it_resolves_exclusions():
    assert "lamp targets 3" in _probe_study_line(None)
    assert "lamp targets 0" in _probe_study_line(["light.study_*"])


# -- bed warmer -------------------------------------------------------------------------------

def test_the_bed_warmer_resolves_as_one_unit():
    household.install_for_test(sources={"bed_warmer": dict(BED)})
    resolved = household.resolved_bed_warmer()
    assert resolved.source == "admin" and resolved.entities == BED


def test_a_repointed_bed_warmer_id_is_still_refused_for_a_guest_whose_restricted_list_names_it():
    repointed = dict(BED, power_main="switch.spare_heater_main")
    household.install_for_test(sources={"bed_warmer": repointed})
    controller, ha_client = _controller(), _raw_ha_client()
    perms = _guest_perms(restricted_entities=[r"^switch\.spare_heater"])

    async def drive():
        with mp.ha_permission_scope(perms, mode="guest") as scope:
            await controller._handle_bed_warmer_intent("turn_off", {}, ha_client)
            return list(scope.denials)

    denials = asyncio.run(drive())
    assert len(denials) >= 1
    assert all(c.args[2].get("entity_id") != "switch.spare_heater_main" for c in ha_client.call_service.await_args_list)


def test_the_same_repoint_is_written_for_an_owner(monkeypatch):
    repointed = dict(BED, power_main="switch.spare_heater_main")
    household.install_for_test(sources={"bed_warmer": repointed})
    controller, ha_client = _controller(), _raw_ha_client()

    async def drive():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            await controller._handle_bed_warmer_intent("turn_off", {}, ha_client)

    asyncio.run(drive())
    ha_client.call_service.assert_awaited_once_with("switch", "turn_off", {"entity_id": "switch.spare_heater_main"})
