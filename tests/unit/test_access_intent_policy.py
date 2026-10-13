"""An unlock, or opening a garage, gate or door cover, needs one named door.

A real ``SmartHomeController`` over a fictional house, a real
``PermissionEnforcingHAClient`` (the controller's own wrap) over a recording
raw client, and a real permission scope. Multi-turn cases (the answer to
"which door?", a follow-up that carries a door) run through ``route_control``
with the rig in ``_room_chat_harness.py``. Writes are read off the raw client,
below the guard, so a path that bypassed the controller's own checks would
show here.

A room, an area or a model's guess never names a door: the CX-H cases pin
that for one door and for several, in the area and by name.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator import room_choice, room_context  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from orchestrator.sequence_executor import ONE_DOOR_STEP_REFUSAL, SequenceExecutor  # noqa: E402
from orchestrator.utterance_kind import classify_utterance  # noqa: E402
from shared import ha_registry as hr  # noqa: E402

from ._room_chat_harness import Chat  # noqa: E402
from .test_room_light_area_tier import Rig, _isolated, _reg, guest  # noqa: E402,F401

OPENING_CALLS = {("lock", "unlock"), ("cover", "open_cover")}


def lock_state(name, state="locked"):
    return {"state": state, "attributes": {"friendly_name": name}}


def cover_state(name, device_class=None, state="closed"):
    attrs = {"friendly_name": name}
    if device_class:
        attrs["device_class"] = device_class
    return {"state": state, "attributes": attrs}


def registry_raw(areas, placement):
    return {
        "areas": [{"area_id": f"area_{k}", "name": n, "aliases": []} for k, n in areas.items()],
        "devices": [],
        "entities": [_reg(e, area=f"area_{a}") for e, a in placement.items()],
    }


THREE_LOCKS = {
    "lock.front_door": lock_state("Front Door Lock"),
    "lock.back_door": lock_state("Back Door Lock"),
    "lock.kitchen_door": lock_state("Kitchen Door Lock"),
}
ONE_LOCK = {"lock.main": lock_state("Door Lock")}


class DoorRig(Rig):
    """The light rig's controller, guard and recording client over door
    states, with an optional area registry."""

    def __init__(self, monkeypatch, states, *, areas=None, placement=None, **kw):
        super().__init__(monkeypatch, states=states, registry=False, **kw)
        if areas:
            snapshot = hr.RegistrySnapshot.from_raw(registry_raw(areas, placement or {}), fetched_at=2.0)
            self.registry._snapshot = snapshot
            self.registry.state = "ok"

    def run_scoped(self, intent, query, perms=None, mode="owner"):
        perms = perms or {"mode": "owner"}

        async def go():
            with mp.ha_permission_scope(perms, mode=mode, utterance=classify_utterance(query), trusted_voice=True) as scope:
                answer = await self.controller.execute_intent(intent, self.raw, query)
                return answer, scope

        return asyncio.run(go())

    @property
    def calls(self):
        out = []
        for call in self.raw.call_service.await_args_list:
            data = call.args[2] if len(call.args) > 2 else {}
            eid = (data or {}).get("entity_id")
            out.append((call.args[0], call.args[1], eid))
        return out

    @property
    def openings(self):
        return [c for c in self.calls if c[:2] in OPENING_CALLS]


def lock_intent(action="unlock", room=None, **extra):
    intent = {"device_type": "lock", "room": room, "action": action, "target_scope": "group", "parameters": {}}
    intent.update(extra)
    return intent


def cover_intent(action="open", room=None, **extra):
    intent = {"device_type": "cover", "room": room, "action": action, "target_scope": "group", "parameters": {}}
    intent.update(extra)
    return intent


# ---------------------------------------------------------------------------
# CX-H: a room or an area never names a door to open
# ---------------------------------------------------------------------------

class TestARoomNeverOpensADoor:
    @pytest.mark.parametrize("room", [None, "kitchen"])
    def test_unlock_the_kitchen_with_one_lock_in_the_area_is_a_hint(self, monkeypatch, room):
        rig = DoorRig(monkeypatch, {"lock.deadbolt": lock_state("Deadbolt"), "lock.shed": lock_state("Shed Padlock")},
                      areas={"kitchen": "Kitchen"}, placement={"lock.deadbolt": "kitchen"})
        answer, scope = rig.run_scoped(lock_intent(room=room), "unlock the kitchen")
        assert rig.openings == [], rig.calls
        assert "Deadbolt" in answer and "unlock the" in answer.lower()
        assert scope.room_choice is None

    @pytest.mark.parametrize("room", [None, "front_porch"])
    def test_unlock_the_front_porch_with_one_lock_named_deadbolt_is_a_hint(self, monkeypatch, room):
        rig = DoorRig(monkeypatch, {"lock.deadbolt": lock_state("Deadbolt"), "lock.shed": lock_state("Shed Padlock")},
                      areas={"front_porch": "Front Porch"}, placement={"lock.deadbolt": "front_porch"})
        answer, _ = rig.run_scoped(lock_intent(room=room), "unlock the front porch")
        assert rig.openings == [] and "Deadbolt" in answer

    def test_the_same_with_two_locks_in_the_area_asks_listing_only_those_two(self, monkeypatch):
        states = {"lock.deadbolt": lock_state("Deadbolt"), "lock.latch": lock_state("Latch"),
                  "lock.shed": lock_state("Shed Padlock")}
        rig = DoorRig(monkeypatch, states, areas={"front_porch": "Front Porch"},
                      placement={"lock.deadbolt": "front_porch", "lock.latch": "front_porch"})
        answer, scope = rig.run_scoped(lock_intent(), "unlock the front porch")
        assert rig.openings == []
        assert answer.startswith("Which door") and "deadbolt" in answer.lower() and "latch" in answer.lower()
        assert "shed" not in answer.lower()
        assert scope.room_choice is not None and scope.room_choice.kind == room_choice.KIND_ACCESS
        assert sorted(c[0] for c in scope.room_choice.candidates) == ["lock.deadbolt", "lock.latch"]

    def test_unlock_the_kitchen_when_the_lock_is_named_kitchen_door_lock_is_a_hint(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent(room="kitchen"), "unlock the kitchen")
        assert rig.openings == [] and "Kitchen Door Lock" in answer

    def test_open_the_garage_area_with_a_garage_cover_in_it_is_a_hint(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"cover.main": cover_state("Main", "garage")},
                      areas={"garage": "Garage"}, placement={"cover.main": "garage"})
        answer, _ = rig.run_scoped(cover_intent(room="garage"), "open the garage area")
        assert rig.openings == [] and "open the" in answer.lower() and "Main" in answer

    def test_the_model_naming_a_room_changes_nothing(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"lock.deadbolt": lock_state("Deadbolt"), "lock.latch": lock_state("Latch")},
                      areas={"kitchen": "Kitchen"}, placement={"lock.deadbolt": "kitchen", "lock.latch": "kitchen"})
        for room in ("kitchen", "front_door", "lock.deadbolt", "all", "all_doors"):
            rig.run_scoped(lock_intent(room=room), "unlock it")
            rig.run_scoped(lock_intent(room=room), "unlock the kitchen")
        assert rig.openings == []

    def test_positive_unlock_the_kitchen_door_writes_the_kitchen_door_lock(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent(room="kitchen"), "unlock the kitchen door")
        assert rig.openings == [("lock", "unlock", "lock.kitchen_door")]
        assert "Kitchen Door Lock" in answer

    def test_positive_lock_the_front_porch_locks_the_areas_lock(self, monkeypatch):
        states = {"lock.deadbolt": lock_state("Deadbolt", "unlocked"), "lock.shed": lock_state("Shed Padlock", "unlocked")}
        rig = DoorRig(monkeypatch, states, areas={"front_porch": "Front Porch"}, placement={"lock.deadbolt": "front_porch"})
        rig.run_scoped(lock_intent("lock"), "lock the front porch")
        assert rig.calls == [("lock", "lock", "lock.deadbolt")]

    def test_positive_is_the_porch_locked_reports_the_areas_locks(self, monkeypatch):
        states = {"lock.deadbolt": lock_state("Deadbolt"), "lock.shed": lock_state("Shed Padlock", "unlocked")}
        rig = DoorRig(monkeypatch, states, areas={"front_porch": "Front Porch"}, placement={"lock.deadbolt": "front_porch"})
        answer, _ = rig.run_scoped(lock_intent("get_status"), "is the front porch locked")
        assert answer == "Deadbolt is locked." and rig.calls == []


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------

class TestLocks:
    def test_unlock_with_no_door_asks_which_and_writes_nothing(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, scope = rig.run_scoped(lock_intent(), "unlock")
        assert rig.calls == []
        assert answer.startswith("Which door:") and scope.room_choice is not None
        assert scope.room_choice.phrase == "door" and len(scope.room_choice.candidates) == 3

    def test_the_car_is_not_found(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, scope = rig.run_scoped(lock_intent(room="car"), "unlock the car")
        assert rig.calls == [] and answer == "I couldn't find a door called car." and scope.room_choice is None

    def test_release_the_deadbolt_on_the_front_door_opens_the_front_lock_only(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS, areas={"front_porch": "Front Porch"},
                      placement={"lock.front_door": "front_porch"})
        rig.run_scoped(lock_intent(room="front_door"), "release the deadbolt on the front door")
        assert rig.calls == [("lock", "unlock", "lock.front_door")]

    @pytest.mark.parametrize("query", ["unlock all the doors", "unlock the doors", "unlock every door", "unlock everything"])
    def test_unlock_all_the_doors_writes_nothing(self, monkeypatch, query):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, scope = rig.run_scoped(lock_intent(room="all_doors"), query)
        assert rig.calls == []
        assert "one door at a time" in answer and scope.room_choice is not None

    def test_lock_up_locks_all_and_the_gate_is_consulted(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent("lock"), "lock up")
        assert sorted(rig.calls) == [("lock", "lock", e) for e in sorted(THREE_LOCKS)]
        assert rig.gate_calls == [tuple(THREE_LOCKS)]
        assert answer == "Done! I've locked all 3 doors."

    def test_lock_with_nothing_named_still_locks_every_lock(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        rig.run_scoped(lock_intent("lock"), "lock the doors")
        assert len(rig.calls) == 3

    def test_lock_the_hall_door_does_not_reach_every_lock(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent("lock", room="hall"), "lock the hall door")
        assert rig.calls == [] and answer == "I couldn't find a door called hall."

    def test_lock_the_front_door_locks_only_it(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        rig.run_scoped(lock_intent("lock", room="front_door"), "lock the front door")
        assert rig.calls == [("lock", "lock", "lock.front_door")]

    def test_a_zero_lock_house(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"cover.x": cover_state("X", "garage")})
        answer, _ = rig.run_scoped(lock_intent(), "unlock the front door")
        assert answer == "I couldn't find any locks in the home automation system." and rig.calls == []

    def test_a_one_lock_house_unlocks_the_bare_noun_only(self, monkeypatch):
        rig = DoorRig(monkeypatch, ONE_LOCK)
        rig.run_scoped(lock_intent(), "unlock the door")
        assert rig.openings == [("lock", "unlock", "lock.main")]
        rig = DoorRig(monkeypatch, ONE_LOCK)
        answer, _ = rig.run_scoped(lock_intent(), "unlock the side door")
        assert rig.calls == [] and answer == "I couldn't find a door called side."

    def test_a_one_lock_house_still_needs_a_noun(self, monkeypatch):
        rig = DoorRig(monkeypatch, ONE_LOCK)
        answer, _ = rig.run_scoped(lock_intent(), "unlock")
        assert rig.calls == [] and "Door Lock" in answer

    def test_a_three_lock_house_never_unlocks_two(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        for query in ("unlock the front door and the back door", "unlock the doors", "unlock front and back"):
            rig.run_scoped(lock_intent(), query)
        assert len(rig.openings) <= 3
        assert len({c[2] for c in rig.openings}) == len(rig.openings)
        assert not any(c[2] == "lock.back_door" for c in rig.openings)

    def test_unlock_by_a_name_without_a_noun_is_a_hint(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent(), "unlock the front")
        assert rig.calls == [] and "Front Door Lock" in answer

    def test_the_model_cannot_pick_a_door_the_words_do_not_name(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        rig.run_scoped(lock_intent(room="back_door", entity_id="lock.back_door"), "unlock the door")
        rig.run_scoped(lock_intent(room="front"), "open sesame")
        assert rig.calls == []

    def test_a_status_question_still_reports_by_name_or_all(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent("get_status", room="front_door"), "is the front door locked")
        assert answer == "Front Door Lock is locked."
        answer, _ = rig.run_scoped(lock_intent("get_status"), "are the doors locked")
        assert answer.count(" is locked") == 3
        assert rig.calls == []

    def test_the_speakers_room_does_not_pick_a_lock(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS, areas={"kitchen": "Kitchen"}, placement={"lock.kitchen_door": "kitchen"})

        intent = lock_intent()

        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("unlock the door")):
                return await rig.controller.execute_intent(intent, rig.raw, "unlock the door", device_room="kitchen")

        answer = asyncio.run(go())
        assert rig.calls == [] and answer.startswith("Which door")
        assert intent["room"] is None, "the speaker's room is not applied to a door"

    def test_no_query_opens_nothing(self, monkeypatch):
        rig = DoorRig(monkeypatch, ONE_LOCK)

        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                return await rig.controller.execute_intent(lock_intent(room="main"), rig.raw)

        asyncio.run(go())
        assert rig.calls == []


class TestGuests:
    def test_a_guest_is_refused_for_every_lock_command(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        for action, query in (("unlock", "unlock the front door"), ("lock", "lock up")):
            answer, scope = rig.run_scoped(lock_intent(action, room="front_door"), query, perms=guest(), mode="guest")
            assert "guest mode" in answer.lower(), (action, answer)
            assert len(scope.denials) >= 1
        assert rig.calls == []

    def test_a_guest_never_hears_the_door_names(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent(), "unlock the door", perms=guest(), mode="guest")
        assert "Front" not in answer and "Back" not in answer and not answer.startswith("Which")

    def test_a_guest_is_refused_to_open_a_garage(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"cover.main": cover_state("Main", "garage")})
        answer, scope = rig.run_scoped(cover_intent(room="garage"), "open the garage", perms=guest(), mode="guest")
        assert rig.calls == [] and "guest mode" in answer.lower() and scope.denials

    def test_a_restricted_lock_is_not_offered_and_not_written(self, monkeypatch):
        perms = {"mode": "guest", "restricted_entities": [r"^lock\.back_door$"], "allowed_domains": ["lock"]}
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent(), "unlock the door", perms=perms, mode="guest")
        assert "Back" not in answer and rig.calls == []
        rig.run_scoped(lock_intent(room="back_door"), "unlock the back door", perms=perms, mode="guest")
        assert rig.calls == []


# ---------------------------------------------------------------------------
# Covers
# ---------------------------------------------------------------------------

TWO_GARAGE = {
    "cover.garage_left": cover_state("Garage Door Left", "garage"),
    "cover.garage_right": cover_state("Garage Door Right", "garage"),
}
BLINDS = {
    "cover.kitchen_blind": cover_state("Kitchen Blind", "blind"),
    "cover.kitchen_shade": cover_state("Kitchen Shade", "shade"),
    "cover.den_blind": cover_state("Den Blind", "blind"),
}


class TestCovers:
    def test_open_the_garage_with_two_doors_asks(self, monkeypatch):
        rig = DoorRig(monkeypatch, TWO_GARAGE)
        answer, scope = rig.run_scoped(cover_intent(room="garage"), "open the garage")
        assert rig.calls == [] and answer.startswith("Which door:") and scope.room_choice is not None

    def test_open_the_left_garage_door_opens_it_only(self, monkeypatch):
        rig = DoorRig(monkeypatch, TWO_GARAGE)
        rig.run_scoped(cover_intent(room="garage"), "open the left garage door")
        assert rig.calls == [("cover", "open_cover", "cover.garage_left")]

    def test_one_garage_door_opens_by_the_bare_noun(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"cover.main": cover_state("Main", "garage"), **BLINDS})
        rig.run_scoped(cover_intent(room="garage"), "open the garage")
        assert rig.calls == [("cover", "open_cover", "cover.main")]

    def test_a_cover_with_no_device_class_is_an_access_cover(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"cover.mystery": cover_state("Mystery"), "cover.other": cover_state("Other")})
        answer, _ = rig.run_scoped(cover_intent(), "open the garage")
        assert rig.calls == [] and answer.startswith("Which door")

    def test_open_the_blinds_in_a_room_opens_all_its_blinds(self, monkeypatch):
        rig = DoorRig(monkeypatch, {**BLINDS, "cover.main": cover_state("Main", "garage")})
        rig.run_scoped(cover_intent(room="kitchen"), "open the kitchen blinds")
        assert sorted(rig.calls) == [("cover", "open_cover", "cover.kitchen_blind"), ("cover", "open_cover", "cover.kitchen_shade")]

    def test_open_the_blinds_with_no_room_never_includes_a_garage_door(self, monkeypatch):
        rig = DoorRig(monkeypatch, {**BLINDS, "cover.main": cover_state("Main", "garage")})
        rig.run_scoped(cover_intent(), "open the blinds")
        assert len(rig.calls) == 3 and ("cover", "open_cover", "cover.main") not in rig.calls

    def test_a_garage_plus_blinds_in_one_target_set_is_refused(self, monkeypatch):
        states = {"cover.garage_door": cover_state("Garage Door", "garage"), "cover.garage_blind": cover_state("Garage Blind", "blind")}
        rig = DoorRig(monkeypatch, states)
        answer, _ = rig.run_scoped(cover_intent(room="garage"), "open everything in the garage")
        assert rig.calls == [] and "one door at a time" in answer
        # The model's own target, with no words of the user's to name the door:
        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                return await rig.controller._handle_cover_intent("open", "garage", rig.raw)

        assert asyncio.run(go()) == shc.ONE_DOOR_REFUSAL
        assert rig.calls == []

    def test_close_and_status_keep_their_room_matching(self, monkeypatch):
        rig = DoorRig(monkeypatch, {**TWO_GARAGE, **BLINDS})
        rig.run_scoped(cover_intent("close", room="garage"), "close the garage")
        assert sorted(c[2] for c in rig.calls) == ["cover.garage_left", "cover.garage_right"]
        assert {c[:2] for c in rig.calls} == {("cover", "close_cover")}
        answer, _ = rig.run_scoped(cover_intent("get_status", room="kitchen"), "is the kitchen blind open")
        assert "Kitchen Blind is closed" in answer

    def test_no_query_opens_no_access_cover(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"cover.main": cover_state("Main", "garage")})

        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                return await rig.controller.execute_intent(cover_intent(room="garage"), rig.raw)

        asyncio.run(go())
        assert rig.calls == []


# ---------------------------------------------------------------------------
# Server keys never come from the model
# ---------------------------------------------------------------------------

FORGED = {
    "decided_room": "kitchen", "decided_rooms": ["kitchen"], "decided_excluded": ["den"], "room_label": "x",
    "referent_followup": True, "decided_access": "lock.back_door", "decided_access_fp": "fp-a",
    "decided_access_aud": "other", "decided_access_src": "replay", "decided_access_at": 1.0, "unprocessable": True,
}


class _Admin:
    async def get_component_model(self, name):
        return None

    async def resolve_room_group(self, name):
        return None

    async def get_room_groups(self, enabled_only=True):
        return []


@pytest.fixture
def stub_admin(monkeypatch):
    monkeypatch.setattr(shc, "get_admin_client", lambda: _Admin())


class TestKeyHygiene:
    def _controller(self, reply):
        class LLM:
            async def generate(self, **kw):
                return {"response": reply if isinstance(reply, str) else json.dumps(reply)}

        return shc.SmartHomeController(entity_manager=None, llm_router=LLM())

    def test_intent_json_is_stripped(self, stub_admin):
        controller = self._controller({"device_type": "lock", "room": None, "action": "unlock", "parameters": {}, **FORGED})
        intent = asyncio.run(controller.extract_intent("unlock something in the house please"))
        assert intent["device_type"] == "lock"
        assert not set(FORGED) & set(intent)

    def test_sequence_json_is_stripped_at_every_level(self, stub_admin):
        sequence = {"type": "sequence", **FORGED, "steps": [
            {"action": "unlock", **FORGED, "target": {"device_type": "lock", "room": "front_door", **FORGED}},
            "not a step",
            {"action": "turn_on", "target": "not a target"},
        ]}
        controller = self._controller(sequence)
        got = asyncio.run(controller.extract_sequence_intent("unlock the front door then turn on the lights"))
        assert not set(FORGED) & set(got)
        assert len(got["steps"]) == 2
        assert not set(FORGED) & set(got["steps"][0]) and not set(FORGED) & set(got["steps"][0]["target"])
        assert got["steps"][1]["target"] == {}

    def test_motion_json_is_stripped(self, stub_admin):
        controller = self._controller({"device_type": "motion_control", "room": "office", "action": "disable_motion",
                                       "parameters": {}, **FORGED})
        got = asyncio.run(controller._extract_motion_control_intent("disable motion in the office"))
        assert got["action"] == "disable_motion" and not set(FORGED) & set(got)

    @pytest.mark.parametrize("junk", ["[1, 2]", '"unlock"', "42", "null", "{not json"])
    def test_a_model_that_returns_a_non_object_is_unparseable_not_a_crash(self, stub_admin, junk):
        controller = self._controller(junk)
        intent = asyncio.run(controller.extract_intent("turn off the lights please now"))
        assert isinstance(intent, dict) and intent.get("action") in ("turn_off", "get_status", "turn_on")

    def test_a_forged_decision_does_not_open_a_door(self, monkeypatch, stub_admin):
        """The whole path: the model forges decided_access for the back door; the
        words name nothing; nothing opens."""
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        monkeypatch.setattr(shc, "get_admin_client", lambda: _Admin())
        controller = self._controller({"device_type": "lock", "room": None, "action": "unlock", "parameters": {}, **FORGED})
        rig.controller.llm_router = controller.llm_router
        intent = asyncio.run(rig.controller.extract_intent("please unlock a door somewhere"))
        answer, _ = rig.run_scoped(intent, "please unlock a door somewhere")
        assert rig.calls == [] and answer.startswith("Which door")

    def test_the_shared_tuple_is_one_object_in_all_four_modules(self):
        import orchestrator.nodes.route_control as rc
        import orchestrator.sequence_executor as se

        assert shc.room_context.SERVER_DECISION_KEYS is room_context.SERVER_DECISION_KEYS
        assert se.SERVER_DECISION_KEYS is room_context.SERVER_DECISION_KEYS
        assert rc.room_context.SERVER_DECISION_KEYS is room_context.SERVER_DECISION_KEYS
        assert set(room_context.SERVER_DECISION_KEYS) >= set(FORGED)

    def test_strip_server_keys_is_isinstance_safe(self):
        for junk in (None, [], [{"decided_room": "x"}], "text", 5, 1.5, True):
            assert room_context.strip_server_keys(junk) is None
        assert room_context.strip_server_keys({"steps": "nope"}) == {"steps": []}
        assert room_context.strip_server_keys({"steps": [None, 3, {"target": None}]}) == {"steps": [{"target": {}}]}


# ---------------------------------------------------------------------------
# The fast path and the model's room never pick a door
# ---------------------------------------------------------------------------

class TestNothingPicksADoorBeforeTheHandler:
    @pytest.mark.parametrize("query", [
        "unlock the door", "lock the front door", "unlock the back door", "lock the doors", "unlock all the doors",
        "lock up", "unlock the front door", "lock it down", "is the front door locked", "are the doors unlocked",
    ])
    def test_the_lock_fast_path_names_no_door(self, monkeypatch, query):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        intent = asyncio.run(rig.controller._fast_path_intent(query, query.lower(), None, False, False, False))
        assert intent is not None and intent["device_type"] == "lock"
        assert intent["room"] is None, "no default door, no 'all doors', no door picked from a word"

    def test_the_unlock_the_door_fast_path_asks_in_a_three_lock_house(self, monkeypatch):
        """The old fast path defaulted to the front door; the words say 'the door'."""
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        intent = asyncio.run(rig.controller._fast_path_intent("unlock the door", "unlock the door", None, False, False, False))
        answer, scope = rig.run_scoped(intent, "unlock the door")
        assert rig.calls == [] and answer.startswith("Which door")

    def test_a_model_door_name_is_not_turned_into_a_room(self, monkeypatch, stub_admin):
        """front_door must stay a door name, not become the Front Porch area."""
        rig = DoorRig(monkeypatch, THREE_LOCKS, areas={"front_porch": "Front Porch"}, placement={"lock.front_door": "front_porch"})
        monkeypatch.setattr(shc, "get_admin_client", lambda: _Admin())

        class LLM:
            async def generate(self, **kw):
                return {"response": json.dumps({"device_type": "lock", "room": "front_door", "action": "unlock",
                                                "target_scope": "group", "parameters": {}})}

        rig.controller.llm_router = LLM()
        intent = asyncio.run(rig.controller.extract_intent("release the deadbolt on the front door"))
        assert intent["room"] == "front_door"
        rig.run_scoped(intent, "release the deadbolt on the front door")
        assert rig.openings == [("lock", "unlock", "lock.front_door")]

    def test_the_lock_prompt_example_keeps_door_names_as_door_names(self):
        source = (SRC / "orchestrator" / "smart_home_controller.py").read_text()
        assert '"room": "front_door", "action": "lock"' in source


# ---------------------------------------------------------------------------
# The automation agent never turns a room into a door
# ---------------------------------------------------------------------------

class TestAgentRoomNeverBecomesADoor:
    def agent(self):
        from unittest.mock import AsyncMock, MagicMock
        from orchestrator.automation_agent import AutomationAgent

        ha = MagicMock()
        ha.call_service = AsyncMock(return_value=True)
        agent = AutomationAgent(ha, MagicMock(), admin_client=MagicMock())
        agent.ha_client = ha
        return agent, ha

    @pytest.mark.parametrize("args", [
        {"domain": "lock", "service": "unlock"},
        {"domain": "cover", "service": "open_cover"},
        {"domain": "Lock", "service": "unlock", "entity_id": ""},
        {"domain": "COVER", "service": "toggle"},
    ])
    def test_no_entity_in_a_room_context_calls_nothing(self, args):
        from orchestrator.automation_agent import NAME_THE_DOOR_TEXT

        agent, ha = self.agent()
        result = asyncio.run(agent._exec_ha_service(dict(args), {"room": "front"}))
        assert result == NAME_THE_DOOR_TEXT and ha.call_service.await_count == 0

    def test_a_homeassistant_call_in_a_room_context_gets_no_synthesised_target(self):
        agent, ha = self.agent()
        asyncio.run(agent._exec_ha_service({"domain": "homeassistant", "service": "turn_off"}, {"room": "front"}))
        for call in ha.call_service.await_args_list:
            assert "entity_id" not in call.args[2], call

    def test_a_light_still_takes_the_room_as_before(self):
        agent, ha = self.agent()
        asyncio.run(agent._exec_ha_service({"domain": "light", "service": "turn_on"}, {"room": "kitchen"}))
        ha.call_service.assert_awaited_once_with("light", "turn_on", {"entity_id": "light.kitchen"})


# ---------------------------------------------------------------------------
# Scenes and routines
# ---------------------------------------------------------------------------

SCENE_STATES = {
    "script.unlock_all_at_night": {"state": "off", "attributes": {"friendly_name": "Unlock All At Night"}},
    "script.good_night": {"state": "off", "attributes": {"friendly_name": "Good Night"}},
    "scene.movie_mode": {"state": "off", "attributes": {"friendly_name": "Movie"}},
    "script.leaving": {"state": "off", "attributes": {"friendly_name": "Leaving"}},
}


class TestSceneGrounding:
    def activate(self, monkeypatch, entity_id, query):
        rig = DoorRig(monkeypatch, SCENE_STATES)
        intent = {"device_type": "scene", "room": None, "action": "activate", "parameters": {"entity_id": entity_id}}
        answer, _ = rig.run_scoped(intent, query)
        return answer, rig.calls

    def test_good_night_never_reaches_the_unlock_script(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "script.unlock_all_at_night", "good night")
        assert calls == [] and answer == shc.UNKNOWN_SCENE_TEXT

    def test_an_id_not_fully_in_the_words_does_not_activate(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "script.unlock_all_at_night", "unlock at night")
        assert calls == [] and "script." not in answer
        answer, calls = self.activate(monkeypatch, "script.unlock_all_at_night", "run the routine")
        assert calls == []

    def test_every_distinguishing_word_activates(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "script.unlock_all_at_night", "run unlock all at night")
        assert calls == [("script", "turn_on", "script.unlock_all_at_night")]

    def test_the_words_scene_and_mode_do_not_have_to_be_said(self, monkeypatch):
        """An id with no name in Home Assistant is grounded by its own words only."""
        rig = DoorRig(monkeypatch, {})
        for query in ("cozy evening", "run the cozy evening", "cozy evening please"):
            assert asyncio.run(rig.controller._scene_grounded("scene.cozy_evening_mode", query)), query
        assert not asyncio.run(rig.controller._scene_grounded("scene.cozy_evening_mode", "cozy"))
        assert not asyncio.run(rig.controller._scene_grounded("scene.mode", "mode"))

    def test_the_friendly_name_grounds_it_too(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "scene.movie_mode", "start the movie")
        assert calls == [("scene", "turn_on", "scene.movie_mode")]

    def test_the_servers_own_phrase_table_grounds_what_the_fast_path_maps(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "script.leaving", "goodbye")
        assert calls == [("script", "turn_on", "script.leaving")]
        answer, calls = self.activate(monkeypatch, "script.good_night", "time for bed")
        assert calls == [("script", "turn_on", "script.good_night")]

    def test_a_phrase_of_another_routine_does_not_ground_this_one(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "script.unlock_all_at_night", "time for bed")
        assert calls == []

    def test_words_like_scene_and_routine_never_ground_anything(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "script.unlock_all_at_night", "run the scene routine script mode")
        assert calls == []

    def test_a_non_text_or_unknown_domain_entity_is_not_activated_or_echoed(self, monkeypatch):
        answer, calls = self.activate(monkeypatch, "lock.front_door", "unlock the front door")
        assert calls == [] and "lock.front_door" not in answer
        answer, calls = self.activate(monkeypatch, ["script.good_night"], "good night")
        assert calls == []

    def test_every_fast_path_phrase_grounds_its_own_entity(self, monkeypatch):
        rig = DoorRig(monkeypatch, {})
        for phrase, entity in shc.SCENE_PHRASES.items():
            assert asyncio.run(rig.controller._scene_grounded(entity, f"please {phrase} now")), phrase


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------

def step(action, target, **extra):
    return {"action": action, "target": target, "parameters": {}, "delay_after": 0, "at_time": None, **extra}


class SequenceRig(DoorRig):
    def plan(self, steps, query):
        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query)):
                return await self.controller.decide_sequence_rooms(steps, query, None)

        return asyncio.run(go())

    def run_plan(self, steps, query, perms=None, mode="owner"):
        """Plan, then run the executor synchronously under the same scope."""
        executor = SequenceExecutor(self.controller, self.raw)

        async def go():
            with mp.ha_permission_scope(perms or {"mode": "owner"}, mode=mode, utterance=classify_utterance(query)):
                text = await self.controller.decide_sequence_rooms(steps, query, None)
                if text is not None:
                    return text
                await executor.execute_sequence(steps, session_id=None, background=False)
                return None

        return asyncio.run(go())


class TestSequences:
    def test_a_model_decided_access_is_stripped_and_asks_which_door(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": None, "decided_access": "lock.back_door",
                                 "decided_access_src": "replay", "decided_access_at": 9e12})]
        text = rig.run_plan(steps, "unlock the door and then turn on the lights")
        assert rig.calls == [] and text.startswith("Which door")

    def test_unlock_the_front_door_and_the_back_door_is_refused(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "front_door"}),
                 step("unlock", {"device_type": "lock", "room": "back_door"})]
        text = rig.run_plan(steps, "unlock the front door and the back door")
        assert rig.calls == [] and text == "I can unlock or open one door at a time."

    def test_one_named_door_in_a_sequence_unlocks_exactly_it(self, monkeypatch):
        rig = SequenceRig(monkeypatch, {**THREE_LOCKS, "light.den": {"state": "off", "attributes": {"friendly_name": "Den"}}})
        steps = [step("turn_on", {"device_type": "light", "entity_id": "light.den"}),
                 step("unlock", {"device_type": "lock", "room": "front_door"})]
        text = rig.run_plan(steps, "turn on the den light then unlock the front door")
        assert text is None
        assert rig.openings == [("lock", "unlock", "lock.front_door")]
        assert "entity_id" not in steps[1]["target"] and steps[1]["target"]["decided_access"] == "lock.front_door"

    def test_a_model_entity_id_for_a_door_is_grounded_or_refused(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        # The model's id is a guess: the words say front, so front is what opens.
        steps = [step("unlock", {"device_type": "lock", "entity_id": "lock.back_door"})]
        assert rig.run_plan(steps, "unlock the front door") is None
        assert rig.openings == [("lock", "unlock", "lock.front_door")]
        # The words name no door: the model's id opens nothing.
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "entity_id": "lock.back_door"})]
        assert rig.run_plan(steps, "unlock the door").startswith("Which door")
        assert rig.calls == []
        # A model id the words contradict is not found.
        steps = [step("unlock", {"device_type": "lock", "entity_id": "lock.shed_door"})]
        assert rig.run_plan(steps, "unlock the shed door").startswith("I couldn't find a door called shed")
        assert rig.calls == []

    @pytest.mark.parametrize("action", ["turn_off", "unlock", "toggle"])
    def test_a_light_typed_step_carrying_a_lock_is_refused_at_planning_and_execution(self, monkeypatch, action):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step(action, {"device_type": "light", "entity_id": "lock.front_door"})]
        assert rig.plan(steps, "unlock the front door") == shc.ONE_DOOR_REFUSAL
        # Execution alone (planning skipped) refuses too:
        executor = SequenceExecutor(rig.controller, rig.raw)

        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                return await executor._execute_step(steps[0], 1, 1)

        assert asyncio.run(go()) == ONE_DOOR_STEP_REFUSAL
        assert rig.calls == []

    def test_execution_refuses_an_opening_step_with_no_planned_decision(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        executor = SequenceExecutor(rig.controller, rig.raw)
        for target in ({"device_type": "lock", "entity_id": "lock.front_door"}, {"device_type": "lock", "room": "front_door"},
                       {"device_type": "light", "entity_id": "lock.front_door"}):
            async def go():
                with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
                    return await executor._execute_step(step("unlock", target), 1, 1)

            assert asyncio.run(go()) == ONE_DOOR_STEP_REFUSAL
        assert rig.calls == []

    def test_an_opening_step_with_a_time_is_refused(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "front_door"}, at_time="07:00")]
        text = rig.plan(steps, "unlock the front door at 7am")
        assert text == shc.SCHEDULED_OPENING_REFUSAL and rig.calls == []

    def test_an_opening_after_a_delay_is_refused(self, monkeypatch):
        rig = SequenceRig(monkeypatch, {**THREE_LOCKS, "light.den": {"state": "off", "attributes": {"friendly_name": "Den"}}})
        steps = [step("turn_on", {"device_type": "light", "entity_id": "light.den"}, delay_after=30),
                 step("unlock", {"device_type": "lock", "room": "front_door"})]
        assert rig.plan(steps, "turn on the den light then in 30 seconds unlock the front door") == shc.SCHEDULED_OPENING_REFUSAL
        steps = [step("turn_on", {"device_type": "light", "entity_id": "light.den"}, at_time="19:00"),
                 step("unlock", {"device_type": "lock", "room": "front_door"})]
        assert rig.plan(steps, "at 7pm turn on the den light then unlock the front door") == shc.SCHEDULED_OPENING_REFUSAL

    def test_a_delay_after_the_opening_is_fine(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "front_door"}, delay_after=0)]
        assert rig.plan(steps, "unlock the front door") is None

    def test_a_locking_step_names_its_lock(self, monkeypatch):
        """Locking has no age or count limit, but the planned step still locks the one named."""
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("lock", {"device_type": "lock", "room": "front_door"})]
        assert rig.run_plan(steps, "lock the front door in a minute") is None
        assert rig.calls == [("lock", "lock", "lock.front_door")]

    def test_a_target_is_rebuilt_from_an_allowlist(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("turn_on", {"device_type": "light", "room": "den", "evil": "x", **FORGED})]
        rig.plan(steps, "turn on the den light")
        assert "evil" not in steps[0]["target"]
        assert "decided_access" not in steps[0]["target"]
        assert steps[0]["target"]["device_type"] == "light"

    def test_blinds_steps_are_not_openings(self, monkeypatch):
        rig = SequenceRig(monkeypatch, {**BLINDS, "cover.main": cover_state("Main", "garage")})
        steps = [step("open", {"device_type": "cover", "room": "kitchen"}, delay_after=5),
                 step("open", {"device_type": "cover", "room": "den"})]
        assert rig.plan(steps, "open the kitchen blinds then open the den blinds") is None

    def test_a_guest_sequence_with_a_lock_step_is_left_to_the_pre_authorization(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "front_door"})]
        decision = mp.authorize_sequence(steps, guest())
        assert decision.allowed is False
        rig.run_plan(steps, "unlock the front door", perms=guest(), mode="guest")
        assert rig.calls == [], "even with the pre-authorization skipped, the guard refuses the write"


# ---------------------------------------------------------------------------
# Multi-turn: the answer to "which door?", and a follow-up that carries a door
# ---------------------------------------------------------------------------

READ_REPLY = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
REF = {"has_context_ref": True, "ref_types": ["pronoun"], "anaphora_types": ["pronoun"], "is_inquiry": False}
DOOR_STATES = {
    "lock.front_door": lock_state("Front Deadbolt"),
    "lock.back_door": lock_state("Back Door Lock"),
    "lock.side_door": lock_state("Side Door Lock"),
}


class DoorChat:
    def __init__(self, monkeypatch, states=None):
        self.chat = Chat(monkeypatch)
        rig = self.chat.rig
        rig.manager._entities_cache = dict(states or DOOR_STATES)
        rig.manager._build_indexes()
        rig.registry._snapshot = None
        rig.registry.state = "unavailable"
        self.now = [1_000_000.0]
        monkeypatch.setattr(shc, "_wall_clock", lambda: self.now[0])
        self.chat.clock = self.now
        monkeypatch.setattr(room_choice, "_now", lambda: self.now[0])
        self.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}

    def say(self, query, **kw):
        kw.setdefault("room", None)
        return self.chat.turn(query, **kw)

    @property
    def calls(self):
        return [(c.args[0], c.args[1], (c.args[2] or {}).get("entity_id")) for c in self.chat.rig.raw.call_service.await_args_list]

    @property
    def openings(self):
        return [c for c in self.calls if c[:2] in OPENING_CALLS]

    def reset(self):
        self.chat.reset_writes()


class TestWhichDoor:
    def test_the_answer_unlocks_the_chosen_door_only(self, monkeypatch):
        door = DoorChat(monkeypatch)
        out = door.say("unlock the door")
        assert out.answer.startswith("Which door:") and door.calls == []
        pending = door.chat.stored_parameters()["awaiting_room_choice"]
        assert pending["kind"] == "access" and pending["fingerprint"] == "fp-a"
        assert sorted(c[0] for c in pending["candidates"]) == sorted(DOOR_STATES)
        out = door.say("the front door")
        assert door.openings == [("lock", "unlock", "lock.front_door")], out.answer
        stored = door.chat.stored_parameters()
        assert stored["decided_access"] == "lock.front_door" and stored["decided_access_fp"] == "fp-a"

    def test_another_callers_answer_unlocks_nothing_and_leaves_the_pending(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the door")
        before = json.dumps(door.chat.store["s1"], sort_keys=True)
        door.chat.llm.reply = READ_REPLY
        door.say("the front door", fp="fp-intruder")
        assert door.calls == [] and json.dumps(door.chat.store["s1"], sort_keys=True) == before
        door.say("the front door")
        assert door.openings == [("lock", "unlock", "lock.front_door")], "the real caller can still answer"

    def test_a_stale_decision_in_the_stored_intent_does_not_reach_the_replay(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the door")
        pending = door.chat.store["s1"]["parameters"]["awaiting_room_choice"]
        pending["intent"].update({"decided_room": "kitchen", "unprocessable": True, "decided_access": "lock.back_door",
                                  "decided_access_fp": "someone-else", "decided_access_src": "replay"})
        door.say("the front door")
        assert door.openings == [("lock", "unlock", "lock.front_door")]
        stored = door.chat.stored_parameters()
        assert "decided_room" not in stored and "unprocessable" not in stored
        assert stored["decided_access_fp"] == "fp-a"

    def test_the_answer_is_one_use(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the door")
        door.say("front")
        door.reset()
        door.say("front")
        assert door.openings == []

    def test_a_late_answer_unlocks_nothing(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the door")
        door.now[0] += room_choice.TTL + 1
        door.chat.llm.reply = READ_REPLY
        door.say("the front door")
        assert door.openings == []

    def test_a_reply_naming_no_candidate_does_not_unlock(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the door")
        door.say("the garage")
        assert door.openings == []

    def test_a_command_reply_cancels_it(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the door")
        door.chat.llm.reply = {"device_type": "light", "room": None, "action": "turn_off", "target_scope": "group", "parameters": {}}
        door.say("turn off the lights")
        assert door.openings == []
        assert "awaiting_room_choice" not in door.chat.stored_parameters() or (
            door.chat.stored_parameters()["awaiting_room_choice"]["kind"] != "access"
        )

    def test_a_guest_who_answers_is_refused(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the door")
        door.say("the front door", perms=guest(), mode="guest")
        assert door.openings == []


class TestCarryRules:
    """The carry rules as functions, apart from the multi-turn rig."""

    AUD = "household"

    def earlier(self, **over):
        base = {"device_type": "lock", "decided_access": "lock.front_door", "decided_access_fp": "fp-a",
                "decided_access_aud": self.AUD, "decided_access_src": "status", "decided_access_at": 5.0}
        base.update(over)
        return base

    def carry(self, earlier, *, device_type="lock", fp="fp-a", aud=None):
        from orchestrator.nodes import route_control as rc

        intent = {"device_type": device_type}
        rc._carry_decision(intent, earlier, fingerprint=fp, audience=aud or self.AUD)
        return intent

    def test_the_same_caller_audience_and_kind_carries_all_five_keys(self):
        intent = self.carry(self.earlier())
        assert intent["decided_access"] == "lock.front_door"
        assert {k for k in intent if k.startswith("decided_access")} == {
            "decided_access", "decided_access_fp", "decided_access_aud", "decided_access_src", "decided_access_at"}

    def test_each_condition_alone_refuses_the_carry(self):
        assert "decided_access" not in self.carry(self.earlier(), fp="fp-b")
        assert "decided_access" not in self.carry(self.earlier(), fp=None)
        assert "decided_access" not in self.carry(self.earlier(decided_access_fp=None), fp=None)
        assert "decided_access" not in self.carry(self.earlier(), aud="owner")
        assert "decided_access" not in self.carry(self.earlier(device_type="cover"))
        assert "decided_access" not in self.carry(self.earlier(), device_type="cover")
        assert "decided_access" not in self.carry(self.earlier(decided_access="cover.front_door"))
        assert "decided_access" not in self.carry(self.earlier(decided_access=["lock.front_door"]))
        assert "decided_access" not in self.carry(self.earlier(), device_type="light")

    def test_a_carried_id_must_be_a_permitted_door_of_the_kind(self):
        honoured = shc.SmartHomeController._honoured_carry
        intent = {"decided_access": "lock.front_door", "decided_access_src": "replay"}
        assert honoured(intent, "lock", ["lock.front_door"], opening=True) == "lock.front_door"
        assert honoured(intent, "lock", ["lock.back_door"], opening=True) is None
        assert honoured(intent, "cover", ["lock.front_door"], opening=True) is None
        assert honoured({**intent, "decided_access_src": "model"}, "lock", ["lock.front_door"], opening=True) is None
        assert honoured({}, "lock", ["lock.front_door"], opening=True) is None
        assert honoured(None, "lock", ["lock.front_door"], opening=True) is None

    def test_drop_decision_removes_every_server_key(self):
        from orchestrator.nodes import route_control as rc

        intent = {"device_type": "lock", "room": "x", **{k: "v" for k in room_context.SERVER_DECISION_KEYS}}
        rc._drop_decision(intent)
        assert intent == {"device_type": "lock", "room": "x"}


class TestCarry:
    def status_then_unlock(self, door, **second):
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked")
        assert door.chat.stored_parameters()["decided_access"] == "lock.front_door"
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        return door.say("unlock it", context_ref_info=REF, **second)

    def test_unlock_it_after_a_status_read_of_one_door_unlocks_that_door(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.now[0] += 10
        self.status_then_unlock(door)
        assert door.openings == [("lock", "unlock", "lock.front_door")]

    def test_a_status_carry_older_than_a_minute_asks_instead(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked")
        door.now[0] += shc.ACCESS_CARRY_SECONDS + 1
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        out = door.say("unlock it", context_ref_info=REF)
        assert door.openings == [] and out.answer.startswith("Which door")

    def test_exactly_at_the_limit_still_carries(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked")
        door.now[0] += shc.ACCESS_CARRY_SECONDS
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        door.say("unlock it", context_ref_info=REF)
        assert door.openings == [("lock", "unlock", "lock.front_door")]

    def test_a_different_fingerprint_does_not_inherit_the_door(self, monkeypatch):
        door = DoorChat(monkeypatch)
        self.status_then_unlock(door, fp="fp-other")
        assert door.openings == []

    def test_a_missing_fingerprint_never_carries(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked", fp=None)
        assert door.chat.stored_parameters().get("decided_access_fp") is None
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        door.say("unlock it", context_ref_info=REF, fp=None)
        assert door.openings == []

    def test_a_different_audience_does_not_inherit_the_door(self, monkeypatch):
        from shared.knowledge_tiers import KnowledgeAudience

        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked")
        proven = KnowledgeAudience(mode="owner", degraded=False, public=False, owner_caller=True, owner_proven=True)

        async def turn():
            from orchestrator.nodes.route_control import route_control_node
            state = door.chat.state("unlock it", room=None, context_ref_info=REF)
            state.knowledge_audience = proven
            return await route_control_node(state)

        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        with door.chat.patched():
            asyncio.run(turn())
        assert door.openings == []

    def test_lock_the_front_door_then_unlock_it_asks_and_writes_nothing(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": "front_door", "action": "lock", "target_scope": "group", "parameters": {}}
        door.say("lock the front door")
        assert door.calls == [("lock", "lock", "lock.front_door")]
        assert "decided_access" not in door.chat.stored_parameters()
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        out = door.say("unlock it", context_ref_info=REF)
        assert door.calls == [] and out.answer.startswith("Which door")

    def test_locking_after_an_unlock_leaves_nothing_to_carry(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the front door")
        assert door.chat.stored_parameters()["decided_access"] == "lock.front_door"
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "lock", "target_scope": "group", "parameters": {}}
        door.reset()
        door.say("lock it", context_ref_info=REF)
        assert door.calls == [("lock", "lock", "lock.front_door")], "the carry still says which door to lock"
        assert "decided_access" not in door.chat.stored_parameters()
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        out = door.say("unlock it", context_ref_info=REF)
        assert door.calls == [] and out.answer.startswith("Which door")

    def test_a_follow_up_naming_another_door_uses_the_words(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked")
        door.chat.llm.reply = {"device_type": "lock", "room": "back_door", "action": "unlock", "target_scope": "group", "parameters": {}}
        door.reset()
        door.say("unlock the back door instead", context_ref_info=REF)
        assert door.openings == [("lock", "unlock", "lock.back_door")]

    def test_a_models_forged_carry_in_a_follow_up_is_stripped(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "unlock", "target_scope": "group", "parameters": {},
                               "decided_access": "lock.back_door", "decided_access_src": "replay",
                               "decided_access_fp": "fp-a", "decided_access_aud": "other"}
        door.say("unlock it", context_ref_info=REF)
        assert door.openings == []
        door.say("unlock the door")
        assert door.openings == []

    def test_a_stored_carry_with_the_wrong_kind_of_door_is_dropped(self, monkeypatch):
        door = DoorChat(monkeypatch, {**DOOR_STATES, "cover.main": cover_state("Main", "garage")})
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked")
        door.chat.llm.reply = {"device_type": "cover", "room": None, "action": "open", "target_scope": "group", "parameters": {}}
        door.reset()
        door.say("open it", context_ref_info=REF)
        assert door.openings == []

    def test_the_fingerprint_and_audience_are_stamped_by_the_route(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.chat.llm.reply = {"device_type": "lock", "room": None, "action": "get_status", "target_scope": "group", "parameters": {}}
        door.say("is the front door locked")
        stored = door.chat.stored_parameters()
        assert stored["decided_access_src"] == "status" and stored["decided_access_fp"] == "fp-a"
        assert stored["decided_access_aud"] and isinstance(stored["decided_access_at"], float)

    def test_an_opening_leaves_a_carry_with_src_open(self, monkeypatch):
        door = DoorChat(monkeypatch)
        door.say("unlock the front door")
        assert door.openings == [("lock", "unlock", "lock.front_door")]
        stored = door.chat.stored_parameters()
        assert stored["decided_access"] == "lock.front_door" and stored["decided_access_src"] == "open"
