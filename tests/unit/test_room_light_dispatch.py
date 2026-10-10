"""Room light commands through the controller (ATHENA-205, phase 2).

Real HAEntityManager over the phase-1 fixture, a recording raw HA client behind
the controller's own permission guard. A room command writes the smallest set
of ids covering the room's lights, each bulb at most once; a group hiding a
permission-denied light is degraded to its permitted lights; the fan-out gate
counts the physical lights beneath the written ids.

``_oracle_leaves`` (from the phase-1 file) is an independent recursion over the
fixture, so uniqueness is never checked with the code under test.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import pytest  # noqa: E402

import orchestrator.smart_home_controller as shc  # noqa: E402
import shared.config  # noqa: E402
from orchestrator import ha_entity_manager as hem  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import write_fanout  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402

from .test_room_light_resolution import GROUPS, _build_entities, _oracle_leaves  # noqa: E402

GALLEY_LEAVES = {f"light.galley_{i}" for i in range(1, 7)} | {"light.accent_galley_strip"}
IMPERATIVE_ON = "turn on the galley lights"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv("HA_ROOM_LIGHT_EXCLUDE_ENTITIES", raising=False)
    monkeypatch.setattr(shc, "_light_groups_cache", None)
    monkeypatch.setattr(shc, "_light_groups_warned", False)
    _runtime.reset_for_test()
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()
    yield
    _runtime.reset_for_test()
    shared.config._clear_cache_for_tests()
    hem._clear_room_light_exclude_cache()


def _fanout_cfg(threshold=6, hard_limit=18):
    cfg = MagicMock()
    cfg.ha_write_fanout_confirm_threshold = threshold
    cfg.ha_write_fanout_hard_limit = hard_limit
    return cfg


def _raw_client():
    client = MagicMock()
    client.call_service = AsyncMock(return_value={"ok": True})
    client.get_state = AsyncMock(return_value={"state": "on"})
    client.get_states = AsyncMock(return_value=[])
    return client


def _written(client):
    ids = []
    for call in client.call_service.await_args_list:
        data = call.args[2] if len(call.args) > 2 else {}
        eid = (data or {}).get("entity_id")
        ids.extend(eid if isinstance(eid, (list, tuple)) else [eid])
    return ids


def _manager(entities=None):
    m = hem.HAEntityManager("http://ha.invalid:8123", "token")
    m._entities_cache = entities if entities is not None else _build_entities()
    m._cache_time = datetime.now()
    m._build_indexes()
    return m


def _guest(**overrides):
    perms = mp.apply_guest_baseline({"mode": "guest"})
    perms.update(overrides)
    return perms


class _Rig:
    def __init__(self, monkeypatch, *, manager=None, groups_raw="", threshold=6, hard_limit=18,
                 room_group=None):
        self.controller = shc.SmartHomeController(entity_manager=manager or _manager(),
                                                  llm_router=MagicMock())
        self.raw = _raw_client()
        self.gate_calls = []
        monkeypatch.setattr(write_fanout, "get_config",
                            lambda: _fanout_cfg(threshold=threshold, hard_limit=hard_limit))
        monkeypatch.setattr(shc, "get_config", lambda: MagicMock(ha_light_groups=groups_raw))
        admin = MagicMock()
        admin.resolve_room_group = AsyncMock(return_value=room_group)
        monkeypatch.setattr(shc, "get_admin_client", lambda: admin)
        real_gate = write_fanout.gate

        def _recording_gate(domain, service=None, entity_ids=None, *a, **kw):
            self.gate_calls.append(tuple(entity_ids or ()))
            return real_gate(domain, service, entity_ids, *a, **kw)

        monkeypatch.setattr(write_fanout, "gate", _recording_gate)

    def run(self, intent, query, perms=None, mode="owner", wrap=None):
        perms = perms or {"mode": "owner"}

        async def _go():
            with mp.ha_permission_scope(perms, mode=mode, utterance=classify_utterance(query)) as scope:
                if wrap is not None:
                    with wrap():
                        answer = await self.controller.execute_intent(intent, self.raw, query)
                else:
                    answer = await self.controller.execute_intent(intent, self.raw, query)
                return answer, scope, write_fanout.take_block()

        return asyncio.run(_go())


def _light_intent(room, action="turn_on", target_scope="group", **extra):
    intent = {"device_type": "light", "room": room, "action": action,
              "target_scope": target_scope, "parameters": extra.pop("parameters", {})}
    intent.update(extra)
    return intent


def _multi(rooms, action="turn_on"):
    return _light_intent("multi_room", action, rooms=rooms)


GROUP_OF_GALLEY = {"display_name": "Ground Floor", "members": [{"room_name": "galley"}]}


def _assert_each_bulb_once(client):
    written = _written(client)
    leaves = [leaf for w in written for leaf in _oracle_leaves(w)]
    assert len(written) == len(set(written))
    assert len(leaves) == len(set(leaves))


# --- owner writes -----------------------------------------------------------


def test_galley_on_writes_one_group_call(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.run(_light_intent("galley"), IMPERATIVE_ON)
    assert _written(rig.raw) == ["light.galley_plus"]


def test_colour_writes_each_leaf_once_and_no_group(monkeypatch):
    rig = _Rig(monkeypatch)
    intent = _light_intent("galley", "set_color", color_description="ocean vibes",
                           parameters={"hs_colors": [[200, 80], [180, 60]]})
    rig.run(intent, "set the galley lights to ocean vibes")
    assert sorted(_written(rig.raw)) == sorted(GALLEY_LEAVES)


def test_all_individual_writes_each_leaf_once(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.run(_light_intent("galley", target_scope="all_individual"), "turn on all the galley lights")
    assert sorted(_written(rig.raw)) == sorted(GALLEY_LEAVES)


@pytest.mark.parametrize("room,action,expected", [
    ("loft", "turn_on", {"light.loft_left", "light.loft_bulb_4"}),
    ("bathroom", "turn_off", {"light.bathroom_vanity", "light.bath_mirror"}),
])
def test_other_rooms_write_their_cover(monkeypatch, room, action, expected):
    rig = _Rig(monkeypatch)
    rig.run(_light_intent(room, action), f"turn {action[5:]} the {room} lights")
    assert set(_written(rig.raw)) == expected
    _assert_each_bulb_once(rig.raw)


# --- HA_LIGHT_GROUPS --------------------------------------------------------


def _resolve(rig, room):
    return asyncio.run(rig.controller._resolve_room_lights(room))


@pytest.mark.parametrize("raw,room,expected", [
    ('{"galley":"light.galley"}', "galley", ["light.galley"]),
    ('{"living room":"light.galley"}', "living_room", ["light.galley"]),
    ('{"living room":"light.galley"}', "Living Room", ["light.galley"]),
    ('{"galley":"light.nope"}', "galley", ["light.galley_plus"]),
    ("{not json", "galley", ["light.galley_plus"]),
    ("{}", "galley", ["light.galley_plus"]),
    ("", "galley", ["light.galley_plus"]),
])
def test_light_group_key_shapes(monkeypatch, raw, room, expected):
    rig = _Rig(monkeypatch, groups_raw=raw)
    assert [m["entity_id"] for m in _resolve(rig, room)] == expected


def test_configured_match_has_the_managers_five_keys(monkeypatch):
    rig = _Rig(monkeypatch, groups_raw='{"galley":"light.galley"}')
    (match,) = _resolve(rig, "galley")
    assert set(match) == {"entity_id", "friendly_name", "members", "state", "type"}
    assert match["type"] == "group" and match["members"] == GROUPS["light.galley"]
    monkeypatch.setattr(shc, "_light_groups_cache", None)
    (plain,) = _resolve(_Rig(monkeypatch, groups_raw='{"den":"light.satellite_den_led_ring"}'), "den")
    assert plain["type"] == "individual" and plain["members"] == []


def test_explicit_config_survives_the_exclusion(monkeypatch):
    rig = _Rig(monkeypatch, groups_raw='{"den":"light.satellite_den_led_ring"}')
    rig.run(_light_intent("den"), "turn on the den lights")
    assert _written(rig.raw) == ["light.satellite_den_led_ring"]


# --- guest degrade ----------------------------------------------------------


@pytest.mark.parametrize("groups_raw", ["", '{"galley":"light.galley_plus"}'], ids=["name", "configured"])
def test_guest_group_is_degraded_to_permitted_leaves(monkeypatch, groups_raw):
    rig = _Rig(monkeypatch, groups_raw=groups_raw)
    perms = _guest(restricted_entities=[r"light\.galley_2$"])
    answer, scope, _ = rig.run(_light_intent("galley"), IMPERATIVE_ON, perms, "guest")
    written = _written(rig.raw)
    assert set(written) == GALLEY_LEAVES - {"light.galley_2"}
    assert all("light.galley_2" not in _oracle_leaves(w) for w in written)
    assert answer == mp.permission_refusal_message(("light",), scope, partial=True)


def test_absent_restricted_member_degrades_the_group(monkeypatch):
    rig = _Rig(monkeypatch)
    perms = _guest(restricted_entities=[r"light\.pantry_missing$"])
    rig.run(_light_intent("pantry"), "turn on the pantry lights", perms, "guest")
    assert _written(rig.raw) == ["light.pantry_bulb"]


ALL_DENIED = [r"light\.(galley|accent_galley).*"]


@pytest.mark.parametrize("path", ["single", "multi", "room_group"])
def test_all_denied_refuses_without_calls_or_a_block(monkeypatch, path):
    rig = _Rig(monkeypatch, room_group=GROUP_OF_GALLEY if path == "room_group" else None)
    intent = {"single": _light_intent("galley"), "multi": _multi(["galley"]),
              "room_group": _light_intent("ground floor")}[path]
    answer, scope, block = rig.run(intent, IMPERATIVE_ON, _guest(restricted_entities=ALL_DENIED), "guest")
    assert rig.raw.call_service.await_count == 0
    assert block is None
    assert answer == mp.permission_refusal_message(("light",), scope)


@pytest.mark.parametrize("path", ["single", "multi", "room_group"])
def test_all_denied_handlers_refuse_themselves_not_only_via_finish(monkeypatch, path):
    """execute_intent's _finish would also produce the refusal from the recorded
    denial; the handler's own answer must not be a success phrase."""
    rig = _Rig(monkeypatch)
    raw = mp.ensure_permission_enforcing(rig.raw)
    c = rig.controller
    calls = {
        "single": lambda: c._dispatch_light_or_room_command(
            "galley", "turn_on", "group", {}, _light_intent("galley"), raw, IMPERATIVE_ON),
        "multi": lambda: c._execute_multi_room_command(
            ["galley"], "turn_on", "group", {}, _multi(["galley"]), raw, IMPERATIVE_ON),
        "room_group": lambda: c._execute_room_group_command(
            GROUP_OF_GALLEY, "turn_on", "group", {}, _light_intent("ground floor"), raw, IMPERATIVE_ON),
    }

    async def _go():
        with mp.ha_permission_scope(_guest(restricted_entities=ALL_DENIED), mode="guest",
                                    utterance=classify_utterance(IMPERATIVE_ON)) as scope:
            return await calls[path](), scope

    answer, scope = asyncio.run(_go())
    assert rig.raw.call_service.await_count == 0
    assert answer == mp.permission_refusal_message(("light",), scope)


# --- denial record ----------------------------------------------------------


class _CounterFake:
    def __init__(self):
        self.samples = []

    def labels(self, **labels):
        counter = self
        key = tuple(sorted(labels.items()))

        class _Child:
            def inc(self_inner):
                counter.samples.append(key)
        return _Child()


def _path_rig(monkeypatch, path, action, **kw):
    rig = _Rig(monkeypatch, room_group=GROUP_OF_GALLEY if path == "room_group" else None, **kw)
    intent = {"single": _light_intent("galley", action), "multi": _multi(["galley"], action),
              "room_group": _light_intent("ground floor", action)}[path]
    return rig, intent


@pytest.mark.parametrize("action", ["turn_on", "turn_off"])
@pytest.mark.parametrize("path", ["single", "multi", "room_group"])
@pytest.mark.parametrize("case", ["all_denied", "partial"])
def test_denial_is_recorded_like_the_guards_and_does_not_halt(monkeypatch, case, path, action):
    fake = _CounterFake()
    monkeypatch.setattr(mp, "ha_write_denied_total", fake)
    rig, intent = _path_rig(monkeypatch, path, action)
    restricted = ALL_DENIED if case == "all_denied" else [r"light\.galley_2$"]
    answer, scope, _ = rig.run(intent, IMPERATIVE_ON, _guest(restricted_entities=restricted), "guest")
    assert [d.reason for d in scope.denials] == ["entity_or_domain_denied"]
    (denial,) = scope.denials
    assert denial.domain == "light" and denial.service == action and denial.targets
    assert fake.samples == [(("domain", "light"), ("scope_mode", "guest"))]
    assert scope.halted is False
    if case == "partial":
        written = _written(rig.raw)
        assert denial.targets == ("light.galley_2",)
        assert set(written) == GALLEY_LEAVES - {"light.galley_2"}
        assert len(written) == len(set(written))
        assert answer == mp.permission_refusal_message(("light",), scope, partial=True)
    else:
        assert rig.raw.call_service.await_count == 0
        assert answer == mp.permission_refusal_message(("light",), scope)


@pytest.mark.parametrize("path", ["single", "multi", "room_group"])
def test_fanout_prompt_wins_over_a_partial_denial_and_replay_records_it_once(monkeypatch, path):
    fake = _CounterFake()
    monkeypatch.setattr(mp, "ha_write_denied_total", fake)
    rig, intent = _path_rig(monkeypatch, path, "turn_on", threshold=5)
    perms = _guest(restricted_entities=[r"light\.galley_2$"])
    permitted = sorted(GALLEY_LEAVES - {"light.galley_2"})
    query = "galley lights please"

    answer, scope, block = rig.run(intent, query, perms, "guest")
    assert block is not None and "say:" in answer
    assert rig.raw.call_service.await_count == 0
    assert scope.denials == [] and fake.samples == []
    assert {i for w in block.writes for i in w.entity_ids} == set(permitted)

    answer, scope, block = rig.run(intent, query, perms, "guest",
                                   wrap=lambda: write_fanout.confirmed(permitted))
    assert block is None
    assert set(_written(rig.raw)) == set(permitted)
    assert answer == mp.permission_refusal_message(("light",), scope, partial=True)
    assert len(scope.denials) == 1 and len(fake.samples) == 1


def test_record_precheck_denial_is_a_noop_without_a_scope():
    mp.record_precheck_denial(None, "light", "turn_on", ["light.x"], "entity_or_domain_denied")


# --- overlap and dedupe -----------------------------------------------------


class _MatchOnlyManager:
    async def get_entities(self):
        return {}

    async def find_lights_by_room(self, room):
        return [
            {"entity_id": "light.g_a", "friendly_name": "A", "members": ["light.m1", "light.m2"],
             "state": "on", "type": "group"},
            {"entity_id": "light.g_b", "friendly_name": "B", "members": ["light.m2", "light.m3"],
             "state": "on", "type": "group"},
        ]


def test_overlapping_match_only_groups_write_each_bulb_once(monkeypatch):
    rig = _Rig(monkeypatch, manager=_MatchOnlyManager())
    rig.run(_light_intent("anywhere"), "turn on the anywhere lights")
    assert set(_written(rig.raw)) == {"light.g_a", "light.m3"}
    assert rig.gate_calls == [("light.m1", "light.m2", "light.m3")]
    match_groups = {"light.g_a": ["light.m1", "light.m2"], "light.g_b": ["light.m2", "light.m3"]}
    leaves = [leaf for w in _written(rig.raw) for leaf in _oracle_leaves(w, match_groups)]
    assert len(leaves) == len(set(leaves))


@pytest.mark.parametrize("path,intent,group", [
    ("multi-overlap", _multi(["galley", "galley main"]), None),
    ("multi-same-room-twice", _multi(["galley", "galley"]), None),
    ("room-group", _light_intent("ground floor"),
     {"display_name": "G", "members": [{"room_name": "galley"}, {"room_name": "galley_main"}]}),
])
def test_rooms_sharing_bulbs_write_each_once(monkeypatch, path, intent, group):
    rig = _Rig(monkeypatch, room_group=group)
    rig.run(intent, IMPERATIVE_ON)
    assert sorted(_written(rig.raw)) == sorted(GALLEY_LEAVES)
    assert rig.gate_calls == [tuple(sorted(GALLEY_LEAVES))]


def test_multi_room_mixes_rooms_once_each(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.run(_multi(["galley", "nook"]), IMPERATIVE_ON)
    assert sorted(_written(rig.raw)) == sorted(GALLEY_LEAVES | {"light.ceiling_light_nook"})


# --- the gate counts leaves -------------------------------------------------


def test_gate_counts_leaves_and_blocks_a_grouped_room(monkeypatch):
    rig = _Rig(monkeypatch)
    answer, _, block = rig.run(_light_intent("galley"), "galley lights please")
    assert rig.raw.call_service.await_count == 0
    assert block is not None
    assert {i for w in block.writes for i in w.entity_ids} == GALLEY_LEAVES
    assert rig.gate_calls == [tuple(sorted(GALLEY_LEAVES))]


def test_confirmed_replay_writes_the_group_once(monkeypatch):
    rig = _Rig(monkeypatch)
    rig.run(_light_intent("galley"), "galley lights please",
            wrap=lambda: write_fanout.confirmed(sorted(GALLEY_LEAVES)))
    assert _written(rig.raw) == ["light.galley_plus"]


def test_hard_limit_counts_leaves_for_an_imperative(monkeypatch):
    rig = _Rig(monkeypatch, hard_limit=5)
    _, _, block = rig.run(_light_intent("galley"), IMPERATIVE_ON)
    assert rig.raw.call_service.await_count == 0
    assert block is not None


# --- status -----------------------------------------------------------------


def _status(rig, room, query, mutate=None):
    if mutate:
        for eid in mutate:
            rig.controller.entity_manager._entities_cache[eid]["state"] = "off"
    answer, _, _ = rig.run({"device_type": "light", "room": room, "action": "get_status"}, query)
    return answer


def _listed(answer):
    body = answer.split(": ", 1)[1].rstrip(".")
    head, _, tail = body.rpartition(" and ")
    return {n for n in ([*head.split(", "), tail] if head else [tail]) if n}


def test_status_lists_the_bathroom_bulbs_only(monkeypatch):
    rig = _Rig(monkeypatch)
    answer = _status(rig, "bathroom", "are the bathroom lights on")
    assert _listed(answer) == {"Bathroom Vanity", "Bath Mirror"}


def test_status_uses_the_per_part_fallback_like_writes(monkeypatch):
    rig = _Rig(monkeypatch)
    assert _status(rig, "nook", "are the nook lights on") == "The Ceiling Light Nook is on."


def test_status_lists_bulbs_of_a_grouped_room_not_groups(monkeypatch):
    rig = _Rig(monkeypatch)
    off = [f"light.galley_{i}" for i in (4, 5, 6)]
    answer = _status(rig, "galley", "are the galley lights on", mutate=off)
    assert _listed(answer) == {"Galley 1", "Galley 2", "Galley 3", "Accent Galley Strip"}
    assert "7 lights" in _status(_Rig(monkeypatch), "galley", "are the galley lights on")


def test_status_follows_the_configured_group(monkeypatch):
    rig = _Rig(monkeypatch, groups_raw='{"galley":"light.galley"}')
    off = [f"light.galley_{i}" for i in (4, 5, 6)]
    answer = _status(rig, "galley", "are the galley lights on", mutate=off)
    assert _listed(answer) == {"Galley 1", "Galley 2", "Galley 3"}
