"""Red/green contract for ATHENA-88 phase 3 (F89): dining venue-phrase
keywords and the narrowed recipe-precedence rule.

Plan: .mozart/plans/active/2026-09-26-deliver-athena-voice-intent-defects.md,
Phase 3. Test contract: same directory,
2026-09-26-deliver-athena-voice-intent-defects.test-contract.md, Phase 3
C1-C4 (r1 amendments).
"""
import sys

import pytest

sys.path.insert(0, "src")

from orchestrator.context.detector import (
    detect_strong_intent,
    detect_context_reference,
    is_conversational_reference,
)
from orchestrator.sequence_executor import detect_sequence_intent
from orchestrator.search_providers.intent_classifier import IntentClassifier

INCIDENT_TURN_2 = "what plae have happy our and outdoor seating?"


# ---------------------------------------------------------------------------
# 1. test_venue_phrasings_detect_dining
# ---------------------------------------------------------------------------

VENUE_PHRASINGS = [
    INCIDENT_TURN_2,  # named member: the incident's own ASR transcript
    "any good bars around here",
    "let's go to a beer garden tonight",
    "any pubs open late",
    "find a gastropub downtown",
    "we found a nice bistro",
    "any diner still open",
    "is there a good eatery around",
    "we want a place with a patio",
    "any spot for drinks tonight",
]


def test_venue_phrasings_population_floor():
    assert len(VENUE_PHRASINGS) == 10


@pytest.mark.parametrize("query", VENUE_PHRASINGS, ids=VENUE_PHRASINGS)
def test_venue_phrasings_detect_dining(query):
    result = detect_strong_intent(query, prev_intent="recipes")
    assert result["detected_intent"] == "dining"
    assert result["should_override_context"] is True


# ---------------------------------------------------------------------------
# 2. test_recipe_requests_beat_dining
# ---------------------------------------------------------------------------

RECIPE_ROWS = ["granola bars recipe", "how to make protein bars", "dinner recipes"]


@pytest.mark.parametrize("query", RECIPE_ROWS, ids=RECIPE_ROWS)
def test_recipe_requests_beat_dining(query):
    result = detect_strong_intent(query)
    assert result["detected_intent"] == "recipes"


# ---------------------------------------------------------------------------
# 3. test_no_collisions
# ---------------------------------------------------------------------------

COLLISION_TABLE = [
    # bob M3: recipe trigger phrase present, but dining match includes a
    # reservation/restaurant word -> precedence must NOT fire; stays dining.
    ("how to make a reservation at a restaurant", "dining"),
    # tessa: dining + recipes co-occurrence with no recipe TRIGGER phrase
    # ("cook" alone isn't one) -> falls through to normal priority -> dining.
    ("cook dinner and grab drinks after", "dining"),
    # Bare "patio" was deliberately NOT added (D7) -- must not collide with
    # weather/control.
    ("what's the weather on the patio", "weather"),
    ("turn off the patio lights", "control"),
    # Bare "brewery" was deliberately NOT added (D7) -- must not collide
    # with events.
    ("events at the brewery tonight", "events"),
    # Unaffected-category regression guards: new dining keywords must not
    # leak into unrelated categories.
    ("chocolate chip cookie recipe", "recipes"),
    ("what's the score of the ravens game", "sports"),
    ("how to make a cocktail", "recipes"),  # bare "cocktail" not added
    ("can I see the restaurant menu", "dining"),
    ("what's on the news today", "news"),
    # Bare "rooftop" was deliberately NOT added (D7) -- no category matches.
    ("what's the view from the rooftop", None),
]


def test_collision_population_floor():
    assert len(COLLISION_TABLE) == 11


@pytest.mark.parametrize("query,expected", COLLISION_TABLE, ids=[q for q, _ in COLLISION_TABLE])
def test_no_collisions(query, expected):
    result = detect_strong_intent(query)
    assert result["detected_intent"] == expected


# ---------------------------------------------------------------------------
# 4. test_incident_transcript_decision_chain
# ---------------------------------------------------------------------------

def test_incident_transcript_decision_chain():
    turn2 = INCIDENT_TURN_2
    turn3 = "yes please"

    # Phase 1's fixed matcher: not a sequence command.
    assert detect_sequence_intent(turn2) is False

    # Phase 1's splitter guard: the embedded "and" does not split a single
    # descriptive request into two fragments.
    assert IntentClassifier().detect_multi_intent(turn2) == [turn2]

    # Exercised in call order (context reference detection runs on every
    # turn in the real pipeline); bob's veto assertion: turn 2 is a fresh
    # dining request, not a reference back to the prior conversation.
    detect_context_reference(turn2)
    assert is_conversational_reference(turn2, True) is False

    # Phase 3's fix: turn 2 resolves to dining (not recipes, despite
    # prev_intent="recipes" from a hypothetical prior recipe turn).
    turn2_result = detect_strong_intent(turn2, prev_intent="recipes")
    assert turn2_result["detected_intent"] == "dining"
    assert turn2_result["should_override_context"] is True

    # Turn 3 ("yes please") carries no strong intent of its own -- nothing
    # forces it to "recipes"; it continues dining (the actual continuation
    # routing is phases 2/5, out of scope here).
    turn3_result = detect_strong_intent(turn3, prev_intent="dining")
    assert turn3_result["has_strong_intent"] is False
    assert turn3_result["detected_intent"] is None
