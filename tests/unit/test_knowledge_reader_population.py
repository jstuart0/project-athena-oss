"""Every base-knowledge reader is on an explicit allowlist.

A new call to a base-knowledge fetcher, or a new string literal naming the
public base-knowledge route, anywhere in src/, apps/ or admin/backend/app must
be added here on purpose, with the audience it reads for. Stdlib only.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("src", "apps", "admin/backend/app")
FETCHERS = {
    "get_base_knowledge",
    "load_visible_knowledge",
    "get_knowledge_context_for_user",
    "build_knowledge_context",
}
PUBLIC_ROUTE = "/api/base-knowledge/public"
# Not valid Python 3 today (a half-edited file); it reads no base knowledge.
UNPARSABLE = {"src/jetson/athena_lite_llm.py"}

# (path, enclosing function, what it is). The audience filter lives in the
# loader; each orchestrator site hands it the request's KnowledgeAudience.
ALLOWED = {
    ("src/orchestrator/helpers.py", "_owner_name", "load_visible_knowledge"),
    ("src/orchestrator/main.py", "build_synthesis_prompt_for_streaming", "get_knowledge_context_for_user"),
    ("src/orchestrator/main.py", "tool_call_node", "build_knowledge_context"),
    ("src/orchestrator/main.py", "tool_call_node", "load_visible_knowledge"),
    ("src/orchestrator/nodes/synthesize.py", "synthesize_node", "get_knowledge_context_for_user"),
    ("src/rag/directions/main.py", "lifespan", PUBLIC_ROUTE),  # Everyone rows only (D13)
    ("src/shared/admin_config.py", "get_base_knowledge", PUBLIC_ROUTE),  # tiers= is required
    ("src/shared/base_knowledge_utils.py", "<module>", "build_knowledge_context"),  # __main__ demo
    ("src/shared/base_knowledge_utils.py", "get_knowledge_context_for_user", "build_knowledge_context"),
    ("src/shared/base_knowledge_utils.py", "get_knowledge_context_for_user", "load_visible_knowledge"),
    ("src/shared/base_knowledge_utils.py", "load_visible_knowledge", "get_base_knowledge"),
}


def _sites():
    found, unparsable = set(), set()
    for root in ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            rel = path.relative_to(REPO_ROOT).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                unparsable.add(rel)
                continue

            def visit(node, function):
                for child in ast.iter_child_nodes(node):
                    inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else function
                    if isinstance(child, ast.Call):
                        name = getattr(child.func, "id", None) or getattr(child.func, "attr", None)
                        if name in FETCHERS:
                            found.add((rel, function, name))
                    elif isinstance(child, ast.Constant) and isinstance(child.value, str) and PUBLIC_ROUTE in child.value:
                        found.add((rel, function, PUBLIC_ROUTE))
                    visit(child, inner)

            visit(tree, "<module>")
    return found, unparsable


def test_readers_are_exactly_the_allowlist():
    found, unparsable = _sites()
    assert unparsable == UNPARSABLE
    assert found == ALLOWED, f"new: {sorted(found - ALLOWED)}\ngone: {sorted(ALLOWED - found)}"


def test_population_floor_and_named_member():
    found, _ = _sites()
    assert len(found) >= 7
    assert ("src/orchestrator/nodes/synthesize.py", "synthesize_node", "get_knowledge_context_for_user") in found
