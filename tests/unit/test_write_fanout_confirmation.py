"""ATHENA-128 Phase 4/5 -- write fan-out confirmation gate.

Phase 4: the gate itself (write_fanout.py), the closed-world drift test
over every SmartHomeController method containing call_service(, and the
gate's own unit tests (thresholds, cues, unbounded, isolation).

Phase 5 (confirmation carriage) tests are appended in the Phase 5 commit.
"""
from __future__ import annotations

import ast
import asyncio
import sys
import unittest.mock as mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")
sys.path.insert(0, "src/orchestrator")

import pytest

from orchestrator import write_fanout
from orchestrator import mode_permission as mp
from orchestrator.utterance_kind import classify_utterance, UtteranceKind
import shared.config as config_module

SRC_PATH = Path("src/orchestrator/smart_home_controller.py")


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clear_config_cache():
    config_module._clear_cache_for_tests()
    yield
    config_module._clear_cache_for_tests()


def _fake_config(threshold=6, hard_limit=18):
    cfg = MagicMock()
    cfg.ha_write_fanout_confirm_threshold = threshold
    cfg.ha_write_fanout_hard_limit = hard_limit
    return cfg


# ---------------------------------------------------------------------------
# 4.5 -- Closed-world drift test
# ---------------------------------------------------------------------------

GATED = {
    "_dispatch_light_or_room_command",
    "_handle_media_intent",
    "_handle_lock_intent",
    "_handle_fan_intent",
    "_handle_cover_intent",
    "_execute_whole_house_command",
    "_execute_multi_room_command",
    "_execute_room_group_command",
    "_run_scene_fallback",
}

ALLOWLIST = {
    "_handle_climate_intent": "single resolved thermostat entity",
    "_handle_bed_warmer_intent": "fixed configured bed-warmer entities (<=5, set by config)",
    "_handle_motion_control_intent": "per-room automation-override helpers, not device fan-out",
    "_handle_scene_intent": "one named scene/script entity; the fallback is delegated (_run_scene_fallback) and gated there",
}


def _iter_call_service_calls(node):
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == "call_service":
            yield inner


def _is_gate_call(call_node):
    return (
        isinstance(call_node, ast.Call)
        and isinstance(call_node.func, ast.Attribute)
        and call_node.func.attr in ("gate", "gate_many")
        and isinstance(call_node.func.value, ast.Name)
        and call_node.func.value.id == "write_fanout"
    )


def _is_check_and_return(stmt, name):
    if not isinstance(stmt, ast.If):
        return False
    test = stmt.test
    if not (isinstance(test, ast.Name) and test.id == name):
        return False
    if len(stmt.body) != 1:
        return False
    ret = stmt.body[0]
    return isinstance(ret, ast.Return) and isinstance(ret.value, ast.Name) and ret.value.id == name


def _stmt_lists(node):
    """Yield every statement list (body) in the tree, recursively,
    including nested defs -- each is a candidate 'statement list S'."""
    if isinstance(node, list):
        yield node
        for item in node:
            yield from _stmt_lists(item)
        return
    for field_name, value in ast.iter_fields(node):
        if isinstance(value, list) and value and isinstance(value[0], ast.AST):
            yield from _stmt_lists(value)
        elif isinstance(value, ast.AST):
            yield from _stmt_lists(value)


def method_is_gated(method_node) -> bool:
    """Every call_service Call in method_node (nested defs included) must
    be a descendant of a statement that comes after some (Assign binding
    write_fanout.gate(/gate_many( to a name X, immediately followed in the
    SAME statement list by `if X: return X`) pair. Multiple such pairs
    (one per branch) may jointly cover the whole method."""
    all_calls = [id(c) for c in _iter_call_service_calls(method_node)]
    if not all_calls:
        return True
    dominated = set()

    for stmts in _stmt_lists(method_node.body):
        for i, stmt in enumerate(stmts):
            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Name)
                and _is_gate_call(stmt.value)
            ):
                bound = stmt.targets[0].id
                if i + 1 < len(stmts) and _is_check_and_return(stmts[i + 1], bound):
                    for later in stmts[i + 2:]:
                        for call in ast.walk(later):
                            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "call_service":
                                dominated.add(id(call))

    return set(all_calls).issubset(dominated)


def _methods_with_call_service(tree):
    result = {}

    class _Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            if node.name == "SmartHomeController":
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if list(_iter_call_service_calls(item)):
                            result[item.name] = item
            self.generic_visit(node)

    _Visitor().visit(tree)
    return result


class TestClosedWorldDriftGuard:
    def test_every_write_method_is_gated_or_allowlisted(self):
        tree = ast.parse(SRC_PATH.read_text())
        methods = _methods_with_call_service(tree)
        population = set(methods)
        assert population == GATED | set(ALLOWLIST), (
            "population drift: new/removed call_service-bearing method",
            population ^ (GATED | set(ALLOWLIST)),
        )
        assert GATED & set(ALLOWLIST) == set()

        not_dominated = [name for name in GATED if not method_is_gated(methods[name])]
        assert not_dominated == [], not_dominated

        assert len(GATED) >= 9
        for named in ("_dispatch_light_or_room_command", "_execute_room_group_command", "_run_scene_fallback"):
            assert named in GATED

    def test_negative_bare_gate_expression_statement_not_gated(self):
        src = """
async def _fake_handler(self, ha_client):
    write_fanout.gate("light", "turn_off", target_lights, original_query)
    await ha_client.call_service("light", "turn_off", {"entity_id": "x"})
"""
        tree = ast.parse(src)
        fn = tree.body[0]
        assert not method_is_gated(fn)

    def test_negative_gate_in_one_elif_arm_only(self):
        src = """
async def _fake_handler(self, ha_client, action):
    if action == "lock":
        prompt = write_fanout.gate("lock", "lock", ids, original_query)
        if prompt:
            return prompt
        await ha_client.call_service("lock", "lock", {"entity_id": "x"})
    elif action == "unlock":
        await ha_client.call_service("lock", "unlock", {"entity_id": "x"})
"""
        tree = ast.parse(src)
        fn = tree.body[0]
        assert not method_is_gated(fn)

    def test_negative_check_tests_different_name(self):
        src = """
async def _fake_handler(self, ha_client):
    prompt = write_fanout.gate("light", "turn_off", target_lights, original_query)
    if other_name:
        return other_name
    await ha_client.call_service("light", "turn_off", {"entity_id": "x"})
"""
        tree = ast.parse(src)
        fn = tree.body[0]
        assert not method_is_gated(fn)

    def test_positive_gate_dominates(self):
        src = """
async def _fake_handler(self, ha_client):
    prompt = write_fanout.gate("light", "turn_off", target_lights, original_query)
    if prompt:
        return prompt
    await ha_client.call_service("light", "turn_off", {"entity_id": "x"})
"""
        tree = ast.parse(src)
        fn = tree.body[0]
        assert method_is_gated(fn)


class TestCanCarryPendingSetterIsSingular:
    """Pattern 1b guard: `can_carry_pending =` assignments appear only in
    mode_permission.py's PermissionScope dataclass and
    write_fanout.pending_carrier."""

    def test_can_carry_pending_assigned_only_in_two_files(self):
        import subprocess
        out = subprocess.run(
            ["grep", "-rn", "can_carry_pending", "src/orchestrator"],
            capture_output=True, text=True,
        ).stdout
        files = set()
        for line in out.splitlines():
            if "=" in line and "==" not in line.split("=", 1)[1][:1]:
                files.add(line.split(":", 1)[0])
        allowed = {"src/orchestrator/mode_permission.py", "src/orchestrator/write_fanout.py"}
        assert files <= allowed, files


# ---------------------------------------------------------------------------
# 4.6 -- gate() unit tests
# ---------------------------------------------------------------------------

def _scope(utterance_text, **overrides):
    perms = {"mode": "owner"}
    uk = classify_utterance(utterance_text)
    cm = mp.ha_permission_scope(perms, mode="owner", utterance=uk)
    return cm


class TestGateThresholds:
    def test_non_imperative_at_threshold_proceeds_over_blocks(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("office lights please") as scope:
            r_at = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(6)), "office lights please")
            assert r_at is None
        with _scope("office lights please") as scope:
            r_over = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(7)), "office lights please")
            assert r_over is not None

    def test_imperative_at_hard_limit_proceeds_over_blocks(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("turn off the office lights") as scope:
            r_at = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(18)), "turn off the office lights")
            assert r_at is None
        with _scope("turn off the office lights") as scope:
            r_over = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(19)), "turn off the office lights")
            assert r_over is not None

    def test_explicit_cue_above_hard_limit_proceeds(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("turn off all the office lights") as scope:
            r = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(30)), "turn off all the office lights")
            assert r is None

    def test_room_group_route_not_named_in_utterance_is_counted(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("turn on the lights") as scope:
            r = write_fanout.gate(
                "light", "turn_on", tuple(f"light.{i}" for i in range(20)), "turn on the lights",
                scope_hint=("room_group", "First Floor"),
            )
            assert r is not None

    def test_whole_house_route_by_unknown_utterance_without_cue_is_counted(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("I want the lights on") as scope:
            assert scope.utterance.kind == UtteranceKind.UNKNOWN
            r = write_fanout.gate(
                "light", "turn_on", tuple(f"light.{i}" for i in range(10)), "I want the lights on",
                scope_hint="whole_house",
            )
            assert r is not None

    def test_all_sentinel_is_unbounded(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("turn off the lights") as scope:
            r = write_fanout.gate("light", "turn_off", ("all",), "turn off the lights")
            assert r is not None
            assert not r.endswith("?")

    def test_zero_disables_threshold_and_hard_limit(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=0, hard_limit=0))
        with _scope("office lights please") as scope:
            r = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(50)), "office lights please")
            assert r is None
        with _scope("turn off the office lights") as scope:
            r2 = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(50)), "turn off the office lights")
            assert r2 is None

    def test_rewording_passes_the_gate_closed_loop(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("office lights please") as scope:
            r = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(7)), "office lights please")
        assert r is not None
        assert "say:" in r.lower()
        say_text = r.split("say:", 1)[1].strip().rstrip(".")
        with _scope(say_text) as scope2:
            assert scope2.utterance.kind == UtteranceKind.IMPERATIVE
            r2 = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(7)), say_text)
            assert r2 is None

    def test_interleaved_requests_isolated_take_block(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))

        async def _one(query, n):
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query)) as scope:
                write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(n)), query)
                await asyncio.sleep(0)
                return write_fanout.take_block()

        async def _drive():
            return await asyncio.gather(_one("office lights please", 7), _one("kitchen lights please", 9))

        b1, b2 = _run(_drive())
        assert b1 is not None and b2 is not None
        assert sum(len(w.entity_ids) for w in b1.writes) == 7
        assert sum(len(w.entity_ids) for w in b2.writes) == 9

    def test_can_carry_pending_false_over_threshold_reworded_no_question_mark(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("office lights please") as scope:
            scope.can_carry_pending = False
            r = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(7)), "office lights please")
        assert r is not None
        assert not r.endswith("?")
        assert "say:" in r.lower()

    def test_pending_carrier_restores_false_after_exception(self):
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner") as scope:
            assert scope.can_carry_pending is False
            with pytest.raises(ValueError):
                with write_fanout.pending_carrier(scope, enabled=True):
                    assert scope.can_carry_pending is True
                    raise ValueError("boom")
            assert scope.can_carry_pending is False

    def test_unbounded_with_can_carry_pending_never_prompts_even_confirmed(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("turn off the lights") as scope:
            scope.can_carry_pending = True
            with write_fanout.confirmed(("all",)):
                r = write_fanout.gate("light", "turn_off", ("all",), "turn off the lights")
        assert r is not None
        assert not r.endswith("?")

    def test_scene_leaving_fallback_gate_many_two_writes(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        with _scope("goodbye") as scope:
            r = write_fanout.gate_many(
                [
                    write_fanout.PlannedWrite("light", "turn_off", ("all",)),
                    write_fanout.PlannedWrite("lock", "lock", ("all",)),
                ],
                "goodbye",
                unbounded=True,
            )
        assert r is not None
        assert not r.endswith("?")
        assert "say:" in r.lower()
        assert "turn off all the lights" in r.lower()
        assert "lock all the locks" in r.lower()


# ---------------------------------------------------------------------------
# 4.6(k) -- sequence step over threshold (codex H1)
# ---------------------------------------------------------------------------

import orchestrator.smart_home_controller as shc
from orchestrator.sequence_executor import SequenceExecutor


class _FakeEntityManagerSeq:
    def __init__(self, n=12):
        self._entities = {f"light.office_{i}": {"state": "on", "attributes": {"friendly_name": f"Office Light {i}"}} for i in range(n)}
        self._n = n

    async def get_entities(self):
        return dict(self._entities)

    async def find_lights_by_room(self, room):
        if room and "office" in room.lower():
            return [{
                "entity_id": f"light.office_{i}", "friendly_name": f"Office Light {i}",
                "members": [], "state": "on", "type": "individual",
            } for i in range(self._n)]
        return []


def _raw_ha_client_seq():
    client = MagicMock()
    client.call_service = AsyncMock(return_value={"ok": True})
    return client


class TestSequenceStepFanoutRefusal:
    def _make_executor(self):
        em = _FakeEntityManagerSeq(n=12)
        controller = shc.SmartHomeController(entity_manager=em, llm_router=MagicMock())
        client = _raw_ha_client_seq()
        executor = SequenceExecutor(controller, client)
        return executor, client

    def test_sequence_step_over_hard_limit_refused_synchronous(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=10))
        executor, client = self._make_executor()
        steps = [{"action": "turn_off", "target": {"device_type": "light", "room": "office"}}]

        async def _drive():
            with mp.ha_permission_scope(
                {"mode": "owner"}, mode="owner", utterance=classify_utterance("turn off the office lights"),
            ):
                return await executor.execute_sequence(steps, session_id="seq-1", background=False)

        result = _run(_drive())
        assert client.call_service.await_count == 0
        assert "skipped" in result.lower()
        step_results = executor._last_results.get("seq-1")
        assert step_results == [{"step": 1, "status": "refused_fanout", "message": step_results[0]["message"]}]
        assert not step_results[0]["message"].endswith("?")
        assert write_fanout.take_block() is None

    def test_sequence_step_over_hard_limit_refused_background(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=10))
        executor, client = self._make_executor()
        steps = [{"action": "turn_off", "target": {"device_type": "light", "room": "office"}}]

        async def _drive():
            with mp.ha_permission_scope(
                {"mode": "owner"}, mode="owner", utterance=classify_utterance("turn off the office lights"),
            ):
                ack = await executor.execute_sequence(steps, session_id="seq-2", background=True)
                task = executor._running_sequences.get("seq-2")
                if task is not None:
                    await task
                return ack

        ack = _run(_drive())
        assert client.call_service.await_count == 0
        step_results = executor._last_results.get("seq-2")
        assert step_results[0]["status"] == "refused_fanout"
        assert write_fanout.take_block() is None
