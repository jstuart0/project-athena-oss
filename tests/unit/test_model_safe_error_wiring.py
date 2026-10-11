"""Closed world: error text reaches the model, the speaker or a client only through model_safe_errors.

Over `src/orchestrator/**/*.py` and the two admin handlers the plan names (`internal_create_memory`, `save_assistant_profile`):

(a) every dict literal that builds a `role: tool` message takes its `content` from `scrub_tool_result(` or `model_safe_error(`;
(b) no sink that a person or a client sees (a `return`, an assignment to `answer`/`message`, a dict value under
    `answer`/`message`/`reason`/`detail`, `HTTPException(detail=...)`, a `yield`) carries the name bound by the
    enclosing `except ... as <name>`, unless it is wrapped in one of the safe functions;
(c) no `raise Exception(...)` builds its message from a RAG response's `.error` (it is `RAGToolError(`), and
    `UserSafeText(` is constructed only inside `make_user_safe`.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ORCH = REPO / "src" / "orchestrator"
# The admin handlers the plan names; the rest of those route files predate this policy.
ADMIN_HANDLERS = {
    REPO / "admin/backend/app/routes/memories.py": {"internal_create_memory"},
    REPO / "admin/backend/app/routes/settings.py": {"save_assistant_profile"},
}
# (file, function) -> why a raw exception may reach that sink. Each entry must still exist.
ALLOWED = {
    ("rag_client.py", "request"): "RAGResponse.error is the raw field for logs and metrics (the DC9 contract); the model only sees model_safe_error of it",
    ("rag_validator.py", "validate_sports_response"): "internal validation record, never spoken or sent to a client",
    ("rag_validator.py", "validate_weather_response"): "internal validation record, never spoken or sent to a client",
    ("rag_validator.py", "validate_airports_response"): "internal validation record, never spoken or sent to a client",
    ("main.py", "handle_motion_event"): "follow-me webhook answer to the Home Assistant automation that posted it (operator surface)",
    ("main.py", "invalidate_feature_cache"): "service-key-only operator route",
    ("main.py", "invalidate_model_cache"): "service-key-only operator route",
    ("main.py", "reset_circuit_breaker"): "service-key-only operator route",
    ("main.py", "reset_all_circuits"): "service-key-only operator route",
    ("main.py", "warmup_session"): "service-key-only operator route",
    ("memory_manager.py", "delete_memory_by_content"): "result dict read by the forget path, which speaks fixed sentences from the deleted count only",
    ("self_building_tools.py", "generate_tool_from_request"): "result dict read by handle_tool_creation_request, which speaks fixed sentences",
    ("self_building_tools.py", "approve_proposal"): "result dict for the admin approval flow (operator surface)",
    ("main.py", "llm_metrics"): "operator route (the plan's P-E 'internal only' list)",
}
SAFE_FUNCTIONS = {"model_safe_error", "tool_error_result", "log_safe", "make_user_safe", "scrub_tool_result"}
SINK_KEYS = {"answer", "message", "reason", "detail", "error"}
SINK_TARGETS = {"answer", "message"}


def _files():
    return sorted(ORCH.rglob("*.py")) + sorted(ADMIN_HANDLERS)


def _parents(tree):
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _inside_safe_call(node, parents, stop):
    current = node
    while current is not stop and current in parents:
        current = parents[current]
        if isinstance(current, ast.Call) and getattr(current.func, "id", getattr(current.func, "attr", None)) in SAFE_FUNCTIONS:
            return True
    return False


def _sink_values(handler):
    """(description, value expression) for every sink inside an except handler body."""
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Return) and node.value is not None and not isinstance(node.value, ast.Dict):
            yield "return", node.value          # a returned dict is judged by its sink keys below
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                name = getattr(target, "id", getattr(target, "attr", None))
                if name in SINK_TARGETS:
                    yield f"assignment to {name}", node.value
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in SINK_KEYS and value is not None:
                    yield f"dict value {key.value!r}", value
        elif isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "HTTPException":
            for keyword in node.keywords:
                if keyword.arg == "detail":
                    yield "HTTPException detail", keyword.value
        elif isinstance(node, (ast.Yield, ast.YieldFrom)) and node.value is not None:
            yield "yield", node.value


def _enclosing_function(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return None


def _tainted_names(handler, parents):
    """The except name plus every local assigned (transitively) from it outside a safe call:
    `error_msg = str(e)` taints `error_msg`."""
    tainted = {handler.name}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            uses = [n for n in ast.walk(node.value) if isinstance(n, ast.Name) and n.id in tainted
                    and not _inside_safe_call(n, parents, node.value)]
            if not uses:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and name.id not in tainted:
                        tainted.add(name.id)
                        changed = True
    return tainted


def violations_in(tree, filename=""):
    parents = _parents(tree)
    found, sinks_seen = [], 0
    for handler in [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler) and n.name]:
        if (filename, _enclosing_function(handler, parents)) in ALLOWED:
            continue
        tainted = _tainted_names(handler, parents)
        for description, value in _sink_values(handler):
            sinks_seen += 1
            for name in (n for n in ast.walk(value) if isinstance(n, ast.Name) and n.id in tainted):
                if not _inside_safe_call(name, parents, value):
                    found.append((handler.lineno, description, name.id))
    return found, sinks_seen


def _scan():
    results = {}
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if path in ADMIN_HANDLERS:
            tree = ast.Module(
                body=[n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ADMIN_HANDLERS[path]],
                type_ignores=[],
            )
            assert tree.body, f"{path.name}: the named handler is gone"
        results[path] = violations_in(tree, path.name)
    return results


SCAN = _scan()


def test_no_except_name_reaches_a_sink_unwrapped():
    assert len(SCAN) >= 40, "population floor: the whole orchestrator tree plus the two admin routes"
    offenders = {f"{p.relative_to(REPO)}:{line} {what} ({name})" for p, (found, _) in SCAN.items() for line, what, name in found}
    assert not offenders, sorted(offenders)


def test_the_scan_is_not_vacuous():
    """Handlers whose sinks are safe still count: music/agent/streaming sites really are scanned."""
    total_sinks = sum(sinks for _, sinks in SCAN.values())
    assert total_sinks >= 20, total_sinks
    for name in ("music_handler.py", "automation_agent.py", "smart_home_controller.py"):
        assert any(p.name == name and sinks for p, (_, sinks) in SCAN.items()), name


def test_the_checker_catches_each_kind_of_sink():
    bad = {
        "return f-string": "def f():\n    try: pass\n    except Exception as e:\n        return f'Sorry. {str(e)}'\n",
        "return concat": "def f():\n    try: pass\n    except Exception as e:\n        return 'Sorry. ' + str(e)\n",
        "assign answer": "def f(state):\n    try: pass\n    except Exception as e:\n        state.answer = f'no {e}'\n",
        "dict reason": "def f():\n    try: pass\n    except Exception as e:\n        return {'created': False, 'reason': str(e)}\n",
        "http detail": "def f():\n    try: pass\n    except Exception as e:\n        raise HTTPException(status_code=500, detail=f'x {e}')\n",
        "sse yield": "def f():\n    try: pass\n    except Exception as e:\n        yield json.dumps({'stage': 'error', 'message': str(e)})\n",
    }
    for label, source in bad.items():
        found, _ = violations_in(ast.parse(source))
        assert found, label
    tainted = {
        "local then return": "def f():\n    try: pass\n    except Exception as e:\n        msg = str(e)\n        return 'Sorry. ' + msg\n",
        "chain then dict": "def f():\n    try: pass\n    except Exception as e:\n        a = str(e)\n        b = a.upper()\n        return {'error': b}\n",
        "local then answer": "def f(state):\n    try: pass\n    except Exception as e:\n        detail = f'{e}'\n        state.answer = detail\n",
    }
    for label, source in tainted.items():
        assert violations_in(ast.parse(source))[0], label
    metrics_only = "def f(m):\n    try: pass\n    except Exception as e:\n        error_msg = str(e)\n        m.record(error_message=error_msg)\n        return {'error': model_safe_error(e)}\n"
    assert violations_in(ast.parse(metrics_only))[0] == []
    assert violations_in(ast.parse("def f():\n    try: pass\n    except Exception as e:\n        return {'error': 'fixed'}\n"))[0] == []
    good = "def f():\n    try: pass\n    except Exception as e:\n        logger.error('x', error=str(e))\n        return f'Sorry. {model_safe_error(e)}'\n"
    assert violations_in(ast.parse(good))[0] == []


# --- (a) tool messages ----------------------------------------------------------------------------------


def _tool_message_dicts():
    for path in sorted(ORCH.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Dict):
                pairs = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
                role = pairs.get("role")
                if isinstance(role, ast.Constant) and role.value == "tool":
                    yield path, node, pairs


def test_every_tool_message_takes_its_content_through_the_scrubber():
    messages = list(_tool_message_dicts())
    assert len(messages) >= 2
    assert {p.name for p, _, _ in messages} >= {"main.py", "automation_agent.py"}
    for path, node, pairs in messages:
        content = pairs.get("content")
        assert content is not None, f"{path.name}:{node.lineno} builds a tool message with no content"
        used = {getattr(c.func, "id", None) for c in ast.walk(content) if isinstance(c, ast.Call)}
        assert used & {"scrub_tool_result", "model_safe_error"}, f"{path.name}:{node.lineno} tool content is not scrubbed"


# --- (c) the RAG failure path ----------------------------------------------------------------------------


def test_no_raise_exception_builds_its_message_from_a_response_error():
    offenders, rag_raises = [], 0
    for path in sorted(ORCH.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
                name = getattr(node.exc.func, "id", None)
                if name == "RAGToolError":
                    rag_raises += 1
                if name == "Exception" and any(isinstance(n, ast.Attribute) and n.attr == "error" for n in ast.walk(node.exc)):
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert not offenders, offenders
    assert rag_raises == 6, "the six failed-RAG-response raises (main.py x3, retrieve.py x3) are all RAGToolError"


def test_user_safe_text_is_built_only_inside_make_user_safe():
    path = ORCH / "model_safe_errors.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    inside = [
        c for fn in ast.walk(tree) if isinstance(fn, ast.FunctionDef) and fn.name == "make_user_safe"
        for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "UserSafeText"
    ]
    assert len(inside) == 1
    for other in sorted(ORCH.rglob("*.py")):
        total = [c for c in ast.walk(ast.parse(other.read_text(encoding="utf-8")))
                 if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "UserSafeText"]
        assert len(total) == (1 if other == path else 0), other.name


def test_every_allowlisted_site_still_exists():
    for (filename, function), reason in ALLOWED.items():
        assert reason
        path = next(p for p in ORCH.rglob("*.py") if p.name == filename)
        names = {n.name for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert function in names, f"{filename}:{function} is gone: drop it from ALLOWED"
