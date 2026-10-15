"""A model's JSON never reaches the controller with a server-decision key.

Every ``json.loads`` in ``smart_home_controller.py`` that parses text from a
model is followed, in the same function, by ``strip_server_keys(<name>)`` on the
very name the parse was assigned to. The room and door decisions (and the
"unprocessable" marker) are the server's to make, so a model that writes
``decided_access`` into its output can't decide which door a command opens.

The two other ``json.loads`` calls parse operator environment variables, not
model output, and are listed with that reason.
"""
from __future__ import annotations

import ast
from pathlib import Path

CONTROLLER = Path(__file__).resolve().parents[2] / "src" / "orchestrator" / "smart_home_controller.py"
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)

OPERATOR_ENV = {
    "_get_light_groups": "HA_LIGHT_GROUPS, an operator setting",
}


def enclosing_function(tree: ast.AST, node: ast.AST):
    best = None
    for fn in ast.walk(tree):
        if isinstance(fn, FUNCTIONS) and fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
            if best is None or fn.lineno >= best.lineno:
                best = fn
    return best


def is_json_loads(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "loads"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "json"
    )


def unstripped(source: str):
    """[(function, line)] for each ``json.loads`` whose assigned name is not
    passed to ``strip_server_keys`` later in the same function."""
    tree = ast.parse(source)
    bad = []
    for assign in ast.walk(tree):
        if not (isinstance(assign, ast.Assign) and is_json_loads(assign.value)):
            continue
        targets = {t.id for t in assign.targets if isinstance(t, ast.Name)}
        fn = enclosing_function(tree, assign)
        stripped = {
            arg.id
            for call in ast.walk(fn)
            if isinstance(call, ast.Call) and getattr(call.func, "attr", getattr(call.func, "id", None)) == "strip_server_keys"
            and call.lineno > assign.lineno
            for arg in call.args if isinstance(arg, ast.Name)
        }
        if not targets or not targets & stripped:
            bad.append((fn.name, assign.lineno))
    # a json.loads that is not a plain `name = json.loads(...)` assignment
    plain = {a.value for a in ast.walk(tree) if isinstance(a, ast.Assign) and is_json_loads(a.value)}
    for call in ast.walk(tree):
        if is_json_loads(call) and call not in plain:
            bad.append((enclosing_function(tree, call).name, call.lineno))
    return sorted(bad)


def loads_sites(source: str):
    tree = ast.parse(source)
    return sorted((enclosing_function(tree, c).name, c.lineno) for c in ast.walk(tree) if is_json_loads(c))


def test_population_floor_and_named_members():
    sites = loads_sites(CONTROLLER.read_text(encoding="utf-8"))
    # The bed warmer's parse moved to shared.household (admin value, then env), so two sites remain.
    assert len(sites) >= 2
    assert "_model_object" in {name for name, _ in sites}
    assert set(OPERATOR_ENV) <= {name for name, _ in sites}, "an allowlisted site no longer exists: drop it"


def test_every_model_parse_is_stripped_on_the_name_it_assigned():
    leftovers = [(name, line) for name, line in unstripped(CONTROLLER.read_text(encoding="utf-8")) if name not in OPERATOR_ENV]
    assert leftovers == []


def test_every_parse_site_is_either_stripped_or_an_operator_setting():
    source = CONTROLLER.read_text(encoding="utf-8")
    covered = {name for name, _ in loads_sites(source)} - {name for name, _ in unstripped(source)}
    assert {name for name, _ in loads_sites(source)} == covered | set(OPERATOR_ENV)


class TestPlantedNegatives:
    GOOD = "def f(text):\n    parsed = json.loads(text)\n    return room_context.strip_server_keys(parsed)\n"

    def test_the_good_shape_passes(self):
        assert unstripped(self.GOOD) == []

    def test_a_strip_on_a_different_name_is_seen(self):
        source = "def f(text, other):\n    parsed = json.loads(text)\n    return strip_server_keys(other)\n"
        assert unstripped(source) == [("f", 2)]

    def test_a_missing_strip_is_seen(self):
        assert unstripped("def f(text):\n    parsed = json.loads(text)\n    return parsed\n") == [("f", 2)]

    def test_a_strip_before_the_parse_does_not_count(self):
        source = "def f(text, parsed):\n    strip_server_keys(parsed)\n    parsed = json.loads(text)\n    return parsed\n"
        assert unstripped(source) == [("f", 3)]

    def test_a_strip_in_another_function_does_not_count(self):
        source = "def f(text):\n    parsed = json.loads(text)\n    return parsed\n\ndef g(parsed):\n    return strip_server_keys(parsed)\n"
        assert unstripped(source) == [("f", 2)]

    def test_a_parse_that_is_not_assigned_is_seen(self):
        assert unstripped("def f(text):\n    return strip_server_keys(json.loads(text))\n") == [("f", 2)]
