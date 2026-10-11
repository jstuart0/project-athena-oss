"""orchestrator.room_context: the room vocabulary, mentions, decisions, the
question and its answer. Fictional houses only.

House A: North/South/Lower Bathroom, Study (alias Den), Galley, Alpha Room,
Living Room (2) and an area named On (unmatchable). House B: Bathroom and
Master Bathroom. Table rows carry the query, the speaker room and the house
in their assertion messages.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from orchestrator import room_context as rc  # noqa: E402
from shared.ha_registry import Area, room_key  # noqa: E402


def area(name, aliases=()):
    return Area(f"area_{room_key(name)}", name, tuple(aliases), room_key(name))


HOUSE_A = rc.RoomDirectory.build(areas=[
    area("North Bathroom"), area("South Bathroom"), area("Lower Bathroom"),
    area("Study", ["Den"]), area("Galley"), area("Alpha Room"), area("Living Room (2)"), area("On"),
])
HOUSE_B = rc.RoomDirectory.build(areas=[area("Bathroom"), area("Master Bathroom")])
NORTH, SOUTH, LOWER = (rc.RoomRef(f"{n}_bathroom", f"{n} bathroom") for n in ("north", "south", "lower"))
GALLEY = rc.RoomRef("galley", "galley")


def decide(query, speaker=None, directory=HOUSE_A, **kwargs):
    mention = rc.find_room_mention(query, directory)
    return rc.decide_room(mention, query=query, speaker_room=speaker, directory=directory, **kwargs)


def keys(refs):
    return sorted(r.key for r in refs)


# ---------------------------------------------------------------------------
# Named rooms
# ---------------------------------------------------------------------------

def test_bathroom_resolves_to_the_speakers_bathroom_when_it_is_one_of_them():
    assert decide("turn on the bathroom lights", "south_bathroom") == rc.Room("south_bathroom", "south bathroom")


def test_bathroom_asks_which_when_speaker_is_in_another_room():
    got = decide("turn on the bathroom lights", "study")
    assert isinstance(got, rc.AskWhich) and keys(got.candidates) == keys([NORTH, SOUTH, LOWER]), got


def test_bathroom_asks_which_when_the_speaker_is_unknown():
    assert isinstance(decide("turn on the bathroom lights", None), rc.AskWhich)


def test_an_exact_name_beats_the_partial_matches_of_longer_names():
    assert decide("turn on the bathroom lights", "bathroom", HOUSE_B) == rc.Room("bathroom", "bathroom")
    assert decide("turn on the bathroom lights", None, HOUSE_B) == rc.Room("bathroom", "bathroom")


def test_master_bathroom_is_never_read_as_bathroom():
    got = decide("turn on the master bathroom lights", None, HOUSE_B)
    assert got == rc.Room("master_bathroom", "master bathroom"), got


def test_the_longest_phrase_wins_over_a_shorter_one_inside_it():
    mention = rc.find_room_mention("lights in the master bathroom", HOUSE_B)
    assert mention.phrase == "master bathroom" and mention.kind == "exact"


@pytest.mark.parametrize("query", ["turn off all the bathrooms", "dim both bathrooms", "every bathroom off"])
def test_a_plural_or_universal_room_phrase_is_all_its_candidates(query):
    got = decide(query, "study")
    assert isinstance(got, rc.Rooms) and sorted(got.keys) == keys([NORTH, SOUTH, LOWER]), (query, got)


def test_all_the_bathroom_lights_quantifies_the_lights_so_it_still_asks():
    assert isinstance(decide("turn on all the bathroom lights", "study"), rc.AskWhich)


def test_living_room_finds_the_numbered_area():
    assert decide("turn on the living room lights", None) == rc.Room("living_room_2", "living room 2")


def test_an_alias_names_its_room():
    assert decide("turn on the den lights", None) == rc.Room("study", "study")


def test_alpha_is_a_room_because_the_house_says_so_not_the_code():
    assert decide("turn on the alpha room lights", None) == rc.Room("alpha_room", "alpha room")
    assert "alpha" not in " ".join(rc.COMMON_ROOM_WORDS)
    assert rc.find_room_mention("turn on the alpha lights", rc.RoomDirectory.empty()).kind == "none"


def test_a_room_word_inside_another_word_is_not_a_mention():
    assert rc.find_room_mention("turn on the garden lights", rc.RoomDirectory.empty()).kind == "none"


def test_the_area_named_on_is_unmatchable():
    assert rc.find_room_mention("turn on the lights", HOUSE_A).kind == "none"
    assert HOUSE_A.unmatchable_count >= 1


def test_two_areas_with_one_key_match_no_phrase_but_still_resolve_a_speaker():
    twin = rc.RoomDirectory.build(areas=[area("Living-Room"), area("Living Room"), area("Study")])
    assert twin.collision_count == 1
    assert rc.find_room_mention("turn on the living room lights", twin).candidates == (
        rc.RoomRef("living_room", "living room"),
    ), "no registry entry matches it, so the generic word is recognized as said"
    assert twin.resolves("living_room") is not None


def test_a_group_derived_name_collapses_onto_the_area_it_names():
    directory = rc.RoomDirectory.build(areas=[area("Galley")], group_friendly_names=["Galley Lights", "Galley Lamp Group"])
    assert directory.keys() == ("galley",)


def test_group_names_alone_make_a_directory_when_the_registry_is_down():
    directory = rc.RoomDirectory.build(group_friendly_names=["Master Bathroom Lights", "Master Bathroom All"])
    assert directory.keys() == ("master_bathroom",)
    assert directory.resolves("master_bathroom") is not None


def test_room_groups_and_their_members_are_rooms():
    group = {"name": "first_floor", "display_name": "First Floor", "aliases": ["downstairs"],
             "members": [{"room_name": "kitchen", "display_name": "Kitchen"}]}
    directory = rc.RoomDirectory.build(room_groups=[group])
    assert directory.resolves("downstairs").key == "first_floor"
    assert directory.resolves("kitchen").key == "kitchen"
    assert directory.members_of("first_floor") == ("kitchen",)


# ---------------------------------------------------------------------------
# Whole-house wording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "all the lights", "turn on all lights", "ALL THE LIGHTS!", "all of the lights", "turn on every light in the house",
    "turn everything off", "lights off everywhere", "turn off the whole house", "entire house off",
    "turn off all the rooms", "every room off",
])
def test_whole_house_wording_is_explicit(query):
    assert rc.is_explicit_whole_house(query), query
    assert isinstance(decide(query, "study"), rc.WholeHouse), query


@pytest.mark.parametrize("query", [
    "turn on the small lamp", "it will fall", "call the plumber", "install the app",
    "turn on the all lights", "turn on the lights", "", "   ",
])
def test_ordinary_wording_is_not_whole_house(query):
    assert not rc.is_explicit_whole_house(query), query
    assert not isinstance(decide(query, "study"), rc.WholeHouse), query


def test_a_room_named_with_all_the_lights_wins():
    assert decide("turn off all the lights in the galley", "study") == rc.Room("galley", "galley")


def test_everything_in_a_named_room_means_that_room():
    assert decide("turn everything off in the study", "galley") == rc.Room("study", "study")


def test_a_room_quantifying_cue_beats_a_named_room():
    assert isinstance(decide("turn off the galley lights and the whole house", "galley"), rc.WholeHouse)


def test_all_the_lights_except_a_room_excludes_that_room():
    got = decide("turn off all the lights except the galley", "study")
    assert got == rc.WholeHouse((GALLEY,))


def test_an_ambiguous_exclusion_excludes_every_partial_match():
    got = decide("turn off all lights except the bathroom", "study")
    assert isinstance(got, rc.WholeHouse) and keys(got.excluded) == keys([NORTH, SOUTH, LOWER])


def test_all_the_lights_names_no_exclusions_by_default():
    assert decide("turn on all the lights", "study") == rc.WholeHouse(())


def test_non_english_text_asks_and_never_means_the_whole_house():
    got = decide("enciende todas las luces", None)
    assert got == rc.AskRoom(), got


def test_every_whole_house_wording_is_also_a_scope_cue_so_the_gate_agrees():
    samples = [
        "all the lights", "all lights", "all of the lights", "every light", "everything", "the whole house",
        "entire house", "every room", "all the rooms", "all rooms", "everywhere", "throughout the house",
        "all over the house", "ALL THE LIGHTS!",
    ]
    for sample in samples:
        assert rc.is_explicit_whole_house(sample), sample
        assert rc.SCOPE_CUE_RE.search(sample), f"scope cue misses {sample!r}"
    for pattern in (rc.WHOLE_HOUSE_LIGHT_RE, rc.WHOLE_HOUSE_ROOM_RE):
        for sample in samples:
            match = pattern.search(sample)
            if match:
                assert rc.SCOPE_CUE_RE.search(match.group(0)), (pattern.pattern, match.group(0))


# ---------------------------------------------------------------------------
# The speaker's room, follow-ups, asking
# ---------------------------------------------------------------------------

def test_the_speakers_room_is_the_default_when_the_directory_knows_it():
    assert decide("turn on the lights", "study") == rc.Room("study", "study")


@pytest.mark.parametrize("speaker", ["home_assistant", "jarvis_web", "basement", "", None, "unknown"])
def test_a_speaker_room_the_directory_does_not_know_asks_which_room(speaker):
    assert decide("turn on the lights", speaker) == rc.AskRoom(), speaker


def test_a_common_word_never_makes_a_speaker_room_count():
    assert rc.RoomDirectory.empty().resolves("kitchen") is None
    assert decide("turn on the lights", "kitchen", rc.RoomDirectory.empty()) == rc.AskRoom()


def test_a_referent_follow_up_keeps_a_whole_house_decision_with_its_exclusions():
    previous = rc.WholeHouse((GALLEY,))
    got = decide("turn them off", "study", prev_decision=previous, referent_followup=True)
    assert got == previous


def test_a_referent_follow_up_that_names_a_room_uses_that_room():
    got = decide("turn them off in the study", "galley", prev_decision=rc.Room("galley", "galley"), referent_followup=True)
    assert got == rc.Room("study", "study")


def test_without_a_referent_the_previous_decision_is_not_used():
    assert decide("turn on the lights", "study", prev_decision=rc.Room("galley", "galley")) == rc.Room("study", "study")


def test_ask_text_names_the_distinguishing_word_of_each_candidate():
    got = decide("turn on the bathroom lights", "study")
    assert rc.ask_text(got) == "Which bathroom: north, south or lower?"


def test_ask_text_uses_full_names_when_the_difference_is_only_a_number():
    twin = rc.RoomDirectory.build(areas=[area("Guest Room 2"), area("Guest Room 3")])
    got = decide("turn on the guest room lights", None, twin)
    assert rc.ask_text(got) == "Which guest room: guest room 2 or guest room 3?"


def test_ask_text_does_not_list_more_than_four_candidates():
    many = rc.RoomDirectory.build(areas=[area(f"{n} Bathroom") for n in ("North", "South", "East", "West", "Lower")])
    text = rc.ask_text(decide("turn on the bathroom lights", None, many))
    assert text == "Which bathroom do you mean?"


def test_ask_text_for_an_unknown_room_is_the_short_question():
    assert rc.ask_text(rc.AskRoom()) == "Which room?"


# ---------------------------------------------------------------------------
# Reading the answer
# ---------------------------------------------------------------------------

CANDIDATES = (NORTH, SOUTH, LOWER)


@pytest.mark.parametrize("reply,kind,room", [
    ("north", "one", NORTH),
    ("the north one", "one", NORTH),
    ("North.", "one", NORTH),
    ("North!", "one", NORTH),
    ("um, the south bathroom please", "one", SOUTH),
    ("lower", "one", LOWER),
    ("bathroom", "several", None),
    ("is the north bathroom light on", "none", None),
    ("turn on the north", "none", None),
    ("purple", "none", None),
    ("", "none", None),
])
def test_match_reply(reply, kind, room):
    got = rc.match_reply(reply, CANDIDATES)
    assert got.kind == kind and got.room == room, (reply, got)


def test_a_command_verb_is_never_an_answer_even_when_it_is_part_of_a_room_name():
    odd = (rc.RoomRef("play_room", "play room"), rc.RoomRef("study", "study"))
    assert rc.match_reply("play", odd).kind == "none"
    assert rc.match_reply("study", odd).kind == "one"


def test_the_gate_and_the_decision_share_one_scope_cue_regex():
    from orchestrator import write_fanout

    assert write_fanout._SCOPE_CUE_RE is rc.SCOPE_CUE_RE


# ---------------------------------------------------------------------------
# Grounding a model's room
# ---------------------------------------------------------------------------

def test_a_model_room_the_utterance_does_not_name_is_dropped():
    assert rc.ground_llm_room("galley", "make it cosy", HOUSE_A) is None


def test_a_model_room_must_match_whole_tokens():
    assert rc.ground_llm_room("den", "turn on the garden lights", rc.RoomDirectory.empty()) is None
    assert rc.ground_llm_room("garden", "turn on the garden lights", rc.RoomDirectory.empty()) == "garden"


def test_a_model_whole_house_needs_whole_house_wording():
    assert rc.ground_llm_room("whole_house", "make it cosy", HOUSE_A) is None
    assert rc.ground_llm_room("whole_house", "turn on all the lights", HOUSE_A) == "whole_house"


def test_a_model_room_is_kept_when_it_names_the_directory_room_the_utterance_names():
    assert rc.ground_llm_room("study", "put the den lights on", HOUSE_A) == "study"


# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------

def test_room_context_is_pure():
    text = (SRC / "orchestrator" / "room_context.py").read_text(encoding="utf-8")
    imports = re.findall(r"^(?:from|import) ([\w.]+)", text, re.M)
    assert set(imports) <= {"__future__", "re", "dataclasses", "typing", "shared.ha_registry"}, imports
