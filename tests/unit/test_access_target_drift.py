"""Picking locks or covers out of an entity inventory by id prefix has a short list of owners.

``access_target`` decides which door an utterance names. A second place that
selects ``lock.*`` or ``cover.*`` entities by id prefix is a second place that
decides what a door is, and drifts from the rules (a cover without a door
device class, a window, a group). The selection sites are listed here by
function, each with its reason; a new one fails until it uses
``access_target`` or is added with a reason.

The sites are not only the handlers' inventory builds: the permission guard
classifies the entity ids a write targets, and the status optimizer reads lock
state. Neither one picks a door to open.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
PREFIXES = ("lock.", "cover.")

ALLOWED = {
    ("orchestrator/smart_home_controller.py", "_handle_lock_intent"): "inventory build for the lock handler; the door itself is chosen by access_target.resolve",
    ("orchestrator/smart_home_controller.py", "_handle_cover_intent"): "inventory build for the cover handler; the door itself is chosen by access_target.resolve",
    ("orchestrator/mode_permission.py", "_opening_denial"): "guard: counts the locks and covers a write targets, to deny a fan-out opening",
    ("orchestrator/mode_permission.py", "automation_opening_cover_targets"): "guard: reads the cover targets of an automation, to deny a scheduled opening",
    ("orchestrator/mode_permission.py", "_read_door"): "guard: reads a cover's state to classify it, never selects one to write",
    ("orchestrator/ha_status_optimizer.py", "optimize_status_query"): "read-only status answer; no write path",
}


def selection_sites(source: str, *, relative: str):
    """[(file, function)] for each `.startswith("lock." | "cover.")` call."""
    tree = ast.parse(source)
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for call in ast.walk(fn):
            if isinstance(call, ast.Call) and getattr(call.func, "attr", None) == "startswith":
                for arg in call.args:
                    if any(isinstance(n, ast.Constant) and n.value in PREFIXES for n in ast.walk(arg)):
                        found.append((relative, fn.name))
    return sorted(set(found))


def scan():
    found = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            tree_source = path.read_text(encoding="utf-8")
            found.extend(selection_sites(tree_source, relative=path.relative_to(SRC).as_posix()))
        except SyntaxError:
            assert not any(p in tree_source for p in PREFIXES), f"{path} is unparseable and selects by prefix"
    return found


def test_the_selection_sites_are_exactly_the_listed_ones():
    assert set(scan()) == set(ALLOWED)


def test_population_floor_and_named_member():
    found = scan()
    assert len(found) >= 6
    assert ("orchestrator/smart_home_controller.py", "_handle_lock_intent") in found


def test_access_target_itself_selects_no_entity_by_prefix():
    assert not [s for s in scan() if s[0] == "orchestrator/access_target.py"]


class TestPlantedNegatives:
    def test_a_new_prefix_selection_is_seen(self):
        source = "def pick(states):\n    return {k: v for k, v in states.items() if k.startswith('lock.')}\n"
        assert selection_sites(source, relative="orchestrator/new.py") == [("orchestrator/new.py", "pick")]

    def test_a_tuple_of_prefixes_is_seen(self):
        source = "def pick(k):\n    return k.startswith(('light.', 'cover.'))\n"
        assert selection_sites(source, relative="x.py") == [("x.py", "pick")]

    def test_other_prefixes_are_not_sites(self):
        assert selection_sites("def f(k):\n    return k.startswith('light.')\n", relative="x.py") == []
