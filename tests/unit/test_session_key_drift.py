"""Nothing but session_keys.py builds a session or conversation-context key.

Any string literal or f-string fragment naming one of the four Redis
namespaces outside the module is a second place that could disagree with the
id -> class rule. Stdlib only.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ROOTS = ("src", "apps", "admin/backend/app")
MODULE = "src/orchestrator/session_keys.py"
NAMESPACES = ("athena:session", "athena:owner_session", "athena:context", "athena:owner_context")
# Not valid Python 3 today; reads no session keys.
UNPARSABLE = {"src/jetson/athena_lite_llm.py"}
CONTEXT_KEY_CALL_SITES = {
    ("src/orchestrator/helpers.py", "store_conversation_context"),
    ("src/orchestrator/main.py", "get_conversation_context"),
    ("src/orchestrator/context/storage.py", "get_conversation_context"),
    ("src/orchestrator/context/storage.py", "store_conversation_context"),
    ("src/orchestrator/context/storage.py", "clear_conversation_context"),
}


def _files():
    for root in ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            yield path.relative_to(REPO_ROOT).as_posix(), path


def _parse(path):
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return None


def test_no_namespace_literal_outside_the_module():
    offenders, unparsable = [], set()
    for rel, path in _files():
        if rel == MODULE:
            continue
        tree = _parse(path)
        if tree is None:
            unparsable.add(rel)
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(ns in node.value for ns in NAMESPACES):
                    offenders.append(f"{rel}:{node.lineno}")
    assert unparsable == UNPARSABLE
    assert offenders == []


def test_the_module_defines_every_namespace_and_builds_keys_from_one_class_function():
    tree = _parse(REPO_ROOT / MODULE)
    literals = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    for ns in NAMESPACES:
        assert any(lit.startswith(ns + ":") for lit in literals), ns
    functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("session_storage_key", "context_storage_key"):
        calls = {getattr(c.func, "id", None) for c in ast.walk(functions[name]) if isinstance(c, ast.Call)}
        assert "id_class" in calls, name


def test_the_five_context_sites_use_the_key_function():
    found = set()
    for rel, function in CONTEXT_KEY_CALL_SITES:
        tree = _parse(REPO_ROOT / rel)
        fn = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function)
        if any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "context_storage_key" for c in ast.walk(fn)):
            found.add((rel, function))
    assert found == CONTEXT_KEY_CALL_SITES


def test_session_manager_uses_the_key_function_at_all_three_redis_sites():
    tree = _parse(REPO_ROOT / "src/orchestrator/session_manager.py")
    methods = {"get_session", "delete_session", "_save_session"}
    used = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef) and fn.name in methods:
            if any(isinstance(c, ast.Call) and getattr(c.func, "id", None) == "session_storage_key" for c in ast.walk(fn)):
                used.add(fn.name)
    assert used == methods
