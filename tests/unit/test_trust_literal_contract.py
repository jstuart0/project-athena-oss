"""Every caller_trust jarvis-web sends is one the orchestrator accepts.

Stdlib only (read from source), so it runs on the unit-min CI requirements;
tests/unit/test_jarvis_trust_contract.py checks the same through both
images' imports.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CALLER_AUTH = REPO_ROOT / "apps" / "jarvis-web" / "backend" / "caller_auth.py"
ORCHESTRATOR_MAIN = REPO_ROOT / "src" / "orchestrator" / "main.py"


def _upstream_trust_values() -> set:
    tree = ast.parse(CALLER_AUTH.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "UPSTREAM_TRUST" for t in node.targets):
            assert isinstance(node.value, ast.Dict)
            values = {v.value for v in node.value.values if isinstance(v, ast.Constant)}
            return values | _owner_trust_value(tree)
    raise AssertionError("UPSTREAM_TRUST not found in caller_auth.py")


def _owner_trust_value(tree) -> set:
    """The one extra value Caller.trust can send: OWNER_TRUST (Bearer owner)."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "OWNER_TRUST" for t in node.targets):
            assert isinstance(node.value, ast.Constant)
            return {node.value.value}
    raise AssertionError("OWNER_TRUST not found in caller_auth.py")


def _query_request_trust_literal() -> set:
    tree = ast.parse(ORCHESTRATOR_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "QueryRequest":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name) and item.target.id == "caller_trust":
                    literals = [n for n in ast.walk(item.annotation)
                                if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == "Literal"]
                    assert len(literals) == 1
                    elts = literals[0].slice.elts if isinstance(literals[0].slice, ast.Tuple) else [literals[0].slice]
                    return {e.value for e in elts}
    raise AssertionError("QueryRequest.caller_trust not found in orchestrator/main.py")


def test_sent_values_are_accepted():
    sent = _upstream_trust_values()
    accepted = _query_request_trust_literal()
    assert {"web_authenticated", "web_owner", "web_local", "web_guest_net", "web_public"} <= sent
    assert sent <= accepted
    assert "web_guest_net" in accepted
