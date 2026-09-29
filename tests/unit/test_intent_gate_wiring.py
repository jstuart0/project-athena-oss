"""The intent_refused node is wired into the real graph (V2.4, T5).

langgraph isn't installed in the unit environment, so this is structural;
the image gate compiles the real graph (create_orchestrator_graph) against
the built orchestrator image.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAIN_PY = REPO_ROOT / "src" / "orchestrator" / "main.py"
NODES_INIT = REPO_ROOT / "src" / "orchestrator" / "nodes" / "__init__.py"


def _graph_builder():
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    return next(fn for fn in tree.body if isinstance(fn, ast.FunctionDef) and fn.name == "create_orchestrator_graph")


def _method_calls(fn, method):
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == method
    ]


def test_intent_refused_node_wired():
    builder = _graph_builder()
    nodes = {
        (ast.literal_eval(c.args[0]), ast.unparse(c.args[1]))
        for c in _method_calls(builder, "add_node") if len(c.args) == 2
    }
    assert ("intent_refused", "intent_refused_node") in nodes
    edges = {tuple(ast.literal_eval(a) for a in c.args) for c in _method_calls(builder, "add_edge")}
    assert ("intent_refused", "finalize") in edges
    classify_edges = [
        c for c in _method_calls(builder, "add_conditional_edges")
        if c.args and ast.literal_eval(c.args[0]) == "classify"
    ]
    assert len(classify_edges) == 1
    mapping = classify_edges[0].args[2]
    pairs = {ast.literal_eval(k): ast.literal_eval(v) for k, v in zip(mapping.keys, mapping.values)}
    assert pairs.get("intent_refused") == "intent_refused"


def test_intent_refused_node_exported():
    tree = ast.parse(NODES_INIT.read_text(encoding="utf-8"))
    exported = next(
        ast.literal_eval(n.value) for n in tree.body
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "__all__" for t in n.targets)
    )
    assert "intent_refused_node" in exported
    assert (REPO_ROOT / "src" / "orchestrator" / "nodes" / "intent_refused.py").exists()
