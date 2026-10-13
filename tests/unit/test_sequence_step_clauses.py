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
from .test_room_light_area_tier import GALLEY_LEAVES, Rig, _isolated  # noqa: E402,F401
from orchestrator.utterance_kind import classify_utterance  # noqa: E402

HALL = {"light.hall_lamp"}


def _light_step(action, room="whole_house", **extra):
    return {"action": action, "target": {"device_type": "light", "room": room, **extra},
            "parameters": {}, "delay_after": 0, "at_time": None}


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
    exclusions, and it is the single-command authority applied per clause."""

    def test_it_agrees_with_decide_room_on_a_single_clause(self):
        for text in ("turn off all the lights except the galley", "turn off all the lights",
                     "turn off all the lights without the galley"):
            single = room_context.whole_house_decision(text)
            seq = room_context.sequence_whole_house(text)
            assert type(single) is type(seq), text
            if isinstance(single, room_context.WholeHouse):
                assert {r.key for r in single.excluded} == {r.key for r in seq.excluded}

    def test_a_comma_does_not_end_a_step(self):
        text = "turn off all the lights except the galley, the study, then turn on the hall"
        seq = room_context.sequence_whole_house(text)
        assert isinstance(seq, room_context.WholeHouse)


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
        ("unlock the lock on the front door", True),
        ("unlock the front door lock please", True),
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
