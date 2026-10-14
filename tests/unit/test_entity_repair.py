"""Near-miss entity words are repaired for routing only."""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from orchestrator import entity_repair
from orchestrator.access_target import TEAM_WORDS
from orchestrator.entity_repair import MAX_QUERY_CHARS, MAX_TOKENS, repair_for_routing

SRC = Path(__file__).resolve().parents[2] / "src" / "orchestrator"
TEAM = "ravens"
assert TEAM in TEAM_WORDS  # the named member the rows below depend on


@pytest.mark.parametrize("heard,meant", [
    (f"whats the scor of the {TEAM} game", f"whats the score of the {TEAM} game"),
    ("how did the falons do", "how did the falcons do"),
    ("core of the giants game", "score of the giants game"),
    ("who won the chargrs game", "who won the chargers game"),
])
def test_known_vocabulary_near_misses_are_repaired(heard, meant):
    repaired = repair_for_routing(heard)
    assert repaired.text == meant
    assert repaired.changes


@pytest.mark.parametrize("query", [
    "is my flight delayed",
    "any flights to boston tonight",
    "turn on night mode",
    "find a place to eat",
    "turn on the lighting in the den",
    "what's the weather in texas",
    "what are the charges on my card",
    "what's for supper",
    "the officer knocked",
    "turn the lightss in the kitchn on",
    "whats the temprature on the thermostt",
    "the lights in the garage",
    "the bears game flight",
])
def test_ordinary_and_room_or_device_words_are_never_repaired(query):
    assert repair_for_routing(query).text == query


@pytest.mark.parametrize("query", [
    "my flight to texas has a bad lighting score",   # sports noun present, ordinary words stay
    "who won the game at the place on the supper night",
])
def test_the_stoplist_holds_inside_a_sports_utterance(query):
    repaired = repair_for_routing(query).text
    for word in ("flight", "lighting", "texas", "place", "supper", "night"):
        if word in query:
            assert word in repaired


def test_a_repaired_team_is_reported():
    team = sorted(w for w in TEAM_WORDS if len(w) >= 7)[0]
    typo = team[:3] + team[4:]  # one letter dropped
    repaired = repair_for_routing(f"who won the {typo} game")
    assert repaired.team == team and team in repaired.text


@pytest.mark.parametrize("query", [
    "core values of the company",
    "what is the core of the idea",
    "scor",
    "the quick brown fox jumps over",
])
def test_ordinary_words_are_left_alone(query):
    assert repair_for_routing(query).text == query
    assert repair_for_routing(query).changes == ()


def test_a_short_sports_noun_needs_a_sports_word_beside_it():
    assert repair_for_routing("core values").text == "core values"
    assert repair_for_routing(f"the {TEAM} core").text == f"the {TEAM} score"
    assert repair_for_routing("the core of the game").text == "the score of the game"


def test_the_caps_leave_a_long_query_alone():
    long_chars = "whats the scor of the game " + "x" * MAX_QUERY_CHARS
    assert repair_for_routing(long_chars).text == long_chars
    many_tokens = "whats the scor of the game " + " ".join(["ok"] * MAX_TOKENS)
    assert repair_for_routing(many_tokens).text == many_tokens
    assert repair_for_routing("whats the scor of the game").text == "whats the score of the game"


def test_short_tokens_are_never_repaired():
    assert repair_for_routing("the lit one").text == "the lit one"


def test_it_is_not_a_second_typo_table_for_the_controller():
    """The controller, the cue check and the router never import it."""
    for name in ("smart_home_controller.py", "access_target.py", "nodes/route_control.py"):
        tree = ast.parse((SRC / name).read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            for alias in (node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else ())
        } | {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
        assert not any("entity_repair" in str(n) for n in imported), name


def test_main_feeds_the_repair_only_to_routing_never_to_the_query():
    tree = ast.parse((SRC / "main.py").read_text(encoding="utf-8"))
    uses = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "repair_for_routing"]
    assert len(uses) == 1
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Attribute) and t.attr == "query" and getattr(t.value, "id", "") == "state"
            for t in node.targets
        ):
            assert "routing" not in ast.dump(node.value), ast.unparse(node)


def test_a_repair_does_not_change_the_write_cue():
    """The cue is judged on the words spoken: a repaired device word is not evidence."""
    from orchestrator import access_target

    spoken = "kitchen lightss please"
    assert repair_for_routing(spoken).text == spoken  # devices are never repaired
    # The cue check reads what was spoken. A typo'd device word is no evidence of a command, and the
    # repaired text is not what the controller or the cue check is given.
    assert access_target.write_cue_problem("light", "turn_on", spoken) == access_target.NO_CUE
    assert "entity_repair" not in Path(access_target.__file__).read_text(encoding="utf-8")
