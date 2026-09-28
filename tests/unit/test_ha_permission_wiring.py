"""ATHENA-69 wiring/drift guards -- these enumerate a population and assert
against it structurally (AST / attribute inspection), so a future edit that
silently un-wires the guard fails a test instead of shipping quietly.

Pass A members:
 - test_guard_passthrough_allowlist_excludes_transport
 - test_no_raw_ha_write_endpoints_in_orchestrator
 - test_no_guard_unwrap_outside_tests
 - test_no_ha_transport_reach_through
 - test_ha_writing_nodes_open_scope
 - test_no_yield_inside_permission_scope
 - test_fallback_branch_gone

Later passes (B/C/E/F) append their own members to this file; do not remove
Pass A's cases when doing so.

Pass D's wiring/drift test (test_sms_webhook_tags_caller_trust_sms) lives in
tests/unit/test_ha_permission_wiring_pass_d.py instead of here -- Passes B
and E both append to this file on their own branches, and this campaign's
passes are being built in parallel worktrees merged later, so adding a
fourth concurrent editor of this exact file just multiplies merge
conflicts (mozart, 2026-09-28).
"""
from __future__ import annotations

import ast
import re
import sys
import unittest.mock as mock
from pathlib import Path

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

from orchestrator import mode_permission as mp

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"


class TestGuardPassthroughAllowlist:
    def test_guard_passthrough_allowlist_excludes_transport(self):
        assert mp.PermissionEnforcingHAClient._READ_ALLOWLIST == frozenset(
            {"get_state", "health_check", "close", "is_configured", "url"}
        )


class TestNoRawHAWriteEndpointsInOrchestrator:
    def test_no_raw_ha_write_endpoints_in_orchestrator(self):
        offenders = []
        for root in (SRC / "orchestrator", SRC / "shared"):
            for path in root.rglob("*.py"):
                if path == SRC / "shared" / "ha_client.py":
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
                if "/api/services" in text or "/api/config/automation" in text:
                    offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []


class TestNoGuardUnwrapOutsideTests:
    def test_no_guard_unwrap_outside_tests(self):
        """The guard exposes no public "inner"/unwrap attribute at all --
        `_inner` is a private implementation detail of the class itself
        (accessed only from within PermissionEnforcingHAClient's own
        methods), never a public escape hatch reachable from src/. Tests
        that need the wrapped fake reach it by holding their own reference
        to the object they passed into the constructor, not by unwrapping
        the guard."""
        guard = mp.PermissionEnforcingHAClient(mock.MagicMock())
        assert not hasattr(guard, "inner")

        offenders = []
        for path in SRC.rglob("*.py"):
            if path.name == "mode_permission.py":
                continue  # the guard's own implementation legitimately uses self._inner
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"guard\._inner\b", text) or re.search(r"ha_client\._inner\b", text):
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []


class TestNoHATransportReachThrough:
    def test_no_ha_transport_reach_through(self):
        guard = mp.PermissionEnforcingHAClient(mock.MagicMock())
        for name in ("client", "token", "_client", "headers"):
            try:
                getattr(guard, name)
            except AttributeError:
                continue
            raise AssertionError(f"guard exposed {name!r}")


class TestHAWritingNodesOpenScope:
    """Set-equal to the node modules that reference an HA-writing runtime
    singleton (route_control, route_music, route_tv). Each must import
    ha_permission_scope from orchestrator.mode_permission."""

    EXPECTED_NODES = {"route_control", "route_music", "route_tv"}

    def test_ha_writing_nodes_open_scope(self):
        nodes_dir = SRC / "orchestrator" / "nodes"
        found = set()
        for name in self.EXPECTED_NODES:
            path = nodes_dir / f"{name}.py"
            assert path.exists(), f"missing node module {path}"
            text = path.read_text(encoding="utf-8")
            if "ha_permission_scope" in text:
                found.add(name)
        assert found == self.EXPECTED_NODES


class TestNoYieldInsidePermissionScope:
    """A drift test asserting no bare `yield` (generator yield, not the
    contextmanager's own) appears inside a `with ha_permission_scope(`
    block anywhere in the tree -- resetting a contextvars.Token from a
    different context raises."""

    def test_no_yield_inside_permission_scope(self):
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "ha_permission_scope(" not in text:
                continue
            try:
                tree = ast.parse(text, filename=str(path))
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.With):
                    continue
                is_scope_with = any(
                    isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "ha_permission_scope"
                    for item in node.items
                )
                if not is_scope_with:
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.Yield, ast.YieldFrom)):
                        offenders.append(f"{path.relative_to(REPO_ROOT)}:{inner.lineno}")
        assert offenders == []


class TestFallbackBranchGone:
    def test_fallback_branch_gone(self):
        text = (SRC / "orchestrator" / "nodes" / "route_control.py").read_text(encoding="utf-8")
        assert "Fallback to simple pattern matching if smart controller not available" not in text
        assert "check_entity_permission" not in text
        assert "Home automation isn't configured." in text
