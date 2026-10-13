"""One exclusion vocabulary: "apart from", "besides" and their peers exclude
rooms everywhere an exclusion is read.

room_context.EXCLUSION_PHRASES is the only list. The room parser, the two
whole-house fast-path guards, the music handler's exclusion regex and the
intent prompt all derive from it or name every phrase in it; a drift scan fails
when a second list of the words appears in the orchestrator. Fictional houses
only.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
import sys
import unittest.mock as mock
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import orchestrator.smart_home_controller as shc  # noqa: E402
from orchestrator import room_context as rc  # noqa: E402
from orchestrator.nodes import _runtime  # noqa: E402
from shared.ha_registry import Area  # noqa: E402

from .test_room_light_area_tier import GALLEY_LEAVES, Rig  # noqa: E402
from .test_room_light_area_tier import _isolated  # noqa: E402,F401  (the autouse runtime/config reset)

ROOT = Path(__file__).resolve().parents[2]
GALLEY = rc.RoomRef("galley", "galley")

HOUSE = rc.RoomDirectory.build(areas=[
    Area("area_galley", "Galley", (), "galley"),
    Area("area_study", "Study", (), "study"),
    Area("area_hall", "Hall", (), "hall"),
])

# Every phrase the plan names plus the two inflections that read as exclusions.
PHRASINGS = [
    "except", "except for", "excepting", "but not", "but", "other than", "excluding",
    "apart from", "aside from", "besides", "not including", "not the",
]


def test_the_vocabulary_holds_every_phrasing_the_matrix_uses():
    spoken = {" ".join(p) for p in rc.EXCLUSION_PHRASES}
    for phrasing in PHRASINGS:
        assert phrasing in spoken or phrasing.split()[0] in spoken, phrasing


# ---------------------------------------------------------------------------
# The room parser
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("phrasing", PHRASINGS)
def test_every_phrasing_excludes_the_named_room(phrasing):
    query = f"turn off all the lights {phrasing} the galley"
    assert rc.has_exclusion(query), query
    mention = rc.find_room_mention(query, HOUSE)
    decision = rc.decide_room(mention, query=query, speaker_room="study", directory=HOUSE)
    assert decision == rc.WholeHouse((GALLEY,)), (query, decision)
    assert rc.explicit_whole_house(query, HOUSE) == rc.WholeHouse((GALLEY,)), query


def test_apart_from_and_besides_lead_with_the_exclusion_not_a_room():
    for phrasing in ("apart from", "aside from", "besides", "not including", "excepting"):
        toks = f"turn off every light {phrasing} the galley".split()
        assert rc._exclusion_start(toks) == toks.index(phrasing.split()[0]), phrasing


@pytest.mark.parametrize("query", [
    "press the button", "ask the butler to turn on the lights", "what is this about",
    "turn off all the lights", "butter the toast",
])
def test_a_word_that_merely_contains_but_is_not_an_exclusion(query):
    assert rc.has_exclusion(query) is False, query


@pytest.mark.parametrize("phrasing", ["apart from", "aside from", "besides", "excluding", "except"])
def test_an_unknown_room_phrase_stops_at_the_exclusion_word(phrasing):
    query = f"turn off the lights in the sunroom {phrasing} the galley"
    assert rc.unknown_room_phrase(query, HOUSE) == "sunroom", phrasing


# ---------------------------------------------------------------------------
# The two whole-house fast-path guards
# ---------------------------------------------------------------------------

def _controller():
    _runtime.set_room_directory(type("P", (), {"current": lambda self: HOUSE})())
    return shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=None)


@pytest.mark.parametrize("phrasing", PHRASINGS)
def test_the_fast_path_guards_defer_every_phrasing(phrasing):
    ctl = _controller()
    assert ctl._defers_to_exclusion_logic(f"turn off all the lights {phrasing} the galley"), phrasing
    assert ctl._defers_to_exclusion_logic(f"kill all the lights {phrasing} the galley"), phrasing


@pytest.mark.parametrize("query", [
    "press the button and turn off all the lights", "the butler said turn off all the lights",
    "what about turning everything off", "turn off all the lights",
])
def test_the_fast_path_guards_do_not_defer_a_word_that_contains_but(query):
    assert _controller()._defers_to_exclusion_logic(query) is False, query


def test_a_deferral_word_with_no_excluded_room_defers_to_the_model():
    ctl = _controller()
    for word in ("without", "unless", "minus", "skip", "leave", "keep"):
        assert ctl._defers_to_exclusion_logic(f"turn off all the lights {word} the galley"), word


def test_apart_from_is_read_as_an_exclusion_not_as_the_room_it_precedes():
    _runtime.set_room_directory(type("P", (), {"current": lambda self: HOUSE})())

    class Admin:
        async def get_component_model(self, name):
            return None

    ctl = shc.SmartHomeController(entity_manager=mock.MagicMock(), llm_router=None)
    with mock.patch.object(shc, "get_admin_client", lambda: Admin()):
        got = asyncio.run(ctl.extract_intent("turn off every light apart from the galley"))
    assert got["room"] == "whole_house" and got["excluded_rooms"] == ["galley"], got


# ---------------------------------------------------------------------------
# End to end: the excluded room's lights are never written
# ---------------------------------------------------------------------------

QUERY = "get everything dark apart from the galley"


class _Admin:
    async def get_component_model(self, name):
        return None


def test_x03_perfect_json_writes_nothing_on_the_excluded_room(monkeypatch):
    rig = Rig(monkeypatch, attic_state="on")
    perfect = json.dumps({"device_type": "light", "room": "whole_house", "excluded_rooms": ["galley"],
                          "action": "turn_off", "target_scope": "group", "parameters": {},
                          "color_description": None})

    class Llm:
        async def generate(self, **kwargs):
            return {"response": perfect}

    rig.controller.llm_router = Llm()
    with mock.patch.object(shc, "get_admin_client", lambda: _Admin()):
        intent = asyncio.run(rig.controller.extract_intent(QUERY))
    assert intent["excluded_rooms"] == ["galley"], intent
    rig.run(intent, QUERY)
    assert rig.written, "the rest of the house is turned off"
    assert not set(rig.written) & GALLEY_LEAVES, rig.written
    assert "light.galley_pair" not in rig.written and "light.galley_all" not in rig.written


def test_x10_the_json_fallback_intent_writes_nothing_on_the_excluded_room(monkeypatch):
    """What extract_intent returns when the model's reply is not JSON: no room,
    no exclusions. The exclusion in the words themselves still holds."""
    rig = Rig(monkeypatch, attic_state="on")
    fallback = {"device_type": "light", "room": None, "action": "turn_off", "target_scope": "group", "parameters": {}}
    rig.run(fallback, "turn off everything apart from the galley")
    assert not set(rig.written) & GALLEY_LEAVES, rig.written
    assert "light.galley_pair" not in rig.written and "light.galley_all" not in rig.written


# ---------------------------------------------------------------------------
# The prompt and the music handler name the same vocabulary
# ---------------------------------------------------------------------------

def test_the_intent_prompt_names_every_phrase():
    golden = (Path(__file__).parent / "fixtures" / "smart_home_intent_prompt.golden.txt").read_text()
    line = next(l for l in golden.splitlines() if l.startswith("EXCLUSIONS:"))
    for phrase in rc.EXCLUSION_PHRASES:
        assert f'"{" ".join(phrase)} [room]"' in line, phrase


def test_the_music_exclusion_regex_is_built_from_the_vocabulary():
    import orchestrator.music_handler as mh

    alternatives = mh._EXCLUSION_ALTERNATION.split("|")
    assert len(alternatives) == len(rc.EXCLUSION_PHRASES)
    assert alternatives[0] == max(alternatives, key=len), "longest phrase first"
    assert alternatives.index(r"but\s+not") < alternatives.index("but")
    assert alternatives.index("excepting") < alternatives.index("except")


@pytest.mark.parametrize("phrasing", ["except", "excepting", "but not", "but", "other than", "excluding",
                                      "apart from", "aside from", "besides", "not including", "not the"])
def test_music_excludes_the_room_after_every_phrase(phrasing):
    import orchestrator.music_handler as mh

    handler = mh.MusicHandler.__new__(mh.MusicHandler)
    configs = {"office": {}, "kitchen": {}}
    got = asyncio.run(handler._parse_exclusion_pattern(
        f"play jazz everywhere {phrasing} the office", configs))
    assert got == ["office"], (phrasing, got)


# ---------------------------------------------------------------------------
# Drift: no second list of the exclusion words in the orchestrator
# ---------------------------------------------------------------------------

PHRASE_STRINGS = frozenset(" ".join(p) for p in rc.EXCLUSION_PHRASES)

# The vocabulary's own home is not a second copy of itself.
DEFINITION_FILE = "src/orchestrator/room_context.py"

# (path, enclosing name) -> why it is not an exclusion parser.
ALLOWLIST = {
    ("src/orchestrator/validator.py", "_extract_key_entities"): "stopword list of a validator; 'but' there is a function word",
    ("src/orchestrator/context/detector.py", "<module>"): "correction cue: 'but' marks a follow-up, not a room exclusion",
}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"\\s[+*]", " ", text).lower().split())


def _is_phrase(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and _norm(node.value) in PHRASE_STRINGS


def _enclosing(stack):
    for node in reversed(stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return "<module>"


def scan_source(source: str):
    """(line, enclosing name, kind): a phrase literal that is a direct operand of
    ``in``, a member of a list/tuple/set, or part of a regex alternation of two
    or more phrases."""
    findings = []

    def visit(node, stack):
        stack = stack + [node]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and any(_is_phrase(e) for e in node.elts):
            findings.append((node.lineno, _enclosing(stack), "member"))
        elif isinstance(node, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn)) for o in node.ops):
            if any(_is_phrase(x) for x in [node.left, *node.comparators]):
                findings.append((node.lineno, _enclosing(stack), "operand"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "|" in node.value:
            parts = [_norm(re.sub(r"[()?:^$]", "", part)) for part in node.value.split("|")]
            if sum(1 for part in parts if part in PHRASE_STRINGS) >= 2:
                findings.append((node.lineno, _enclosing(stack), "alternation"))
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(ast.parse(source), [])
    return findings


def _tree_findings():
    found = {}
    for path in sorted((ROOT / "src/orchestrator").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == DEFINITION_FILE:
            continue
        for line, name, kind in scan_source(path.read_text(encoding="utf-8")):
            found.setdefault((rel, name), []).append((line, kind))
    return found


def test_no_second_exclusion_list_outside_the_allowlist():
    unexpected = {k: v for k, v in _tree_findings().items() if k not in ALLOWLIST}
    assert not unexpected, f"exclusion words listed outside room_context.EXCLUSION_PHRASES: {unexpected}"


def test_every_allowlist_entry_still_matches_a_finding():
    stale = [key for key in ALLOWLIST if key not in _tree_findings()]
    assert not stale, f"allowlist entries that match nothing (delete them): {stale}"


def test_the_scan_floor_sees_real_code_in_at_least_two_files():
    assert len({path for path, _ in _tree_findings()}) >= 2
    assert len(list((ROOT / "src/orchestrator").rglob("*.py"))) > 20


def test_the_scanner_reports_a_planted_list():
    assert scan_source("def f(q):\n    return any(p in q for p in ['except', 'but not'])\n") == [(2, "f", "member")]


def test_the_scanner_reports_a_planted_in_operand():
    assert scan_source("def f(q):\n    return 'apart from' in q\n") == [(2, "f", "operand")]


def test_the_scanner_reports_a_planted_regex_alternation():
    assert scan_source('PATTERN = "(?:except|besides|other\\\\s+than)\\\\s+x"\n') == [(1, "<module>", "alternation")]


def test_the_scanner_ignores_unrelated_literals():
    assert scan_source("a = ['red', 'green']\nb = 'press' in 'the button'\nc = 'a|b'\n") == []
