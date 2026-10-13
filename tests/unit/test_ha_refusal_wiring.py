"""Every consumer of a guard denial builds its answer from the denial's reason
(ATHENA-233, D20), so a one-door or template refusal can't be flattened into
"I can't control the locks in guest mode" and a guest can't be told to "name
the door". The population is read from the source, not listed by hand.
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, List, Set

ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "src" / "orchestrator"

# Slice consumers that may still use the domain-keyed wording: media players
# only (the guard's door and template reasons are about locks, covers and
# automation configs).
DOMAIN_ONLY_CONSUMERS: Dict[str, str] = {
    "nodes/route_music.py": "writes media_player only; no door or template reason can be recorded there",
    "nodes/route_tv.py": "writes media_player only; no door or template reason can be recorded there",
}
# Named members: these must be found, each with at least this many slice sites.
NAMED_MEMBERS: Dict[str, int] = {
    "smart_home_controller.py": 1,
    "nodes/route_control.py": 4,
}


def _is_denials_slice(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Slice)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "denials"
    )


def _called_names(tree: ast.AST) -> Set[str]:
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", ""))
    return names


def slice_sites(source: str) -> List[bool]:
    """One entry per ``scope.denials[...:]`` slice in ``source``: False when
    the nearest enclosing ``if`` (else the function) builds an answer with the
    domain-keyed ``permission_refusal_message`` and not ``refusal_message``. A
    slice that only inspects reasons has neither call and is fine."""
    tree = ast.parse(source)
    parents: Dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    sites: List[bool] = []
    for node in ast.walk(tree):
        if not _is_denials_slice(node):
            continue
        scope: ast.AST = node
        while scope in parents and not isinstance(scope, (ast.If, ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents[scope]
        called = _called_names(scope)
        sites.append(not ("permission_refusal_message" in called and "refusal_message" not in called))
    return sites


def _population() -> Dict[str, List[bool]]:
    found: Dict[str, List[bool]] = {}
    for path in sorted(ORCHESTRATOR.rglob("*.py")):
        sites = slice_sites(path.read_text(encoding="utf-8"))
        if sites:
            found[path.relative_to(ORCHESTRATOR).as_posix()] = sites
    return found


def test_every_denial_slice_consumer_uses_the_reason_aware_message():
    population = _population()
    offenders = {
        rel: sites for rel, sites in population.items()
        if rel not in DOMAIN_ONLY_CONSUMERS and not all(sites)
    }
    assert offenders == {}, f"build the answer with refusal_message(scope.denials[n:], scope): {offenders}"


def test_the_population_has_its_named_members_and_a_floor():
    population = _population()
    for rel, minimum in NAMED_MEMBERS.items():
        assert len(population.get(rel, [])) >= minimum, rel
    assert sum(len(v) for v in population.values()) >= 7
    assert set(DOMAIN_ONLY_CONSUMERS) <= set(population), "an allowlisted file no longer slices denials: remove it"


def test_the_domain_only_allowlist_really_is_domain_only():
    for rel in DOMAIN_ONLY_CONSUMERS:
        text = (ORCHESTRATOR / rel).read_text(encoding="utf-8")
        assert '"lock"' not in text and '"cover"' not in text, rel


def test_a_planted_domain_keyed_consumer_is_reported():
    planted = (
        "def f(scope, n):\n"
        "    if len(scope.denials) > n:\n"
        "        domains = tuple(d.domain for d in scope.denials[n:])\n"
        "        return permission_refusal_message(domains, scope)\n"
    )
    assert slice_sites(planted) == [False]
    fixed = planted.replace("permission_refusal_message(domains, scope)", "refusal_message(scope.denials[n:], scope)")
    assert slice_sites(fixed) == [True, True]
    assert slice_sites("def f(scope, n):\n    return any(d.reason == 'x' for d in scope.denials[n:])\n") == [True]


def test_no_other_module_builds_the_door_phrases():
    owners: Set[str] = set()
    for path in ORCHESTRATOR.rglob("*.py"):
        if "I can unlock or open one door at a time" in path.read_text(encoding="utf-8"):
            owners.add(path.relative_to(ORCHESTRATOR).as_posix())
    # mode_permission owns the phrase; the controller and the executor still
    # carry their own copies, pinned equal in test_ha_permission_guard.py.
    assert owners == {"mode_permission.py", "smart_home_controller.py", "sequence_executor.py"}
