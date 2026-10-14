"""The unknown-speaker identity answer: vocabulary, reply and backstop.

Synthetic names only. The pure module is stdlib-only; the reader tests use the
real `load_visible_knowledge` against a fake admin client.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from orchestrator import identity_answer as ia
from shared import base_knowledge_utils as bku
from shared.knowledge_tiers import KnowledgeAudience

OWNER = "Olive Owner"
ADDRESS = "42 Example Lane, Sampletown, ST 00000"

NAME_QUESTIONS = [
    "what's my name", "What is my name?", "whats my name", "what is my name again",
    "say my name", "tell me my name", "can you tell me my name", "please tell me my full name",
    "who am I", "Who am I?", "do you know who I am", "do you know me", "do you know my name",
    "what do you call me", "what do people call me", "what am I called", "hey, what's my name",
    "Jarvis, what is my name", "what is my name, Athena", "what is my name please",
    "okay jarvis who am i", "what’s my name",
]
HOME_QUESTIONS = [
    "where do I live", "Where do I live?", "do you know where I live", "what's my address",
    "what is my home address", "where's my house", "where is my home", "tell me my address",
    "what's the address of my house", "hey jarvis where do i live",
]
NOT_IDENTITY = [
    "what's the address here", "who lives here", "who's the owner", "what is the owner's name",
    "who am I kidding", "what's my name for the reservation at seven", "my name is Sam",
    "what is her name", "what's your name", "where do you live", "what's the weather",
    "what's my favorite color", "how do I get to my house", "what time is it", "",
    "hey kids what's my name", "what is my name and turn on the lights in the kitchen please now",
]


@pytest.mark.parametrize("query", NAME_QUESTIONS)
def test_name_frames(query):
    assert ia.identity_question(query) == ia.NAME


@pytest.mark.parametrize("query", HOME_QUESTIONS)
def test_home_frames(query):
    assert ia.identity_question(query) == ia.HOME


@pytest.mark.parametrize("query", NOT_IDENTITY, ids=lambda q: q or "empty")
def test_house_and_other_questions_are_not_identity(query):
    assert ia.identity_question(query) is None


def test_configured_vocative_is_stripped_only_when_supplied():
    assert ia.identity_question("friday what's my name") is None
    assert ia.identity_question("friday what's my name", ["Friday"]) == ia.NAME
    assert ia.identity_question("what's my name friday", ["Friday"]) == ia.NAME


def test_more_than_twelve_words_is_never_identity():
    assert ia.identity_question("what is my name again") == ia.NAME
    assert ia.identity_question("um " * 13 + "what is my name") is None


def test_long_input_is_linear_and_safe():
    assert ia.identity_question("what is my name " * 5000) is None
    assert ia.identity_question("x" * 100_000) is None


def test_pre_check_only_passes_possible_identity_questions():
    for q in NAME_QUESTIONS + HOME_QUESTIONS:
        assert ia.may_be_identity_question(q), q
    for q in ("what time is it", "turn on the lights", "", "hello"):
        assert not ia.may_be_identity_question(q), q


@pytest.mark.parametrize(
    "kind,expected",
    [(None, ia.NAME_REPLY), ("owner", None), ("guest", None), ("household", None)],
)
def test_reply_only_for_nobody_addressed(kind, expected):
    assert ia.reply_for("say my name", kind) == expected


def test_named_member_say_my_name_is_none_and_unknown_gets_name_reply():
    assert ia.reply_for("say my name", "household") is None
    assert ia.reply_for("say my name", None) == ia.NAME_REPLY
    assert ia.reply_for("where do I live", None) == ia.HOME_REPLY
    assert ia.reply_for("what's the weather", None) is None


def test_replies_state_no_name_or_address():
    for reply in (ia.NAME_REPLY, ia.HOME_REPLY):
        assert "can't tell who's speaking" in reply
        assert not any(ch.isdigit() for ch in reply)


BACKSTOP_ROWS = [
    # query, answer, kind, expected
    ("what's my name", f"Your name is {OWNER}.", None, ia.NAME_REPLY),
    ("who am I", "You're Olive, the owner.", None, ia.NAME_REPLY),
    ("what do they call me around here", "They call you OLIVE!", None, ia.NAME_REPLY),
    ("do I have a name on file", "Yes: owner, olive-owner.", None, ia.NAME_REPLY),
    ("what's my address", f"You live at {ADDRESS}.", None, ia.HOME_REPLY),
    ("where do I live", "At 42 Example Lane.", None, ia.HOME_REPLY),
    ("what's my address", "You live at 42 EXAMPLE LANE, sampletown!", None, ia.HOME_REPLY),
    # no leak -> untouched
    ("what's my name", "I can't tell who's speaking.", None, None),
    ("who am I", "Someone who asks good questions.", None, None),
    # not a first-person question
    ("what's the owner's name", f"The owner is {OWNER}.", None, None),
    ("who lives here", f"{OWNER} lives here.", None, None),
    ("what's the address here", f"It's {ADDRESS}.", None, None),
    # addressed callers are never rewritten
    ("what's my name", f"Your name is {OWNER}.", "owner", None),
    ("what's my name", f"Your name is {OWNER}.", "household", None),
    ("what's my name", f"Your name is {OWNER}.", "guest", None),
    # nothing to compare against / nothing said
    ("what's my name", "", None, None),
    ("what's my name", None, None, None),
]


@pytest.mark.parametrize("query,answer,kind,expected", BACKSTOP_ROWS)
def test_backstop(query, answer, kind, expected):
    assert ia.backstop(query, answer, kind, OWNER, ADDRESS) == expected


def test_backstop_without_visible_rows_has_nothing_to_match():
    assert ia.backstop("what's my name", f"Your name is {OWNER}.", None, None, None) is None


def test_backstop_does_not_treat_a_city_default_as_an_address():
    assert ia.backstop("where do I live", "You live in Sampletown.", None, OWNER, "42 Example Lane, Sampletown") is None


# --- the readers around it ---------------------------------------------------------


def _audience(mode="owner", *, owner_proven=False):
    return KnowledgeAudience(mode=mode, degraded=False, public=False, owner_caller=owner_proven, owner_proven=owner_proven)


def test_assistant_name_from_cleans_like_a_guest_name():
    rows = [
        {"category": "general", "key": "assistant_name", "value": "<b>Hal</b>"},
        {"category": "general", "key": "assistant_name_alt", "value": "  Hal   Nine "},
        {"category": "user", "key": "assistant_name", "value": "Wrong Category"},
    ]
    assert bku.assistant_name_from(rows) == "Hal Nine"
    assert bku.assistant_name_from(rows[:1]) is None
    assert bku.assistant_name_from([]) is None


def test_extract_property_address_has_no_default_fallback():
    assert bku.extract_property_address([{"category": "location", "key": "default", "value": "Sampletown"}]) is None
    assert bku.extract_property_address(
        [{"category": "property", "key": "address", "value": f" {ADDRESS} "}]) == ADDRESS


def _names(monkeypatch, rows, profile_name="Jarvis", *, hang=False):
    from . import _public_audience_harness  # noqa: F401  (imports orchestrator.main first; helpers-first has a known cycle)
    from orchestrator import helpers

    helpers._assistant_names_cache.update(names=(), expires_at=0.0)
    monkeypatch.setattr(helpers, "get_assistant_profile", mock.AsyncMock(return_value={"assistant_name": profile_name}))
    admin = mock.MagicMock()

    async def _get(**kw):
        if hang:
            await asyncio.sleep(5)
        return rows

    admin.get_base_knowledge = _get
    monkeypatch.setattr(helpers, "get_admin_client", lambda: admin)
    return helpers


def test_row_name_is_per_audience_and_never_enters_the_global_cache(monkeypatch):
    rows = [{"category": "general", "key": "assistant_name", "value": "Hal", "applies_to": "both"}]
    helpers = _names(monkeypatch, rows)
    owner = _audience("owner", owner_proven=True)
    assert asyncio.run(helpers.configured_assistant_names()) == ("Jarvis",)
    assert asyncio.run(helpers.configured_assistant_names(owner)) == ("Hal", "Jarvis")
    assert asyncio.run(helpers.configured_assistant_names()) == ("Jarvis",)
    assert helpers._assistant_names_cache["names"] == ("Jarvis",)


def test_public_audience_sees_no_row(monkeypatch):
    rows = [{"category": "general", "key": "assistant_name", "value": "Hal", "applies_to": "both"}]
    helpers = _names(monkeypatch, rows)
    assert asyncio.run(helpers.configured_assistant_names(KnowledgeAudience.UNRESOLVED)) == ("Jarvis",)


def test_row_lookup_fails_soft_within_the_bound(monkeypatch):
    rows = [{"category": "general", "key": "assistant_name", "value": "Hal", "applies_to": "both"}]
    helpers = _names(monkeypatch, rows, hang=True)
    monkeypatch.setattr(helpers, "ASSISTANT_NAME_LOOKUP_TIMEOUT_SECONDS", 0.05)
    got = asyncio.run(helpers.configured_assistant_names(_audience("owner", owner_proven=True)))
    assert got == ("Jarvis",)


def test_public_permissions_are_addressed_by_nobody_whatever_the_context_says(monkeypatch):
    from types import SimpleNamespace

    from . import _public_audience_harness  # noqa: F401
    from orchestrator import helpers

    monkeypatch.setattr(helpers, "_assistant_names_cache", {"names": ("Jarvis",), "expires_at": float("inf")})
    guest_audience = KnowledgeAudience(mode="guest", degraded=False, public=False, owner_caller=False, owner_proven=False)
    monkeypatch.setattr(helpers, "get_admin_client", lambda: mock.MagicMock(get_base_knowledge=mock.AsyncMock(return_value=[])))

    def state(permissions):
        return SimpleNamespace(query="who am I", permissions=permissions, knowledge_audience=guest_audience,
                               context={"speaker_first_name": "Pat"})

    assert asyncio.run(helpers.identity_reply_for(state({"audience": "household"}))) is None  # named member: control
    assert asyncio.run(helpers.identity_reply_for(state({"audience": "public"}))) == ia.NAME_REPLY
