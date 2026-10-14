"""``UtteranceClassification.command_text``: the imperative clauses of an
utterance, cut at the clause boundaries (commas, okay/hey/names, and/then/but
after a command, a time phrase before a verb), without the narration around.

Pure, synthetic utterances.
"""
from __future__ import annotations

import pytest

from orchestrator.utterance_kind import (
    MAX_CLASSIFIED_CHARS,
    UtteranceKind,
    analyze_clauses,
    classify_utterance,
    command_query,
    cue_text,
    is_narration,
)

NAMES = ("nebu",)


def command(text, names=NAMES):
    return classify_utterance(text, names).command_text


@pytest.mark.parametrize("text,expected", [
    # Commas and sentence ends.
    ("the porch is lit, turn on the hall light", "turn on the hall light"),
    ("The porch is lit. Turn on the hall light.", "Turn on the hall light"),
    ("turn on the hall light, the kids are asleep", "turn on the hall light"),
    ("turn on the hall light! the kids are asleep.", "turn on the hall light"),
    # Discourse markers with and without punctuation.
    ("the blue led ring okay turn on the hall light", "turn on the hall light"),
    ("the blue led ring, ok, turn on the hall light", "turn on the hall light"),
    ("the blue led ring alright turn on the hall light", "turn on the hall light"),
    ("the blue led ring all right turn on the hall light", "turn on the hall light"),
    ("the blue led ring hey nebu turn on the hall light", "turn on the hall light"),
    # Trailing narration is excluded.
    ("turn on the hall light the way they do at night", "turn on the hall light the way they do at night"),
    ("turn the lights on, I love it", "turn the lights on"),
    ("turn the lights on. they look great.", "turn the lights on"),
    # An object continued after "and" or a comma stays with its command.
    ("turn on the kitchen and hall lights", "turn on the kitchen and hall lights"),
    ("turn on the kitchen, hall, and porch lights", "turn on the kitchen, hall, and porch lights"),
    # Two commands.
    ("turn on the light and make it blue", "turn on the light and make it blue"),
    ("turn off the hall light then turn on the porch light", "turn off the hall light then turn on the porch light"),
    ("turn off the kitchen but leave the porch light on", "turn off the kitchen but leave the porch light on"),
    # Fillers and openers.
    ("so turn off the lights", "turn off the lights"),
    ("please turn off the lights", "please turn off the lights"),
    ("just dim the lights", "dim the lights"),
    ("yeah leave the lights on", "leave the lights on"),
    ("thank you turn off the lights", "turn off the lights"),
    ("in 5 minutes lock the door", "in 5 minutes lock the door"),
    ("at 11 turn off the lights", "at 11 turn off the lights"),
    ("could you turn off the lights", "could you turn off the lights"),
    # A time phrase before the verb belongs to the command.
    ("before bed turn off the lights", "before bed turn off the lights"),
    ("until dinner dim the lights", "until dinner dim the lights"),
    # Negated clauses are never part of it.
    ("don't turn off the hall light, turn on the porch light", "turn on the porch light"),
    ("do not turn off the hall light", None),
    # Quotation marks are dropped from what is kept.
    ('I said "turn off the lights"', "I said turn off the lights"),
], ids=str)
def test_command_text(text, expected):
    assert command(text) == expected


@pytest.mark.parametrize("text", [
    "", "   ", "the lights", "lights", "thank you", "hello there", "what time is it", "is the porch light on",
])
def test_no_command_in_these(text):
    assert command(text) is None


def test_none_and_empty_input_classify_as_unknown_and_never_raise():
    for value in (None, "", "   ", "\n"):
        assert classify_utterance(value).kind == UtteranceKind.UNKNOWN
        assert classify_utterance(value).command_text is None
    assert analyze_clauses(None).clauses == ()
    assert cue_text(None) is None
    assert is_narration(None) is False


@pytest.mark.parametrize("text", [
    "turn on " * 200, "é" * 500, "İ" * 200 + " turn on the lights", "\x00\x01 turn on the lights", "'" * 80,
    '"' * 80, ", , , , okay okay okay", "if " * 100,
])
def test_hostile_input_never_raises(text):
    classify_utterance(text, NAMES)
    cue_text(text, NAMES)
    is_narration(text, NAMES)


def test_only_a_prefix_is_analysed_and_a_longer_utterance_goes_on_whole():
    long_text = "turn on the hall light " + "x" * (MAX_CLASSIFIED_CHARS + 50)
    uk = classify_utterance(long_text)
    assert command_query(uk, long_text) == long_text


def test_in_the_kitchen_then_the_command_keeps_the_kitchen():
    uk = classify_utterance("In the kitchen. Turn on the lights.", NAMES)
    assert uk.kind == UtteranceKind.IMPERATIVE
    assert uk.command_text == "Turn on the lights"
    assert uk.room == "kitchen" and uk.device_type == "light" and uk.target_state == "on"
    # The extractor and the room decision are handed the room too.
    assert command_query(uk, "In the kitchen. Turn on the lights.") == "Turn on the lights in the kitchen"


def test_narration_supplies_no_room():
    uk = classify_utterance("the kitchen lights are too bright. Turn on the lights.", NAMES)
    assert uk.command_text == "Turn on the lights"
    assert uk.room is None


def test_a_room_the_command_names_wins_over_an_earlier_one():
    uk = classify_utterance("In the kitchen. Turn on the hall light.", NAMES)
    assert uk.room == "hall"
    assert command_query(uk, "In the kitchen. Turn on the hall light.") == "Turn on the hall light"


def test_the_device_and_state_come_from_the_command_text():
    uk = classify_utterance("the fan is loud, okay, turn on the hall light", NAMES)
    assert (uk.device_type, uk.target_state) == ("light", "on")


def test_the_command_text_keeps_the_original_case_and_spacing_of_the_speaker():
    assert command("Okay,   Turn   ON the Hall Light") == "Turn   ON the Hall Light"


def test_an_assistant_name_that_is_not_configured_is_not_a_boundary():
    # "hey kids" is not a vocative; the default names are always boundaries.
    assert command("okay honey turn off the lights") is None
    assert command("okay athena turn off the lights") == "turn off the lights"
    assert command("okay nebu turn off the lights", names=()) is None
    assert command("okay nebu turn off the lights", names=("nebu",)) == "turn off the lights"


def test_the_core_text_drops_the_vocatives_around_a_whole_utterance():
    assert classify_utterance("okay, goodnight").core_text == "goodnight"
    assert classify_utterance("hey athena goodbye").core_text == "goodbye"
    assert classify_utterance("goodbye, nebu", NAMES).core_text == "goodbye"
    assert classify_utterance("goodbye nebu", NAMES).core_text == "goodbye"
    assert classify_utterance("goodbye, nebu").core_text == "goodbye, nebu"
    assert classify_utterance("then goodnight").core_text == "goodnight"


def test_a_question_keeps_the_question_rules_and_no_command_text():
    for text in ("is it ok to turn off the lights", "did they turn off at 6pm", "are the porch lights on"):
        uk = classify_utterance(text)
        if uk.kind == UtteranceKind.STATE_QUESTION:
            assert uk.command_text is None
    assert classify_utterance("did the office light turn off at 6pm").kind == UtteranceKind.STATE_QUESTION
    # A question about permission is no longer a command.
    assert classify_utterance("is it ok to turn off the lights").kind != UtteranceKind.IMPERATIVE
