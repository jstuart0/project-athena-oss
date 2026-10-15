"""The room settings have one reader each, and the scene fallback keeps a denial.

AST only (no imports of the code under test), so it runs in the guards job.

(a) ``ha_light_groups`` / ``ha_satellite_room_map`` are read only by the two
    ``shared.household`` resolvers; every other process asks the resolver, so
    admin's value can't be bypassed.
(b) ``_get_light_groups()`` has exactly three callers.
(c) The scene fallback reaches a room's lights only through
    ``_configured_room_targets`` (the leaf-checked step), never the accessor.
(d) A permission denial is re-raised ahead of every catch-all around a write
    in the scene path.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable, Set, Tuple

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("src", "admin/backend/app", "apps")
ATTRIBUTES = {"ha_light_groups", "ha_satellite_room_map"}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
CONTROLLER = ROOT / "src" / "orchestrator" / "smart_home_controller.py"

RESOLVER_READERS = {
    ("src/shared/household.py", "room_lights"),
    ("src/shared/household.py", "speaker_rooms"),
}
# A file that has never parsed (it is not run anywhere); any other that fails to parse fails the test.
UNPARSEABLE = {"src/jetson/athena_lite_llm.py"}
LIGHT_GROUP_CALLERS = {"_resolve_room_lights", "_whole_house_lamp_rooms", "_configured_room_targets"}
SCENE_FUNCTIONS = ("_handle_scene_intent", "_run_scene_fallback", "_scene_fallback_room_lights")


def enclosing_function(tree: ast.AST, node: ast.AST) -> str:
    best = None
    for fn in ast.walk(tree):
        if isinstance(fn, FUNCTIONS) and fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
            if best is None or fn.lineno >= best.lineno:
                best = fn
    return best.name if best is not None else "<module>"


def attribute_readers(source: str, path: str) -> Set[Tuple[str, str]]:
    tree = ast.parse(source)
    return {
        (path, enclosing_function(tree, node))
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr in ATTRIBUTES
    }


def light_group_callers(source: str) -> Set[str]:
    tree = ast.parse(source)
    return {
        enclosing_function(tree, node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_get_light_groups"
    }


def function_named(tree: ast.AST, name: str):
    return next(n for n in ast.walk(tree) if isinstance(n, FUNCTIONS) and n.name == name)


def handler_names(handler: ast.ExceptHandler) -> Set[str]:
    if handler.type is None:
        return {"<bare>"}
    nodes = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {n.id for n in nodes if isinstance(n, ast.Name)}


def is_catch_all(handler: ast.ExceptHandler) -> bool:
    return bool(handler_names(handler) & {"<bare>", "Exception", "BaseException"})


def try_shapes(source: str, functions: Iterable[str]):
    """[(function, denial_handler_index or None, catch_all_index or None)] per Try."""
    tree = ast.parse(source)
    shapes = []
    for name in functions:
        for node in ast.walk(function_named(tree, name)):
            if isinstance(node, ast.Try):
                denial = next((i for i, h in enumerate(node.handlers) if "HAWritePermissionDenied" in handler_names(h)), None)
                catch_all = next((i for i, h in enumerate(node.handlers) if is_catch_all(h)), None)
                shapes.append((name, denial, catch_all))
    return shapes


def python_files():
    for directory in SCAN_DIRS:
        yield from sorted((ROOT / directory).rglob("*.py"))


# --- (a) ---------------------------------------------------------------------

def test_only_the_resolvers_read_the_two_environment_attributes():
    files = list(python_files())
    assert len(files) > 300, "the scan must cover the repo, not an empty glob"
    found: Set[Tuple[str, str]] = set()
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if relative == "src/shared/config.py" or relative in UNPARSEABLE:
            continue
        found |= attribute_readers(path.read_text(encoding="utf-8"), relative)
    assert found == RESOLVER_READERS


def test_the_gateway_reads_the_resolver_not_the_environment_attribute():
    source = (ROOT / "src/gateway/main.py").read_text(encoding="utf-8")
    assert attribute_readers(source, "src/gateway/main.py") == set()
    assert "household.speaker_rooms()" in source


class TestAttributeReadPlantedNegatives:
    def test_a_read_in_another_function_is_seen(self):
        assert attribute_readers("def f(cfg):\n    return cfg.ha_light_groups\n", "x.py") == {("x.py", "f")}

    def test_a_read_of_the_satellite_map_is_seen(self):
        assert attribute_readers("def g(c):\n    return c.ha_satellite_room_map\n", "x.py") == {("x.py", "g")}

    def test_an_unrelated_attribute_is_not(self):
        assert attribute_readers("def f(cfg):\n    return cfg.other\n", "x.py") == set()


# --- (b) and (c) -------------------------------------------------------------

def test_the_light_group_accessor_has_exactly_three_callers():
    assert light_group_callers(CONTROLLER.read_text(encoding="utf-8")) == LIGHT_GROUP_CALLERS


def test_no_other_module_calls_the_accessor():
    for path in (ROOT / "src").rglob("*.py"):
        if path == CONTROLLER or path.relative_to(ROOT).as_posix() in UNPARSEABLE:
            continue
        assert light_group_callers(path.read_text(encoding="utf-8")) == set(), path


def test_the_scene_fallback_never_reads_the_accessor_directly():
    tree = ast.parse(CONTROLLER.read_text(encoding="utf-8"))
    fallback = function_named(tree, "_run_scene_fallback")
    assert not any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_get_light_groups"
        for n in ast.walk(fallback)
    )
    calls = {n.func.attr for n in ast.walk(fallback) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_scene_fallback_room_lights" in calls


class TestAccessorPlantedNegatives:
    def test_an_extra_caller_is_seen(self):
        source = "def a():\n    return _get_light_groups()\ndef _run_scene_fallback():\n    return _get_light_groups().get('x')\n"
        assert light_group_callers(source) == {"a", "_run_scene_fallback"}

    def test_a_reference_without_a_call_is_not_a_caller(self):
        assert light_group_callers("def a():\n    return _get_light_groups\n") == set()


# --- (d) ---------------------------------------------------------------------

def test_a_denial_is_reraised_ahead_of_every_catch_all_in_the_scene_path():
    shapes = try_shapes(CONTROLLER.read_text(encoding="utf-8"), SCENE_FUNCTIONS)
    guarded = [s for s in shapes if s[1] is not None]
    for name, denial, catch_all in guarded:
        assert catch_all is None or denial < catch_all, f"{name}: the denial handler must come first"
    by_function = {name: [s for s in guarded if s[0] == name] for name in SCENE_FUNCTIONS}
    assert len(by_function["_handle_scene_intent"]) == 2, "the activation try and the outer handler"
    assert len(by_function["_scene_fallback_room_lights"]) == 1, "the configured-room write"


def test_no_catch_all_in_the_scene_path_lacks_a_denial_handler():
    shapes = try_shapes(CONTROLLER.read_text(encoding="utf-8"), SCENE_FUNCTIONS)
    unguarded = [s for s in shapes if s[1] is None and s[2] is not None]
    assert unguarded == [], "a write behind a catch-all must re-raise a denial first (the two 'all' branches included)"
    assert [s for s in shapes if s[0] == "_run_scene_fallback" and s[1] is not None and s[2] is not None] != [], (
        "the 'all' branches are scanned and guarded"
    )


class TestDenialPlantedNegatives:
    GOOD = (
        "async def f():\n    try:\n        await w()\n    except HAWritePermissionDenied:\n        raise\n"
        "    except Exception:\n        pass\n"
    )
    WRONG_ORDER = (
        "async def f():\n    try:\n        await w()\n    except Exception:\n        pass\n"
        "    except HAWritePermissionDenied:\n        raise\n"
    )
    MISSING = "async def f():\n    try:\n        await w()\n    except Exception:\n        pass\n"
    BARE = "async def f():\n    try:\n        await w()\n    except:\n        pass\n"

    def test_the_good_shape_is_denial_first(self):
        (name, denial, catch_all), = try_shapes(self.GOOD, ["f"])
        assert denial == 0 and catch_all == 1

    def test_a_wrong_order_is_seen(self):
        (name, denial, catch_all), = try_shapes(self.WRONG_ORDER, ["f"])
        assert denial > catch_all

    def test_a_missing_handler_is_seen(self):
        assert try_shapes(self.MISSING, ["f"]) == [("f", None, 0)]

    def test_a_bare_except_is_a_catch_all(self):
        assert try_shapes(self.BARE, ["f"]) == [("f", None, 0)]
