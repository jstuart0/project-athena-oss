"""The pure door resolver (``orchestrator.access_target``).

An opening acts on exactly one entity, and only when the user's own words name
it. These cases pin every rule in order, the ``named_by`` of each source, that
a room or an area is never a ``Named`` (one entity in it or several), what a
model's guess can and cannot do, the answer to "which door?", and that the
module stays pure (no logger, no I/O).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest  # noqa: E402

from orchestrator import access_target as at  # noqa: E402
from orchestrator import room_context  # noqa: E402
from shared.ha_registry import Area  # noqa: E402

LOCKS = {
    "lock.front_door": "Front Door Lock",
    "lock.back_door": "Back Door Lock",
    "lock.kitchen_door": "Kitchen Door Lock",
}
ONE_LOCK = {"lock.main": "Door Lock"}
GARAGE = {"cover.garage_left": "Garage Door Left", "cover.garage_right": "Garage Door Right"}
ONE_GARAGE = {"cover.garage": "Garage Door"}


def area(key, name):
    return Area(f"area_{key}", name, (), key)


DIRECTORY = room_context.RoomDirectory.build(areas=[
    area("kitchen", "Kitchen"), area("front_porch", "Front Porch"), area("garage", "Garage"),
])


def lock(query, entities=LOCKS, **kw):
    kw.setdefault("model_target", None)
    kw.setdefault("carried", None)
    return at.resolve(query, domain="lock", entities=entities, directory=DIRECTORY, **kw)


def cover(query, entities=GARAGE, **kw):
    kw.setdefault("model_target", None)
    kw.setdefault("carried", None)
    return at.resolve(query, domain="cover", entities=entities, directory=DIRECTORY, **kw)


def ids(outcome):
    return [r.entity_id for r in outcome.refs]


# ---------------------------------------------------------------------------
# Rule 1: a carried decision
# ---------------------------------------------------------------------------

class TestCarried:
    def test_a_carried_door_is_named_by_carry(self):
        out = lock("unlock it", carried="lock.back_door")
        assert isinstance(out, at.Named) and out.named_by == at.CARRIED and ids(out) == ["lock.back_door"]

    def test_a_carry_works_with_no_utterance_at_all(self):
        out = lock(None, carried="lock.back_door")
        assert isinstance(out, at.Named) and out.named_by == at.CARRIED

    def test_a_carry_outside_the_permitted_set_is_ignored(self):
        out = lock("unlock it", carried="lock.not_permitted")
        assert isinstance(out, at.Unnamed)

    def test_words_naming_a_different_door_beat_the_carry(self):
        out = lock("unlock the front door", carried="lock.back_door")
        assert isinstance(out, at.Named) and ids(out) == ["lock.front_door"] and out.named_by == at.TOKENS_AND_NOUN

    def test_words_naming_the_carried_door_keep_it(self):
        out = lock("unlock the back door", carried="lock.back_door")
        assert isinstance(out, at.Named) and out.named_by == at.CARRIED

    def test_a_non_text_carry_is_ignored(self):
        assert isinstance(lock("unlock it", carried=["lock.back_door"]), at.Unnamed)
        assert isinstance(lock("unlock it", carried=5), at.Unnamed)


class TestCarryIsOnlyForABareReferent:
    PATIO = {"lock.front_door": "Front Door Lock", "lock.patio": "Back Patio Door Lock"}

    @pytest.mark.parametrize("query", [
        "unlock it", "unlock that", "unlock the door", "unlock the lock", "open it", "unlock the front door",
        "unlock it please", "is it locked", "unlock",
    ])
    def test_a_bare_referent_or_the_carried_doors_own_name_keeps_it(self, query):
        out = lock(query, self.PATIO, carried="lock.front_door")
        assert isinstance(out, at.Named) and out.named_by == at.CARRIED and ids(out) == ["lock.front_door"], query

    @pytest.mark.parametrize("query", [
        "unlock the back door",      # part of another name
        "unlock the side door",      # a name nothing answers to
        "unlock the shed",           # an unknown word
        "unlock the doors",          # plural
        "unlock all the locks",
        "unlock the deadbolt on the garage",
    ])
    def test_any_other_word_drops_the_carry_for_a_lock(self, query):
        out = lock(query, self.PATIO, carried="lock.front_door")
        assert not (isinstance(out, at.Named) and out.named_by == at.CARRIED), query
        assert not (isinstance(out, at.Named) and ids(out) == ["lock.front_door"] and "front" not in query), query

    def test_a_partial_name_resolves_to_the_other_door_not_the_carry(self):
        out = lock("unlock the back door", self.PATIO, carried="lock.front_door")
        assert isinstance(out, at.Ambiguous) and ids(out) == ["lock.patio"]

    COVERS = {"cover.garage": "Garage Door", "cover.gate": "Front Gate"}

    @pytest.mark.parametrize("query", ["open the gate", "open the opener", "open the blinds too", "open the garage blinds",
                                       "open the doors"])
    def test_a_noun_or_talk_of_blinds_that_is_not_the_carried_covers_drops_it(self, query):
        out = cover(query, self.COVERS, carried="cover.garage")
        assert not (isinstance(out, at.Named) and out.named_by == at.CARRIED), query

    @pytest.mark.parametrize("query", ["open it", "open the garage door", "open the door", "open the garage", "open that"])
    def test_a_cover_referent_keeps_it(self, query):
        out = cover(query, self.COVERS, carried="cover.garage")
        assert isinstance(out, at.Named) and out.named_by == at.CARRIED, query

    def test_the_answer_to_which_door_is_trusted_whatever_the_first_words_said(self):
        out = lock("unlock the kitchen", self.PATIO, carried="lock.front_door", carried_trusted=True)
        assert isinstance(out, at.Named) and out.named_by == at.CARRIED
        out = lock("unlock the kitchen", self.PATIO, carried="lock.not_here", carried_trusted=True)
        assert not isinstance(out, at.Named) or out.named_by != at.CARRIED


class TestReplyAndHints:
    def test_a_reply_that_repeats_the_label_is_an_answer(self):
        labels = (at.Ref("lock.a", "Front Door Lock"), at.Ref("lock.b", "Back Door Lock"))
        for reply in ("front door lock", "the front door lock", "Front Door Lock please"):
            out = at.match_reply(reply, labels)
            assert out.kind == "one" and out.ref.entity_id == "lock.a", reply

    def test_a_reply_that_leads_with_or_holds_a_command_verb_is_no_answer(self):
        labels = (at.Ref("lock.a", "Front Door Lock"), at.Ref("lock.b", "Back Door Lock"))
        for reply in ("lock the front door", "unlock front door lock", "front door unlock", "open front", "lock front door lock"):
            assert at.match_reply(reply, labels).kind == "none", reply

    @pytest.mark.parametrize("label,domain,expected", [
        ("Front Door Lock", "lock", "Front Door Lock"), ("Front Entry", "lock", "Front Entry door"),
        ("Kwikset 910", "lock", "Kwikset 910 door"), ("Main", "cover", "Main door"), ("Garage", "cover", "Garage"),
    ])
    def test_the_hint_phrase_always_carries_an_access_noun(self, label, domain, expected):
        assert at.hint_phrase(label, domain) == expected

    @pytest.mark.parametrize("label,domain", [("Front Entry", "lock"), ("Kwikset 910", "lock"), ("Main", "cover")])
    def test_saying_the_hinted_phrase_names_it_among_several(self, label, domain):
        entities = {f"{domain}.a": label, f"{domain}.b": "Other Place Thing"}
        verb = "unlock" if domain == "lock" else "open"
        out = at.resolve(f"{verb} the {at.hint_phrase(label, domain)}", domain=domain, model_target=None,
                         carried=None, entities=entities)
        assert isinstance(out, at.Named) and out.named_by in at.OPENING_SOURCES and ids(out) == [f"{domain}.a"]

    def test_my_garage_picks_the_entity_whose_name_says_garage(self):
        entities = {"cover.g": "Garage Door", "cover.b": "Living Room Blinds"}
        out = at.resolve("open my garage", domain="cover", model_target=None, carried=None, entities=entities)
        assert isinstance(out, at.Named) and ids(out) == ["cover.g"] and out.named_by == at.BARE_NOUN

    def test_two_garage_doors_among_blinds_ask_only_about_the_garage_ones(self):
        entities = {"cover.l": "Garage Door Left", "cover.r": "Garage Door Right", "cover.b": "Living Room Blinds"}
        out = at.resolve("open the garage", domain="cover", model_target=None, carried=None, entities=entities)
        assert isinstance(out, at.Unnamed) and sorted(ids(out)) == ["cover.l", "cover.r"]

    def test_a_model_target_with_the_domain_prefix_still_narrows(self):
        out = at.narrow_to_target("close the garage left door", "cover.garage_left", "cover")
        assert out == "left garage", "the prefix word cover must not block narrowing"

    def test_cover_intent_kind(self):
        assert at.cover_intent_kind("open the garage blinds") == "plain"
        assert at.cover_intent_kind("open the garage door blinds") == "access"
        assert at.cover_intent_kind("open the gate") == "access"
        assert at.cover_intent_kind("open the blinds") == "plain"
        assert at.cover_intent_kind("open it") is None


# ---------------------------------------------------------------------------
# Rule 2: universal wording
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    "unlock all the doors", "unlock all of the locks", "lock up", "lock down", "lock it all", "lock it up",
    "lock everything", "unlock every door", "unlock the doors", "lock my locks", "lock both doors",
])
def test_universal_wording_is_every(query):
    out = lock(query)
    assert isinstance(out, at.Every) and sorted(ids(out)) == sorted(LOCKS)


def test_hall_is_not_all():
    """Whole tokens only: 'hall' never reads as 'all' (the old substring bug)."""
    out = lock("lock the hall door")
    assert not isinstance(out, (at.Every, at.Unnamed))
    assert isinstance(out, at.NotFound) and out.phrase == "hall"


def test_a_quantifier_far_from_a_noun_is_not_universal():
    assert not isinstance(lock("unlock the front door and all"), at.Every)


def test_every_for_covers():
    assert isinstance(cover("open all the doors"), at.Every)
    assert isinstance(cover("open everything"), at.Every)


# ---------------------------------------------------------------------------
# Rule 3: the words match the names
# ---------------------------------------------------------------------------

class TestNamesInTheWords:
    def test_name_and_noun_is_tokens_plus_noun(self):
        out = lock("unlock the kitchen door")
        assert isinstance(out, at.Named) and ids(out) == ["lock.kitchen_door"] and out.named_by == at.TOKENS_AND_NOUN

    def test_a_name_without_a_noun_is_only_tokens(self):
        out = lock("unlock the kitchen")
        assert isinstance(out, at.Named) and ids(out) == ["lock.kitchen_door"] and out.named_by == at.TOKENS
        assert out.named_by not in at.OPENING_SOURCES

    def test_the_front_alone_is_tokens(self):
        out = lock("unlock the front")
        assert isinstance(out, at.Named) and out.named_by == at.TOKENS

    def test_a_tie_is_ambiguous(self):
        two = {"lock.a": "Front Door Lock", "lock.b": "Front Door"}
        out = lock("unlock the front door", two)
        assert isinstance(out, at.Ambiguous) and sorted(ids(out)) == ["lock.a", "lock.b"]

    def test_the_most_specific_name_wins(self):
        two = {"lock.a": "Front Door", "lock.b": "Front Side Door"}
        out = lock("unlock the front side door", two)
        assert isinstance(out, at.Named) and ids(out) == ["lock.b"]
        out = lock("unlock the front door", two)
        assert isinstance(out, at.Named) and ids(out) == ["lock.a"]

    def test_part_of_a_name_is_ambiguous_not_named(self):
        halves = {"lock.l": "Side Door Left", "lock.r": "Side Door Right"}
        out = lock("unlock the side door", halves)
        assert isinstance(out, at.Ambiguous) and sorted(ids(out)) == ["lock.l", "lock.r"]

    def test_substrings_never_match(self):
        out = lock("unlock the frontier door")
        assert not isinstance(out, at.Named)

    def test_covers_by_name(self):
        out = cover("open the left garage door")
        assert isinstance(out, at.Named) and ids(out) == ["cover.garage_left"] and out.named_by == at.TOKENS_AND_NOUN

    def test_the_case_and_marks_of_a_name_do_not_matter(self):
        out = lock("UNLOCK THE Kitchen DOOR!")
        assert isinstance(out, at.Named) and ids(out) == ["lock.kitchen_door"]


# ---------------------------------------------------------------------------
# Rule 4: a room or area never names a door
# ---------------------------------------------------------------------------

class TestAreaNeverNames:
    ONE = {"lock.deadbolt": "Deadbolt"}
    TWO = {"lock.deadbolt": "Deadbolt", "lock.latch": "Latch"}

    def test_one_door_in_the_area_is_in_area_never_named(self):
        out = lock("unlock the front porch", self.ONE, area_entities={"front_porch": ("lock.deadbolt",)})
        assert isinstance(out, at.InArea) and ids(out) == ["lock.deadbolt"]
        assert not isinstance(out, at.Named)

    def test_several_doors_in_the_area_are_in_area(self):
        out = lock("unlock the front porch", self.TWO, area_entities={"front_porch": ("lock.deadbolt", "lock.latch")})
        assert isinstance(out, at.InArea) and sorted(ids(out)) == ["lock.deadbolt", "lock.latch"]

    def test_only_the_doors_of_that_area_are_offered(self):
        three = {**self.TWO, "lock.shed": "Shed Padlock"}
        out = lock("unlock the front porch", three, area_entities={"front_porch": ("lock.deadbolt", "lock.latch")})
        assert sorted(ids(out)) == ["lock.deadbolt", "lock.latch"]

    def test_area_members_that_are_not_permitted_are_dropped(self):
        out = lock("unlock the front porch", self.ONE, area_entities={"front_porch": ("lock.deadbolt", "lock.secret")})
        assert ids(out) == ["lock.deadbolt"]

    def test_a_room_word_that_is_a_locks_name_is_still_only_tokens(self):
        out = lock("unlock the kitchen", {"lock.k": "Kitchen Door Lock"}, area_entities={"kitchen": ("lock.k",)})
        assert isinstance(out, at.Named) and out.named_by == at.TOKENS

    def test_an_area_that_holds_none_of_them_resolves_nothing(self):
        out = lock("unlock the kitchen", self.ONE, area_entities={"garage": ("lock.deadbolt",)})
        assert not isinstance(out, at.InArea)

    def test_the_garage_noun_is_not_an_area_for_a_cover(self):
        """'my garage' is the bare noun for a cover, not the Garage area."""
        out = cover("open my garage", ONE_GARAGE, area_entities={"garage": ("cover.garage",)})
        assert isinstance(out, at.Named) and out.named_by == at.BARE_NOUN

    def test_the_garage_area_is_an_area(self):
        out = cover("open the garage area", ONE_GARAGE, area_entities={"garage": ("cover.garage",)})
        assert isinstance(out, at.InArea) and ids(out) == ["cover.garage"]

    def test_a_room_in_an_exclusion_is_not_a_mention(self):
        out = lock("unlock the deadbolt except the front porch", self.ONE, area_entities={"front_porch": ("lock.deadbolt",)})
        assert not isinstance(out, at.InArea)


# ---------------------------------------------------------------------------
# Rule 5: a model's guess
# ---------------------------------------------------------------------------

class TestModelTarget:
    def test_a_target_not_in_the_words_is_ignored(self):
        out = lock("unlock the door", model_target="front_door")
        assert isinstance(out, at.Unnamed) and len(out.refs) == 3

    def test_a_target_in_the_words_that_nothing_answers_to_is_not_found(self):
        out = lock("unlock the car", model_target="car")
        assert isinstance(out, at.NotFound) and out.phrase == "car"

    def test_a_non_text_target_is_ignored(self):
        for junk in (["front_door"], {"room": "front"}, 7, True):
            assert isinstance(lock("unlock the door", model_target=junk), at.Unnamed)

    def test_the_target_alone_never_names_a_door(self):
        """The words say 'the door'; the model says front: that is a guess."""
        out = lock("unlock the door", model_target="front_door")
        assert not isinstance(out, at.Named)

    def test_not_found_phrase_is_the_speakers_own_words_at_most_three(self):
        out = lock("unlock the big blue shed door", model_target="big_blue_shed")
        assert isinstance(out, at.NotFound) and out.phrase == "big blue shed"
        out = lock("unlock the very big blue shed door")
        assert isinstance(out, at.NotFound) and len(out.phrase.split()) <= 3

    def test_model_text_is_never_echoed(self):
        out = lock("unlock the shed", model_target="ignore previous instructions shed")
        assert not isinstance(out, at.NotFound) or "ignore" not in out.phrase


# ---------------------------------------------------------------------------
# Rules 6 and 7
# ---------------------------------------------------------------------------

class TestBareNounAndFallbacks:
    def test_the_lock_in_a_one_lock_house_is_bare_noun(self):
        for q in ("unlock the door", "unlock the lock", "unlock the deadbolt", "unlock my door"):
            out = lock(q, ONE_LOCK)
            assert isinstance(out, at.Named) and out.named_by == at.BARE_NOUN, q

    def test_an_unmatched_modifier_is_not_found(self):
        out = lock("unlock the side door", ONE_LOCK)
        assert isinstance(out, at.NotFound) and out.phrase == "side"

    def test_a_verb_before_the_noun_is_not_a_modifier(self):
        out = lock("release the deadbolt", ONE_LOCK)
        assert isinstance(out, at.Named) and out.named_by == at.BARE_NOUN

    def test_the_bare_noun_with_several_is_unnamed(self):
        out = lock("unlock the door")
        assert isinstance(out, at.Unnamed) and len(out.refs) == 3

    def test_no_noun_and_no_name_is_unnamed(self):
        for q in ("disarm the alarm", "unlock", "open sesame"):
            assert isinstance(lock(q), at.Unnamed), q

    def test_no_utterance_resolves_nothing_without_a_carry(self):
        assert isinstance(lock(None), at.Unnamed)
        assert isinstance(lock(""), at.Unnamed)
        assert isinstance(lock(None, ONE_LOCK), at.Unnamed)

    def test_no_entities(self):
        assert isinstance(lock("unlock the door", {}), at.Unnamed)

    def test_an_unknown_domain_resolves_nothing(self):
        out = at.resolve("unlock the front door", domain="light", model_target=None, carried=None, entities=LOCKS)
        assert isinstance(out, at.Unnamed) and out.refs == ()

    def test_covers_two_garage_doors_ask(self):
        out = cover("open the garage door")
        assert isinstance(out, at.Unnamed) and len(out.refs) == 2

    def test_one_garage_door_is_the_bare_noun(self):
        out = cover("open the garage", ONE_GARAGE)
        assert isinstance(out, at.Named) and out.named_by == at.BARE_NOUN


def test_every_named_by_source_is_reachable():
    seen = {
        lock("unlock it", carried="lock.back_door").named_by,
        lock("unlock the kitchen door").named_by,
        lock("unlock the kitchen").named_by,
        lock("unlock the door", ONE_LOCK).named_by,
    }
    assert seen == {at.CARRIED, at.TOKENS_AND_NOUN, at.TOKENS, at.BARE_NOUN}
    assert at.TOKENS not in at.OPENING_SOURCES and {at.CARRIED, at.TOKENS_AND_NOUN, at.BARE_NOUN} == at.OPENING_SOURCES


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

class TestAccessCovers:
    @pytest.mark.parametrize("device_class", ["garage", "gate", "door", "", None, "unknown", 5, ["door"]])
    def test_a_way_in_or_anything_unknown_is_access(self, device_class):
        assert at.is_access_cover("cover.x", {"device_class": device_class}) is True

    @pytest.mark.parametrize("device_class", sorted(at.NON_ACCESS_DEVICE_CLASSES))
    def test_only_the_named_classes_are_not(self, device_class):
        assert at.is_access_cover("cover.x", {"device_class": device_class}) is False
        assert at.is_access_cover("cover.x", {"device_class": device_class.upper()}) is False

    def test_a_missing_class_or_attributes_fail_closed(self):
        assert at.is_access_cover("cover.x", {}) is True
        assert at.is_access_cover("cover.x", None) is True
        assert at.is_access_cover("cover.x", "garage") is True

    def test_the_non_access_set_is_the_documented_one(self):
        assert at.NON_ACCESS_DEVICE_CLASSES == {"awning", "blind", "curtain", "damper", "shade", "shutter", "window"}

    def test_opening_domain_and_actions(self):
        assert at.opening_domain("lock.x", "light") == "lock"
        assert at.opening_domain(None, "cover") == "cover"
        assert at.opening_domain("light.x", "lock") is None
        assert at.opening_domain(["lock.x"], "light") is None
        assert at.is_opening_action("lock", "unlock") and at.is_opening_action("lock", "turn_off")
        assert at.is_opening_action("cover", "open") and at.is_opening_action("cover", "turn_on")
        assert not at.is_opening_action("lock", "lock") and not at.is_opening_action("cover", "close")
        assert not at.is_opening_action("light", "unlock") and not at.is_opening_action("lock", None)


# ---------------------------------------------------------------------------
# The answer to "which door?"
# ---------------------------------------------------------------------------

class TestMatchReply:
    CANDIDATES = (at.Ref("lock.a", "Front Deadbolt"), at.Ref("lock.b", "Back Door Lock"))

    def test_the_front_door_matches_front_deadbolt(self):
        out = at.match_reply("the front door", self.CANDIDATES)
        assert out.kind == "one" and out.ref.entity_id == "lock.a"

    def test_one_word_matches(self):
        assert at.match_reply("back", self.CANDIDATES).ref.entity_id == "lock.b"
        assert at.match_reply("front please", self.CANDIDATES).ref.entity_id == "lock.a"

    def test_two_fitting_is_several(self):
        out = at.match_reply("door", (at.Ref("lock.a", "Front Door"), at.Ref("lock.b", "Back Door")))
        assert out.kind == "several" and len(out.matches) == 2

    def test_a_reply_of_only_generic_nouns_reads_the_names_whole(self):
        out = at.match_reply("the deadbolt", (at.Ref("lock.a", "Deadbolt"), at.Ref("lock.b", "Back Lock")))
        assert out.kind == "one" and out.ref.entity_id == "lock.a"

    def test_a_command_or_other_words_are_no_answer(self):
        assert at.match_reply("unlock the front door", self.CANDIDATES).kind == "none"
        assert at.match_reply("lock them", self.CANDIDATES).kind == "none"
        assert at.match_reply("the garage", self.CANDIDATES).kind == "none"
        assert at.match_reply("yes", self.CANDIDATES).kind == "none"
        assert at.match_reply("", self.CANDIDATES).kind == "none"
        assert at.match_reply(None, self.CANDIDATES).kind == "none"

    def test_a_word_in_no_name_is_no_answer(self):
        assert at.match_reply("kitchen", self.CANDIDATES).kind == "none"
        assert at.match_reply("front kitchen", self.CANDIDATES).kind == "none", "every word must belong to the name"
        assert at.match_reply("front back", self.CANDIDATES).kind == "none"


# ---------------------------------------------------------------------------
# The module stays pure
# ---------------------------------------------------------------------------

class TestPurity:
    SOURCE = (SRC / "orchestrator" / "access_target.py").read_text()

    def test_no_logger_and_no_io(self):
        tree = ast.parse(self.SOURCE)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        assert not imported & {"logging", "structlog", "asyncio", "httpx", "requests", "os", "socket", "time"}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not names & {"logger", "log", "print", "open"}

    def test_it_imports_only_the_vocabulary_modules(self):
        tree = ast.parse(self.SOURCE)
        orchestrator = {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == "orchestrator"
            for alias in node.names
        }
        assert orchestrator == {"room_context"}
