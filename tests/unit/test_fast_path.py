"""The deterministic fast-path vocabulary, its exclusions and the moved confirmation vocabulary.

Clock cases pin DEFAULT_TIMEZONE=America/New_York and freeze the UTC instant
(`tests/unit/_clock_fixture.py`), so the pod's process zone never matters.
"""
from __future__ import annotations

import ast
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, "src")

from ._clock_fixture import clock  # noqa: F401,E402  (fixture)

from shared import fast_path_vocab as vocab  # noqa: E402

NY = "America/New_York"
UTC = timezone.utc
MAIN_PY = Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "main.py"

# Literal values captured from the base tree (e5a78bc) before the vocabulary moved.
BASE_BARE_AFFIRMATION_PATTERN = (
    r'^(?:yes|yeah|yep|yup|ok|okay|sure)(?:\s+(?:please|thanks|thank\ you|do\ it|go\ ahead))?$'
    r'|^(?:do\ it|go\ ahead)$'
)
BASE_BARE_NEGATION_PATTERN = r'^(?:no|nope|nah)(?:\s+(?:thanks|thank\ you))?$'
BASE_TUPLES = {
    "AFFIRMATION_WORDS": ("yes", "yeah", "yep", "yup", "ok", "okay", "sure"),
    "NEGATION_WORDS": ("no", "nope", "nah"),
    "GRATITUDE_WORDS": ("thanks", "thank you"),
    "POLITE_SUFFIX_WORDS": ("please", "thanks", "thank you"),
    "PROCEED_PHRASES": ("do it", "go ahead"),
}


def _now(y, mo, d, h, mi) -> datetime:
    from zoneinfo import ZoneInfo

    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(NY))


# --- the table ------------------------------------------------------------------


def test_table_replies_and_named_members():
    now = _now(2026, 10, 10, 9, 5)
    assert vocab.reply_for("hello", now) == ("greeting", "Hello. How can I help?")
    assert vocab.reply_for("Thank you!", now) == ("thanks", "You're welcome.")
    assert vocab.reply_for("Bye.", now) == ("farewell", "Goodbye.")
    assert vocab.reply_for("see you", now) == ("farewell", "See you later.")
    assert vocab.reply_for("How are you?", now) == ("smalltalk", "I'm doing well. How can I help?")
    for ack in ("got it", "cool", "great", "perfect", "nice", "awesome", "never mind", "nevermind",
                "that's all", "That's it."):
        assert vocab.reply_for(ack, now) == ("ack", "Okay."), ack
    assert vocab.reply_for("what time is it", now) == ("time", "It's 9:05 AM.")
    assert vocab.reply_for("What's the date?", now) == ("date", "Today is Saturday, October 10, 2026.")


@pytest.mark.parametrize("query", [
    "who won the game", "", "   ", None, "hello there friend", "what time is it in Tokyo",
    "thanks for turning on the lights", "turn off the lights",
])
def test_non_matches_are_none(query):
    assert vocab.reply_for(query, _now(2026, 10, 10, 9, 5)) is None
    assert vocab.is_fast_path_candidate(query) is False


def test_is_fast_path_candidate_agrees_with_reply_for():
    now = _now(2026, 10, 10, 9, 5)
    for key in list(vocab.REPLIES) + ["good morning", "okay", "who won", "no thanks"]:
        assert vocab.is_fast_path_candidate(key) is (vocab.reply_for(key, now) is not None), key


# --- exclusions: scenes and confirmations ------------------------------------------


def test_named_exclusions():
    now = _now(2026, 10, 10, 9, 5)
    assert vocab.reply_for("good morning", now) is None
    assert vocab.reply_for("Goodbye.", now) is None
    assert vocab.reply_for("good night", now) is None
    assert vocab.reply_for("okay", now) is None
    assert vocab.reply_for("no thanks", now) is None
    assert vocab.reply_for("yes please", now) is None
    assert vocab.reply_for("what time is it", now)[0] == "time"


def test_table_is_disjoint_from_scene_and_confirmation_vocabulary():
    assert len(vocab.REPLIES) >= 30, "population floor"
    checked = 0
    for key in vocab.REPLIES:
        assert vocab.normalize(key) == key, key
        assert not any(p in key for p in vocab.SCENE_TRIGGER_PHRASES), key
        assert not any(p in key for p in vocab.SCENE_OVERRIDE_PHRASES), key
        reply = vocab.normalize_reply(key)
        assert not vocab.BARE_AFFIRMATION_RE.match(reply), key
        assert not vocab.BARE_NEGATION_RE.match(reply), key
        assert vocab.is_fast_path_candidate(key), key
        checked += 1
    assert checked == len(vocab.REPLIES)


def test_exclusion_rule_holds_even_if_the_table_listed_the_phrase(monkeypatch):
    """The exclusion is its own rule, not a side effect of the table's contents."""
    poisoned = dict(vocab.REPLIES)
    for phrase in ("good morning", "goodbye", "party time", "okay", "no thanks", "yes please", "do it"):
        poisoned[phrase] = ("greeting", "x")
    monkeypatch.setattr(vocab, "REPLIES", poisoned)
    now = _now(2026, 10, 10, 9, 5)
    for phrase in ("good morning", "goodbye", "party time", "okay", "no thanks", "yes please", "do it"):
        assert vocab.reply_for(phrase, now) is None, phrase
    assert vocab.reply_for("hello", now) is not None


def test_every_scene_phrase_is_excluded_when_embedded(monkeypatch):
    poisoned = dict(vocab.REPLIES)
    poisoned["hello"] = ("greeting", "x")
    monkeypatch.setattr(vocab, "REPLIES", poisoned)
    # A table key that merely contains a scene phrase is still excluded.
    for phrase in vocab.SCENE_TRIGGER_PHRASES + vocab.SCENE_OVERRIDE_PHRASES:
        key = vocab.normalize(phrase)
        if not key:
            continue
        monkeypatch.setitem(vocab.REPLIES, key, ("greeting", "x"))
        assert vocab.reply_for(phrase, _now(2026, 10, 10, 9, 5)) is None, phrase


# --- the clock ------------------------------------------------------------------


@pytest.mark.parametrize("utc,expected", [
    (datetime(2026, 10, 10, 4, 0, tzinfo=UTC), "It's 12:00 AM."),     # local midnight
    (datetime(2026, 10, 10, 16, 0, tzinfo=UTC), "It's 12:00 PM."),    # local noon
    (datetime(2026, 10, 11, 3, 59, tzinfo=UTC), "It's 11:59 PM."),    # 23:59 local
    (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), "It's 3:00 AM."),        # spring forward: 02:00 EST -> 03:00 EDT
    (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), "It's 1:30 AM."),      # fall back, first 01:30
    (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), "It's 1:30 AM."),      # fall back, second 01:30
])
def test_time_is_the_property_clock(clock, utc, expected):
    from orchestrator.fast_path import fast_path_reply

    clock.property_zone(NY)
    clock.frozen_utc(utc)
    reply = fast_path_reply("what time is it")
    assert reply is not None and reply.kind == "time"
    assert reply.text == expected


@pytest.mark.parametrize("utc,expected", [
    # 23:30 local on Oct 10 is already Oct 11 in UTC; the date is the local one.
    (datetime(2026, 10, 11, 3, 30, tzinfo=UTC), "Today is Saturday, October 10, 2026."),
    (datetime(2026, 10, 11, 4, 0, tzinfo=UTC), "Today is Sunday, October 11, 2026."),
])
def test_date_is_the_local_date_not_utc(clock, utc, expected):
    from orchestrator.fast_path import fast_path_reply

    clock.property_zone(NY)
    clock.frozen_utc(utc)
    assert fast_path_reply("what day is it").text == expected


def test_fast_path_reply_never_raises(monkeypatch):
    import orchestrator.fast_path as fp

    def boom(*_a, **_k):
        raise RuntimeError("clock failed")

    monkeypatch.setattr(fp, "reply_for", boom)
    assert fp.fast_path_reply("hello") is None


# --- the moved confirmation vocabulary ----------------------------------------------


def test_confirmation_vocabulary_equals_base_values():
    assert vocab.BARE_AFFIRMATION_RE.pattern == BASE_BARE_AFFIRMATION_PATTERN
    assert vocab.BARE_NEGATION_RE.pattern == BASE_BARE_NEGATION_PATTERN
    assert vocab.BARE_AFFIRMATION_RE.flags == re.compile("").flags
    assert vocab.BARE_NEGATION_RE.flags == re.compile("").flags
    for name, value in BASE_TUPLES.items():
        assert getattr(vocab, name) == value, name


def test_old_homes_expose_the_identical_objects():
    from orchestrator import write_fanout
    from orchestrator.context import detector

    assert write_fanout.BARE_AFFIRMATION_RE is vocab.BARE_AFFIRMATION_RE
    assert write_fanout.BARE_NEGATION_RE is vocab.BARE_NEGATION_RE
    assert write_fanout.normalize_reply is vocab.normalize_reply
    for name in BASE_TUPLES:
        assert getattr(detector, name) is getattr(vocab, name), name


def test_classify_node_uses_the_shared_scene_constants():
    source = MAIN_PY.read_text(encoding="utf-8")
    literals = {n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not {"movie mode", "watch a movie", "set the mood"} & literals, "a scene phrase is spelled out in main.py again"
    assert source.count("scene_patterns = SCENE_TRIGGER_PHRASES") == 2
    assert "scene_patterns = SCENE_OVERRIDE_PHRASES" in source
    assert "goodbye" in vocab.SCENE_TRIGGER_PHRASES and "party vibes" in vocab.SCENE_OVERRIDE_PHRASES


def test_helpers_wrapper_keeps_its_signature_and_defers_to_the_table(clock):
    from orchestrator.helpers import _direct_general_info_response

    clock.property_zone(NY)
    clock.frozen_utc(datetime(2026, 10, 10, 13, 5, tzinfo=UTC))
    assert _direct_general_info_response("hello") == "Hello. How can I help?"
    assert _direct_general_info_response("what time is it") == "It's 9:05 AM."
    assert _direct_general_info_response("good morning") is None
    assert _direct_general_info_response("who won the game") is None
