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

Pass E members (gateway -- P5, D17/D24/D27):
 - test_gateway_simple_command_call_sites_unchanged
 - test_orchestrator_query_callers_tag_caller_trust
 - test_scanner_detects_untagged_synthetic_caller (negative member)
 - test_livekit_browser_token_sites_use_default_ttl

Later passes (B/C/F) append their own members to this file; do not remove
Pass A's or Pass E's cases when doing so.
"""
from __future__ import annotations

import ast
import re
import sys
import unittest.mock as mock
from pathlib import Path
from typing import Optional

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


# ---------------------------------------------------------------------------
# Pass E (gateway -- P5, D17/D24/D27)
# ---------------------------------------------------------------------------


class _RawHAServiceCallVisitor(ast.NodeVisitor):
    """Records every (function name) whose body contains a string literal
    or f-string chunk mentioning "/api/services/" -- the raw HA
    service-call endpoint (Pattern H)."""

    def __init__(self):
        self.func_stack: list[str] = []
        self.found_funcs: set[str] = set()

    def visit_FunctionDef(self, node):
        self.func_stack.append(node.name)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Constant(self, node):
        if isinstance(node.value, str) and "/api/services/" in node.value and self.func_stack:
            self.found_funcs.add(self.func_stack[-1])
        self.generic_visit(node)


class TestGatewaySimpleCommandCallSitesUnchanged:
    """Pattern H drift guard, gateway's share: the set of functions in
    src/gateway/ that call HA's raw /api/services/ endpoint must stay
    exactly the two already accounted for -- execute_simple_command
    (turn_on/turn_off, now gated by fast_path_allowed() + the lights-only
    check) and send_satellite_announcement (a Wyoming satellite TTS
    announcement, not a device write -- explicitly out of scope, D17/P5).
    A new raw HA writer appearing anywhere else in the gateway fails this
    test instead of shipping unguarded."""

    EXPECTED_CALL_SITES = {
        ("src/gateway/simple_commands.py", "execute_simple_command"),
        ("src/gateway/main.py", "send_satellite_announcement"),
    }

    def test_gateway_simple_command_call_sites_unchanged(self):
        found = set()
        for path in sorted((SRC / "gateway").rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "/api/services/" not in text:
                continue
            try:
                tree = ast.parse(text, filename=str(path))
            except SyntaxError:
                continue
            visitor = _RawHAServiceCallVisitor()
            visitor.visit(tree)
            relpath = str(path.relative_to(REPO_ROOT))
            for func_name in visitor.found_funcs:
                found.add((relpath, func_name))
        assert found == self.EXPECTED_CALL_SITES


_QUERY_URL_SUFFIXES = ("/query", "/query/stream")


def _query_url_tail(node: ast.AST) -> Optional[str]:
    """The literal string suffix of a URL expression: the full value for a
    plain string constant, or the trailing constant chunk of an f-string
    (JoinedStr) -- e.g. f"{BASE}/query" -> "/query"."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values:
        last = node.values[-1]
        if isinstance(last, ast.Constant) and isinstance(last.value, str):
            return last.value
    return None


def _is_query_url(node: ast.AST) -> bool:
    tail = _query_url_tail(node)
    if tail is None:
        return False
    return any(tail == suffix or tail.endswith(suffix) for suffix in _QUERY_URL_SUFFIXES)


class _QueryCallVisitor(ast.NodeVisitor):
    """Finds every httpx .post(url, json=...) / .stream("POST", url,
    json=...) call whose url ends in /query or /query/stream, and records
    (enclosing function node, json= keyword value node).

    "Enclosing function" is the OUTERMOST named function on the stack, not
    the innermost -- jarvis-web's chat_stream posts from a nested
    `async def generate():` closure, and the plan's caller table names
    chat_stream, not generate. ast.walk() on the outer function still
    descends into the inner one, so dict-literal resolution below is
    unaffected by using the outer node."""

    def __init__(self):
        self.func_stack: list[ast.AST] = []
        self.found: list[tuple[ast.AST, ast.AST]] = []

    def visit_FunctionDef(self, node):
        self.func_stack.append(node)
        self.generic_visit(node)
        self.func_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node):
        url_node = None
        if isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr == "post" and node.args:
                url_node = node.args[0]
            elif attr == "stream" and len(node.args) >= 2:
                method_node = node.args[0]
                if (
                    isinstance(method_node, ast.Constant)
                    and isinstance(method_node.value, str)
                    and method_node.value.upper() == "POST"
                ):
                    url_node = node.args[1]
        if url_node is not None and _is_query_url(url_node):
            json_kw = next((kw for kw in node.keywords if kw.arg == "json"), None)
            if json_kw is not None and self.func_stack:
                self.found.append((self.func_stack[0], json_kw.value))
        self.generic_visit(node)


def _resolve_caller_trust_tag(func_node: ast.AST, json_value: ast.AST):
    """Resolve the caller_trust tag for a json= value passed to a /query
    POST: a literal dict, or a Name resolved to the last `<name> = {...}`
    dict-literal assignment in the same function (M3's fix), plus any
    later `<name>["caller_trust"] = ...` mutation in the same function.
    Returns None if no caller_trust key is present anywhere; a literal
    value for a Constant; or the unparsed source for a non-constant
    expression (e.g. jarvis-web's `caller.trust` attribute access)."""
    dict_node = None
    name = None
    if isinstance(json_value, ast.Dict):
        dict_node = json_value
    elif isinstance(json_value, ast.Name):
        name = json_value.id
        for node in ast.walk(func_node):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name
                and isinstance(node.value, ast.Dict)
            ):
                dict_node = node.value
    else:
        return "<dynamic-json-body>"

    value_node = None
    if dict_node is not None:
        for key_node, val_node in zip(dict_node.keys, dict_node.values):
            if isinstance(key_node, ast.Constant) and key_node.value == "caller_trust":
                value_node = val_node

    if name is not None:
        for node in ast.walk(func_node):
            if (
                isinstance(node, ast.Assign)
                and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Subscript)
                and isinstance(node.targets[0].value, ast.Name)
                and node.targets[0].value.id == name
            ):
                key = node.targets[0].slice
                if isinstance(key, ast.Constant) and key.value == "caller_trust":
                    value_node = node.value

    if value_node is None:
        return None
    if isinstance(value_node, ast.Constant):
        return value_node.value
    try:
        return ast.unparse(value_node)
    except Exception:
        return "<unparseable>"


_QUERY_CALLER_SCAN_ROOTS = [
    SRC / "gateway",
    REPO_ROOT / "apps",
    REPO_ROOT / "admin" / "backend" / "app",
]


def _scan_query_callers() -> dict:
    """Population of every real /query or /query/stream caller under
    src/gateway, apps, and admin/backend/app, keyed by (relpath,
    function_name), mapped to its resolved caller_trust tag (None if
    untagged)."""
    results = {}
    for root in _QUERY_CALLER_SCAN_ROOTS:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if ".post(" not in text and ".stream(" not in text:
                continue
            try:
                tree = ast.parse(text, filename=str(path))
            except SyntaxError:
                continue
            visitor = _QueryCallVisitor()
            visitor.visit(tree)
            for func_node, json_value in visitor.found:
                key = (str(path.relative_to(REPO_ROOT)), func_node.name)
                results[key] = _resolve_caller_trust_tag(func_node, json_value)
    return results


class TestOrchestratorQueryCallersTagCallerTrust:
    """D24 fail-closed contract: every real /query or /query/stream caller
    is enumerated (population is set-equal, always -- a caller that
    disappears or a new untagged caller that appears both fail this test),
    and every caller already expected to carry a caller_trust tag by this
    point in the implementation-pass sequence carries the right one.

    As of Pass E, only the three gateway sites (this pass) are tagged.
    sms_webhook.py's tag lands in Pass D and jarvis-web's in Pass F --
    this worktree ran Pass E directly after Pass A (Pass E's only
    dependency per the plan's pass table), so both are still untagged
    right now. Rather than fail until D and F land, an as-yet-untagged
    PENDING member is accepted with value None; once its owning pass
    lands, this same test starts enforcing the exact value with no edit
    required here."""

    EXPECTED_POPULATION = {
        ("src/gateway/main.py", "route_to_orchestrator"),
        ("src/gateway/wyoming_bridge.py", "_process_query"),
        ("src/gateway/livekit_integration.py", "_handle_query"),
        ("admin/backend/app/routes/sms_webhook.py", "route_to_orchestrator"),
        ("apps/jarvis-web/backend/main.py", "chat"),
        ("apps/jarvis-web/backend/main.py", "chat_stream"),
    }

    # Exact literal caller_trust value once tagged.
    EXPECTED_LITERAL_TAG = {
        ("src/gateway/main.py", "route_to_orchestrator"): "household",
        ("src/gateway/wyoming_bridge.py", "_process_query"): "household",
        ("src/gateway/livekit_integration.py", "_handle_query"): "household",
        ("admin/backend/app/routes/sms_webhook.py", "route_to_orchestrator"): "sms",
    }

    # jarvis-web's tag is `caller.trust`, an attribute expression rather
    # than a literal -- checked for presence/exact source, not equality
    # against a plain string.
    JARVIS_WEB_MEMBERS = {
        ("apps/jarvis-web/backend/main.py", "chat"),
        ("apps/jarvis-web/backend/main.py", "chat_stream"),
    }

    # Not yet landed as of Pass E (owning pass in parentheses).
    PENDING_UNTIL_OWNING_PASS = {
        ("admin/backend/app/routes/sms_webhook.py", "route_to_orchestrator"),  # Pass D
        ("apps/jarvis-web/backend/main.py", "chat"),  # Pass F
        ("apps/jarvis-web/backend/main.py", "chat_stream"),  # Pass F
    }

    def test_orchestrator_query_callers_tag_caller_trust(self):
        found = _scan_query_callers()

        assert set(found.keys()) == self.EXPECTED_POPULATION

        for member, expected in self.EXPECTED_LITERAL_TAG.items():
            actual = found[member]
            if member in self.PENDING_UNTIL_OWNING_PASS and actual is None:
                continue
            assert actual == expected, f"{member}: expected {expected!r}, got {actual!r}"

        for member in self.JARVIS_WEB_MEMBERS:
            actual = found[member]
            if actual is None:
                continue  # Pass F not landed yet
            assert actual == "caller.trust", f"{member}: expected caller.trust, got {actual!r}"

        # Named member (M3): the wiring test must resolve json=<Name> back
        # to its dict literal, not just handle sms_webhook.py's inline dict.
        assert found[("src/gateway/wyoming_bridge.py", "_process_query")] == "household"

    def test_scanner_detects_untagged_synthetic_caller(self):
        """Negative member: a synthetic, untagged /query caller must still
        be reported by the scanner -- proves the AST walk finds real
        callers rather than silently skipping anything it can't resolve."""
        source = (
            "import httpx\n\n"
            "async def synthetic_caller():\n"
            "    payload = {'query': 'hi', 'mode': 'owner'}\n"
            "    async with httpx.AsyncClient() as client:\n"
            "        return await client.post('http://orchestrator/query', json=payload)\n"
        )
        tree = ast.parse(source)
        visitor = _QueryCallVisitor()
        visitor.visit(tree)

        assert len(visitor.found) == 1
        func_node, json_value = visitor.found[0]
        assert func_node.name == "synthetic_caller"
        assert _resolve_caller_trust_tag(func_node, json_value) is None


class TestLiveKitBrowserTokenSitesUseDefaultTTL:
    """Pattern K drift guard: every generate_room_token( call site either
    passes no ttl_minutes (the three browser-facing sites, which pick up
    AthenaConfig.livekit_user_token_ttl_minutes) or passes
    ttl_minutes=24 * 60 explicitly (the two server-side Athena
    participant tokens, D27)."""

    EXPECTED_BROWSER_SITES = {
        ("src/gateway/livekit_service.py", 408),
        ("src/gateway/livekit_routes.py", 126),
        ("src/gateway/livekit_routes.py", 156),
    }
    EXPECTED_ATHENA_SITES = {
        ("src/gateway/livekit_service.py", 416),
        ("src/gateway/livekit_routes.py", 187),
    }

    def test_livekit_browser_token_sites_use_default_ttl(self):
        browser_sites = set()
        athena_sites = set()
        other_sites = []

        for relpath in ("src/gateway/livekit_service.py", "src/gateway/livekit_routes.py"):
            path = REPO_ROOT / relpath
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "generate_room_token"
                ):
                    continue
                ttl_kw = next((kw for kw in node.keywords if kw.arg == "ttl_minutes"), None)
                site = (relpath, node.lineno)
                if ttl_kw is None:
                    browser_sites.add(site)
                elif (
                    isinstance(ttl_kw.value, ast.BinOp)
                    and isinstance(ttl_kw.value.op, ast.Mult)
                    and ast.unparse(ttl_kw.value) == "24 * 60"
                ):
                    athena_sites.add(site)
                else:
                    other_sites.append((site, ast.unparse(ttl_kw.value)))

        assert other_sites == []
        assert browser_sites == self.EXPECTED_BROWSER_SITES
        assert athena_sites == self.EXPECTED_ATHENA_SITES
