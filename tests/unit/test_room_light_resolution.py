"""Room name -> light entities (ATHENA-205, phase 1).

Drives the real HAEntityManager over a seeded entity cache. A room resolves
to the smallest set of ids that covers its lights, no physical bulb sits under
two returned ids, and a light that only *mentions* the room (another room's
name, a satellite's LED ring) is not included.

``_oracle_leaves`` is deliberately its own recursion over the fixture's group
table: it must not import the implementation it checks.
"""
from __future__ import annotations

import sys
import unicodedata
from datetime import datetime

import pytest
import structlog.testing

sys.path.insert(0, "src")

import shared.config  # noqa: E402
from orchestrator import ha_entity_manager as hem  # noqa: E402

# --- fixture ---------------------------------------------------------------

GROUPS = {
    "light.galley": [f"light.galley_{i}" for i in range(1, 7)],
    "light.galley_main": [f"light.galley_{i}" for i in range(1, 7)],
    "light.galley_plus": ["light.galley_main", "light.accent_galley_strip"],
    "light.tower_3": ["light.tower_2"],
    "light.tower_2": ["light.tower_1"],
    "light.tower_1": ["light.tower_bulb_1", "light.tower_bulb_2", "light.tower_bulb_3",
                      "light.tower_status_led"],
    "light.loft_left": ["light.loft_bulb_1", "light.loft_bulb_2", "light.loft_bulb_3"],
    "light.loft_right": ["light.loft_bulb_3", "light.loft_bulb_4"],
    "light.pantry": ["light.pantry_bulb", "light.pantry_missing"],
    "light.loop_a": ["light.loop_b"],
    "light.loop_b": ["light.loop_a"],
    "light.hollow": [],
}

NFD_CAFE = unicodedata.normalize("NFD", "Café Pendant")
NFC_CAFE = unicodedata.normalize("NFC", "café")

PLAIN_LIGHTS = {
    **{f"light.galley_{i}": None for i in range(1, 7)},
    "light.accent_galley_strip": "Accent Galley Strip",
    "light.under_cabinet_galley": "Under Cabinet Galley",
    "light.satellite_den_led_ring": "Satellite Galley LED Ring",
    **{f"light.tower_bulb_{i}": None for i in range(1, 4)},
    "light.tower_status_led": "Tower Status LED",
    "light.tower_reading_lamp": "Tower Reading Lamp",
    **{f"light.loft_bulb_{i}": None for i in range(1, 5)},
    "light.pantry_bulb": "Pantry Bulb",
    "light.ceiling_light_nook": "Ceiling Light Nook",
    "light.bathroom_vanity": "Bathroom Vanity",
    "light.master_bathroom_vanity": "Master Bathroom Vanity",
    "light.guest_bathroom_ceiling": "Guest Bathroom Ceiling",
    "light.bath_mirror": "Bath Mirror",
    "light.hall_sconce": "Hall Sconce",
    "light.hallway_ceiling": "Hallway Ceiling",
    "light.entrance_pendant": "Entrance Pendant",
    "light.porch_lantern": "Porch Lantern",
    "light.master_bedroom_lamp": "Master Bedroom Lamp",
    "light.primary_bedroom_lamp": "Primary Bedroom Lamp",
    "light.den_led_ring": "Den LED Ring",
    "light.hub_den_lamp": "Hub Den Lamp",
    "light.satellite_nursery_led_ring": "Satellite Nursery LED Ring",
    "light.bedroom_ceiling": "Bedroom Ceiling",
    "light.bed_reading_lamp": "Bed Reading Lamp",
    "light.office_desk": "Office Desk",
    "light.work_bench": "Work Bench",
    "light.studio_lamp": "Lamp",
    "light.cellar_bulb": "__no_friendly_name__",
    "light.living_room_floor": "Living Room Floor",
    "light.café_pendant": NFD_CAFE,
    "light.status_led_attic": "Status LED Attic",
    "light.attic_status_led": "Attic Status LED",
    "light.attic_bulb": "Attic Bulb",
}


def _entity(entity_id, friendly="", members=None):
    attrs = {}
    if friendly != "__no_friendly_name__":
        attrs["friendly_name"] = friendly or entity_id.split(".", 1)[1].replace("_", " ").title()
    if members is not None:
        attrs["entity_id"] = list(members)
    return {"entity_id": entity_id, "state": "on", "attributes": attrs}


def _build_entities():
    entities = {}
    for eid, friendly in PLAIN_LIGHTS.items():
        entities[eid] = _entity(eid, friendly or "")
    for gid, members in GROUPS.items():
        entities[gid] = _entity(gid, members=members)
    entities["switch.galley_fan"] = {"entity_id": "switch.galley_fan", "state": "on",
                                    "attributes": {"friendly_name": "Galley Fan"}}
    return entities


def _oracle_leaves(entity_id, groups=GROUPS):
    """Independent leaf expansion over the fixture's group table."""
    out, seen = set(), set()

    def walk(node):
        if node in seen:
            return
        seen.add(node)
        if node in groups:
            for child in groups[node]:
                walk(child)
        else:
            out.add(node)

    walk(entity_id)
    return out or {entity_id}


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch):
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()
    yield
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()


@pytest.fixture
def manager():
    m = hem.HAEntityManager("http://ha.invalid:8123", "token")
    m._entities_cache = _build_entities()
    m._cache_time = datetime.now()
    m._build_indexes()
    return m


async def _ids(manager, room):
    return [m["entity_id"] for m in await manager.find_lights_by_room(room)]


GALLEY = {"light.galley_plus"}
BATHROOM = {"light.bathroom_vanity", "light.bath_mirror"}
HALL = {"light.hall_sconce", "light.hallway_ceiling"}
LIVING = {"light.living_room_floor"}

ROWS = [
    ("galley", GALLEY),
    ("tower", {"light.tower_3", "light.tower_reading_lamp"}),
    ("loft", {"light.loft_left", "light.loft_bulb_4"}),
    ("pantry", {"light.pantry"}),
    ("bathroom", BATHROOM),
    ("bath", BATHROOM),
    ("master bathroom", {"light.master_bathroom_vanity"}),
    ("master bedroom", {"light.master_bedroom_lamp", "light.primary_bedroom_lamp"}),
    ("primary bedroom", {"light.master_bedroom_lamp", "light.primary_bedroom_lamp"}),
    # said word is a synonym value, not a key prefix: reverse lookup reaches the key
    ("restroom", BATHROOM),
    ("study", {"light.office_desk"}),
    ("lounge", LIVING),
    ("corridor", HALL),
    # one part's synonyms and fallback decision never leak into another part
    ("hall and nook", HALL | {"light.ceiling_light_nook"}),
    ("nook and hall", HALL | {"light.ceiling_light_nook"}),
    # only anchored hit is an excluded LED: no fallback to the anywhere hits
    ("den", set()),
    # only an anywhere hit, and it is an excluded LED
    ("nursery", set()),
    ("hollow", {"light.hollow"}),
    ("bedroom", {"light.bedroom_ceiling"}),
    ("office", {"light.office_desk"}),
    ("hall", HALL),
    ("hallway", HALL),
    ("front", {"light.entrance_pendant"}),
    ("outside", {"light.porch_lantern"}),
    ("living_room", LIVING),
    ("living room", LIVING),
    ("galley lights", GALLEY),
    ("the galley lights", GALLEY),
    ("the studio lamps", {"light.studio_lamp"}),
    ("cellar", {"light.cellar_bulb"}),
    ("café", {"light.café_pendant"}),
    (NFC_CAFE, {"light.café_pendant"}),
    (unicodedata.normalize("NFD", "café"), {"light.café_pendant"}),
    ("nook", {"light.ceiling_light_nook"}),
    ("galley and nook", GALLEY | {"light.ceiling_light_nook"}),
    ("galley and attic", GALLEY | {"light.attic_bulb"}),
    ("attic", {"light.attic_bulb"}),
    ("loop", {"light.loop_a"}),
    ("nowhere", set()),
    ("", set()),
    ("lights", set()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("room,expected", ROWS, ids=[repr(r) for r, _ in ROWS])
async def test_room_resolves_to_expected_cover(manager, room, expected):
    ids = await _ids(manager, room)
    assert set(ids) == expected
    assert len(ids) == len(set(ids))
    assert ids == await _ids(manager, room), "order must be deterministic"


@pytest.mark.asyncio
@pytest.mark.parametrize("room,_expected", ROWS, ids=[repr(r) for r, _ in ROWS])
async def test_no_physical_bulb_sits_under_two_returned_ids(manager, room, _expected):
    ids = await _ids(manager, room)
    leaves = [leaf for i in ids for leaf in _oracle_leaves(i)]
    assert len(leaves) == len(set(leaves))


@pytest.mark.asyncio
async def test_hallway_and_hall_agree_and_exclude_entrance(manager):
    assert await _ids(manager, "hall") == await _ids(manager, "hallway")
    assert "light.entrance_pendant" not in await _ids(manager, "hall")


@pytest.mark.asyncio
async def test_light_that_only_mentions_the_room_is_excluded(manager):
    ids = set(await _ids(manager, "galley"))
    assert "light.satellite_den_led_ring" not in ids
    assert "light.under_cabinet_galley" not in ids
    assert "light.master_bathroom_vanity" not in set(await _ids(manager, "master bedroom"))


@pytest.mark.asyncio
async def test_returned_dicts_keep_the_five_key_shape(manager):
    for room in ("galley", "loft", "pantry"):
        for match in await manager.find_lights_by_room(room):
            assert set(match) == {"entity_id", "friendly_name", "members", "state", "type"}
    (galley,) = await manager.find_lights_by_room("galley")
    assert galley["type"] == "group" and galley["members"] == GROUPS["light.galley_plus"]
    split = {m["entity_id"]: m for m in await manager.find_lights_by_room("loft")}
    assert split["light.loft_bulb_4"]["type"] == "individual"
    assert split["light.loft_bulb_4"]["members"] == []


# --- exclusion ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,room,expected",
    [
        ("[]", "attic", {"light.attic_bulb", "light.attic_status_led"}),
        (r'["^light\\.galley_plus$"]', "galley", {"light.galley"}),
        (r'["galley_3$"]', "galley", GALLEY),
        ('["unterminated', "attic", {"light.attic_bulb"}),
        ('{"a":1}', "attic", {"light.attic_bulb"}),
        ('["(unclosed", "^light\\\\.attic_bulb$"]', "attic", {"light.attic_status_led"}),
    ],
    ids=["opt-out", "excluded-group", "member-only-pattern", "malformed-json",
         "wrong-shape", "one-invalid-regex"],
)
async def test_exclusion_config(manager, monkeypatch, raw, room, expected):
    monkeypatch.setenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raw)
    shared.config._clear_cache_for_tests()
    assert set(await _ids(manager, room)) == expected


@pytest.mark.asyncio
async def test_default_exclusion_never_filters_a_member_of_a_candidate_group(manager):
    assert set(await _ids(manager, "tower")) == {"light.tower_3", "light.tower_reading_lamp"}
    leaves = hem.expand_light_leaves(["light.tower_3"], manager._entities_cache)
    assert "light.tower_status_led" in leaves["light.tower_3"]


@pytest.mark.asyncio
async def test_invalid_pattern_warning_carries_only_its_index(manager, monkeypatch):
    monkeypatch.setenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", '["ok", "(bad-secret-pattern"]')
    shared.config._clear_cache_for_tests()
    with structlog.testing.capture_logs() as logs:
        await _ids(manager, "attic")
    warnings = [e for e in logs if e["event"] == "room_light_exclude_pattern_invalid"]
    assert warnings == [{"event": "room_light_exclude_pattern_invalid", "index": 1,
                         "log_level": "warning"}]


@pytest.mark.asyncio
async def test_exclusion_patterns_are_compiled_once_per_config_value(manager, monkeypatch):
    first = hem._room_light_exclude_patterns()
    assert hem._room_light_exclude_patterns() is first
    monkeypatch.setenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", "[]")
    shared.config._clear_cache_for_tests()
    assert hem._room_light_exclude_patterns() == []


@pytest.mark.asyncio
async def test_resolution_log_carries_counts_only(manager):
    with structlog.testing.capture_logs() as logs:
        await _ids(manager, "attic")
    (entry,) = [e for e in logs if e["event"] == "room_lights_resolved"]
    assert set(entry) == {"event", "log_level", "source", "candidates", "excluded", "targets"}
    assert entry["excluded"] == 1 and entry["source"] == "name_anchored"


# --- expand_light_leaves / cover_light_targets --------------------------------


def test_expand_cycle_is_its_own_leaf():
    out = hem.expand_light_leaves(["light.loop_a", "light.loop_b"], _build_entities())
    assert out == {"light.loop_a": frozenset({"light.loop_a"}),
                   "light.loop_b": frozenset({"light.loop_b"})}


def test_expand_is_depth_unbounded():
    out = hem.expand_light_leaves(["light.tower_3"], _build_entities())
    assert out["light.tower_3"] == {
        "light.tower_bulb_1", "light.tower_bulb_2", "light.tower_bulb_3", "light.tower_status_led"}


def test_expand_absent_member_is_a_leaf():
    out = hem.expand_light_leaves(["light.pantry", "light.never_seen"], _build_entities())
    assert out["light.pantry"] == {"light.pantry_bulb", "light.pantry_missing"}
    assert out["light.never_seen"] == {"light.never_seen"}


def test_expand_extra_members_only_when_entity_map_has_no_list():
    entities = _build_entities()
    extra = {
        "light.pantry": ["light.should_be_ignored"],
        "light.match_only": ["light.m1", "light.m2"],
        "light.galley_1": ["light.also_ignored_never"],
    }
    out = hem.expand_light_leaves(["light.pantry", "light.match_only"], entities, extra)
    assert out["light.pantry"] == {"light.pantry_bulb", "light.pantry_missing"}
    assert out["light.match_only"] == {"light.m1", "light.m2"}
    # a plain light with no list in the map falls back to the extra list
    assert hem.expand_light_leaves(["light.galley_1"], entities, extra)["light.galley_1"] == {
        "light.also_ignored_never"}


def test_expand_does_not_mutate_inputs():
    entities = _build_entities()
    snapshot = repr(sorted(entities.items()))
    hem.expand_light_leaves(list(GROUPS), entities)
    hem.cover_light_targets(list(GROUPS), entities)
    assert repr(sorted(entities.items())) == snapshot


def test_cover_over_match_only_groups_uses_the_same_member_map():
    extra = {"light.g_a": ["light.m1", "light.m2"], "light.g_b": ["light.m2", "light.m3"]}
    out = hem.cover_light_targets(["light.g_a", "light.g_b"], {}, (), extra)
    assert out == ["light.g_a", "light.m3"]
    assert out == hem.cover_light_targets(["light.g_b", "light.g_a"], {}, (), extra)


def test_cover_tie_between_equal_covers_goes_to_the_exact_room_name():
    entities = _build_entities()
    ids = ["light.galley", "light.galley_main"]
    assert hem.cover_light_targets(ids, entities, ("galley",)) == ["light.galley"]
    assert hem.cover_light_targets(ids, entities, [("main", "galley")]) == ["light.galley"]


def test_fixture_population_floor():
    assert len([e for e in _build_entities() if e.startswith("light.")]) >= 38
    assert len(ROWS) >= 22


def test_cover_exact_room_name_beats_shorter_and_earlier_ids():
    entities = {
        "light.a": _entity("light.a", members=["light.b1", "light.b2"]),
        "light.kids_room": _entity("light.kids_room", members=["light.b1", "light.b2"]),
    }
    ids = ["light.a", "light.kids_room"]
    assert hem.cover_light_targets(ids, entities, ("kids", "room")) == ["light.kids_room"]
    assert hem.cover_light_targets(ids, entities, ()) == ["light.a"]


@pytest.mark.asyncio
async def test_anywhere_tier_hit_is_returned_when_exclusion_is_off(manager, monkeypatch):
    monkeypatch.setenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", "[]")
    shared.config._clear_cache_for_tests()
    assert set(await _ids(manager, "nursery")) == {"light.satellite_nursery_led_ring"}
    # anchored hit exists, so the anywhere hit (hub_den_lamp) still stays out
    assert set(await _ids(manager, "den")) == {"light.den_led_ring"}


def test_expand_empty_member_list_is_its_own_leaf_unless_extra_members_has_one():
    entities = _build_entities()
    assert entities["light.hollow"]["attributes"]["entity_id"] == []
    assert hem.expand_light_leaves(["light.hollow"], entities)["light.hollow"] == {"light.hollow"}
    extra = {"light.hollow": ["light.h1", "light.h2"]}
    assert hem.expand_light_leaves(["light.hollow"], entities, extra)["light.hollow"] == {
        "light.h1", "light.h2"}


def test_cover_over_an_empty_member_list():
    entities = _build_entities()
    extra = {"light.hollow": ["light.h1", "light.h2"]}
    assert hem.cover_light_targets(["light.hollow", "light.h1"], entities, (), extra) == [
        "light.hollow"]
    assert hem.cover_light_targets(["light.hollow", "light.h1"], entities) == [
        "light.h1", "light.hollow"]


def test_cover_partial_overlap_with_equal_leaf_counts_is_deterministic():
    entities = {
        "light.p_x": _entity("light.p_x", members=["light.a", "light.b"]),
        "light.p_y": _entity("light.p_y", members=["light.b", "light.c"]),
    }
    expected = ["light.c", "light.p_x"]
    assert hem.cover_light_targets(["light.p_x", "light.p_y"], entities) == expected
    assert hem.cover_light_targets(["light.p_y", "light.p_x"], entities) == expected
    assert hem.cover_light_targets(["light.p_x", "light.p_y"], entities, ("p", "y")) == [
        "light.a", "light.p_y"]
