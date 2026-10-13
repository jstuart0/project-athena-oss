"""Sequence steps and door wording: exclusions, clauses, and the cover words.

D15: a whole-house step of a sequence spares what the words spare (the
server's parse, never the model's list) or asks. D30: one word set for cover
wording and cover classification, a cover step reads its own clause, and the
"lock" noun after an opening verb is a verb once a negator or an object
follows it.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import access_target, room_context  # noqa: E402
from orchestrator import mode_permission as mp  # noqa: E402
from orchestrator.sequence_executor import SequenceExecutor  # noqa: E402

from .test_access_intent_policy import (  # noqa: E402,F401
    THREE_LOCKS, DoorRig, SequenceRig, cover_intent, cover_state, lock_intent, lock_state, step,
)
from .test_room_light_area_tier import GALLEY_LEAVES, Rig, _isolated, _state, guest, light  # noqa: E402,F401
from orchestrator.utterance_kind import classify_utterance  # noqa: E402

HALL = {"light.hall_lamp"}


def light_intent(action, **extra):
    return light("whole_house", action, **extra)


def guest_perms():
    return guest(allowed_intents=["control"])


def _light_step(action, room="whole_house", **extra):
    return {"action": action, "target": {"device_type": "light", "room": room, **extra},
            "parameters": {}, "delay_after": 0, "at_time": None}


def _rig(monkeypatch, **kw):
    """The light rig with the fan-out limits off."""
    return Rig(monkeypatch, threshold=0, hard_limit=0, **kw)


class SequenceLightRig(Rig):
    def __init__(self, monkeypatch, **kw):
        super().__init__(monkeypatch, threshold=0, hard_limit=0, **kw)

    def plan(self, steps, query):
        async def go():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query)):
                return await self.controller.decide_sequence_rooms(steps, query, None)

        return asyncio.run(go())

    def run(self, steps, query):
        executor = SequenceExecutor(self.controller, self.raw)

        async def go():
            # A vouched voice caller: the whole house reaches every area's lamps.
            with mp.ha_permission_scope(
                {"mode": "owner"}, mode="owner", utterance=classify_utterance(query), trusted_voice=True,
            ):
                text = await self.controller.decide_sequence_rooms(steps, query, None)
                if text is not None:
                    return text
                await executor.execute_sequence(steps, session_id=None, background=False)

        return asyncio.run(go())

    @property
    def written(self):
        out = set()
        for call in self.raw.call_service.await_args_list:
            data = call.args[2] if len(call.args) > 2 else {}
            eid = (data or {}).get("entity_id")
            out.update(eid if isinstance(eid, list) else [eid])
        return out


# ---------------------------------------------------------------------------
# D15: whole-house steps keep exclusions
# ---------------------------------------------------------------------------

class TestWholeHouseStepKeepsExclusions:
    QUERY = "Turn off all the lights except the galley, then turn on the hall in 5 minutes"

    def test_the_galley_is_spared_and_the_plan_says_so(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_on", room="hall")]
        assert rig.plan(steps, self.QUERY) is None
        assert steps[0]["target"]["decided_excluded"] == ["galley"]
        assert steps[0]["target"]["excluded_rooms"] == ["galley"]
        assert steps[1]["target"].get("decided_excluded") in (None, [])

    def test_the_galley_is_never_written(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off")]
        assert rig.run(steps, "Turn off all the lights except the galley, then turn on the hall") is None
        assert rig.written, "nothing was written at all"
        assert not (rig.written & GALLEY_LEAVES), rig.written
        assert {"light.hall_lamp", "light.study_lamp"} <= rig.written

    def test_the_server_parse_wins_over_a_model_list(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off", excluded_rooms=["study"])]
        assert rig.plan(steps, "turn off all the lights except the galley") is None
        assert steps[0]["target"]["decided_excluded"] == ["galley"]
        assert steps[0]["target"]["excluded_rooms"] == ["galley"]

    def test_a_model_list_with_no_exclusion_wording_is_dropped(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off", excluded_rooms=["study"])]
        assert rig.plan(steps, "turn off all the lights") is None
        assert "excluded_rooms" not in steps[0]["target"]
        assert steps[0]["target"]["decided_excluded"] == []

    def test_no_exclusion_wording_is_the_whole_house_as_before(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off")]
        assert rig.plan(steps, "turn off all the lights") is None
        assert steps[0]["target"]["decided_room"] == "whole_house"

    @pytest.mark.parametrize("query", [
        "turn off all the lights without the galley, then turn on the hall",
        "turn off all the lights then keep the hall lit",
        "turn off all the lights and leave the porch on",
    ])
    def test_wording_the_parser_cannot_read_asks_and_writes_nothing(self, monkeypatch, query):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_on", room="hall")]
        text = rig.run(steps, query)
        assert text == "Which room?", text
        assert rig.written == set() or rig.written == {None}
        assert rig.raw.call_service.await_count == 0

    def test_one_unreadable_clause_asks_even_when_another_clause_is_readable(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_off")]
        query = "turn off all the lights without the galley, then turn off all the lights except the study"
        assert rig.run(steps, query) == "Which room?"
        assert rig.raw.call_service.await_count == 0

    def test_a_following_step_after_and_is_not_part_of_the_exclusion(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_on", room="hall")]
        assert rig.plan(steps, "turn off all the lights except the galley and turn on the hall") is None
        assert steps[0]["target"]["decided_excluded"] == ["galley"]

    def test_a_list_of_spared_rooms_is_all_carried(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_on", room="hall")]
        assert rig.plan(steps, "turn off all the lights except the galley and the study, then turn on the hall") is None
        assert sorted(steps[0]["target"]["decided_excluded"]) == ["galley", "study"]

    def test_the_model_chose_the_whole_house_for_words_that_do_not_say_it(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off")]
        assert rig.plan(steps, "turn off the lights") == "Which room?"


class TestSequenceWholeHouseAuthority:
    """room_context.sequence_whole_house is the one reader of a sequence's
    exclusions: the single-command authority applied per clause."""

    def test_it_agrees_with_decide_room_on_a_single_clause(self):
        for text in ("turn off all the lights except the galley", "turn off all the lights",
                     "turn off all the lights without the galley", "turn off all the lights in the kitchen"):
            single = room_context.whole_house_decision(text)
            seq = room_context.sequence_whole_house(text)
            if isinstance(single, room_context.WholeHouse):
                assert len(seq) == 1 and {r.key for r in single.excluded} == {r.key for r in seq[0].excluded}, text
            else:
                assert isinstance(seq, room_context.AskRoom), text

    HOUSE = room_context.RoomDirectory.build(areas=[
        type("A", (), {"area_id": f"area_{k}", "name": k.title(), "aliases": (), "key": k})()
        for k in ("galley", "study", "hall")
    ])

    def test_a_comma_does_not_end_a_step(self):
        text = "turn off all the lights except the galley, the study, then turn on the hall"
        seq = room_context.sequence_whole_house(text, self.HOUSE)
        assert len(seq) == 1 and {r.key for r in seq[0].excluded} == {"galley", "study"}

    PLAY = room_context.RoomDirectory.build(areas=[
        type("A", (), {"area_id": "area_play", "name": "Play Room", "aliases": (), "key": "play_room"})(),
        type("A", (), {"area_id": "area_kitchen", "name": "Kitchen", "aliases": (), "key": "kitchen"})(),
    ])

    def test_and_play_room_is_a_room_not_a_step(self):
        seq = room_context.sequence_whole_house("turn off all the lights except the kitchen and play room", self.PLAY)
        assert len(seq) == 1 and {r.key for r in seq[0].excluded} == {"kitchen", "play_room"}

    def test_and_play_music_is_a_step(self):
        clauses = room_context.step_clauses("turn off all the lights except the kitchen and play some music", self.PLAY)
        assert len(clauses) == 2
        seq = room_context.sequence_whole_house("turn off all the lights except the kitchen and play some music", self.PLAY)
        assert len(seq) == 1 and {r.key for r in seq[0].excluded} == {"kitchen"}

    def test_but_and_a_non_light_verb_is_a_second_command(self):
        seq = room_context.sequence_whole_house("turn off all the lights but lock the front door")
        assert len(seq) == 1 and seq[0].excluded == ()
        assert room_context.has_exclusion("turn off all the lights but lock the front door") is False

    def test_but_a_non_light_command_naming_a_room_is_its_own_step(self):
        seq = room_context.sequence_whole_house("turn off all the lights but lock the hall door", self.HOUSE)
        assert len(seq) == 1 and seq[0].excluded == ()

    def test_but_and_a_light_verb_is_still_an_exclusion(self):
        assert room_context.has_exclusion("turn on all the lights but turn off the kitchen") is True

    def test_each_whole_house_clause_has_its_own_exclusions(self):
        seq = room_context.sequence_whole_house(
            "turn off all the lights except the galley, then at 11 turn off all the lights", self.HOUSE,
        )
        assert [[r.key for r in d.excluded] for d in seq] == [["galley"], []]

    def test_a_clause_with_a_room_and_no_exclusion_asks(self):
        assert isinstance(room_context.sequence_whole_house("turn off all the lights in the kitchen"), room_context.AskRoom)

    def test_no_clause_that_says_the_house_asks(self):
        assert isinstance(room_context.sequence_whole_house("turn off the hall lights"), room_context.AskRoom)


class TestPerStepAndKitchenSequences:
    def test_the_second_whole_house_step_includes_the_galley(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_off")]
        assert rig.plan(steps, "turn off all the lights except the galley, then at 11 turn off all the lights") is None
        assert steps[0]["target"]["decided_excluded"] == ["galley"]
        assert steps[1]["target"]["decided_excluded"] == []
        assert "excluded_rooms" not in steps[1]["target"]

    def test_a_clause_count_that_does_not_match_the_steps_asks(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_off"), _light_step("turn_off")]
        text = rig.plan(steps, "turn off all the lights except the galley, then at 11 turn off all the lights")
        assert text == "Which room?"

    def test_a_kitchen_clause_with_a_model_whole_house_step_never_writes_the_house(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off"), _light_step("turn_on", room="hall")]
        text = rig.run(steps, "turn off all the lights in the kitchen, then turn on the hall")
        assert text == "Which room?" and rig.raw.call_service.await_count == 0

    def test_but_lock_the_front_door_turns_off_everything_and_locks(self, monkeypatch):
        rig = SequenceLightRig(monkeypatch)
        steps = [_light_step("turn_off")]
        assert rig.plan(steps, "turn off all the lights but lock the front door") is None
        assert steps[0]["target"]["decided_excluded"] == []


# ---------------------------------------------------------------------------
# D32: a group never reaches a bulb of a room the speaker left out
# ---------------------------------------------------------------------------

class TestWholeHouseExclusionNeverLeaksThroughAGroup:
    def _written(self, rig, perms=None, mode="owner", trusted=True):
        rig.run(light_intent("turn_off", excluded_rooms=["galley"]), "turn off all the lights except the galley",
                perms, mode, trusted)
        return set(rig.written)

    def test_an_untrusted_owner_does_not_write_a_galley_bulb_through_the_hall_group(self, monkeypatch):
        rig = _rig(monkeypatch, attic_state="on")
        written = self._written(rig, trusted=False)
        assert "light.hall_lamp" in written
        assert not written & GALLEY_LEAVES, written

    def test_a_trusted_owner_is_unchanged(self, monkeypatch):
        rig = _rig(monkeypatch, attic_state="on")
        written = self._written(rig)
        assert "light.study_lamp" in written and not written & GALLEY_LEAVES

    def test_a_guest_does_not_write_a_galley_bulb_through_a_configured_group(self, monkeypatch):
        rig = _rig(monkeypatch, attic_state="on", groups='{"hall": "light.hall_pair", "study": "light.study_lamp"}')
        written = self._written(rig, guest_perms(), "guest")
        assert "light.hall_lamp" in written and "light.study_lamp" in written
        assert not written & GALLEY_LEAVES, written

    def test_the_same_with_no_registry(self, monkeypatch):
        rig = _rig(monkeypatch, registry=False, groups='{"galley": "light.galley_all"}')
        written = self._written(rig, trusted=False)
        assert "light.hall_lamp" in written and not written & GALLEY_LEAVES, written

    def test_a_bulb_in_the_excluded_area_that_the_room_group_does_not_list_is_still_spared(self, monkeypatch):
        from .test_room_light_area_tier import _reg, house
        raw, states = house(attic_state="on")
        raw["entities"].append(_reg("light.counter_strip", area="area_galley"))
        states["light.counter_strip"] = _state("light.counter_strip")
        states["light.hall_extra"] = _state("light.hall_extra", members=["light.hall_lamp", "light.counter_strip"])
        rig = _rig(monkeypatch, attic_state="on", states=states, groups='{"galley": "light.galley_pair"}')
        from shared import ha_registry as hr
        rig.registry._snapshot = hr.RegistrySnapshot.from_raw(raw, fetched_at=1.0)
        written = self._written(rig, trusted=False)
        assert "light.hall_lamp" in written
        assert "light.counter_strip" not in written, written

    def test_excluding_den_does_not_drop_a_garden_group(self, monkeypatch):
        states = {
            "light.den_lamp": _state("light.den_lamp"), "light.garden_lamp": _state("light.garden_lamp"),
            "light.den": _state("light.den", members=["light.den_lamp"], name="Den"),
            "light.garden": _state("light.garden", members=["light.garden_lamp"], name="Garden"),
        }
        rig = _rig(monkeypatch, registry=False, states=states)
        rig.run(light_intent("turn_off", excluded_rooms=["den"]), "turn off all the lights except the den", None, "owner", False)
        assert "light.garden_lamp" in rig.written and "light.den_lamp" not in rig.written, rig.written


# ---------------------------------------------------------------------------
# D30 (a): one word set for cover wording and cover classification
# ---------------------------------------------------------------------------

BAY = {"cover.bay_window": cover_state("Bay Window")}
SHUTTER = {"cover.workshop_shutter": cover_state("Workshop Shutter")}


class TestWindowWordedDoorClassedCovers:
    def test_the_two_word_sets_differ_only_by_the_named_device_class_only_words(self):
        assert access_target.NON_ACCESS_COVER_WORDS - access_target._NAME_PLAIN_WORDS == access_target.DEVICE_CLASS_ONLY_COVER_WORDS
        assert access_target._NAME_PLAIN_WORDS < access_target.NON_ACCESS_COVER_WORDS
        assert mp.NAME_PLAIN_COVER_WORDS == access_target._NAME_PLAIN_WORDS

    @pytest.mark.parametrize("name,expected", [
        ("Bay Window", True), ("Workshop Shutter", True), ("Living Room Drapes", False),
        ("Fireplace Damper", False), ("Living Room Blinds", False), ("Garage Window", True),
        ("Window Opener", True),
    ])
    def test_the_guard_and_the_resolver_classify_alike(self, name, expected):
        attrs = {"friendly_name": name}
        assert access_target.is_access_cover("cover.x", attrs) is expected
        assert mp.cover_is_access("cover.x", attrs) is expected

    @pytest.mark.parametrize("states,query", [
        (BAY, "close the bay window"), (SHUTTER, "close the workshop shutter"),
        ({**BAY, "cover.living_room_blinds": cover_state("Living Room Blinds", "blind")}, "close the bay window"),
    ])
    def test_close_reaches_it(self, monkeypatch, states, query):
        rig = DoorRig(monkeypatch, states)
        answer, _ = rig.run_scoped(cover_intent("close"), query)
        closed = [c for c in rig.calls if c[:2] == ("cover", "close_cover")]
        assert closed and closed[0][2] in ("cover.bay_window", "cover.workshop_shutter"), (answer, rig.calls)
        assert len(closed) == 1, rig.calls
        assert rig.openings == []

    def test_the_named_window_is_closed_and_not_every_blind(self, monkeypatch):
        states = {**BAY, "cover.living_room_blinds": cover_state("Living Room Blinds", "blind"),
                  "cover.den_blinds": cover_state("Den Blinds", "blind")}
        rig = DoorRig(monkeypatch, states)
        rig.run_scoped(cover_intent("close"), "close the bay window")
        assert [c[2] for c in rig.calls] == ["cover.bay_window"]

    def test_closing_the_windows_reaches_both_kinds(self, monkeypatch):
        states = {**BAY, "cover.den_window": cover_state("Den Window", "window")}
        rig = DoorRig(monkeypatch, states)
        rig.run_scoped(cover_intent("close"), "close the windows")
        assert {c[2] for c in rig.calls} == {"cover.bay_window", "cover.den_window"}

    @pytest.mark.parametrize("states,query,name", [
        (BAY, "is the bay window open", "Bay Window"), (SHUTTER, "what's the workshop shutter status", "Workshop Shutter"),
    ])
    def test_status_reaches_it(self, monkeypatch, states, query, name):
        rig = DoorRig(monkeypatch, states)
        answer, _ = rig.run_scoped(cover_intent("get_status"), query)
        assert name in answer and "couldn't find" not in answer, answer
        assert rig.calls == []

    @pytest.mark.parametrize("states,query,name", [
        (BAY, "open the bay window", "Bay Window"),
        (SHUTTER, "open the workshop shutter", "Workshop Shutter"),
        ({**BAY, "cover.living_room_blinds": cover_state("Living Room Blinds", "blind")}, "open the bay window", "Bay Window"),
    ])
    def test_open_goes_to_the_door_path_with_the_naming_hint(self, monkeypatch, states, query, name):
        rig = DoorRig(monkeypatch, states)
        answer, scope = rig.run_scoped(cover_intent("open"), query)
        assert rig.calls == [], rig.calls
        assert "couldn't find" not in answer, answer
        assert f"{name} door" in answer, answer

    def test_open_the_bay_window_by_its_full_name_opens_it(self, monkeypatch):
        rig = DoorRig(monkeypatch, BAY)
        rig.run_scoped(cover_intent("open"), "open the bay window door")
        assert rig.openings == [("cover", "open_cover", "cover.bay_window")]

    def test_only_a_window_door_with_no_plain_cover_still_gets_the_hint_for_windows(self, monkeypatch):
        rig = DoorRig(monkeypatch, BAY)
        answer, _ = rig.run_scoped(cover_intent("open"), "open the windows")
        assert rig.calls == [] and "couldn't find" not in answer

    def test_plain_blinds_still_open_by_room_and_words(self, monkeypatch):
        rig = DoorRig(monkeypatch, {**BAY, "cover.living_room_blinds": cover_state("Living Room Blinds")})
        rig.run_scoped(cover_intent("open", room="living room"), "open the living room blinds")
        assert [c[:3] for c in rig.calls] == [("cover", "open_cover", "cover.living_room_blinds")]

    def test_unclassified_drapes_are_plain_by_name_now(self, monkeypatch):
        rig = DoorRig(monkeypatch, {"cover.drapes": cover_state("Living Room Drapes")})
        rig.run_scoped(cover_intent("open", room="living room"), "open the living room drapes")
        assert [c[:3] for c in rig.calls] == [("cover", "open_cover", "cover.drapes")]

    def test_a_garage_with_a_bay_window_is_still_one_door_at_a_time(self, monkeypatch):
        states = {**BAY, "cover.garage": cover_state("Garage Door", "garage")}
        rig = DoorRig(monkeypatch, states)
        answer, _ = rig.run_scoped(cover_intent("open"), "open the garage door")
        assert rig.openings == [("cover", "open_cover", "cover.garage")]


# ---------------------------------------------------------------------------
# D30 (b): a cover step reads its own clause
# ---------------------------------------------------------------------------

BLINDS_AND_LOCKS = {**THREE_LOCKS, "cover.living_room_blinds": cover_state("Living Room Blinds", "blind"),
                    "cover.garage": cover_state("Garage Door", "garage")}


class TestEachStepReadsItsOwnClause:
    def test_open_the_blinds_and_unlock_the_front_door_runs_both(self, monkeypatch):
        rig = SequenceRig(monkeypatch, BLINDS_AND_LOCKS)
        steps = [step("open", {"device_type": "cover", "room": "living_room"}),
                 step("unlock", {"device_type": "lock", "room": "front_door"})]
        assert rig.run_plan(steps, "open the blinds and unlock the front door") is None
        assert ("cover", "open_cover", "cover.living_room_blinds") in rig.calls
        assert ("lock", "unlock", "lock.front_door") in rig.calls
        assert len(rig.calls) == 2, rig.calls

    def test_the_order_does_not_matter(self, monkeypatch):
        rig = SequenceRig(monkeypatch, BLINDS_AND_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "front_door"}),
                 step("open", {"device_type": "cover", "room": "living_room"})]
        assert rig.run_plan(steps, "unlock the front door and then open the blinds") is None
        assert ("lock", "unlock", "lock.front_door") in rig.calls
        assert ("cover", "open_cover", "cover.living_room_blinds") in rig.calls

    def test_two_openings_are_still_refused(self, monkeypatch):
        rig = SequenceRig(monkeypatch, BLINDS_AND_LOCKS)
        steps = [step("open", {"device_type": "cover", "room": "garage"}),
                 step("unlock", {"device_type": "lock", "room": "front_door"})]
        text = rig.run_plan(steps, "open the garage and unlock the front door")
        assert text == "I can unlock or open one door at a time." and rig.calls == []

    def test_a_garage_step_beside_a_blinds_clause_is_not_plain(self):
        plain = shc.SmartHomeController._cover_step_plain
        assert plain("open the blinds and unlock the front door", None) is True
        assert plain("open the garage and close the blinds", "garage") is False
        assert plain("open the blinds and open the garage door", None) is False
        assert plain("open the blinds", None) is True
        assert plain("unlock the front door", None) is False
        assert plain("", None) is False


# ---------------------------------------------------------------------------
# D30 (c): the "lock" noun exemption
# ---------------------------------------------------------------------------

class TestTheLockNounExemption:
    @pytest.mark.parametrize("query,ok", [
        ("unlock the front door lock", True),
        ("unlock the Front Door Lock", True),

        ("unlock the front door lock please", True),
        ("unlock the front door lock now thanks", True),
        ("unlock the front door lock back", False),
        ("unlock front door lock front", False),
        ("unlock the front door lock for me", True),
        ("unlock the lock on the front door", False),
        ("unlock front door lock back door", False),
        ("unlock the front door lock the back door", False),
        ("unlock the front door no lock it", False),
        ("unlock the front door lock the back door", False),
        ("unlock the front door lock it", False),
        ("unlock the front door lock them all", False),
        ("unlock the front door lock all", False),
        ("unlock the front door lock this", False),
        ("unlock the front door lock that", False),
        ("unlock the front door don't lock", False),
        ("unlock the front door not lock", False),
        ("unlock the front door, lock the back door", False),
    ])
    def test_opening_verb_ok(self, query, ok):
        assert shc.opening_verb_ok(query, "lock") is ok

    @pytest.mark.parametrize("query", [
        "unlock the front door no lock it",
        "unlock the front door lock the back door",
        "unlock the front door lock it",
    ])
    def test_nothing_unlocks_through_the_controller(self, monkeypatch, query):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        answer, _ = rig.run_scoped(lock_intent(room="front_door"), query)
        assert rig.openings == [], (answer, rig.calls)

    def test_nothing_unlocks_through_a_sequence(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "front_door"})]
        text = rig.run_plan(steps, "unlock the front door no lock it")
        assert rig.openings == [] and text == shc.VERB_UNCLEAR_TEXT

    def test_the_noun_still_unlocks(self, monkeypatch):
        rig = DoorRig(monkeypatch, THREE_LOCKS)
        rig.run_scoped(lock_intent(room="front_door"), "unlock the Front Door Lock")
        assert rig.openings == [("lock", "unlock", "lock.front_door")]


class TestTheNounAllowlistThroughASequence:
    def test_unlock_front_door_lock_back_door_does_not_open_the_back_door(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "back"})]
        text = rig.run_plan(steps, "unlock front door lock back door")
        assert rig.openings == [], rig.calls
        assert text == shc.VERB_UNCLEAR_TEXT

    def test_unlock_the_front_door_lock_still_opens_the_front_door(self, monkeypatch):
        rig = SequenceRig(monkeypatch, THREE_LOCKS)
        steps = [step("unlock", {"device_type": "lock", "room": "front_door"})]
        assert rig.run_plan(steps, "unlock the front door lock please") is None
        assert rig.openings == [("lock", "unlock", "lock.front_door")]


class TestCoverStepPlainIsFailSafe:
    plain = staticmethod(shc.SmartHomeController._cover_step_plain)

    def test_a_target_no_clause_says_is_not_plain(self):
        assert self.plain("open the blinds and unlock the front door", "workshop") is False

    def test_a_garage_target_with_no_blinds_clause_naming_it_is_not_plain(self):
        assert self.plain("open the blinds and unlock the front door", "garage") is False
        assert self.plain("open the garage blinds", "garage") is True

    def test_a_gate_target_is_not_plain_beside_a_blinds_clause(self):
        assert self.plain("open the blinds and the gate", "gate") is False

    def test_a_room_target_does_not_turn_blinds_into_a_door(self):
        assert self.plain("open the blinds and unlock the front door", "living_room") is True

    def test_a_non_text_target_falls_back_to_the_clauses(self):
        assert self.plain("open the blinds", 7) is True
        assert self.plain("open the blinds", None) is True
