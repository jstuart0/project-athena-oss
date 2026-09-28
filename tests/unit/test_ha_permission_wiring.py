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

Later passes (C/E/F) append their own members to this file; do not remove
Pass A/B's cases when doing so.
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
        bypassed ensure_permission_enforcing."""
        allowed_receivers = {
            "ha_client", "self.ha_client", "self.ha", "self._inner", "inner", "self._ha_raw",
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
        write_methods = public_async_methods - {"get_state", "health_check", "close"}
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
        "scene": "_handle_scene_intent",
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
        for device_type, handler_name in self._DEVICE_TYPE_TO_HANDLER.items():
            node = funcs.get(handler_name)
            assert node is not None, f"missing handler {handler_name}"
            actual = self._literal_call_service_domains(node)
            expected = set(mp.CONTROL_DEVICE_DOMAINS.get(device_type, ()))
            if actual != expected:
                mismatches[device_type] = (expected, actual)
        assert mismatches == {}

        assert set(mp.CONTROL_DEVICE_DOMAINS["bed_warmer"]) == {"switch", "select"}
        assert set(mp.CONTROL_DEVICE_DOMAINS["whole_house"]) == {"light"}
