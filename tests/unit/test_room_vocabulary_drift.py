"""No module outside orchestrator.room_context keeps its own list of room
words.

A room list in a second place drifts: the controller carried ten copies, each
with a house's room names in it, and they disagreed about "master bathroom".
Room words now come from one place (RoomDirectory, with the generic
COMMON_ROOM_WORDS), and this test fails when a list or regex alternation of
three or more room nouns appears anywhere else in the orchestrator or the
gateway.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("src/orchestrator", "src/gateway")

# Room nouns that identify a "room list". A literal holding three or more of
# these is a finding.
ROOM_NOUNS = frozenset({
    "office", "kitchen", "bedroom", "living room", "bathroom", "master bedroom", "master bath",
    "master bathroom", "guest room", "hallway", "hall", "basement", "attic", "garage", "porch",
    "deck", "patio", "dining room", "den", "family room", "study", "library", "laundry room",
    "alpha", "beta", "first floor", "second floor", "downstairs", "upstairs", "living_room",
    "master_bedroom", "dining_room",
})
THRESHOLD = 3

# (path, name of the enclosing function or assignment) -> why it is not a
# second vocabulary. Every entry must still match a finding (a stale entry
# fails), so deleting the code forces deleting the entry.
ALLOWLIST = {
    ("src/orchestrator/room_context.py", "COMMON_ROOM_WORDS"):
        "the one generic vocabulary; it only recognizes a spoken room word",
    # Out of scope for the room-context work: motion and occupancy helpers
    # that match sensor names, not rooms a command targets.
    ("src/orchestrator/smart_home_controller.py", "_get_window_sensor_status"):
        "sensor_room_tokens: name fragments of occupancy sensors (motion, out of scope)",
    ("src/orchestrator/smart_home_controller.py", "OUTDOOR_SENSOR_PATTERNS"):
        "names of exterior motion sensors to skip (motion, out of scope)",
    # Generic-noun hints in classifiers. They decide which pipeline a
    # sentence goes down, never which lights a command writes, and hold no
    # house names. Follow-up: route them through COMMON_ROOM_WORDS.
    ("src/orchestrator/complexity_detector.py", "extract_complexity_features"):
        "counts device and room nouns to estimate how complex a command is",
    ("src/orchestrator/context/detector.py", "ROOM_INDICATORS"):
        "generic hint that a short follow-up names a place",
    ("src/orchestrator/intent_classifier.py", "__init__"):
        "generic room nouns for entity tagging in the classifier",
    ("src/orchestrator/main.py", "_pattern_based_classification"):
        "generic hint that 'do that in the kitchen' is a follow-up command",
    ("src/gateway/main.py", "is_athena_query_keywords"):
        "keyword hint for whether a request is a home-control query",
}


def _normalized(text: str) -> str:
    return text.strip().lower()


def _enclosing(stack):
    """The function a literal sits in, else the module-level name it is
    assigned to."""
    for node in reversed(stack):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    for node in reversed(stack):
        if isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return node.target.id
    return "<module>"


def scan_source(source: str):
    """(line, enclosing name, kind) for every room list or alternation."""
    findings = []
    tree = ast.parse(source)

    def visit(node, stack):
        stack = stack + [node]
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            strings = [_normalized(e.value) for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if sum(1 for t in strings if t in ROOM_NOUNS) >= THRESHOLD:
                findings.append((node.lineno, _enclosing(stack), "literal"))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "|" in node.value:
            parts = [_normalized(p) for p in re.split(r"[|()?:]", node.value)]
            if sum(1 for t in parts if t in ROOM_NOUNS) >= THRESHOLD:
                findings.append((node.lineno, _enclosing(stack), "alternation"))
        for child in ast.iter_child_nodes(node):
            visit(child, stack)

    visit(tree, [])
    return findings


def _tree_findings():
    found = {}
    for base in SCANNED:
        for path in sorted((ROOT / base).rglob("*.py")):
            rel = str(path.relative_to(ROOT))
            for line, name, kind in scan_source(path.read_text(encoding="utf-8")):
                found.setdefault((rel, name), []).append((line, kind))
    return found


def test_no_second_room_vocabulary_outside_the_allowlist():
    found = _tree_findings()
    unexpected = {key: v for key, v in found.items() if key not in ALLOWLIST}
    assert not unexpected, (
        "room-word lists found outside room_context (use RoomDirectory / COMMON_ROOM_WORDS): "
        f"{unexpected}"
    )


def test_every_allowlist_entry_still_matches_a_finding():
    found = _tree_findings()
    stale = [key for key in ALLOWLIST if key not in found]
    assert not stale, f"allowlist entries that match nothing (delete them): {stale}"


def test_the_scanner_detects_a_planted_three_noun_tuple():
    planted = "def f():\n    names = ('kitchen', 'office', 'den')\n"
    assert scan_source(planted) == [(2, "f", "literal")]


def test_the_scanner_detects_a_planted_regex_alternation():
    planted = 'import re\nPATTERN = re.compile("kitchen|office|den")\n'
    assert scan_source(planted) == [(2, "PATTERN", "alternation")]


def test_the_scanner_detects_a_planted_list_with_a_house_room_name():
    assert scan_source("rooms = ['alpha', 'beta', 'study', 'misc']\n")


def test_the_scanner_ignores_two_nouns_and_unrelated_lists():
    assert scan_source("a = ['kitchen', 'office']\nb = ['red', 'green', 'blue']\n") == []


def test_the_floor_the_scanner_sees_real_code():
    assert len(list((ROOT / "src/orchestrator").rglob("*.py"))) > 20
    assert ("src/orchestrator/room_context.py", "COMMON_ROOM_WORDS") in _tree_findings()
