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

Pass B members:
 - test_every_controller_ha_client_method_wraps_first
 - test_call_service_methods_subset_of_ha_client_methods
 - test_controller_call_service_receivers_are_ha_client
 - test_every_ha_holder_constructor_wraps
 - test_lifespan_wraps_ha_client
 - test_ha_client_write_surface_is_intercepted
 - test_control_device_domains_equal_handler_writes


Pass E members (gateway -- P5, D17/D24/D27):
 - test_gateway_simple_command_call_sites_unchanged
 - test_orchestrator_query_callers_tag_caller_trust
 - test_scanner_detects_untagged_synthetic_caller (negative member)
 - test_livekit_browser_token_sites_use_default_ttl


Pass C members:
 - test_entry_points_use_resolve_request_authorization
 - test_no_client_mode_trust
 - test_pin_branch_only_via_trust_helper

Pass F members (jarvis-web -- P6, D19):
 - test_jarvis_web_mutating_routes_classified
 - test_websockets_importable_in_ci

Later passes append their own members to this file; do not remove
Pass A/B/C/E/F cases when doing so.

Pass D's wiring/drift test (test_sms_webhook_tags_caller_trust_sms) lives in
tests/unit/test_ha_permission_wiring_pass_d.py instead of here -- Passes B
and E both append to this file on their own branches, and this campaign's
passes are being built in parallel worktrees merged later, so adding a
fourth concurrent editor of this exact file just multiplies merge
conflicts (mozart, 2026-09-28).
"""
from __future__ import annotations

import asyncio
import ast
import importlib.util
import os
import re
import sys
import unittest.mock as mock
from pathlib import Path
from typing import Optional

import pytest

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
            {"get_state", "get_states", "health_check", "close", "is_configured"}
        )

    def test_guard_exposes_get_states(self):
        """Pass H: get_states() (bulk /api/states read) passes through the
        guard -- the fix for the _ha_raw exception this replaces."""
        inner = mock.MagicMock()
        inner.get_states = mock.AsyncMock(return_value=[{"entity_id": "sensor.x"}])
        guard = mp.PermissionEnforcingHAClient(inner)
        result = asyncio.run(guard.get_states())
        assert result == [{"entity_id": "sensor.x"}]
        inner.get_states.assert_awaited_once()

    def test_guard_still_blocks_headers_and_url(self):
        """Pass H: killing the _ha_raw exception must not accidentally
        widen the allowlist -- .headers was never exposed, and .url (used
        by nothing in src/ anymore) is removed too, not just left as-is."""
        guard = mp.PermissionEnforcingHAClient(mock.MagicMock())
        with pytest.raises(AttributeError):
            guard.headers
        with pytest.raises(AttributeError):
            guard.url


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
# Pass B
# ---------------------------------------------------------------------------

_CONTROLLER_PATH = SRC / "orchestrator" / "smart_home_controller.py"

_EXPECTED_HA_CLIENT_METHODS = {
    "execute_intent", "_handle_climate_intent", "_handle_media_intent",
    "_handle_bed_warmer_intent", "_handle_light_status_query", "_handle_lock_intent",
    "_handle_fan_intent", "_handle_cover_intent", "_handle_scene_intent",
    "_execute_whole_house_command", "_execute_multi_room_command",
    "_execute_room_group_command", "_handle_motion_control_intent",
}


def _controller_functions():
    tree = ast.parse(_CONTROLLER_PATH.read_text(encoding="utf-8"), filename=str(_CONTROLLER_PATH))
    funcs = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            funcs[node.name] = node
    return funcs


class TestEveryControllerHAClientMethodWrapsFirst:
    def test_every_controller_ha_client_method_wraps_first(self):
        funcs = _controller_functions()
        assert len(_EXPECTED_HA_CLIENT_METHODS) >= 13
        missing = []
        for name in _EXPECTED_HA_CLIENT_METHODS:
            node = funcs.get(name)
            assert node is not None, f"missing method {name}"
            # ensure_permission_enforcing(ha_client) must appear before any
            # other use of ha_client in the function body (the "first
            # executable statement" contract) -- checked by line number,
            # not a fixed line-count window, since docstring length varies.
            wrap_line = None
            first_use_line = None
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Name)
                    and inner.func.id == "ensure_permission_enforcing"
                    and inner.args
                    and isinstance(inner.args[0], ast.Name)
                    and inner.args[0].id == "ha_client"
                ):
                    wrap_line = inner.lineno if wrap_line is None else min(wrap_line, inner.lineno)
                elif (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "ha_client"
                ):
                    if first_use_line is None or inner.lineno < first_use_line:
                        first_use_line = inner.lineno
            if wrap_line is None:
                missing.append(name)
            elif first_use_line is not None and first_use_line < wrap_line:
                missing.append(name)
        assert missing == [], missing
        assert "_handle_lock_intent" in _EXPECTED_HA_CLIENT_METHODS


class TestCallServiceMethodsSubsetOfHAClientMethods:
    def test_call_service_methods_subset_of_ha_client_methods(self):
        """Every `.call_service(` receiver across the guarded modules is a
        known ha_client-like reference -- never a bare/raw variable that
        bypassed ensure_permission_enforcing. Pass H removed music_handler's
        `self._ha_raw` unwrapped-client exception entirely (it now reads
        via the guard's `get_states()` passthrough like everything else),
        so there is no read-only carve-out left to allowlist here."""
        allowed_receivers = {
            "ha_client", "self.ha_client", "self.ha", "self._inner", "inner",
            "self.music.ha",  # FollowMeAudioService reaches MusicHandler's already-wrapped self.ha
        }
        offenders = []
        paths = [
            _CONTROLLER_PATH,
            SRC / "orchestrator" / "sequence_executor.py",
            SRC / "orchestrator" / "automation_agent.py",
            SRC / "orchestrator" / "music_handler.py",
            SRC / "orchestrator" / "tv_handler.py",
            SRC / "orchestrator" / "follow_me_audio.py",
        ]
        pattern = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\.call_service\(")
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                receiver = m.group(1)
                if path.name == "mode_permission.py" and receiver in ("self._inner",):
                    continue
                if receiver not in allowed_receivers:
                    offenders.append(f"{path.name}:{receiver}")
        assert offenders == []

    def test_no_ha_raw_anywhere(self):
        """Pass H: music_handler.py's unwrapped-client exception (a
        `self._ha_raw` attribute holding the raw, unguarded
        HomeAssistantClient) is gone entirely -- that attribute reference
        must never reappear anywhere in src/. Scoped to the actual `self.`
        attribute access, not prose mentioning the removed name in a
        docstring or comment explaining the history."""
        offenders = []
        for path in SRC.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "self._ha_raw" in text or "._ha_raw =" in text:
                offenders.append(str(path.relative_to(REPO_ROOT)))
        assert offenders == []


class TestControllerCallServiceReceiversAreHAClient:
    def test_controller_call_service_receivers_are_ha_client(self):
        text = _CONTROLLER_PATH.read_text(encoding="utf-8")
        count = len(re.findall(r"\bha_client\.call_service\(", text))
        assert count >= 60


class TestEveryHAHolderConstructorWraps:
    EXPECTED_HOLDERS = {"SequenceExecutor", "AutomationAgent", "MusicHandler", "TVHandler", "FollowMeAudioService"}

    HOLDER_FILES = {
        "SequenceExecutor": SRC / "orchestrator" / "sequence_executor.py",
        "AutomationAgent": SRC / "orchestrator" / "automation_agent.py",
        "MusicHandler": SRC / "orchestrator" / "music_handler.py",
        "TVHandler": SRC / "orchestrator" / "tv_handler.py",
        "FollowMeAudioService": SRC / "orchestrator" / "follow_me_audio.py",
    }

    def test_every_ha_holder_constructor_wraps(self):
        found = set()
        for name, path in self.HOLDER_FILES.items():
            text = path.read_text(encoding="utf-8")
            if "ensure_permission_enforcing(ha_client)" in text:
                found.add(name)
        assert found == self.EXPECTED_HOLDERS
        assert "AutomationAgent" in self.EXPECTED_HOLDERS


class TestLifespanWrapsHAClient:
    def test_lifespan_wraps_ha_client(self):
        text = (SRC / "orchestrator" / "main.py").read_text(encoding="utf-8")
        assert "ha_client = ensure_permission_enforcing(ha_client_raw)" in text
        assert "_runtime.set_ha_client(ha_client)" in text
        # locals-first (R2-H1): the raw client is constructed into its own
        # local before being wrapped, never registered unwrapped.
        set_idx = text.index("_runtime.set_ha_client(ha_client)")
        wrap_idx = text.index("ha_client = ensure_permission_enforcing(ha_client_raw)")
        assert wrap_idx < set_idx


class TestHAClientWriteSurfaceIsIntercepted:
    def test_ha_client_write_surface_is_intercepted(self):
        import inspect
        from shared.ha_client import HomeAssistantClient

        public_async_methods = {
            name for name, member in inspect.getmembers(HomeAssistantClient, predicate=inspect.iscoroutinefunction)
            if not name.startswith("_")
        }
        write_methods = public_async_methods - {"get_state", "get_states", "health_check", "close"}
        assert write_methods == {"call_service", "create_automation", "delete_automation", "disable_automation"}
        for name in write_methods:
            assert hasattr(mp.PermissionEnforcingHAClient, name), f"guard doesn't intercept {name}"


class TestControlDeviceDomainsEqualHandlerWrites:
    """For each device_type -> handler, set(CONTROL_DEVICE_DOMAINS[device_type])
    == that handler's literal call_service first-argument domain set
    (==, not superset/subset -- an over-broad entry over-refuses degraded
    owners as surely as a missing one under-refuses guests)."""

    _DEVICE_TYPE_TO_HANDLER = {
        "climate": "_handle_climate_intent",
        "media": "_handle_media_intent",
        "media_player": "_handle_media_intent",
        "tv": "_handle_media_intent",
        "speaker": "_handle_media_intent",
        "bed_warmer": "_handle_bed_warmer_intent",
        "motion_control": "_handle_motion_control_intent",
        "lock": "_handle_lock_intent",
        "fan": "_handle_fan_intent",
        "cover": "_handle_cover_intent",
        # ATHENA-128 4.3: the good_night/leaving/morning/home fallback
        # (light/lock domains) was extracted from _handle_scene_intent
        # into _run_scene_fallback so it could be gated once at its top --
        # the domain surface for "scene" now spans both functions.
        "scene": ("_handle_scene_intent", "_run_scene_fallback"),
        "whole_house": "_execute_whole_house_command",
        "light": "_dispatch_light_or_room_command",
        "oven": "_handle_appliance_intent",
        "fridge": "_handle_appliance_intent",
        "freezer": "_handle_appliance_intent",
        "appliance": "_handle_appliance_intent",
        "sensor": "_handle_sensor_intent",
    }

    @staticmethod
    def _literal_call_service_domains(node: ast.AST) -> set:
        """Literal domain strings passed as call_service's first argument,
        either directly (a string constant) or indirectly via a local
        variable that's assigned string literal(s) elsewhere in the same
        function (e.g. _handle_scene_intent's `domain = 'scene'` /
        `domain = 'script'` branches feeding `call_service(domain, ...)`).
        """
        assigned_literals: dict = {}
        for inner in ast.walk(node):
            if isinstance(inner, ast.Assign) and isinstance(inner.value, ast.Constant) and isinstance(inner.value.value, str):
                for target in inner.targets:
                    if isinstance(target, ast.Name):
                        assigned_literals.setdefault(target.id, set()).add(inner.value.value)

        domains = set()
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "call_service"
                and inner.args
            ):
                first = inner.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    domains.add(first.value)
                elif isinstance(first, ast.Name) and first.id in assigned_literals:
                    domains.update(assigned_literals[first.id])
        return domains

    def test_control_device_domains_equal_handler_writes(self):
        funcs = _controller_functions()
        mismatches = {}
        for device_type, handler_names in self._DEVICE_TYPE_TO_HANDLER.items():
            if isinstance(handler_names, str):
                handler_names = (handler_names,)
            actual = set()
            for handler_name in handler_names:
                node = funcs.get(handler_name)
                assert node is not None, f"missing handler {handler_name}"
                actual |= self._literal_call_service_domains(node)
            expected = set(mp.CONTROL_DEVICE_DOMAINS.get(device_type, ()))
            if actual != expected:
                mismatches[device_type] = (expected, actual)
        assert mismatches == {}

        assert set(mp.CONTROL_DEVICE_DOMAINS["bed_warmer"]) == {"switch", "select"}
        assert set(mp.CONTROL_DEVICE_DOMAINS["whole_house"]) == {"light"}


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


def _resolve_payload_tag(func_node: ast.AST, json_value: ast.AST, key: str):
    """Resolve the value of payload key `key` for a json= value passed to
    a /query POST: a literal dict, or a Name resolved to the last
    `<name> = {...}` dict-literal assignment in the same function (M3's
    fix), plus any later `<name>[key] = ...` mutation in the same
    function. Returns None if `key` is present nowhere; a literal value
    for a Constant; or the unparsed source for a non-constant expression
    (e.g. jarvis-web's `caller.trust` attribute access, or a passthrough
    of a client-supplied request field)."""
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
            if isinstance(key_node, ast.Constant) and key_node.value == key:
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
                subscript_key = node.targets[0].slice
                if isinstance(subscript_key, ast.Constant) and subscript_key.value == key:
                    value_node = node.value

    if value_node is None:
        return None
    if isinstance(value_node, ast.Constant):
        return value_node.value
    try:
        return ast.unparse(value_node)
    except Exception:
        return "<unparseable>"


def _resolve_caller_trust_tag(func_node: ast.AST, json_value: ast.AST):
    """caller_trust view of _resolve_payload_tag, kept so the existing
    caller_trust drift test reads unchanged."""
    return _resolve_payload_tag(func_node, json_value, "caller_trust")


_QUERY_CALLER_SCAN_ROOTS = [
    SRC / "gateway",
    REPO_ROOT / "apps",
    REPO_ROOT / "admin" / "backend" / "app",
]


def _scan_query_callers(key: str = "caller_trust") -> dict:
    """Population of every real /query or /query/stream caller under
    src/gateway, apps, and admin/backend/app, keyed by (relpath,
    function_name), mapped to its resolved value for payload `key`
    (None if absent)."""
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
                results[(str(path.relative_to(REPO_ROOT)), func_node.name)] = _resolve_payload_tag(
                    func_node, json_value, key
                )
    return results


class TestOrchestratorQueryCallersTagCallerTrust:
    """D24 fail-closed contract: every real /query or /query/stream caller
    is enumerated (population is set-equal, always -- a caller that
    disappears or a new untagged caller that appears both fail this test),
    and every caller carries the right caller_trust tag.

    Passes D (sms_webhook.py) and F (jarvis-web) have both landed on this
    branch -- there is no longer a pending/untagged member. Every value
    below is enforced unconditionally."""

    EXPECTED_POPULATION = {
        ("src/gateway/main.py", "route_to_orchestrator"),
        ("src/gateway/wyoming_bridge.py", "_process_query"),
        ("src/gateway/livekit_integration.py", "_handle_query"),
        ("admin/backend/app/routes/sms_webhook.py", "route_to_orchestrator"),
        ("apps/jarvis-web/backend/main.py", "chat"),
        ("apps/jarvis-web/backend/main.py", "chat_stream"),
    }

    # Exact literal caller_trust value.
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

    def test_orchestrator_query_callers_tag_caller_trust(self):
        found = _scan_query_callers()

        assert set(found.keys()) == self.EXPECTED_POPULATION

        for member, expected in self.EXPECTED_LITERAL_TAG.items():
            actual = found[member]
            assert actual == expected, f"{member}: expected {expected!r}, got {actual!r}"

        for member in self.JARVIS_WEB_MEMBERS:
            actual = found[member]
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


class TestOrchestratorQueryCallersTagSupportsFollowup:
    """ATHENA-128 5.1 / D14: every follow-up-capable /query caller sets
    `supports_followup` to a literal True in server code (never derived
    from a request field); Wyoming, whose session ends with each wake,
    omits the key. Same population as the caller_trust test."""

    EXPECTED_SUPPORTS_FOLLOWUP = {
        ("src/gateway/main.py", "route_to_orchestrator"): True,
        ("src/gateway/livekit_integration.py", "_handle_query"): True,
        ("admin/backend/app/routes/sms_webhook.py", "route_to_orchestrator"): True,
        ("apps/jarvis-web/backend/main.py", "chat"): True,
        ("apps/jarvis-web/backend/main.py", "chat_stream"): True,
    }
    WYOMING = ("src/gateway/wyoming_bridge.py", "_process_query")
    GATEWAY = ("src/gateway/main.py", "route_to_orchestrator")

    def test_orchestrator_query_callers_tag_supports_followup(self):
        found = _scan_query_callers("supports_followup")

        assert set(found.keys()) == TestOrchestratorQueryCallersTagCallerTrust.EXPECTED_POPULATION
        assert set(self.EXPECTED_SUPPORTS_FOLLOWUP) | {self.WYOMING} == set(found.keys())

        for member in self.EXPECTED_SUPPORTS_FOLLOWUP:
            assert found[member] is True, f"{member}: expected literal True, got {found[member]!r}"

        assert found[self.WYOMING] is None, f"Wyoming must not set supports_followup, got {found[self.WYOMING]!r}"

    def test_gateway_supports_followup_true_only_from_ha_conversation(self):
        """route_to_orchestrator is shared, so its literal is guarded by a
        kwarg; the only call site passing it is the HA conversation
        handler, with a literal True."""
        tree = ast.parse((SRC / "gateway" / "main.py").read_text(encoding="utf-8"))
        sites = []
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "route_to_orchestrator"
                ):
                    kw = {k.arg: k.value for k in node.keywords}
                    sites.append((func.name, kw))
        assert len(sites) >= 1
        followup_sites = [(name, kw) for name, kw in sites if "supports_followup" in kw]
        assert [name for name, _ in followup_sites] == ["ha_conversation"]
        value = followup_sites[0][1]["supports_followup"]
        assert isinstance(value, ast.Constant) and value.value is True
        voice = followup_sites[0][1].get("voice_device_id")
        assert voice is not None and ast.unparse(voice) == "request.device_id"

    def test_gateway_sends_voice_device_id_never_device_id(self):
        """The HA device id is a fingerprint input only. Sent as the
        orchestrator's device_id it would trigger the guest-session
        lookup by device (and let POST /api/user-sessions bind a guest to
        a satellite)."""
        assert _scan_query_callers("voice_device_id")[self.GATEWAY] == "voice_device_id"
        assert _scan_query_callers("device_id")[self.GATEWAY] is None

    def test_passthrough_of_request_field_fails_literal_check(self):
        source = (
            "import httpx\n\n"
            "async def synthetic_caller(request):\n"
            "    payload = {'query': 'hi', 'supports_followup': request.supports_followup}\n"
            "    async with httpx.AsyncClient() as client:\n"
            "        return await client.post('http://orchestrator/query', json=payload)\n"
        )
        visitor = _QueryCallVisitor()
        visitor.visit(ast.parse(source))
        assert len(visitor.found) == 1
        func_node, json_value = visitor.found[0]
        resolved = _resolve_payload_tag(func_node, json_value, "supports_followup")
        assert resolved == "request.supports_followup"
        assert resolved is not True

    def test_literal_true_resolves_true(self):
        """Positive control for the check above: the same shape with a
        literal resolves to True, so the negative fails for the right
        reason."""
        source = (
            "import httpx\n\n"
            "async def synthetic_caller(request):\n"
            "    payload = {'query': 'hi'}\n"
            "    payload['supports_followup'] = True\n"
            "    async with httpx.AsyncClient() as client:\n"
            "        return await client.post('http://orchestrator/query', json=payload)\n"
        )
        visitor = _QueryCallVisitor()
        visitor.visit(ast.parse(source))
        func_node, json_value = visitor.found[0]
        assert _resolve_payload_tag(func_node, json_value, "supports_followup") is True


class TestOrchestratorQueryCallersTagInterfaceType:
    """Every /query caller names its channel with a server-chosen literal
    (never a value copied from a request): the three speech edges say
    "voice", SMS says "text", jarvis-web's two chat routes say "chat". The
    orchestrator renders the answer by this value, so an edge that left it
    off would silently inherit the QueryRequest default."""

    EXPECTED = {
        ("src/gateway/main.py", "route_to_orchestrator"): "voice",
        ("src/gateway/wyoming_bridge.py", "_process_query"): "voice",
        ("src/gateway/livekit_integration.py", "_handle_query"): "voice",
        ("admin/backend/app/routes/sms_webhook.py", "route_to_orchestrator"): "text",
        ("apps/jarvis-web/backend/main.py", "chat"): "chat",
        ("apps/jarvis-web/backend/main.py", "chat_stream"): "chat",
    }

    def test_orchestrator_query_callers_tag_interface_type(self):
        found = _scan_query_callers("interface_type")

        assert set(found) == TestOrchestratorQueryCallersTagCallerTrust.EXPECTED_POPULATION
        assert found == self.EXPECTED
        assert sorted(found.values()) == ["chat", "chat", "text", "voice", "voice", "voice"]
        # Named member: jarvis-web's value is the literal, not message.interface_type.
        assert found[("apps/jarvis-web/backend/main.py", "chat")] == "chat"

    def test_a_request_field_passthrough_is_not_a_literal(self):
        source = (
            "import httpx\n\n"
            "async def synthetic_caller(message):\n"
            "    payload = {'query': 'hi', 'interface_type': message.interface_type or 'chat'}\n"
            "    async with httpx.AsyncClient() as client:\n"
            "        return await client.post('http://orchestrator/query', json=payload)\n"
        )
        visitor = _QueryCallVisitor()
        visitor.visit(ast.parse(source))
        func_node, json_value = visitor.found[0]
        resolved = _resolve_payload_tag(func_node, json_value, "interface_type")
        assert resolved not in {"voice", "text", "chat"}


class TestLiveKitBrowserTokenSitesUseDefaultTTL:
    """Pattern K drift guard: every generate_room_token( call site either
    passes no ttl_minutes (the three browser-facing sites, which pick up
    AthenaConfig.livekit_user_token_ttl_minutes) or passes
    ttl_minutes=24 * 60 explicitly (the two server-side Athena
    participant tokens, D27)."""

    EXPECTED_BROWSER_SITES = {
        ("src/gateway/livekit_service.py", 409),
        ("src/gateway/livekit_routes.py", 126),
        ("src/gateway/livekit_routes.py", 156),
    }
    EXPECTED_ATHENA_SITES = {
        ("src/gateway/livekit_service.py", 417),
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


# ---------------------------------------------------------------------------
# Pass C
# ---------------------------------------------------------------------------

_MAIN_PY = SRC / "orchestrator" / "main.py"


class TestEntryPointsUseResolveRequestAuthorization:
    EXPECTED_FUNCTIONS = {"process_query", "process_query_stream", "process_query_stream_v2", "chat_completions"}

    def test_entry_points_use_resolve_request_authorization(self):
        tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"), filename=str(_MAIN_PY))
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in self.EXPECTED_FUNCTIONS:
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "resolve_request_authorization"
                    ):
                        found.add(node.name)
                        break
        assert found == self.EXPECTED_FUNCTIONS


class TestNoClientModeTrust:
    def test_no_client_mode_trust(self):
        """Tripwire only (M1): no 'request.mode if request.mode' pattern
        anywhere in main.py -- the load-bearing guard is
        test_entry_points_use_resolve_request_authorization above."""
        text = _MAIN_PY.read_text(encoding="utf-8")
        assert "request.mode if request.mode" not in text


class TestPinBranchOnlyViaTrustHelper:
    """D24 / Pass H: activate_owner_override is never called directly
    anywhere outside mode_permission.py's own implementation --
    handle_owner_mode_utterance is the sole entry point. As of Pass H,
    handle_owner_mode_utterance is called from all four query entry
    points, not just process_query (codex full-diff, Medium): the two SSE
    stream endpoints and the OpenAI-compatible streaming branch previously
    let a PIN utterance fall through to normal chat synthesis instead of
    being refused/verified.

    "Enclosing function" is the OUTERMOST named function on the stack, not
    the innermost -- chat_completions' call is inside a nested
    `async def openai_stream_generator():` closure, same reasoning as the
    D36 caller-trust census scanner above."""

    EXPECTED_ENTRY_POINTS = {
        "process_query", "process_query_stream", "process_query_stream_v2", "chat_completions",
    }

    def test_pin_branch_only_via_trust_helper(self):
        tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"), filename=str(_MAIN_PY))

        activate_calls = 0
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "activate_owner_override"
            ):
                activate_calls += 1
        assert activate_calls == 0

        found_in = set()

        class _PinCallVisitor(ast.NodeVisitor):
            def __init__(self):
                self.func_stack: list[ast.AST] = []

            def visit_FunctionDef(self, node):
                self.func_stack.append(node)
                self.generic_visit(node)
                self.func_stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, node):
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "handle_owner_mode_utterance"
                    and self.func_stack
                ):
                    found_in.add(self.func_stack[0].name)
                self.generic_visit(node)

        _PinCallVisitor().visit(tree)

        assert found_in == self.EXPECTED_ENTRY_POINTS


# ---------------------------------------------------------------------------
# Pass F (jarvis-web -- P6, D19)
# ---------------------------------------------------------------------------

_JARVIS_BACKEND = REPO_ROOT / "apps" / "jarvis-web" / "backend"


def _load_jarvis_web_main():
    """Load apps/jarvis-web/backend/main.py under a private synthetic
    module name -- main.py is a name several services in this repo share
    (see tests/unit/test_jarvis_web_appliances_ha_entities.py:20-25)."""
    if str(_JARVIS_BACKEND) not in sys.path:
        sys.path.insert(0, str(_JARVIS_BACKEND))
    os.environ.setdefault("SERVICE_API_KEY", "test-key-wiring")
    spec = importlib.util.spec_from_file_location("_wiring_test_jarvis_web_main", _JARVIS_BACKEND / "main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_wiring_test_jarvis_web_main"] = module
    spec.loader.exec_module(module)
    return module


def _jarvis_route_keys(app):
    """Every registered route of a jarvis-web app: "METHOD /path" per HTTP
    method, "WS /path" per WebSocket, "MOUNT /path" per mount."""
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import Mount, Route, WebSocketRoute
    from shared.route_walk import iter_routes

    keys = {}
    for walked in iter_routes(app):
        route, path = walked.route, walked.path
        if isinstance(route, Mount):
            keys[f"MOUNT {path}"] = route
        elif isinstance(route, (APIWebSocketRoute, WebSocketRoute)):
            keys[f"WS {path}"] = route
        elif isinstance(route, (APIRoute, Route)):
            for method in route.methods or ():
                if method != "HEAD":
                    keys[f"{method} {path}"] = route
    return keys


def _jarvis_census_findings(app, classification, dependencies):
    """Problems with a census: unclassified or stale entries, and gated
    HTTP routes missing their class's dependency."""
    findings = []
    keys = _jarvis_route_keys(app)
    for key in sorted(set(keys) - set(classification)):
        findings.append(f"unclassified: {key}")
    for key in sorted(set(classification) - set(keys)):
        findings.append(f"stale: {key}")
    for key, kind in classification.items():
        route = keys.get(key)
        if route is None or kind == "public" or key.startswith(("WS ", "MOUNT ")):
            continue
        calls = [d.call for d in getattr(getattr(route, "dependant", None), "dependencies", [])]
        if dependencies[kind] not in calls:
            findings.append(f"missing dependency: {key} ({kind})")
    return findings


class TestJarvisWebRoutesClassified:
    """D14: every jarvis-web route (all methods, WebSockets and mounts) is
    classified, set-equal against what FastAPI registered, and every gated
    HTTP route carries exactly its class's dependency."""

    NAMED = {
        "POST /livekit/rooms": "owner_only",
        "GET /api/welcome": "guest_read",
        "GET /api/sensors/summary": "household_read",
        "POST /api/chat": "relay_chat",
        "GET /api/health": "public",
    }

    def test_jarvis_web_routes_classified(self):
        module = _load_jarvis_web_main()
        classification = module.ROUTE_CLASSIFICATION
        assert _jarvis_census_findings(module.app, classification, module.ROUTE_DEPENDENCIES) == []
        for key, kind in self.NAMED.items():
            assert classification.get(key) == kind, key
        for key in ("WS /ma/ws", "WS /ma/sendspin", "MOUNT /static", "MOUNT /logos", "GET /"):
            assert key in classification, key
        assert classification["WS /ma/ws"] == classification["WS /ma/sendspin"] == "owner_only"
        assert len(classification) >= 55
        owner_only = {k for k, v in classification.items() if v == "owner_only"}
        assert len(owner_only) == 29

    def test_only_health_and_the_sign_in_page_are_public(self):
        """D28: without JARVIS_ENABLE_DOCS the anonymous surface is
        /api/health plus GET / (which answers a non-browser with the static
        401 page); the static mounts are gated by _BrowserStaticFiles."""
        from starlette.routing import Mount

        module = _load_jarvis_web_main()
        classification = module.ROUTE_CLASSIFICATION
        assert {k for k, v in classification.items() if v == "public"} == {"GET /api/health", "GET /"}
        from shared.route_walk import iter_routes

        mounts = {f"MOUNT {w.path}": w.route for w in iter_routes(module.app) if isinstance(w.route, Mount)}
        assert set(mounts) == {"MOUNT /static", "MOUNT /logos"}
        for key, mount in mounts.items():
            assert classification[key] == "browser", key
            assert isinstance(mount.app, module._BrowserStaticFiles), key

    def test_census_self_test_reports_problems(self):
        """A synthetic app with an unclassified GET and a household_read
        route missing its dependency: both are reported."""
        from fastapi import Depends, FastAPI

        async def gate():
            return None

        async def other():
            return None

        app = FastAPI()

        @app.get("/unclassified")
        async def unclassified():
            return {}

        @app.get("/reads", dependencies=[Depends(other)])
        async def reads():
            return {}

        findings = _jarvis_census_findings(
            app, {"GET /reads": "household_read", "GET /gone": "guest_read"}, {"household_read": gate, "guest_read": gate},
        )
        assert "unclassified: GET /unclassified" in findings
        assert "missing dependency: GET /reads (household_read)" in findings
        assert "stale: GET /gone" in findings  # tessa CN-f: an entry for a route that no longer exists


class TestWebsocketsImportableInCI:
    def test_websockets_importable_in_ci(self):
        """MUSIC_WS_AVAILABLE (and therefore the WS entries in
        ROUTE_CLASSIFICATION, and CI's coverage of the two owner_only WS
        gates) depends on the `websockets` package being importable. This
        is a tripwire: if it silently stops being a dependency, the census's
        two WS members above go untested in CI."""
        import websockets  # noqa: F401


# ---------------------------------------------------------------------------
# Pass H2 (xander delta review, item 2)
# ---------------------------------------------------------------------------


class TestSensorAndStatusFastPathsNeverCallService:
    """route_control_node's sensor/presence fast paths
    (nodes/route_control.py, the `device_type == "sensor"` branch and the
    presence-pattern branch) both dispatch to
    SmartHomeController._handle_sensor_intent, and the HA status-bulk-query
    fast path dispatches to ha_status_optimizer.optimize_status_query.
    Neither opens an ha_permission_scope -- both run BEFORE/OUTSIDE the
    permission scope, which is only correct because they are read-only
    (sensors, not switches/locks/etc). A future edit that adds a write --
    even an innocuous-looking one, e.g. clearing a stale sensor cache via a
    HA service call -- would silently bypass the guard entirely. This is a
    drift tripwire, not a coverage test: it asserts neither function's body
    ever references `call_service` anywhere in the tree.
    """

    def _assert_no_call_service(self, path: Path, func_name: str, node: ast.AST) -> None:
        offenders = [
            inner.lineno
            for inner in ast.walk(node)
            if isinstance(inner, ast.Attribute) and inner.attr == "call_service"
        ]
        assert offenders == [], (
            f"{func_name} in {path.relative_to(REPO_ROOT)} references "
            f".call_service at line(s) {offenders} -- it runs before/outside "
            f"ha_permission_scope, so any write here bypasses the guard "
            f"entirely."
        )

    def test_handle_sensor_intent_never_calls_service(self):
        funcs = _controller_functions()
        node = funcs.get("_handle_sensor_intent")
        assert node is not None, "missing SmartHomeController._handle_sensor_intent"
        self._assert_no_call_service(_CONTROLLER_PATH, "_handle_sensor_intent", node)

    def test_optimize_status_query_never_calls_service(self):
        path = SRC / "orchestrator" / "ha_status_optimizer.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        node = None
        for candidate in ast.walk(tree):
            if isinstance(candidate, ast.AsyncFunctionDef) and candidate.name == "optimize_status_query":
                node = candidate
                break
        assert node is not None, "missing optimize_status_query"
        self._assert_no_call_service(path, "optimize_status_query", node)

    def test_route_control_fast_paths_route_to_handle_sensor_intent(self):
        """Belt and suspenders: confirm the two fast-path branches in
        route_control_node actually dispatch through _handle_sensor_intent
        (and not some other, unaudited path) before the scope opens."""
        path = SRC / "orchestrator" / "nodes" / "route_control.py"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        node = None
        for candidate in ast.walk(tree):
            if isinstance(candidate, ast.AsyncFunctionDef) and candidate.name == "route_control_node":
                node = candidate
                break
        assert node is not None, "missing route_control_node"

        scope_open_line = None
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.With)
                and any(
                    isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Name)
                    and item.context_expr.func.id == "ha_permission_scope"
                    for item in inner.items
                )
            ):
                scope_open_line = inner.lineno if scope_open_line is None else min(scope_open_line, inner.lineno)

        sensor_call_lines = [
            inner.lineno
            for inner in ast.walk(node)
            if isinstance(inner, ast.Attribute) and inner.attr == "_handle_sensor_intent"
        ]
        assert len(sensor_call_lines) >= 2, "expected both fast-path branches to call _handle_sensor_intent"
        if scope_open_line is not None:
            assert all(line < scope_open_line for line in sensor_call_lines), (
                "a _handle_sensor_intent call site moved inside/after the "
                "permission scope -- fast paths must run before it opens"
            )
