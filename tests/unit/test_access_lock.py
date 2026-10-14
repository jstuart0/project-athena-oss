"""A lock that is not a door (ATHENA-269): the predicate, and "all the locks"
reads and unnamed writes that leave such locks out."""
from __future__ import annotations

import asyncio
import unittest.mock as mock

import pytest

import orchestrator.smart_home_controller as shc
from orchestrator import access_target as at
from orchestrator import mode_permission as mp
from orchestrator.utterance_kind import classify_utterance

pytestmark = pytest.mark.filterwarnings("ignore")

DOORS = {
    "lock.front": {"state": "locked", "attributes": {"friendly_name": "Front Door"}},
    "lock.back": {"state": "unavailable", "attributes": {"friendly_name": "Back Door"}},
    "lock.side": {"state": "locked", "attributes": {"friendly_name": "Side Door"}},
    "lock.garage": {"state": "locked", "attributes": {"friendly_name": "Garage Entry"}},
}
DOOR_NOUN_OVERRIDES = [
    ("lock.a", "Front Door Keypad"), ("lock.b", "Side Panel Door"), ("lock.c", "Kids Room Door"),
    ("lock.d", "Garage Keypad"), ("lock.e", "Back Gate Child Lock"), ("lock.deadbolt_panel", "Panel"),
]
NOT_DOORS = {
    "lock.hp": {"state": "unlocked", "attributes": {"friendly_name": "Heat Pump Child Lock"}},
    "lock.split": {"state": "unlocked", "attributes": {"friendly_name": "Mini Split Lock"}},
    "lock.pad": {"state": "unlocked", "attributes": {"friendly_name": "Alarm Keypad"}},
    "lock.box": {"state": "unlocked", "attributes": {"friendly_name": "Gun Safe"}},
    "lock.cls": {"state": "unlocked", "attributes": {"friendly_name": "Odd", "device_class": "child_lock"}},
}


@pytest.mark.parametrize("entity_id,entity", sorted(DOORS.items()))
def test_doors_are_access_locks(entity_id, entity):
    assert at.is_access_lock(entity_id, entity["attributes"])


@pytest.mark.parametrize("entity_id,entity", sorted(NOT_DOORS.items()))
def test_named_member_that_is_not_a_door(entity_id, entity):
    assert not at.is_access_lock(entity_id, entity["attributes"])


def test_floor_and_named_members():
    assert len(DOORS) >= 4 and len(NOT_DOORS) >= 5
    assert not at.is_access_lock("lock.x", {"friendly_name": "Heat Pump Child Lock"})
    assert at.is_access_lock("lock.safe_room", {"friendly_name": "Safe Room Door"})
    assert at.is_access_lock("lock.unnamed", None)


class _Manager:
    def __init__(self, entities):
        self.entities = dict(entities)
        self.invalidated = []

    async def get_entities(self, force_refresh=False, max_age=None):
        return dict(self.entities)

    async def get_entity(self, entity_id, max_age=None):
        return self.entities.get(entity_id)

    def invalidate(self, ids):
        self.invalidated.extend(ids)


def _controller(entities):
    return shc.SmartHomeController(entity_manager=_Manager(entities), llm_router=mock.MagicMock())


def _ask(controller, action, query):
    ha = mock.MagicMock()
    ha.call_service = mock.AsyncMock(return_value=True)
    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query), trusted_voice=True):
            return await controller._handle_lock_intent(action, None, ha, query)

    return asyncio.run(go()), ha


def test_all_the_locks_leaves_out_child_and_keypad_locks_and_summarises():
    answer, _ = _ask(_controller({**DOORS, **NOT_DOORS}), "get_status", "are all the locks locked")
    assert answer.startswith("3 doors are locked; the back door is unavailable. I left out ")


def test_a_named_child_lock_can_still_be_read():
    answer, _ = _ask(_controller({**DOORS, **NOT_DOORS}), "get_status", "is the heat pump child lock locked")
    assert answer == "Heat Pump Child Lock is unlocked."


def test_lock_everything_never_writes_to_a_child_lock():
    controller = _controller({**DOORS, **NOT_DOORS})
    answer, ha = _ask(controller, "lock", "lock all the doors")
    written = {c.args[2]["entity_id"] for c in ha.call_service.call_args_list}
    assert written and written <= set(DOORS)
    assert set(controller.entity_manager.invalidated) == written


def test_only_non_door_locks_means_no_door_locks():
    answer, _ = _ask(_controller(NOT_DOORS), "get_status", "are all the locks locked")
    assert "door locks" in answer


def test_closing_a_cover_marks_it_stale_for_the_next_read():
    controller = _controller({"cover.garage": {"state": "open", "attributes": {"friendly_name": "Garage Door"}}})
    ha = mock.MagicMock()
    ha.call_service = mock.AsyncMock(return_value=True)

    async def go():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("close the garage door"),
                                    trusted_voice=True):
            return await controller._handle_cover_intent("close", None, ha, "close the garage door")

    assert asyncio.run(go()).startswith("Done!")
    assert controller.entity_manager.invalidated == ["cover.garage"]


@pytest.mark.parametrize("entity_id,name", DOOR_NOUN_OVERRIDES)
def test_a_door_noun_always_makes_a_lock_a_door(entity_id, name):
    assert at.is_access_lock(entity_id, {"friendly_name": name, "device_class": "keypad"})


def test_broad_wording_keeps_every_lock_and_door_wording_names_what_it_skipped():
    entities = {**DOORS, **NOT_DOORS}
    for query in ("lock up", "lock everything", "good night, lock up"):
        controller = _controller(entities)
        answer, ha = _ask(controller, "lock", query)
        written = {c.args[2]["entity_id"] for c in ha.call_service.call_args_list}
        assert written == set(entities), query
    controller = _controller(entities)
    answer, ha = _ask(controller, "lock", "lock all the doors")
    assert "I left out" in answer and "Heat Pump Child Lock" in answer
    answer, _ = _ask(_controller(entities), "get_status", "are the doors locked")
    assert answer.startswith("3 doors are locked; the back door is unavailable.") and "I left out" in answer
    answer, _ = _ask(_controller(entities), "get_status", "status of everything locked")
    assert "left out" not in answer


def _read(controller, perms, mode, query="are the doors locked"):
    async def go():
        with mp.ha_permission_scope(perms, mode=mode, utterance=classify_utterance(query), read_only=True):
            return await controller._handle_lock_intent("get_status", None, mock.MagicMock(), query)

    return asyncio.run(go())


RESTRICT_SIDE = [r"^lock\.side$"]


def test_public_and_unverified_audiences_count_only_permitted_locks():
    controller = _controller(DOORS)
    degraded = {"mode": "degraded", "restricted_entities": RESTRICT_SIDE, "allowed_domains": []}
    public = {"mode": "degraded", "audience": "public", "restricted_entities": RESTRICT_SIDE, "allowed_domains": []}
    assert _read(controller, degraded, "degraded") == "2 doors are locked; the back door is unavailable."
    assert _read(controller, public, "owner") == "2 doors are locked; the back door is unavailable."
    nothing = {"mode": "degraded", "restricted_entities": [r"^lock\."], "allowed_domains": []}
    assert _read(controller, nothing, "degraded") == shc.LOCK_READ_REFUSAL


def test_a_verified_guest_hears_the_count_and_the_owner_is_unchanged():
    controller = _controller(DOORS)
    assert _read(controller, {"mode": "owner"}, "owner") == "3 doors are locked; the back door is unavailable."
    guest_perms = mp.apply_guest_baseline({"mode": "guest"})
    answer = _read(controller, guest_perms, "guest")
    assert answer == "3 doors are locked; one is unavailable."  # counted; the restricted lock is never named
    assert "Back" not in answer and "Front" not in answer
