"""Red/green contract for ATHENA-88 phase 1 (F87): word-boundary sequence
timing matcher and safe multi-intent splitter guard.

Covers:
  - `orchestrator.sequence_executor.detect_sequence_intent` (classify gate,
    action-gated bare temporals)
  - `orchestrator.smart_home_controller.SmartHomeController.detect_sequence_intent`
    (controller path, unconditional bare temporals)
  - `orchestrator.search_providers.intent_classifier.IntentClassifier.detect_multi_intent`
    (splitter guard, D2)
  - AST proof that both detectors share one timing implementation with no
    banned bare-substring indicator left behind (D3, bob L1).

Plan: .mozart/plans/active/2026-09-26-deliver-athena-voice-intent-defects.md,
Phase 1. Test contract: same directory,
2026-09-26-deliver-athena-voice-intent-defects.test-contract.md, Phase 1
plus r1/r2/r3 amendments.
"""
import ast
import sys
from pathlib import Path

import pytest

# Insert src/ so `orchestrator.*` imports resolve without the package installed.
sys.path.insert(0, "src")

from orchestrator.sequence_executor import detect_sequence_intent
from orchestrator.smart_home_controller import SmartHomeController
from orchestrator.search_providers.intent_classifier import IntentClassifier

REPO_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Test 1: test_classify_gate_rejects_non_sequences
# ---------------------------------------------------------------------------

NEGATIVES = [
    # r0 (16)
    "what plae have happy our and outdoor seating?",  # named member: incident turn 2
    "what place has happy hour and outdoor seating?",
    "what time is it",
    "that sounds good",
    "chat with me",
    "restaurants in Baltimore",
    "authentic thai food",
    "sometimes",
    "a bicycle shop",
    "what happened after the game",
    "afternoon tea spots",
    "where's my flashlight",
    "what is the weather tonight",
    "what's on this evening",
    "what was that again",
    "turn off the second floor lights",
    # r1 (+8)
    "set the thermostat at 72",
    "set the lights at 5 percent",
    "set the heat at 7 degrees",
    "the restaurant at 5 main street",
    "see you in a few days",
    "what time is sunset",
    "how many times did the door open",
    "what time does the store open tomorrow",
    # r2 (+3)
    "the restaurant at 5 north main street",  # named member: address gate
    "apartment at 12 unit b",
    "meet me at 5 north charles st",
]


def test_negatives_population_floor():
    assert len(NEGATIVES) == 27


@pytest.mark.parametrize("query", NEGATIVES, ids=NEGATIVES)
def test_classify_gate_rejects_non_sequences(query):
    assert detect_sequence_intent(query) is False


# ---------------------------------------------------------------------------
# Test 2: test_classify_gate_keeps_real_sequences
# ---------------------------------------------------------------------------

POSITIVES = [
    # r0 (14)
    "at 5pm",  # named member
    "turn on the lights at 5pm",
    "turn the lights off at 10:30 pm",
    "turn off the lights in 10 minutes",
    "wait 5 seconds then turn off the fan",
    "turn the lights on then off",
    "flash the lights 3 times",
    "blink the office lights",
    "dim the lights at sunset",
    "turn on the porch light tonight",
    "remind me later",
    "set the lights to red at seven o'clock",
    "repeat that three times",
    "turn the fan on and off",
    # r1 (+8)
    "wait a few minutes then turn off the fan",
    "turn on the porch lights after sunset",
    "keep flashing the lights",
    "blinking the kitchen lights",
    "flash the lights twice",
    "flash the lights a couple of times",
    "turn off the lights at noon",
    "turn on the fan at seven",
    # r2 (+1)
    "turn off the lights at 5",
    # r3 (+1)
    "remind me at 5",
]


def test_positives_population_floor():
    assert len(POSITIVES) == 24


@pytest.mark.parametrize("query", POSITIVES, ids=POSITIVES)
def test_classify_gate_keeps_real_sequences(query):
    assert detect_sequence_intent(query) is True


# ---------------------------------------------------------------------------
# Test 3: test_brightness_exclusions_unchanged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", ["set all lights at half", "lights at 50"])
def test_brightness_exclusions_unchanged(query):
    assert detect_sequence_intent(query) is False


# ---------------------------------------------------------------------------
# Test 4: test_controller_detector_shares_matcher
# ---------------------------------------------------------------------------

CONTROLLER_NEGATIVES = [
    "turn off the second floor lights",
    "sometimes the lights flicker, turn them off",
    "set the thermostat at 72",
    "set the lights at 5 percent",
]

CONTROLLER_POSITIVES = [
    "turn on the lights at 5pm",
    "turn the lights on then off",
    "flash the lights twice",
    "turn on the porch light later",  # bob L3: bare "later" unconditional on controller path
]


@pytest.mark.parametrize("query", CONTROLLER_NEGATIVES, ids=CONTROLLER_NEGATIVES)
def test_controller_detector_shares_matcher_negatives(query):
    assert SmartHomeController.detect_sequence_intent(None, query) is False


@pytest.mark.parametrize("query", CONTROLLER_POSITIVES, ids=CONTROLLER_POSITIVES)
def test_controller_detector_shares_matcher_positives(query):
    assert SmartHomeController.detect_sequence_intent(None, query) is True


def test_controller_detector_shares_matcher_scene_exclusion_kept():
    assert SmartHomeController.detect_sequence_intent(None, "good night") is False


# ---------------------------------------------------------------------------
# Test 5: test_splitter_keeps_descriptive_and_whole
# ---------------------------------------------------------------------------

DESCRIPTIVE_WHOLE_QUERIES = [
    "what place has happy hour and outdoor seating?",
    "what plae have happy our and outdoor seating?",
    "restaurants with a patio and live music",
    "turn on the lights and the fan",
    "bars with a patio and open late",  # r1
    "find a bar that has trivia and is open late",  # r1
]


def test_descriptive_whole_population_floor():
    assert len(DESCRIPTIVE_WHOLE_QUERIES) == 6


@pytest.mark.parametrize("query", DESCRIPTIVE_WHOLE_QUERIES, ids=DESCRIPTIVE_WHOLE_QUERIES)
def test_splitter_keeps_descriptive_and_whole(query):
    assert IntentClassifier().detect_multi_intent(query) == [query]


# ---------------------------------------------------------------------------
# F37 (reconciliation round 1, codex r2 High): the pre-standalone-guard
# word-count filter silently dropped a genuine one-word second fragment
# instead of falling back to the whole query -- "turn off lights and fan"
# returned only ["turn off lights"], losing "and fan" entirely.
# ---------------------------------------------------------------------------

SHORT_FRAGMENT_WHOLE_QUERIES = [
    "turn off lights and fan",
    "what is the weather and traffic",
]


def test_short_fragment_whole_population_floor():
    assert len(SHORT_FRAGMENT_WHOLE_QUERIES) == 2


@pytest.mark.parametrize(
    "query", SHORT_FRAGMENT_WHOLE_QUERIES, ids=SHORT_FRAGMENT_WHOLE_QUERIES
)
def test_splitter_keeps_short_second_fragment_whole(query):
    assert IntentClassifier().detect_multi_intent(query) == [query]


# ---------------------------------------------------------------------------
# Test 6: test_splitter_still_splits_real_multi_intent
# ---------------------------------------------------------------------------

def test_splitter_still_splits_real_multi_intent():
    classifier = IntentClassifier()

    assert len(classifier.detect_multi_intent("what is the weather and turn on the lights")) == 2
    assert len(classifier.detect_multi_intent("what time is it and what is the weather")) == 2
    assert len(classifier.detect_multi_intent("turn off the lights and open the garage")) == 2
    assert len(classifier.detect_multi_intent("play some jazz and set the lights to blue")) == 2
    # r3
    assert len(classifier.detect_multi_intent("turn off the lights and increase the heat")) == 2
    # r2 (codex F20) — named member "...and make it warmer"
    assert len(classifier.detect_multi_intent("turn off the lights and make it warmer")) == 2
    assert len(classifier.detect_multi_intent("turn off the tv and dim the lights")) == 2
    assert len(classifier.detect_multi_intent("lock the door and lower the thermostat")) == 2

    assert classifier.detect_multi_intent("turn off the kitchen and living room lights") == [
        "turn off the kitchen and living room lights"
    ]


# ---------------------------------------------------------------------------
# Test 7: test_has_sequence_timing_is_the_single_implementation
# ---------------------------------------------------------------------------

BANNED_INDICATORS = {
    "wait", "then", "after that", "seconds", "second", "minutes", "minute",
    "pause", "delay", "times", "repeat", "cycle", "loop", "again",
    "on and off", "off and on", "flash", "blink", "on then off", "off then on",
    " at ", "at 6", "at 7", "at 8", "at 9", "at 10", "at 11", "at 12",
    " pm", " am", "o'clock", "oclock", "tonight", "tomorrow", "morning",
    "evening", "noon", "midnight", "schedule",
}


def _find_class_method(tree: ast.Module, class_name: str, method_name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method_name:
                    return item
    raise AssertionError(f"{class_name}.{method_name} not found")


def _find_module_function(tree: ast.Module, func_name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise AssertionError(f"module-level function {func_name} not found")


def _string_constants(node: ast.AST) -> set:
    return {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _called_names(node: ast.AST) -> set:
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                names.add(n.func.attr)
    return names


def _constants_after_last_exclusion_if(func_node: ast.FunctionDef) -> set:
    """String constants in the statements after the last exclusion `if`.

    The exclusion prologues (scene/brightness/casual-then/emotional/mqtt)
    are unchanged per D1 and legitimately reuse plain-English words (e.g.
    the emotional-exclusion carve-out's `action_words = [..., 'schedule',
    ...]`) that coincide with the banned bare-substring indicator set for
    an unrelated reason. The ban targets the code that used to hold
    delay_patterns/loop_patterns/schedule_patterns, not those prologues, so
    the scan is scoped to what follows them — the same scoping the plan
    specifies for sequence_executor.detect_sequence_intent.
    """
    exclusion_if_indexes = [
        i for i, stmt in enumerate(func_node.body) if isinstance(stmt, ast.If)
    ]
    assert exclusion_if_indexes, "expected at least one exclusion prologue if-statement"
    remaining_stmts = func_node.body[exclusion_if_indexes[-1] + 1:]
    remaining_constants = set()
    for stmt in remaining_stmts:
        remaining_constants |= _string_constants(stmt)
    return remaining_constants


def test_has_sequence_timing_is_the_single_implementation():
    controller_src = (REPO_ROOT / "src/orchestrator/smart_home_controller.py").read_text()
    controller_tree = ast.parse(controller_src)
    method_node = _find_class_method(controller_tree, "SmartHomeController", "detect_sequence_intent")

    assert "has_sequence_timing" in _called_names(method_node)
    assert not (_constants_after_last_exclusion_if(method_node) & BANNED_INDICATORS)

    seq_src = (REPO_ROOT / "src/orchestrator/sequence_executor.py").read_text()
    seq_tree = ast.parse(seq_src)
    func_node = _find_module_function(seq_tree, "detect_sequence_intent")

    assigned_names = {
        target.id
        for n in ast.walk(func_node)
        if isinstance(n, ast.Assign)
        for target in n.targets
        if isinstance(target, ast.Name)
    }
    assert not ({"delay_patterns", "loop_patterns", "schedule_patterns"} & assigned_names)
    assert not (_constants_after_last_exclusion_if(func_node) & BANNED_INDICATORS)
