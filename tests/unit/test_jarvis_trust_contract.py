"""Cross-image trust set: every caller_trust jarvis-web can send is one the
orchestrator's QueryRequest accepts, and web_owner is produced only for an
authenticated Bearer caller with the owner role.

Neither image is imported whole: jarvis-web's caller_auth is loaded directly
(it needs only fastapi/httpx, which the jarvis-web job has) and the
orchestrator's accepted values are read from its source, so the test runs in
the jarvis-web job, which has no orchestrator dependencies. The shared
own-/gst- session id rule is pinned in test_owner_session_ids.py.
"""
from __future__ import annotations

import ast
import itertools
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "apps" / "jarvis-web" / "backend"
ORCHESTRATOR_MAIN = REPO_ROOT / "src" / "orchestrator" / "main.py"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
import os  # noqa: E402

os.environ.setdefault("SERVICE_API_KEY", "test-key-jarvis-trust-contract")
import caller_auth as ca  # noqa: E402

SOURCES = ["edge", "bearer", "local", "guest_network", "service", "none"]
ROLES = [None, "owner", "operator"]
CLASSES = sorted(
    value for name, value in vars(ca).items()
    if name.startswith("CLASS_") and isinstance(value, str)
)


def _accepted() -> set:
    tree = ast.parse(ORCHESTRATOR_MAIN.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "QueryRequest":
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and getattr(item.target, "id", None) == "caller_trust":
                    literals = [n for n in ast.walk(item.annotation)
                                if isinstance(n, ast.Subscript) and getattr(n.value, "id", None) == "Literal"]
                    assert len(literals) == 1
                    sl = literals[0].slice
                    return {e.value for e in (sl.elts if isinstance(sl, ast.Tuple) else [sl])}
    raise AssertionError("QueryRequest.caller_trust not found")


def test_the_enumeration_covers_every_class_source_and_role():
    assert len(CLASSES) >= 7  # authenticated, local, guest_net, service, public, not_household, relay
    assert ca.CLASS_AUTHENTICATED in CLASSES and ca.CLASS_RELAY in CLASSES
    assert len(list(itertools.product(CLASSES, SOURCES, ROLES))) >= 126


@pytest.mark.parametrize("caller_class,source,role", list(itertools.product(CLASSES, SOURCES, ROLES)))
def test_every_trust_jarvis_web_can_send_is_accepted_by_the_orchestrator(caller_class, source, role):
    caller = ca.Caller(caller_class, "owner", True, role, "", source)
    assert caller.trust in _accepted()
    expected_owner = caller_class == ca.CLASS_AUTHENTICATED and source == "bearer" and role == "owner"
    assert (caller.trust == "web_owner") is expected_owner


def test_web_owner_is_accepted_and_is_the_only_value_beyond_the_class_table():
    sent = set(ca.UPSTREAM_TRUST.values()) | {ca.OWNER_TRUST}
    accepted = _accepted()
    assert sent <= accepted
    assert "web_owner" in sent and "web_local" in sent
    assert ca.OWNER_TRUST == "web_owner"


def test_every_browser_class_maps_to_a_trust_value():
    for cls in ca.BROWSER_CLASSES | {ca.CLASS_PUBLIC}:
        assert cls in ca.UPSTREAM_TRUST
    assert ca.UPSTREAM_TRUST[ca.CLASS_GUEST_NET] == "web_guest_net"
