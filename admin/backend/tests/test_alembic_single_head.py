"""The migration graph has exactly one head, and it is 064.

Parsed with ``ast`` so no database or alembic environment is needed. A second
head (two campaigns numbering the same revision, a branch left behind) makes
``alembic upgrade head`` refuse to run on the house install.
"""
import ast
from pathlib import Path

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"
EXPECTED_HEAD = "064"
MIN_REVISIONS = 64


def _assigned(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == name and node.value is not None:
            return ast.literal_eval(node.value)
    raise AssertionError(f"no {name} assignment")


def _graph():
    parents = {}
    for path in sorted(VERSIONS.glob("*.py")):
        tree = ast.parse(path.read_text())
        revision = _assigned(tree, "revision")
        down = _assigned(tree, "down_revision")
        assert revision not in parents, f"{revision} defined twice ({path.name})"
        parents[revision] = tuple(down) if isinstance(down, (tuple, list)) else (down,)
    return parents


def test_exactly_one_head_and_it_is_064():
    parents = _graph()
    referenced = {p for ps in parents.values() for p in ps if p is not None}
    heads = sorted(set(parents) - referenced)
    assert heads == [EXPECTED_HEAD], heads


def test_064_descends_from_063_and_the_graph_is_connected():
    parents = _graph()
    assert len(parents) >= MIN_REVISIONS
    assert parents[EXPECTED_HEAD] == ("063",)
    roots = [r for r, ps in parents.items() if ps == (None,)]
    assert len(roots) == 1, roots
    for revision, ps in parents.items():
        for parent in ps:
            assert parent is None or parent in parents, f"{revision} points at missing {parent}"
