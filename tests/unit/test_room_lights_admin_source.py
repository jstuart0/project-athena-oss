"""Room lights from the admin setting: resolution order, missing ids, and the
scene fallback's leaf checks.

Same fictional house and rig as ``test_room_light_area_tier.py``; the admin
value reaches the controller through the directory provider's resolver, the way
the lifespan wires it.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

# Import the services from src/ whatever else runs in this process (CI sets no PYTHONPATH).
_SRC = str(_Path(__file__).resolve().parents[2] / "src")
if _SRC not in _sys.path:
    _sys.path.insert(0, _SRC)

from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import room_directory as rd  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402
from shared import household  # noqa: E402
from shared.household_settings import RoomLightEntry, RoomLightsSection, SpeakerRoomEntry, SpeakerRoomsSection  # noqa: E402

from .test_room_light_area_tier import Rig, _isolated, _state, guest, light  # noqa: F401


def use_sources(rig, *, admin=None, env=""):
    """The effective room lights: ``admin`` is ``{room: [ids]}`` (None: unset), ``env`` the
    ``HA_LIGHT_GROUPS`` string."""
    section = None if admin is None else RoomLightsSection(
        rooms=[RoomLightEntry(room=room, lights=list(ids)) for room, ids in admin.items()])
    _runtime.set_room_directory(rd.RoomDirectoryProvider(
        rig.registry, None,
        room_lights=lambda: household.room_lights_from(section, env),
        speaker_rooms=lambda: household.speaker_rooms_from(None, "", outage_age=None),
    ))


def resolved_ids(rig, room):
    return sorted(m["entity_id"] for m in asyncio.run(rig.controller._resolve_room_lights(room)))


# ---------------------------------------------------------------------------
# Resolution order
# ---------------------------------------------------------------------------

def test_an_admin_room_decides_the_lights(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={"galley": ["light.hall_lamp", "light.study_lamp"]})
    assert resolved_ids(rig, "galley") == ["light.hall_lamp", "light.study_lamp"]


def test_the_server_setting_applies_when_admin_has_nothing_stored(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, env=json.dumps({"galley": "light.hall_pair"}))
    assert resolved_ids(rig, "galley") == ["light.hall_pair"]


def test_a_section_with_no_rooms_falls_back_to_the_server_setting(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, env=json.dumps({"galley": "light.hall_pair"}))
    _runtime.set_room_directory(rd.RoomDirectoryProvider(
        rig.registry, None,
        room_lights=lambda: household.room_lights_from(RoomLightsSection(rooms=None), json.dumps({"galley": "light.hall_pair"})),
        speaker_rooms=lambda: household.speaker_rooms_from(None, "", outage_age=None),
    ))
    assert resolved_ids(rig, "galley") == ["light.hall_pair"]


def test_admin_wins_over_the_server_setting_and_replaces_it_whole(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={"galley": ["light.hall_lamp"]}, env=json.dumps({"galley": "light.hall_pair", "study": "light.study_lamp"}))
    assert resolved_ids(rig, "galley") == ["light.hall_lamp"]
    assert resolved_ids(rig, "study") == ["light.study_lamp"], "no env entry for study: the area answers, not the server setting"
    assert asyncio.run(rig.controller._resolve_room_lights("study"))[0]["entity_id"] == "light.study_lamp"


def test_an_explicitly_empty_admin_list_ignores_the_server_setting(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={}, env=json.dumps({"galley": "light.hall_pair"}))
    assert "light.hall_pair" not in resolved_ids(rig, "galley")


def test_neither_source_leaves_the_area_tier(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig)
    baseline = resolved_ids(rig, "galley")
    assert baseline and "light.hall_pair" not in baseline


def test_part_missing_writes_the_present_lights_and_reports_the_rest_as_not_responding(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={"galley": ["light.hall_lamp", "light.study_lamp", "light.nope"]})
    matches = asyncio.run(rig.controller._resolve_room_lights("galley"))
    assert [m["entity_id"] for m in matches] == ["light.hall_lamp", "light.study_lamp"] and matches.missing == 1
    answer = rig.run(light("galley"), "turn on the galley lights")
    assert sorted(rig.written) == ["light.hall_lamp", "light.study_lamp"]
    assert answer == "Turned on the galley lights. 1 isn't responding."


def test_missing_ids_count_toward_everything_down(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={"galley": ["light.attic_a", "light.nope"]})
    answer = rig.run(light("galley"), "turn on the galley lights")
    assert rig.written == [] and answer == "The galley lights aren't responding right now."


def test_a_multi_room_command_counts_each_rooms_missing_ids(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={"galley": ["light.hall_lamp", "light.nope"], "study": ["light.study_lamp", "light.nope_2"]})
    answer = rig.run(light("multi_room", rooms=["galley", "study"]), "the galley and study lights please")
    assert sorted(rig.written) == ["light.hall_lamp", "light.study_lamp"]
    assert answer == "Turned on the galley and study lights. 2 aren't responding."


def test_a_room_group_command_counts_each_rooms_missing_ids(monkeypatch):
    groups = [{"name": "upper", "display_name": "Upper", "aliases": [],
               "members": [{"room_name": "galley"}, {"room_name": "study"}]}]
    rig = Rig(monkeypatch, room_groups=groups, resolve_group=groups[0])
    use_sources(rig, admin={"galley": ["light.hall_lamp", "light.nope"], "study": ["light.study_lamp"]})
    answer = rig.run(light("upper"), "turn on the upper lights")
    assert sorted(rig.written) == ["light.hall_lamp", "light.study_lamp"]
    assert answer == "Turned on the upper lights. 1 isn't responding."


def test_the_same_light_in_two_rooms_is_written_once(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={"galley": ["light.hall_lamp"], "study": ["light.hall_lamp", "light.study_lamp"]})
    rig.run(light("multi_room", rooms=["galley", "study"]), "the galley and study lights please")
    assert sorted(rig.written) == ["light.hall_lamp", "light.study_lamp"]


def test_the_directory_takes_satellite_rooms_and_light_rooms_from_admin(monkeypatch):
    rig = Rig(monkeypatch)
    speakers = SpeakerRoomsSection(entries=[SpeakerRoomEntry(key="assist_satellite.loft_voice", room="Loft")])
    provider = rd.RoomDirectoryProvider(
        rig.registry, None,
        room_lights=lambda: household.room_lights_from(
            RoomLightsSection(rooms=[RoomLightEntry(room="Sunroom", lights=["light.hall_lamp"])]), ""),
        speaker_rooms=lambda: household.speaker_rooms_from(speakers, "", outage_age=None),
    )
    directory = provider.current()
    assert directory.resolves("sunroom") is not None and directory.resolves("loft") is not None


# ---------------------------------------------------------------------------
# The scene fallback
# ---------------------------------------------------------------------------

LR_STATES = {s["entity_id"]: s for s in [
    _state("light.lr_a"), _state("light.lr_b"), _state("light.lr_c"),
    _state("light.lr_group", members=["light.lr_a", "light.lr_b"]),
    _state("light.office_lamp"),
]}
BRANCHES = [
    pytest.param("start movie mode", id="movie"),
    pytest.param("good morning", id="good_morning"),
    pytest.param("i'm home", id="home"),
]
BRANCH_SCENE = {"start movie mode": "scene.movie_mode", "good morning": "script.good_morning", "i'm home": "script.arriving"}
BRANCH_ROOM = {"start movie mode": "living_room", "good morning": "office", "i'm home": "living_room"}


def fallback_rig(monkeypatch, admin):
    rig = Rig(monkeypatch, registry=False, states=dict(LR_STATES), exclude="[]")
    use_sources(rig, admin=admin)
    return rig


def run_fallback(rig, query, *, perms=None, mode="owner", check=None):
    ha = mp.ensure_permission_enforcing(rig.raw)

    async def go():
        with mp.ha_permission_scope(perms or {"mode": "owner"}, mode=mode, utterance=classify_utterance(query)) as scope:
            answer = await rig.controller._run_scene_fallback(query, ha, query)
            return answer, scope

    return asyncio.run(go())


@pytest.mark.parametrize("query", BRANCHES)
def test_the_owner_fallback_writes_every_listed_light_in_one_call(monkeypatch, query):
    rig = fallback_rig(monkeypatch, {"living_room": ["light.lr_c", "light.lr_group"], "office": ["light.office_lamp", "light.lr_c"]})
    answer, _ = run_fallback(rig, query)
    assert answer is not None
    (call,) = rig.raw.call_service.await_args_list
    assert call.args[:2] == ("light", "turn_on")
    assert sorted(call.args[2]["entity_id"]) == (
        sorted(["light.lr_c", "light.lr_group"]) if BRANCH_ROOM[query] == "living_room" else ["light.lr_c", "light.office_lamp"])


@pytest.mark.parametrize("query", BRANCHES)
def test_a_guest_never_writes_a_restricted_leaf_or_a_group_holding_it(monkeypatch, query):
    rig = fallback_rig(monkeypatch, {"living_room": ["light.lr_group", "light.lr_c"], "office": ["light.lr_group", "light.lr_c"]})
    perms = guest(allowed_intents=["control"], restricted_entities=[r"^light\.lr_b$"])
    answer, scope = run_fallback(rig, query, perms=perms, mode="guest")
    written = [
        e for c in rig.raw.call_service.await_args_list
        for e in (c.args[2]["entity_id"] if isinstance(c.args[2]["entity_id"], list) else [c.args[2]["entity_id"]])
    ]
    assert answer is not None and written, answer
    assert "light.lr_b" not in written and "light.lr_group" not in written
    assert set(written) == {"light.lr_a", "light.lr_c"}
    assert any("light.lr_b" in d.targets for d in scope.denials), "the dropped bulb is recorded"


@pytest.mark.parametrize("query", BRANCHES)
def test_a_guest_whose_every_listed_light_is_denied_gets_the_refusal_and_no_write(monkeypatch, query):
    rig = fallback_rig(monkeypatch, {"living_room": ["light.lr_group"], "office": ["light.lr_group"]})
    perms = guest(allowed_intents=["control"], restricted_entities=[r"^light\.lr_"])
    answer, scope = run_fallback(rig, query, perms=perms, mode="guest")
    assert answer == mp.permission_refusal_message(("light",), scope)
    assert rig.raw.call_service.await_args_list == []
    assert scope.denials


@pytest.mark.parametrize("query", BRANCHES)
def test_a_denial_raised_inside_the_fallback_reaches_the_caller_not_a_scene_failure(monkeypatch, query):
    rig = fallback_rig(monkeypatch, {"living_room": ["light.lr_c"], "office": ["light.lr_c"]})

    async def call_service(domain, service, data):
        if domain in ("scene", "script"):
            raise RuntimeError("no such scene")
        raise mp.HAWritePermissionDenied("denied", reason="entity_or_domain_denied")

    rig.raw.call_service = AsyncMock(side_effect=call_service)
    entity = BRANCH_SCENE[query]

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            return await rig.controller._handle_scene_intent("activate", {"entity_id": entity}, rig.raw, original_query=query)

    with pytest.raises(mp.HAWritePermissionDenied):
        asyncio.run(go())


def test_execute_intent_turns_a_fallback_denial_into_the_refusal(monkeypatch):
    rig = fallback_rig(monkeypatch, {"living_room": ["light.lr_c"]})

    async def call_service(domain, service, data):
        if domain in ("scene", "script"):
            raise RuntimeError("no such scene")
        raise mp.HAWritePermissionDenied("denied", reason="entity_or_domain_denied")

    rig.raw.call_service = AsyncMock(side_effect=call_service)
    intent = {"device_type": "scene", "action": "activate", "parameters": {"entity_id": "scene.movie_mode"}}
    query = "start movie mode"

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query)) as scope:
            answer = await rig.controller.execute_intent(intent, rig.raw, query)
            return answer, mp.permission_refusal_message(("scene",), scope)

    answer, refusal = asyncio.run(go())
    assert any(c.args[0] == "light" for c in rig.raw.call_service.await_args_list), "the fallback ran"
    assert answer == refusal and "couldn't activate" not in answer.lower()


def test_a_failed_fallback_write_is_logged_by_class_and_falls_through(monkeypatch):
    import structlog

    rig = fallback_rig(monkeypatch, {"living_room": ["light.lr_c"]})
    rig.raw.call_service = AsyncMock(side_effect=ConnectionError("http://ha.invalid refused"))
    with structlog.testing.capture_logs() as logs:
        answer, _ = run_fallback(rig, "start movie mode")
    assert answer is None
    failed = [e for e in logs if e["event"] == "scene_fallback_write_failed"]
    assert failed == [{"event": "scene_fallback_write_failed", "error_class": "ConnectionError", "log_level": "warning"}]


# ---------------------------------------------------------------------------
# Admin list with every id missing; the "all" fallback branches under a denial
# ---------------------------------------------------------------------------

def test_an_admin_list_with_every_id_missing_is_not_responding_and_never_the_area(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig, admin={"galley": ["light.nope_1", "light.nope_2"]})
    matches = asyncio.run(rig.controller._resolve_room_lights("galley"))
    assert list(matches) == [] and matches.missing == 2
    answer = rig.run(light("galley"), "turn on the galley lights")
    assert rig.written == [] and answer == "The galley lights aren't responding right now."


def test_an_environment_list_with_every_id_missing_still_falls_through_to_the_area(monkeypatch):
    rig = Rig(monkeypatch)
    use_sources(rig)
    baseline = resolved_ids(rig, "galley")
    use_sources(rig, env=json.dumps({"galley": "light.nope_1"}))
    assert resolved_ids(rig, "galley") == baseline and baseline


ALL_BRANCHES = [
    pytest.param("good night, all the lights off", "script.good_night", id="good_night"),
    pytest.param("goodbye, all the lights off and all the doors locked", "script.leaving", id="leaving"),
]


@pytest.mark.parametrize("query,entity", ALL_BRANCHES)
def test_a_denied_all_branch_write_reaches_the_caller_as_a_denial(monkeypatch, query, entity):
    rig = fallback_rig(monkeypatch, {})
    rig.raw.call_service = AsyncMock(side_effect=mp.HAWritePermissionDenied("denied", reason="entity_or_domain_denied"))

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query)):
            return await rig.controller._run_scene_fallback(query, rig.raw, query)

    with pytest.raises(mp.HAWritePermissionDenied):
        asyncio.run(go())
    assert rig.raw.call_service.await_count == 1, "the branch reached its write, so the gate did not hide the test"


@pytest.mark.parametrize("query,entity", ALL_BRANCHES)
def test_a_guest_missing_scene_falls_to_the_all_branch_and_is_refused_not_told_a_basic_version(monkeypatch, query, entity):
    rig = fallback_rig(monkeypatch, {})

    async def call_service(domain, service, data):
        if domain in ("scene", "script"):
            raise RuntimeError("no such scene")

    rig.raw.call_service = AsyncMock(side_effect=call_service)
    perms = guest(allowed_intents=["control"], restricted_entities=[r"^light\."])

    async def go():
        with mp.ha_permission_scope(perms, mode="guest", utterance=classify_utterance(query)) as scope:
            try:
                answer = await rig.controller._handle_scene_intent("activate", {"entity_id": entity}, rig.raw, original_query=query)
            except mp.HAWritePermissionDenied:
                return None, scope
            return answer, scope

    answer, scope = asyncio.run(go())
    assert answer is None, answer
    assert scope.denials, "the guard recorded the denied all-lights write"


def test_the_mapping_and_its_source_come_from_one_read(monkeypatch):
    """A resolver answer that changes between two reads (env, then admin) must not mix:
    an env-sourced room with every id missing still falls through to the area."""
    rig = Rig(monkeypatch)
    use_sources(rig)
    baseline = resolved_ids(rig, "galley")
    answers = [
        household.room_lights_from(None, json.dumps({"galley": "light.nope_1"})),
        household.room_lights_from(RoomLightsSection(rooms=[RoomLightEntry(room="galley", lights=["light.nope_1"])]), ""),
    ]
    calls = []

    def flip():
        calls.append(1)
        return answers[min(len(calls), 2) - 1]

    _runtime.set_room_directory(rd.RoomDirectoryProvider(
        rig.registry, None, room_lights=flip,
        speaker_rooms=lambda: household.speaker_rooms_from(None, "", outage_age=None),
    ))
    assert resolved_ids(rig, "galley") == baseline and baseline
