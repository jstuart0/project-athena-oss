"""ATHENA-128 Phase 4/5 -- write fan-out confirmation gate.

Phase 4: the gate itself (write_fanout.py), the closed-world drift test
over every SmartHomeController method containing call_service(, and the
gate's own unit tests (thresholds, cues, unbounded, isolation).

Phase 5: confirmation carriage through route_control_node (5.4) --
store, resolve rules 0-6, identity binding, nonce claim, expiry.
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
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
    # Single-target writes, gated with one entity so commands pass
    # unchanged and a real state question is never written silently
    # (with routing reverted by the kill switch).
    "_handle_climate_intent",
    "_handle_bed_warmer_intent",
    "_handle_motion_control_intent",
    "_handle_scene_intent",
}

# Every write-bearing method is gated; an entry here needs a stated reason.
ALLOWLIST: dict = {}


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


def _methods_with_call_service(tree, class_name="SmartHomeController"):
    result = {}

    class _Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            if node.name == class_name:
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if list(_iter_call_service_calls(item)):
                            result[item.name] = item
            self.generic_visit(node)

    _Visitor().visit(tree)
    return result


SEQ_SRC_PATH = Path("src/orchestrator/sequence_executor.py")
# SequenceExecutor's own writes (steps that name an entity directly skip
# execute_intent); every one is gated, none allowlisted.
SEQ_GATED = {"_execute_direct_action"}
SEQ_ALLOWLIST: dict = {}


class TestClosedWorldDriftGuard:
    def test_every_sequence_executor_write_is_gated(self):
        methods = _methods_with_call_service(ast.parse(SEQ_SRC_PATH.read_text()), "SequenceExecutor")
        assert set(methods) == SEQ_GATED | set(SEQ_ALLOWLIST), set(methods) ^ (SEQ_GATED | set(SEQ_ALLOWLIST))
        assert SEQ_GATED & set(SEQ_ALLOWLIST) == set()
        not_dominated = [name for name in SEQ_GATED if not method_is_gated(methods[name])]
        assert not_dominated == [], not_dominated
        assert "_execute_direct_action" in methods

    def test_negative_new_ungated_sequence_executor_write_is_caught(self):
        src = """
class SequenceExecutor:
    async def _execute_direct_action(self, entity_id, action, parameters):
        refusal = write_fanout.gate("light", "turn_off", (entity_id,), None)
        if refusal:
            return refusal
        await self.ha_client.call_service("light", "turn_off", {"entity_id": entity_id})

    async def _new_writer(self):
        await self.ha_client.call_service("lock", "unlock", {"entity_id": "lock.front"})
"""
        methods = _methods_with_call_service(ast.parse(src), "SequenceExecutor")
        assert set(methods) == {"_execute_direct_action", "_new_writer"}
        assert set(methods) != SEQ_GATED | set(SEQ_ALLOWLIST)
        assert method_is_gated(methods["_execute_direct_action"])
        assert not method_is_gated(methods["_new_writer"])

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

        assert len(GATED) >= 13
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

    def test_a_real_question_blocks_at_one_entity(self, monkeypatch):
        """CR22: whatever the limits, a real STATE_QUESTION's write is
        confirmed or reworded -- positive control: the same single write
        from a command proceeds."""
        for limits in ((6, 18), (0, 0)):
            monkeypatch.setattr(write_fanout, "get_config", lambda l=limits: _fake_config(*l))
            with _scope("is the office light on") as scope:
                assert write_fanout.gate("light", "turn_off", ("light.office_0",), "is the office light on") is not None
                assert scope.fanout_block is not None
            with _scope("turn off the office light"):
                assert write_fanout.gate("light", "turn_off", ("light.office_0",), "turn off the office light") is None

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
        assert r.startswith("That would affect all the lights and locks. "), r


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

        recorded = []
        real_steps = executor._execute_sequence_steps

        async def _spy(*a, **kw):
            results = await real_steps(*a, **kw)
            recorded.append(results)
            return results

        executor._execute_sequence_steps = _spy
        result = _run(_drive())
        assert client.call_service.await_count == 0
        step_results = recorded[0]
        assert step_results == [{"step": 1, "status": "refused_fanout", "message": step_results[0]["message"]}]
        assert "say:" in step_results[0]["message"]
        assert not step_results[0]["message"].endswith("?")
        assert result == f"Sequence complete. Skipped: {step_results[0]['message']}"
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
                assert task is not None
                return ack, await task

        with mock.patch("orchestrator.sequence_executor.logger") as seq_logger:
            ack, step_results = _run(_drive())
        assert client.call_service.await_count == 0
        assert step_results[0]["status"] == "refused_fanout"
        assert any(
            "sequence_step_fanout_refused" in str(c.args[0]) for c in seq_logger.warning.call_args_list
        ), seq_logger.warning.call_args_list
        assert "seq-2" not in executor._running_sequences
        assert write_fanout.take_block() is None


# ---------------------------------------------------------------------------
# 5.4 -- confirmation carriage through route_control_node
# ---------------------------------------------------------------------------

import time as _time_mod

from orchestrator.nodes import _runtime
from orchestrator.nodes import route_control as rc_module
from orchestrator.nodes import route_control_node
from orchestrator.state import OrchestratorState

OFFICE_N = 11
LIGHT_OFF_JSON = (
    '{"device_type": "light", "room": "office", "action": "turn_off", '
    '"target_scope": "group", "parameters": {}}'
)
PROMPT_11 = "That would turn off 11 lights in the office. Should I go ahead?"
EXPIRED_TEXT = "That request expired. Please say it again."
NEUTRAL_TEXT = "I'm not sure what you're agreeing to."
DECLINED_TEXT = "Okay, I won't."


class _FakeEntityManager54:
    def __init__(self, n_lights=OFFICE_N, n_locks=0):
        self.n_lights = n_lights
        self._locks = {
            f"lock.door_{i}": {"state": "locked", "attributes": {"friendly_name": f"Door {i} Lock"}}
            for i in range(n_locks)
        }

    async def get_entities(self):
        ents = {
            f"light.office_{i}": {"state": "on", "attributes": {"friendly_name": f"Office Light {i}"}}
            for i in range(self.n_lights)
        }
        ents.update(self._locks)
        return ents

    async def find_lights_by_room(self, room):
        if room and "office" in room.lower():
            return [
                {"entity_id": f"light.office_{i}", "friendly_name": f"Office Light {i}",
                 "members": [], "state": "on", "type": "individual"}
                for i in range(self.n_lights)
            ]
        return []

    async def get_all_light_groups(self):
        return []


class _LLM54:
    def __init__(self, response_text=LIGHT_OFF_JSON):
        self.response_text = response_text
        self.generate = AsyncMock(side_effect=self._generate)

    async def _generate(self, **kwargs):
        return {"response": self.response_text}


class _NxCache:
    """A cache client whose .client.set has real SET NX semantics."""

    def __init__(self):
        self.keys = {}
        self.client = self

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


def _written(client, domain=None):
    """Entity ids written through the raw HA client (optionally one domain)."""
    ids = []
    for call in client.call_service.await_args_list:
        args = list(call.args)
        call_domain = args[0] if args else call.kwargs.get("domain")
        if domain and call_domain != domain:
            continue
        data = args[2] if len(args) > 2 else (call.kwargs.get("service_data") or call.kwargs.get("data") or {})
        eid = (data or {}).get("entity_id")
        if isinstance(eid, (list, tuple)):
            ids.extend(eid)
        elif eid:
            ids.append(eid)
    return ids


def _raw_client_54(fail_domains=()):
    client = MagicMock()

    async def _call_service(domain, service, data=None, *a, **kw):
        if domain in fail_domains:
            raise RuntimeError(f"{domain} not found")
        return {"ok": True}

    client.call_service = AsyncMock(side_effect=_call_service)
    client.get_state = AsyncMock(return_value={"state": "on"})
    client.get_states = AsyncMock(return_value=[])
    return client


def _fp(trust="household", device="voice-a", room="office", mode="owner"):
    return write_fanout.caller_fingerprint(trust, device, room, mode)


def _state54(query, *, fingerprint=None, supports_followup=True, session_id="sess-1",
             prev_context=None, context_ref_info=None, mode="owner", room="office"):
    state = OrchestratorState(query=query)
    state.mode = mode
    state.permissions = {"mode": mode}
    state.room = room
    state.session_id = session_id
    state.prev_context = prev_context
    state.context_ref_info = context_ref_info if context_ref_info is not None else {}
    state.node_timings = {}
    state.supports_followup = supports_followup
    state.caller_fingerprint = fingerprint if fingerprint is not None else _fp(mode=mode)
    return state


class _Harness:
    """One runtime (controller, raw HA client, entity manager, cache) shared
    by every route_control_node call made through it, plus a single call
    log that records context stores and execute_intent in order."""

    def __init__(self, *, llm_text=LIGHT_OFF_JSON, n_lights=OFFICE_N, n_locks=0,
                 cache=None, fail_domains=(), threshold=6, hard_limit=18, flags=None):
        self.flags = dict(flags or {})
        self.em = _FakeEntityManager54(n_lights=n_lights, n_locks=n_locks)
        self.llm = _LLM54(llm_text)
        self.controller = shc.SmartHomeController(entity_manager=self.em, llm_router=self.llm)
        self.client = _raw_client_54(fail_domains)
        self.cache = cache
        self.cfg = _fake_config(threshold=threshold, hard_limit=hard_limit)
        self.log = []
        self.stores = []
        self.extract_calls = []

        real_execute = self.controller.execute_intent
        real_extract = self.controller.extract_intent

        async def _execute(intent, *a, **kw):
            self.log.append(("execute_intent", intent.get("action")))
            return await real_execute(intent, *a, **kw)

        async def _extract(query, *a, **kw):
            self.extract_calls.append((query, kw))
            return await real_extract(query, *a, **kw)

        self.controller.execute_intent = _execute
        self.controller.extract_intent = _extract

    async def _store(self, **kw):
        self.log.append(("store", kw.get("ttl"), "pending_write_confirmation" in (kw.get("parameters") or {})))
        self.stores.append(kw)
        return True

    def _install(self):
        _runtime.set_smart_controller(self.controller)
        _runtime.set_entity_manager(self.em)
        _runtime.set_ha_client(self.client)
        _runtime.set_sequence_executor(None)
        _runtime.set_automation_agent(None)
        _runtime.set_cache_client(self.cache)

    @contextlib.contextmanager
    def patched(self, now=None):
        """The module patches for route_control_node calls. Concurrent runs
        (asyncio.gather) must share ONE of these around the gather: nested
        per-run patches stopping in a different order would unpatch a run
        that's still in flight."""
        async def _feature_config(name):
            if name in self.flags:
                return {"enabled": bool(self.flags[name]), "config": {}}
            return {"enabled": name in ("status_bulk_query", "status_skip_synthesis")}

        patches = [
            mock.patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock, side_effect=_feature_config),
            mock.patch("orchestrator.nodes.route_control.get_automation_system_mode", new_callable=AsyncMock, return_value="pattern"),
            mock.patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            mock.patch("orchestrator.nodes.route_control.store_conversation_context", side_effect=self._store),
            mock.patch.object(write_fanout, "get_config", lambda: self.cfg),
        ]
        if now is not None:
            patches.append(mock.patch("time.time", return_value=now))
        for p in patches:
            p.start()
        try:
            yield
        finally:
            for p in reversed(patches):
                p.stop()

    async def arun(self, state, now=None, patch=True):
        self._install()
        if not patch:
            return await route_control_node(state)
        with self.patched(now=now):
            return await route_control_node(state)

    def run(self, state, now=None):
        return _run(self.arun(state, now=now))

    def last_pending(self):
        return self.stores[-1]["parameters"]["pending_write_confirmation"]


def _prev_with_pending(*, fingerprint, expires_at, nonce="nonce-1", n=OFFICE_N, query="office lights off please"):
    """The context shape 5.2 stores, for tests that need to control
    expires_at or the fingerprint directly (the others use _first_turn)."""
    return {
        "intent": "control",
        "query": query,
        "response": PROMPT_11,
        "entities": {"room": "office", "device_type": "light"},
        "parameters": {
            "action": "get_status",
            "device_type": "light",
            "room": "office",
            "pending_write_confirmation": {
                "intent": {"device_type": "light", "room": "office", "action": "turn_off",
                           "target_scope": "group", "parameters": {}},
                "writes": [["light", "turn_off", [f"light.office_{i}" for i in range(n)]]],
                "fingerprint": fingerprint,
                "nonce": nonce,
                "expires_at": expires_at,
            },
        },
    }


def _first_turn(h, *, supports_followup=True, query="office lights off please", fingerprint=None):
    """Drives the real first turn and returns (state, prev_context as the
    next turn would read it from the store)."""
    state = h.run(_state54(query, supports_followup=supports_followup, fingerprint=fingerprint))
    if not h.stores:
        return state, None
    stored = h.stores[-1]
    prev = {k: stored[k] for k in ("intent", "query", "entities", "parameters", "response")}
    return state, prev


YES_NO = {"anaphora_types": ["yes_no"], "has_context_ref": True, "is_continuation": False}


class TestPendingStored:
    def test_non_imperative_over_threshold_followup_stores_pending(self):
        h = _Harness()
        state, prev = _first_turn(h)
        assert _written(h.client) == []
        assert state.answer == PROMPT_11
        assert len(h.stores) == 1
        stored = h.stores[0]
        assert stored["ttl"] == 60
        assert stored["parameters"]["action"] == "get_status"
        pending = stored["parameters"]["pending_write_confirmation"]
        assert pending["fingerprint"] == state.caller_fingerprint and pending["fingerprint"]
        assert pending["nonce"]
        assert [w[:2] for w in pending["writes"]] == [["light", "turn_off"]]
        assert sorted(pending["writes"][0][2]) == sorted(f"light.office_{i}" for i in range(OFFICE_N))
        assert pending["intent"]["action"] == "turn_off"

    def test_no_followup_gets_rewording_and_no_store_then_rewording_executes(self):
        h = _Harness()
        state, prev = _first_turn(h, supports_followup=False)
        assert _written(h.client) == []
        assert "say: turn off all the office lights" in state.answer
        assert not state.answer.endswith("?")
        assert h.stores == []

        say = state.answer.split("say:", 1)[1].strip().rstrip(".")
        h2 = _Harness()
        h2.run(_state54(say, supports_followup=False))
        assert len(set(_written(h2.client, "light"))) == OFFICE_N

    def test_missing_fingerprint_is_not_a_followup_surface(self):
        h = _Harness()
        state = _state54("office lights off please")
        state.caller_fingerprint = None
        out = h.run(state)
        assert not out.answer.endswith("?")
        assert h.stores == []
        assert _written(h.client) == []

    def test_seven_locks_from_non_imperative_prompts(self):
        h = _Harness(
            llm_text='{"device_type": "lock", "room": null, "action": "unlock", "target_scope": "group", "parameters": {}}',
            n_lights=0, n_locks=7,
        )
        out = h.run(_state54("doors please", room=None))
        assert _written(h.client) == []
        assert out.answer.endswith("?"), out.answer
        assert "unlock 7" in out.answer
        assert h.stores and h.stores[-1]["ttl"] == 60


class TestBareRepliesResolve:
    def test_affirmations_replay_exactly_the_pending_writes(self):
        for phrase in ("yes", "Yes.", "Yes, please.", "Okay, do it.", "yeah go ahead"):
            h = _Harness()
            state, prev = _first_turn(h)
            assert prev is not None
            h.log.clear()
            out = h.run(_state54(phrase, prev_context=prev, context_ref_info=YES_NO))
            assert sorted(set(_written(h.client, "light"))) == sorted(f"light.office_{i}" for i in range(OFFICE_N)), phrase
            assert len(_written(h.client)) == OFFICE_N, phrase
            clear_idx = next(i for i, e in enumerate(h.log) if e[0] == "store" and e[2] is False)
            exec_idx = next(i for i, e in enumerate(h.log) if e[0] == "execute_intent")
            assert clear_idx < exec_idx, (phrase, h.log)

    def test_negations_decline_without_writing(self):
        for phrase in ("No.", "No, thanks."):
            h = _Harness()
            _, prev = _first_turn(h)
            h.stores.clear()
            h.log.clear()
            out = h.run(_state54(phrase, prev_context=prev, context_ref_info=YES_NO))
            assert _written(h.client) == [], phrase
            assert out.answer == DECLINED_TEXT
            assert h.stores and "pending_write_confirmation" not in h.stores[-1]["parameters"]
            assert not any(e[0] == "execute_intent" for e in h.log), phrase

    def test_qualified_yes_does_not_replay_and_clears(self):
        h = _Harness(llm_text='{"device_type": "light", "room": "office", "action": "get_status", "target_scope": "group", "parameters": {}}')
        prev = _prev_with_pending(fingerprint=_fp(), expires_at=_time_mod.time() + 60)
        out = h.run(_state54("yes, just the desk lamp", prev_context=prev, context_ref_info=YES_NO))
        assert _written(h.client) == []
        assert h.stores and "pending_write_confirmation" not in h.stores[0]["parameters"]
        assert h.stores[0]["parameters"]["action"] == "get_status"


class TestExpiry:
    NOW = 1_900_000_000.0

    def test_expires_at_equal_now_is_expired(self):
        h = _Harness()
        prev = _prev_with_pending(fingerprint=_fp(), expires_at=self.NOW)
        state = _state54("yes", prev_context=prev, context_ref_info=YES_NO)
        out = h.run(state, now=self.NOW)
        assert _written(h.client) == []
        assert out.answer == EXPIRED_TEXT
        assert h.extract_calls == []
        assert out.prev_context is None
        assert not any(e[0] == "execute_intent" for e in h.log)
        assert h.stores and "pending_write_confirmation" not in h.stores[0]["parameters"]

    def test_expires_at_now_plus_one_is_valid(self):
        h = _Harness()
        prev = _prev_with_pending(fingerprint=_fp(), expires_at=self.NOW + 1)
        h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO), now=self.NOW)
        assert len(set(_written(h.client, "light"))) == OFFICE_N

    def test_expired_prev_context_is_nulled_before_any_dispatch(self):
        h = _Harness()
        seen = []
        real_execute = h.controller.execute_intent

        async def _execute(intent, *a, **kw):
            seen.append(state.prev_context)
            return await real_execute(intent, *a, **kw)

        h.controller.execute_intent = _execute
        prev = _prev_with_pending(fingerprint=_fp(), expires_at=self.NOW)
        state = _state54("turn off the office lights", prev_context=prev, context_ref_info=YES_NO)
        h.run(state, now=self.NOW)
        assert seen and all(p is None for p in seen)

    def test_expired_then_new_utterance_is_context_free(self):
        h = _Harness()
        prev = _prev_with_pending(fingerprint=_fp(), expires_at=self.NOW)
        ref = {"anaphora_types": ["pronoun"], "has_context_ref": True, "is_continuation": True}
        h.run(_state54("turn them off", prev_context=prev, context_ref_info=ref), now=self.NOW)
        assert h.extract_calls, "extract_intent must run for a non-yes/no utterance"
        for _, kw in h.extract_calls:
            assert kw.get("prev_query") is None
            assert not kw.get("prev_intent_entities")


class TestIdentityBinding:
    def test_wrong_fingerprint_same_session_neutral_no_store(self):
        h = _Harness()
        _, prev = _first_turn(h, fingerprint=_fp(device="voice-a"))
        h.stores.clear()
        h.log.clear()
        with mock.patch.object(rc_module.logger, "warning") as warn:
            out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO, fingerprint=_fp(device="voice-b")))
        assert _written(h.client) == []
        assert out.answer == NEUTRAL_TEXT
        assert h.stores == []
        assert not any(e[0] == "execute_intent" for e in h.log)
        assert any(c.args and c.args[0] == "pending_write_resolved_cross_identity" for c in warn.call_args_list)

    def test_wrong_fingerprint_non_yes_no_neither_clears_nor_merges(self):
        """A mismatched caller can't clear someone else's pending or read
        its context (Risks: 'can't replay, decline, clear or learn')."""
        h = _Harness(llm_text='{"device_type": "light", "room": "kitchen", "action": "turn_on", "target_scope": "group", "parameters": {}}')
        prev = _prev_with_pending(fingerprint=_fp(device="voice-a"), expires_at=_time_mod.time() + 60)
        ref = {"anaphora_types": ["pronoun"], "has_context_ref": True, "is_continuation": True}
        h.run(_state54("turn them on", prev_context=prev, context_ref_info=ref, fingerprint=_fp(device="voice-b")))
        assert not any(s["query"] == prev["query"] for s in h.stores), h.stores
        for _, kw in h.extract_calls:
            assert kw.get("prev_query") is None

    def test_session_b_without_pending_does_nothing(self):
        h = _Harness(llm_text='{"device_type": "light", "room": "office", "action": "get_status", "target_scope": "group", "parameters": {}}')
        _first_turn(h)
        h.client.call_service.reset_mock()
        out = h.run(_state54("yes", session_id="sess-B", prev_context=None, context_ref_info={}))
        assert _written(h.client) == []

    def test_guest_yes_to_owner_pending_never_writes(self):
        h = _Harness()
        _, prev = _first_turn(h)
        out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO, mode="guest"))
        assert _written(h.client) == []
        assert _fp(mode="guest") != prev["parameters"]["pending_write_confirmation"]["fingerprint"]

    def test_ha_assist_device_id_is_a_fingerprint_input(self):
        a = write_fanout.caller_fingerprint("household", "ha-device-1", "office", "owner")
        b = write_fanout.caller_fingerprint("household", "ha-device-2", "office", "owner")
        assert a != b
        h = _Harness()
        _, prev = _first_turn(h, fingerprint=a)
        out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO, fingerprint=b))
        assert _written(h.client) == []
        assert out.answer == NEUTRAL_TEXT


class TestReplaySafety:
    def test_double_yes_replays_once_with_redis_nx(self):
        cache = _NxCache()
        h = _Harness(cache=cache)
        _, prev = _first_turn(h)

        async def _both():
            with h.patched():
                return await asyncio.gather(
                    h.arun(_state54("yes", prev_context=prev, context_ref_info=YES_NO), patch=False),
                    h.arun(_state54("yes", prev_context=prev, context_ref_info=YES_NO), patch=False),
                )

        outs = _run(_both())
        assert len(_written(h.client, "light")) == OFFICE_N
        assert sorted(o.answer for o in outs).count("Already done.") == 1
        assert any(k.startswith("athena:fanout_nonce:") for k in cache.keys)

    def test_double_yes_replays_once_with_process_fallback(self):
        h = _Harness(cache=None)
        _, prev = _first_turn(h)

        async def _both():
            with h.patched():
                return await asyncio.gather(
                    h.arun(_state54("yes", prev_context=prev, context_ref_info=YES_NO), patch=False),
                    h.arun(_state54("yes", prev_context=prev, context_ref_info=YES_NO), patch=False),
                )

        _run(_both())
        assert len(_written(h.client, "light")) == OFFICE_N

    def test_replay_that_resolves_a_twelfth_entity_reasks(self):
        h = _Harness()
        _, prev = _first_turn(h)
        h.em.n_lights = OFFICE_N + 1
        h.stores.clear()
        out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO))
        assert _written(h.client) == []
        assert out.answer.endswith("?")
        assert "12" in out.answer
        assert h.stores and h.stores[-1]["ttl"] == 60
        new_pending = h.last_pending()
        assert len(new_pending["writes"][0][2]) == OFFICE_N + 1
        assert new_pending["nonce"] != prev["parameters"]["pending_write_confirmation"]["nonce"]

    def test_non_yes_no_follow_up_does_not_execute_pending_set(self):
        h = _Harness(llm_text='{"device_type": "light", "room": "office", "action": "turn_on", "target_scope": "group", "parameters": {}}')
        _, prev = _first_turn(_Harness())
        ref = {"anaphora_types": ["pronoun"], "has_context_ref": True, "is_continuation": True}
        out = h.run(_state54("turn them on", prev_context=prev, context_ref_info=ref))
        assert not any(c.args[1] == "turn_off" for c in h.client.call_service.await_args_list)
        assert h.extract_calls, "the follow-up must be re-extracted"
        cleared = [s for s in h.stores if s["ttl"] == 300 and "pending_write_confirmation" not in s["parameters"]]
        assert cleared, h.stores


class TestReplayPrecheck:
    """5.3 rule 5: the replay runs the same D14 domain precheck as the
    normal path, before execute_intent. The precheck's authorize_ha_write
    is patched in route_control only, so the guard can't mask a skipped
    precheck."""

    def _replay(self, allow_light):
        h = _Harness()
        _, prev = _first_turn(h)
        h.log.clear()
        h.client.call_service.reset_mock()

        def _decide(domain, service, data, perms):
            return MagicMock(allowed=(allow_light or domain != "light"))

        with mock.patch.object(rc_module, "authorize_ha_write", side_effect=_decide):
            out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO))
        return h, out

    def test_precheck_denial_stops_replay_before_execute(self):
        h, out = self._replay(allow_light=False)
        assert not any(e[0] == "execute_intent" for e in h.log)
        assert _written(h.client) == []
        assert out.error == "permission_denied"

    def test_precheck_allow_is_the_positive_control(self):
        h, out = self._replay(allow_light=True)
        assert any(e[0] == "execute_intent" for e in h.log)
        assert len(_written(h.client, "light")) == OFFICE_N


class TestUnboundedFallback:
    GOOD_NIGHT = (
        '{"device_type": "scene", "room": null, "action": "turn_on", "target_scope": "group", '
        '"parameters": {"entity_id": "scene.good_night"}}'
    )

    def test_good_night_missing_scene_is_reworded_never_pending(self):
        """Plan 5.4 lists this case twice ('-> the rewording, no store' and
        '-> prompt (unbounded)'); D7 rule 0 decides it: unbounded never
        prompts."""
        h = _Harness(llm_text=self.GOOD_NIGHT, fail_domains=("scene", "script"))
        out = h.run(_state54("good night"))
        assert _written(h.client, "light") == []
        assert not out.answer.endswith("?")
        assert "say:" in out.answer
        assert h.stores == []

        out2 = h.run(_state54("yes", prev_context=None, context_ref_info=YES_NO))
        assert _written(h.client, "light") == []


class TestPrePlanReaderSeesReadSentinel:
    def test_pending_context_through_continuation_reads_get_status(self):
        """Old code (no 5.3) handed the pending context: the continuation
        branch copies prev parameters and must see a read."""
        h = _Harness(llm_text='{"parameters": {}}')
        prev = _prev_with_pending(fingerprint=_fp(), expires_at=_time_mod.time() + 60)

        async def _no_resolve(*a, **kw):
            return False

        ref = {"anaphora_types": ["yes_no"], "has_context_ref": True, "is_continuation": True}
        with mock.patch.object(rc_module, "_resolve_pending_write_confirmation", _no_resolve):
            h.run(_state54("yes", prev_context=prev, context_ref_info=ref))
        actions = [e[1] for e in h.log if e[0] == "execute_intent"]
        assert actions == ["get_status"]
        assert _written(h.client) == []


class TestReplyNormalization:
    def test_bare_affirmation_and_negation_regexes(self):
        yes = ["yes", "Yes.", "Yes, please.", "Okay, do it.", "yeah go ahead", "go ahead", "do it", "sure thanks"]
        no = ["no", "No.", "No, thanks.", "nope", "nah thank you"]
        neither = ["yes, just the desk lamp", "no, only the ceiling light", "whatever", "yes turn on the kitchen"]
        for p in yes:
            assert write_fanout.BARE_AFFIRMATION_RE.match(write_fanout.normalize_reply(p)), p
            assert not write_fanout.BARE_NEGATION_RE.match(write_fanout.normalize_reply(p)), p
        for p in no:
            assert write_fanout.BARE_NEGATION_RE.match(write_fanout.normalize_reply(p)), p
            assert not write_fanout.BARE_AFFIRMATION_RE.match(write_fanout.normalize_reply(p)), p
        for p in neither:
            n = write_fanout.normalize_reply(p)
            assert not write_fanout.BARE_AFFIRMATION_RE.match(n), p
            assert not write_fanout.BARE_NEGATION_RE.match(n), p


# ---------------------------------------------------------------------------
# Reconcile: replies through the real classify path, nonce fail-closed,
# identity before expiry, foreign pendings, fingerprint without identity
# ---------------------------------------------------------------------------

ALREADY_DONE_TEXT = "Already done."


def _import_main_for_classify():
    """orchestrator.main with the same import preamble as
    test_context_continuation.py (config_loader stub, service key)."""
    import os
    os.environ.setdefault("SERVICE_API_KEY", "test-key-fanout-replies")
    os.environ.setdefault("ADMIN_API_URL", "http://localhost:8080")
    loader = mock.MagicMock()
    loader.get_config = config_module.get_config
    loader.ADMIN_API_URL = os.environ["ADMIN_API_URL"]
    loader.get_feature_flag = AsyncMock(return_value=False)
    loader.get_feature_flags = AsyncMock(return_value={})
    loader.clear_cache = AsyncMock()
    sys.modules.setdefault("orchestrator.config_loader", loader)
    import orchestrator.nodes  # noqa: F401
    import orchestrator.main as main_module
    return main_module


class TestRealClassifyPathReplies:
    """A reply to the prompt goes through the real classify_node (anaphora
    detector -> continuation decision -> context_ref_info) and then
    route_control_node, exactly as in production."""

    def _classify(self, monkeypatch, reply, prev):
        from types import SimpleNamespace
        from orchestrator.search_providers.intent_classifier import IntentClassifier
        from orchestrator.state import ConversationContext, IntentCategory

        main_module = _import_main_for_classify()
        cache = SimpleNamespace(get=AsyncMock(return_value={
            "intent": "general_info", "confidence": 0.5, "entities": {}, "complexity": "simple",
        }))
        _runtime.set_cache_client(cache)
        _runtime.set_llm_router(MagicMock())
        _runtime.set_intent_classifier(IntentClassifier())
        monkeypatch.setattr(main_module, "get_conversation_context", AsyncMock(return_value=ConversationContext(**prev)))
        state = OrchestratorState(query=reply)
        state.session_id = "sess-1"
        state.mode = "owner"
        state.node_timings = {}
        classified = _run(main_module.classify_node(state))
        assert classified.intent == IntentCategory.CONTROL, (reply, classified.intent)
        classified.permissions = {"mode": "owner"}
        classified.room = "office"
        classified.supports_followup = True
        classified.caller_fingerprint = _fp()
        return classified

    @pytest.mark.parametrize("reply", ["go ahead.", "do it.", "Yes, please."])
    def test_affirmations_replay_through_real_classify(self, monkeypatch, reply):
        h = _Harness()
        _, prev = _first_turn(h)
        classified = self._classify(monkeypatch, reply, prev)
        out = h.run(classified)
        assert sorted(set(_written(h.client, "light"))) == sorted(f"light.office_{i}" for i in range(OFFICE_N)), (reply, out.answer)

    def test_no_thanks_declines_through_real_classify(self, monkeypatch):
        h = _Harness()
        _, prev = _first_turn(h)
        classified = self._classify(monkeypatch, "No, thanks.", prev)
        out = h.run(classified)
        assert _written(h.client) == []
        assert out.answer == DECLINED_TEXT

    def test_detector_tags_are_unchanged(self):
        """The global yes_no tag is not widened (it feeds
        decide_context_continuation everywhere)."""
        from orchestrator.context.detector import detect_context_reference
        assert "yes_no" not in detect_context_reference("go ahead").get("anaphora_types", [])
        assert "yes_no" not in detect_context_reference("do it").get("anaphora_types", [])
        assert "yes_no" in detect_context_reference("yes please").get("anaphora_types", [])

    def test_bare_reply_vocabulary_is_the_detectors(self):
        from orchestrator.context import detector
        for word in detector.AFFIRMATION_WORDS:
            assert write_fanout.BARE_AFFIRMATION_RE.match(word), word
        for word in detector.NEGATION_WORDS:
            assert write_fanout.BARE_NEGATION_RE.match(word), word
        for phrase in detector.PROCEED_PHRASES:
            assert write_fanout.BARE_AFFIRMATION_RE.match(phrase), phrase


class _RaisingRedis:
    def __init__(self):
        self.client = self

    async def set(self, *a, **kw):
        raise ConnectionError("redis down")


class _SlowRedis:
    """Answers the claim -- successfully -- only after ~1 s. With the
    timeout in place the claim is abandoned first; without it the test
    doesn't hang, it replays and fails."""

    DELAY_SECONDS = 1.0

    def __init__(self):
        self.client = self

    async def set(self, *a, **kw):
        await asyncio.sleep(self.DELAY_SECONDS)
        return True


class TestNonceClaimFailsClosed:
    def _yes_after_pending(self, cache):
        h = _Harness(cache=cache)
        _, prev = _first_turn(h)
        h.client.call_service.reset_mock()
        out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO))
        return h, out

    def test_redis_error_loses_the_claim(self):
        h, out = self._yes_after_pending(_RaisingRedis())
        assert _written(h.client) == []
        assert out.answer == ALREADY_DONE_TEXT

    def test_redis_timeout_loses_the_claim(self, monkeypatch):
        monkeypatch.setattr(rc_module, "NONCE_CLAIM_TIMEOUT_SECONDS", 0.05)
        cache = _SlowRedis()
        h = _Harness(cache=cache)
        _, prev = _first_turn(h)
        h.client.call_service.reset_mock()
        t0 = _time_mod.perf_counter()
        out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO))
        elapsed = _time_mod.perf_counter() - t0
        assert elapsed < 0.5, elapsed
        assert _written(h.client) == []
        assert out.answer == ALREADY_DONE_TEXT

    def test_redis_error_does_not_fall_back_to_the_process_set(self, monkeypatch):
        rc_module._NONCE_FALLBACK.clear()
        monkeypatch.setattr(rc_module, "get_cache_client", lambda: _RaisingRedis())
        assert _run(rc_module._claim_nonce("n-raise")) is False
        assert "n-raise" not in rc_module._NONCE_FALLBACK

    def test_process_set_only_without_redis(self, monkeypatch):
        """Positive control: no Redis configured (no cache client, or one
        with no connection) -> the process-local set claims exactly once."""
        from types import SimpleNamespace
        for cache in (None, SimpleNamespace(client=None)):
            rc_module._NONCE_FALLBACK.clear()
            monkeypatch.setattr(rc_module, "get_cache_client", lambda c=cache: c)
            assert _run(rc_module._claim_nonce("n-local")) is True
            assert _run(rc_module._claim_nonce("n-local")) is False

    def test_working_redis_claims_once(self, monkeypatch):
        cache = _NxCache()
        monkeypatch.setattr(rc_module, "get_cache_client", lambda: cache)
        assert _run(rc_module._claim_nonce("n-redis")) is True
        assert _run(rc_module._claim_nonce("n-redis")) is False


class TestIdentityBeforeExpiry:
    NOW = 1_900_000_000.0

    def test_foreign_expired_pending_is_neither_cleared_nor_answered(self):
        h = _Harness()
        prev = _prev_with_pending(fingerprint=_fp(device="voice-a"), expires_at=self.NOW)
        out = h.run(
            _state54("yes", prev_context=prev, context_ref_info=YES_NO, fingerprint=_fp(device="voice-b")),
            now=self.NOW,
        )
        assert _written(h.client) == []
        assert out.answer not in (EXPIRED_TEXT, NEUTRAL_TEXT), out.answer
        assert h.stores == [], h.stores
        # Context-free: the reply went through normal extraction with no
        # previous turn to merge.
        assert h.extract_calls, "an expired foreign pending falls through to the normal flow"
        for _, kw in h.extract_calls:
            assert kw.get("prev_query") is None

    def test_own_expired_pending_still_answers_expired(self):
        """Positive control for the test above."""
        h = _Harness()
        prev = _prev_with_pending(fingerprint=_fp(), expires_at=self.NOW)
        out = h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO), now=self.NOW)
        assert out.answer == EXPIRED_TEXT
        assert h.stores and "pending_write_confirmation" not in h.stores[0]["parameters"]


class TestForeignPendingIsNotOverwritten:
    def test_cross_identity_state_question_success_skips_the_store(self):
        h = _Harness()
        prev = _prev_with_pending(fingerprint=_fp(device="voice-a"), expires_at=_time_mod.time() + 60)
        ref = {"anaphora_types": [], "has_context_ref": False}
        out = h.run(_state54(
            "are the office lights on", prev_context=prev, context_ref_info=ref, fingerprint=_fp(device="voice-b"),
        ))
        assert _written(h.client) == []
        assert out.answer and out.answer != NEUTRAL_TEXT
        assert h.stores == [], h.stores

    def test_cross_identity_block_is_reworded_not_stored(self):
        h = _Harness()
        prev = _prev_with_pending(fingerprint=_fp(device="voice-a"), expires_at=_time_mod.time() + 60)
        ref = {"anaphora_types": [], "has_context_ref": False}
        out = h.run(_state54(
            "office lights off please", prev_context=prev, context_ref_info=ref, fingerprint=_fp(device="voice-b"),
        ))
        assert _written(h.client) == []
        assert not out.answer.endswith("?"), out.answer
        assert "say:" in out.answer
        assert h.stores == [], h.stores

    def test_cross_identity_command_success_skips_the_store(self):
        """A different caller's successful (under-threshold) command runs,
        but doesn't store its context over the pending."""
        h = _Harness(n_lights=3)
        prev = _prev_with_pending(fingerprint=_fp(device="voice-a"), expires_at=_time_mod.time() + 60, n=3)
        ref = {"anaphora_types": [], "has_context_ref": False}
        h.run(_state54(
            "turn off the office lights", prev_context=prev, context_ref_info=ref, fingerprint=_fp(device="voice-b"),
        ))
        assert len(set(_written(h.client, "light"))) == 3
        assert h.stores == [], h.stores

    def test_same_caller_command_success_still_stores(self):
        """Positive control for the test above."""
        h = _Harness(n_lights=3)
        ref = {"anaphora_types": [], "has_context_ref": False}
        h.run(_state54("turn off the office lights", prev_context=None, context_ref_info=ref))
        assert len(set(_written(h.client, "light"))) == 3
        assert h.stores and h.stores[-1]["ttl"] == 300

    def test_same_caller_success_still_stores(self):
        """Positive control: with no foreign pending the same turn stores
        its success context as before."""
        h = _Harness()
        ref = {"anaphora_types": [], "has_context_ref": False}
        h.run(_state54("are the office lights on", prev_context=None, context_ref_info=ref))
        assert h.stores and h.stores[-1]["ttl"] == 300


class TestFingerprintNeedsIdentity:
    def test_no_trust_and_no_device_is_none(self):
        assert write_fanout.caller_fingerprint(None, None, "office", "owner") is None
        assert write_fanout.caller_fingerprint("", "", "office", "owner") is None

    def test_either_identity_input_is_enough(self):
        assert write_fanout.caller_fingerprint("household", None, "office", "owner")
        assert write_fanout.caller_fingerprint(None, "ha-device-1", "office", "owner")


# ---------------------------------------------------------------------------
# Reconcile: a question's "all" is not a scope cue; the sequence executor
# keeps no per-session state
# ---------------------------------------------------------------------------

class TestQuestionScopeCueIsNotAnExemption:
    def test_all_in_a_question_is_counted(self, monkeypatch):
        """Only reachable with routing bypassed (kill switch): the gate
        sees the real STATE_QUESTION and must not read its "all" as an
        explicit write scope."""
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        q = "are all the office lights on"
        assert classify_utterance(q).kind == UtteranceKind.STATE_QUESTION
        with _scope(q) as scope:
            r = write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(12)), q)
            assert r is not None
            assert scope.fanout_block is not None

    def test_all_in_a_command_still_exempts(self, monkeypatch):
        """Positive control."""
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=18))
        q = "turn off all the office lights"
        with _scope(q):
            assert write_fanout.gate("light", "turn_off", tuple(f"light.{i}" for i in range(30)), q) is None


class TestSequenceExecutorKeepsNoPerSessionState:
    def test_no_per_session_results_accumulate(self, monkeypatch):
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=10))
        em = _FakeEntityManagerSeq(n=12)
        controller = shc.SmartHomeController(entity_manager=em, llm_router=MagicMock())
        executor = SequenceExecutor(controller, _raw_ha_client_seq())
        steps = [{"action": "turn_off", "target": {"device_type": "light", "room": "office"}}]

        async def _drive(session_id):
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("turn off the office lights")):
                return await executor.execute_sequence(steps, session_id=session_id, background=False)

        for i in range(3):
            _run(_drive(f"seq-{i}"))
        assert set(vars(executor)) == {"smart_controller", "ha_client", "_running_sequences"}
        assert executor._running_sequences == {}


# ---------------------------------------------------------------------------
# 3.6(f): the kill switch at runtime (routing reverts; the gate still reads
# the real classification)
# ---------------------------------------------------------------------------

PROBE_PHRASE = "are the office lights currently on or off right now"
KILL_SWITCH_ON = {"state_question_routing_kill_switch": True}
PROMPT_12 = "That would turn off 12 lights in the office. Should I go ahead?"


class TestKillSwitchRuntime:
    def _drive(self, query, *, threshold=6, hard_limit=18, n_lights=12, supports_followup=True):
        h = _Harness(n_lights=n_lights, flags=KILL_SWITCH_ON, threshold=threshold, hard_limit=hard_limit)
        seen = []
        wrapped = h.controller.execute_intent

        async def _spy(intent, *a, **kw):
            scope = mp.current_ha_scope()
            seen.append((intent.get("action"), scope.read_only, scope.utterance.kind))
            return await wrapped(intent, *a, **kw)

        h.controller.execute_intent = _spy
        metric = MagicMock()
        with mock.patch.object(write_fanout, "ha_write_fanout_confirm_total", metric):
            out = h.run(_state54(query, supports_followup=supports_followup))
        outcomes = [c.kwargs.get("outcome") for c in metric.labels.call_args_list]
        return h, out, seen, outcomes

    def test_probe_reaches_extraction_writable_and_the_gate_prompts(self):
        h, out, seen, outcomes = self._drive(PROBE_PHRASE)
        assert h.extract_calls, "the probe must reach extract_intent with routing reverted"
        assert seen and all(read_only is False for _, read_only, _ in seen), seen
        assert all(kind == UtteranceKind.STATE_QUESTION for _, _, kind in seen), seen
        assert _written(h.client) == []
        assert out.answer == PROMPT_12
        assert h.stores and "pending_write_confirmation" in h.stores[-1]["parameters"]
        assert outcomes == ["requested"]

    def test_both_limits_zero_still_confirm_a_real_question(self):
        """CR22 (replaces "both limits 0 -> 12 writes"): with routing
        reverted and the limits off, the probe still reaches a writable
        extraction -- the switch really bypasses routing -- but a real
        question never writes silently."""
        h, out, seen, outcomes = self._drive(PROBE_PHRASE, threshold=0, hard_limit=0)
        assert seen and all(read_only is False for _, read_only, _ in seen), seen
        assert _written(h.client) == []
        assert out.answer == PROMPT_12

    def test_question_under_the_threshold_is_confirmed_not_written(self):
        h, out, seen, outcomes = self._drive(PROBE_PHRASE, n_lights=3)
        assert _written(h.client) == []
        assert out.answer == "That would turn off 3 lights in the office. Should I go ahead?"
        assert outcomes == ["requested"]

    def test_single_entity_question_is_confirmed(self):
        h, out, seen, outcomes = self._drive(PROBE_PHRASE, n_lights=1)
        assert _written(h.client) == []
        assert out.answer == "That would turn off 1 light in the office. Should I go ahead?"

    def test_question_on_a_surface_without_follow_up_gets_the_rewording(self):
        h, out, seen, outcomes = self._drive(PROBE_PHRASE, n_lights=3, supports_followup=False)
        assert _written(h.client) == []
        assert out.answer.startswith("That would turn off 3 lights in the office. To do it, say: ")
        assert outcomes == ["reworded"]

    def test_misread_command_still_works_after_yes(self):
        """A command the classifier misread as a question is one "yes" away
        (routing reverted, t = h = 0)."""
        h = _Harness(n_lights=3, flags=KILL_SWITCH_ON, threshold=0, hard_limit=0)
        first = h.run(_state54(PROBE_PHRASE))
        assert first.answer.endswith("Should I go ahead?")
        stored = h.stores[-1]
        prev = {k: stored[k] for k in ("intent", "query", "entities", "parameters", "response")}
        h.run(_state54("yes", prev_context=prev, context_ref_info=YES_NO))
        assert len(set(_written(h.client, "light"))) == 3

    def test_explicit_all_command_is_exempt_scope(self):
        h, out, seen, outcomes = self._drive("turn off all the office lights")
        assert len(set(_written(h.client, "light"))) == 12
        assert "exempt_scope" in outcomes, outcomes

    def test_imperative_command_is_exempt_imperative(self):
        h, out, seen, outcomes = self._drive("turn off the office lights")
        assert len(set(_written(h.client, "light"))) == 12
        assert outcomes == ["exempt_imperative"], outcomes


# ---------------------------------------------------------------------------
# Handler-level gates: each handler feeds the gate its real target set
# ---------------------------------------------------------------------------

B4_N = 8


class _B4EntityManager:
    def __init__(self, n=B4_N):
        self._entities = {}
        for i in range(n):
            self._entities[f"fan.office_{i}"] = {"state": "on", "attributes": {"friendly_name": f"Office Fan {i}"}}
            self._entities[f"cover.office_{i}"] = {"state": "open", "attributes": {"friendly_name": f"Office Blind {i}"}}
            self._entities[f"media_player.office_tv_{i}"] = {"state": "on", "attributes": {"friendly_name": f"Office TV {i}"}}

    async def get_entities(self):
        return dict(self._entities)

    async def find_lights_by_room(self, room):
        return [{
            "entity_id": f"light.{room}_group", "friendly_name": f"{room} lights", "state": "on", "type": "group",
            "members": [f"light.{room}_{i}" for i in range(B4_N // 2)],
        }]

    async def get_all_light_groups(self):
        return [
            {"entity_id": f"light.{r}_group", "friendly_name": f"{r} lights",
             "members": [f"light.{r}_{i}" for i in range(B4_N // 2)]}
            for r in ("office", "kitchen")
        ]


_B4_INTENT = {"device_type": "light"}
_B4_ROOM_GROUP = {"display_name": "First Floor", "members": [{"room_name": "office"}, {"room_name": "kitchen"}]}
_B4_HANDLERS = {
    "fan": lambda c, raw, q: c._handle_fan_intent("turn_off", "office", raw, q),
    "cover": lambda c, raw, q: c._handle_cover_intent("close", "office", raw, q),
    "media": lambda c, raw, q: c._handle_media_intent("turn_off", {}, q, None, raw),
    "whole_house": lambda c, raw, q: c._execute_whole_house_command("turn_off", "group", {}, _B4_INTENT, raw, q),
    "multi_room": lambda c, raw, q: c._execute_multi_room_command(
        ["office", "kitchen"], "turn_off", "group", {}, _B4_INTENT, raw, q),
    "room_group": lambda c, raw, q: c._execute_room_group_command(
        _B4_ROOM_GROUP, "turn_off", "group", {}, _B4_INTENT, raw, q),
}
_B4_QUERY = "please"  # UNKNOWN, no scope cue, no room names


def _run_b4_handler(name, *, threshold):
    controller = shc.SmartHomeController(entity_manager=_B4EntityManager(), llm_router=MagicMock())
    raw = _raw_client_54()

    async def _drive():
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(_B4_QUERY)):
            with mock.patch.object(write_fanout, "get_config", lambda: _fake_config(threshold=threshold, hard_limit=18)):
                answer = await _B4_HANDLERS[name](controller, raw, _B4_QUERY)
            return answer, write_fanout.take_block()

    answer, block = _run(_drive())
    return answer, block, raw


class TestHandlerLevelGates:
    def test_query_is_an_uncued_non_imperative(self):
        assert classify_utterance(_B4_QUERY).kind == UtteranceKind.UNKNOWN

    @pytest.mark.parametrize("name", sorted(_B4_HANDLERS))
    def test_over_threshold_targets_block_with_the_real_target_set(self, name):
        answer, block, raw = _run_b4_handler(name, threshold=6)
        assert raw.call_service.await_count == 0, (name, raw.call_service.await_args_list)
        assert block is not None, name
        assert sum(len(w.entity_ids) for w in block.writes) == B4_N, (name, block)
        assert "say:" in answer, (name, answer)

    @pytest.mark.parametrize("name", sorted(_B4_HANDLERS))
    def test_positive_control_threshold_off_writes_every_target(self, name):
        answer, block, raw = _run_b4_handler(name, threshold=0)
        assert block is None, name
        assert len(set(_written(raw))) == B4_N, (name, _written(raw), answer)


# ---------------------------------------------------------------------------
# Sequence step between the threshold and the hard limit
# ---------------------------------------------------------------------------

class TestSequenceImperativeStepUnderHardLimit:
    def test_imperative_step_between_limits_executes(self, monkeypatch):
        """t < n <= h: an IMPERATIVE's step proceeds -- which needs the gate
        to read the request's classification from the scope, since a
        sequence step's execute_intent call carries no utterance."""
        monkeypatch.setattr(write_fanout, "get_config", lambda: _fake_config(threshold=6, hard_limit=10))
        em = _FakeEntityManagerSeq(n=8)
        controller = shc.SmartHomeController(entity_manager=em, llm_router=MagicMock())
        client = _raw_ha_client_seq()
        executor = SequenceExecutor(controller, client)
        steps = [{"action": "turn_off", "target": {"device_type": "light", "room": "office"}}]

        async def _drive():
            with mp.ha_permission_scope(
                {"mode": "owner"}, mode="owner", utterance=classify_utterance("turn off the office lights"),
            ):
                return await executor.execute_sequence(steps, session_id="seq-8", background=False)

        result = _run(_drive())
        assert result == "Sequence complete."
        assert client.call_service.await_count == 8


class TestReplyNormalizationIsLinear:
    """A long run of "!"/"?"/"." that doesn't end the input made the old
    end-anchored `[.!?]+$` patterns quadratic (40 K chars: ~6 s)."""

    CRAFTED = "yes" + "!" * 100_000 + "x"
    BUDGET_SECONDS = 0.5

    def test_normalize_reply_is_linear(self):
        t0 = _time_mod.perf_counter()
        write_fanout.normalize_reply(self.CRAFTED)
        assert _time_mod.perf_counter() - t0 < self.BUDGET_SECONDS

    def test_anaphora_types_are_linear(self):
        from orchestrator.context.detector import _compute_anaphora_types
        t0 = _time_mod.perf_counter()
        _compute_anaphora_types(self.CRAFTED)
        assert _time_mod.perf_counter() - t0 < self.BUDGET_SECONDS

    def test_trailing_punctuation_still_stripped(self):
        from orchestrator.context.detector import _compute_anaphora_types
        assert write_fanout.normalize_reply("Yes, please!!?") == "yes please"
        assert "yes_no" in _compute_anaphora_types("yes please?!.")


# ---------------------------------------------------------------------------
# Round 3 / Q1: an unidentified-device question must not leave an earlier
# write context for the reply to continue
# ---------------------------------------------------------------------------

UNIDENTIFIED_TEXT = "I couldn't tell which device you're asking about. Which one do you mean?"
NO_DEVICE_JSON = '{"room": null, "action": "turn_off", "target_scope": "group", "parameters": {}}'
KITCHEN_LIGHTS_OFF_JSON = (
    '{"device_type": "light", "room": "kitchen", "action": "turn_off", "target_scope": "group", "parameters": {}}'
)
ROOM_ONLY = {"anaphora_types": ["room_only"], "has_context_ref": True, "ref_types": ["room_only"], "is_continuation": True}


class _TwoRoomEntityManager:
    """3 office + 3 kitchen lights (under the fan-out threshold, so the gate
    can't be what stops a write) plus a thermostat."""

    ROOMS = ("office", "kitchen")

    async def get_entities(self):
        ents = {}
        for room in self.ROOMS:
            for i in range(3):
                ents[f"light.{room}_{i}"] = {"state": "on", "attributes": {"friendly_name": f"{room.title()} Light {i}"}}
        return ents

    async def find_lights_by_room(self, room):
        for known in self.ROOMS:
            if room and known in room.lower():
                return [{"entity_id": f"light.{known}_{i}", "friendly_name": f"{known.title()} Light {i}",
                         "members": [], "state": "on", "type": "individual"} for i in range(3)]
        return []

    async def get_all_light_groups(self):
        return []

    async def get_climate_state(self):
        return {"entity_id": "climate.thermostat", "state": "heat", "current_temp": 68, "target_temp": 70}


def _prev_from_store(stored):
    return {k: stored[k] for k in ("intent", "query", "entities", "parameters", "response")}


def _clarification_scenario(reply, reply_ref, reply_llm_text):
    """T1 a stored write ("turn off the office lights"), T2 a question
    neither the classifier nor the LLM can attach to a device, T3 the
    reply. Returns (harness, T2 state, T3 state, writes during T3)."""
    h = _Harness(llm_text=NO_DEVICE_JSON)
    h.em = _TwoRoomEntityManager()
    h.controller.entity_manager = h.em

    t1 = h.run(_state54("turn off the office lights", context_ref_info={}))
    assert len(set(_written(h.client, "light"))) == 3, t1.answer
    prev = _prev_from_store(h.stores[-1])

    t2 = h.run(_state54("is the heater on", prev_context=prev, context_ref_info={}))
    assert t2.answer == UNIDENTIFIED_TEXT, t2.answer
    if h.stores and h.stores[-1]["query"] == "is the heater on":
        prev = _prev_from_store(h.stores[-1])

    h.client.call_service.reset_mock()
    h.llm.response_text = reply_llm_text
    t3 = h.run(_state54(reply, prev_context=prev, context_ref_info=reply_ref))
    return h, t2, t3, _written(h.client)


class TestUnidentifiedDeviceReplyNeverWrites:
    def test_room_reply_does_not_continue_the_earlier_write(self):
        h, t2, t3, writes = _clarification_scenario("the kitchen", ROOM_ONLY, KITCHEN_LIGHTS_OFF_JSON)
        assert writes == [], (writes, t3.answer)
        assert t3.answer

    def test_yes_reply_does_not_continue_the_earlier_write(self):
        h, t2, t3, writes = _clarification_scenario("yes", YES_NO, KITCHEN_LIGHTS_OFF_JSON)
        assert writes == [], (writes, t3.answer)
        # "yes" names nothing; the extractor sees the question it answers.
        assert h.extract_calls[-1][0] == "is the heater on (yes)", h.extract_calls[-1]

    def test_unidentified_question_stores_a_read_only_clarification(self):
        h, t2, t3, writes = _clarification_scenario("the kitchen", ROOM_ONLY, KITCHEN_LIGHTS_OFF_JSON)
        stored = next(s for s in h.stores if s["query"] == "is the heater on")
        assert stored["ttl"] == 60
        assert stored["parameters"] == {"action": "get_status", "room": None, "awaiting_state_question": True}
        assert stored["response"] == UNIDENTIFIED_TEXT

    def test_room_reply_is_answered_as_the_question(self):
        h, t2, t3, writes = _clarification_scenario("the kitchen", ROOM_ONLY, KITCHEN_LIGHTS_OFF_JSON)
        assert writes == []
        assert t3.error is None
        assert "kitchen" in t3.answer.lower() and "light" in t3.answer.lower(), t3.answer

    def test_device_reply_is_answered(self):
        h, t2, t3, writes = _clarification_scenario("the thermostat", ROOM_ONLY, NO_DEVICE_JSON)
        assert writes == []
        assert "thermostat" in t3.answer.lower(), t3.answer

    def test_explicit_command_reply_executes(self):
        h, t2, t3, writes = _clarification_scenario(
            "turn on the kitchen lights", {}, KITCHEN_LIGHTS_OFF_JSON.replace("turn_off", "turn_on"),
        )
        assert sorted(set(writes)) == [f"light.kitchen_{i}" for i in range(3)], (writes, t3.answer)
        assert {c.args[1] for c in h.client.call_service.await_args_list} == {"turn_on"}

    def test_unidentified_again_asks_again(self):
        h, t2, t3, writes = _clarification_scenario("yes", YES_NO, NO_DEVICE_JSON)
        assert writes == []
        assert t3.answer == UNIDENTIFIED_TEXT

    def test_no_clarification_stored_over_a_foreign_pending(self):
        h = _Harness(llm_text=NO_DEVICE_JSON)
        prev = _prev_with_pending(fingerprint=_fp(device="voice-a"), expires_at=_time_mod.time() + 60)
        out = h.run(_state54(
            "is the heater on", prev_context=prev, context_ref_info={}, fingerprint=_fp(device="voice-b"),
        ))
        assert out.answer == UNIDENTIFIED_TEXT
        assert h.stores == [], h.stores


class TestNormalExtractionUnidentifiedDevice:
    """tessa N11: a referent question with no context goes through normal
    extraction; an LLM intent without a device_type gets the clarification
    (and its store), not a guessed device."""

    def test_is_it_on_without_context(self):
        h = _Harness(llm_text=NO_DEVICE_JSON)
        out = h.run(_state54("is it on", prev_context=None, context_ref_info={}))
        assert h.extract_calls, "normal extraction must run"
        assert out.answer == UNIDENTIFIED_TEXT
        assert _written(h.client) == []
        assert h.stores and h.stores[-1]["parameters"]["awaiting_state_question"] is True
        assert h.stores[-1]["ttl"] == 60


# ---------------------------------------------------------------------------
# Round 3 / K1: with the kill switch on, a real question never writes
# silently -- the single-target handlers included
# ---------------------------------------------------------------------------

CLIMATE_SET_JSON = (
    '{"device_type": "climate", "room": null, "action": "set_temperature", "target_scope": "group", '
    '"parameters": {"temperature": 72}}'
)
GOOD_NIGHT_SCENE_JSON = (
    '{"device_type": "scene", "room": null, "action": "turn_on", "target_scope": "group", '
    '"parameters": {"entity_id": "scene.good_night"}}'
)


def _kill_switch_harness(llm_text):
    h = _Harness(llm_text=llm_text, flags=KILL_SWITCH_ON)
    h.em = _TwoRoomEntityManager()
    h.controller.entity_manager = h.em
    return h


class TestKillSwitchSingleTargetHandlers:
    def test_furnace_question_never_sets_the_temperature(self):
        h = _kill_switch_harness(CLIMATE_SET_JSON)
        out = h.run(_state54("is the furnace on", room=None))
        assert h.client.call_service.await_count == 0, h.client.call_service.await_args_list
        assert out.answer == "That would change 1 thermostat. Should I go ahead?", out.answer

    def test_furnace_question_without_follow_up_gets_the_rewording(self):
        h = _kill_switch_harness(CLIMATE_SET_JSON)
        out = h.run(_state54("is the furnace on", room=None, supports_followup=False))
        assert h.client.call_service.await_count == 0
        assert out.answer == "That would change 1 thermostat. To do it, say: change the thermostat.", out.answer

    def test_thermostat_command_still_executes(self):
        h = _kill_switch_harness(CLIMATE_SET_JSON)
        h.run(_state54("set the thermostat to 72", room=None))
        assert [c.args[:2] for c in h.client.call_service.await_args_list] == [("climate", "set_temperature")]

    def test_good_night_question_never_activates_the_scene(self):
        h = _kill_switch_harness(GOOD_NIGHT_SCENE_JSON)
        out = h.run(_state54("is good night mode on?", room=None))
        assert h.client.call_service.await_count == 0, h.client.call_service.await_args_list
        # "good night" resolves to a scene or a script ("routine").
        assert out.answer in (
            "That would activate 1 scene. Should I go ahead?",
            "That would activate 1 routine. Should I go ahead?",
        ), out.answer

    def test_good_night_command_still_activates_the_scene(self):
        h = _kill_switch_harness(GOOD_NIGHT_SCENE_JSON)
        h.run(_state54("activate good night mode", room=None))
        calls = [c.args[:2] for c in h.client.call_service.await_args_list]
        assert calls in ([("scene", "turn_on")], [("script", "turn_on")]), calls

    def test_questions_classify_as_questions(self):
        for q in ("is the furnace on", "is good night mode on?"):
            assert classify_utterance(q).kind == UtteranceKind.STATE_QUESTION, q


def _bed_warmer_config():
    import json as _json
    cfg = MagicMock()
    cfg.ha_bed_warmer_entities = _json.dumps({
        "level_left": "select.bed_left", "level_right": "select.bed_right", "power_main": "switch.bed_main",
        "power_side_a": "switch.bed_a", "power_side_b": "switch.bed_b",
    })
    return cfg


class TestSingleTargetGatesDirect:
    """Each formerly allowlisted handler, called directly under a scope: a
    real question is blocked at one target; the same call from a command
    proceeds (positive control)."""

    def _call(self, name, query):
        controller = shc.SmartHomeController(entity_manager=_TwoRoomEntityManager(), llm_router=MagicMock())
        raw = _raw_client_54()
        calls = {
            "climate": lambda: controller._handle_climate_intent("set_temperature", {"temperature": 72}, query, raw),
            "bed_warmer": lambda: controller._handle_bed_warmer_intent("turn_off", {}, raw, query),
            "motion": lambda: controller._handle_motion_control_intent("leave_lights_on", {}, raw, "office", query),
            "scene": lambda: controller._handle_scene_intent("turn_on", {"entity_id": "scene.good_night"}, raw, query),
        }

        async def _drive():
            with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(query)):
                with mock.patch.object(write_fanout, "get_config", lambda: _fake_config(6, 18)), \
                        mock.patch.object(shc, "get_config", _bed_warmer_config):
                    answer = await calls[name]()
                return answer, write_fanout.take_block()

        answer, block = _run(_drive())
        return answer, block, raw

    @pytest.mark.parametrize("name", ["climate", "bed_warmer", "motion", "scene"])
    def test_question_is_blocked_at_one_target(self, name):
        answer, block, raw = self._call(name, "is it on in the office?")
        assert raw.call_service.await_count == 0, (name, raw.call_service.await_args_list)
        assert block is not None and sum(len(w.entity_ids) for w in block.writes) == 1, (name, block)

    @pytest.mark.parametrize("name", ["climate", "bed_warmer", "motion", "scene"])
    def test_command_proceeds(self, name):
        answer, block, raw = self._call(name, "please do it now")
        assert block is None, name
        assert raw.call_service.await_count >= 1, (name, answer)


# ---------------------------------------------------------------------------
# Round 4: a timed question can't write through the sequence executor's
# direct-entity path
# ---------------------------------------------------------------------------

DIRECT_ENTITY_SEQUENCE_JSON = (
    '{"type": "sequence", "acknowledge": "Okay.", "steps": [{"action": "turn_off", '
    '"target": {"entity_id": "light.office_0"}, "parameters": {}, "delay_after": 0, "at_time": null}]}'
)
TIMED_QUESTION = "did the office light turn off after 5 seconds"
TIMED_COMMAND = "turn off the office light after 5 seconds"


class _SequenceHarness(_Harness):
    """The _Harness runtime plus a real SequenceExecutor over the same raw
    HA client."""

    def _install(self):
        super()._install()
        self.executor = SequenceExecutor(self.controller, self.client)
        _runtime.set_sequence_executor(self.executor)

    async def arun_sequence(self, state):
        out = await self.arun(state)
        tasks = list(self.executor._running_sequences.values())
        results = [await t for t in tasks]
        return out, results


def _run_timed(query, *, kill_switch):
    h = _SequenceHarness(llm_text=DIRECT_ENTITY_SEQUENCE_JSON, n_lights=3,
                         flags=KILL_SWITCH_ON if kill_switch else None)
    out, results = _run(h.arun_sequence(_state54(query, context_ref_info={})))
    return h, out, results


class TestSequenceDirectEntityWrites:
    def test_population_timed_question_is_a_state_question_and_a_sequence(self):
        assert classify_utterance(TIMED_QUESTION).kind == UtteranceKind.STATE_QUESTION
        assert shc.SmartHomeController(entity_manager=MagicMock(), llm_router=MagicMock()).detect_sequence_intent(TIMED_QUESTION)

    def test_timed_question_under_the_kill_switch_never_writes(self):
        """codex r4: routing reverted, the question is extracted as a
        sequence whose step names an entity directly."""
        h, out, results = _run_timed(TIMED_QUESTION, kill_switch=True)
        assert h.client.call_service.await_count == 0, h.client.call_service.await_args_list
        assert results and results[0][0]["status"] == "refused_fanout", results
        assert results[0][0]["message"].startswith("That would turn off 1 light"), results
        assert not results[0][0]["message"].endswith("?")
        assert write_fanout.take_block() is None

    def test_timed_question_without_the_kill_switch_takes_the_read_path(self):
        h, out, results = _run_timed(TIMED_QUESTION, kill_switch=False)
        assert h.client.call_service.await_count == 0
        assert results == [], "a question is not scheduled as a sequence"
        assert "sequence" not in out.answer.lower() and out.answer != "Okay.", out.answer

    def test_timed_command_executes_with_the_kill_switch(self):
        h, out, results = _run_timed(TIMED_COMMAND, kill_switch=True)
        assert [c.args[:2] for c in h.client.call_service.await_args_list] == [("light", "turn_off")]
        assert results == [[]]

    def test_timed_command_executes_without_the_kill_switch(self):
        h, out, results = _run_timed(TIMED_COMMAND, kill_switch=False)
        assert [c.args[:2] for c in h.client.call_service.await_args_list] == [("light", "turn_off")]
        assert results == [[]]

    def test_referent_timed_question_is_not_scheduled(self):
        """A referent question skips the question dispatch; it must still
        not be scheduled as a sequence (answer: the sequence acknowledgment)."""
        q = "did they turn off after 5 seconds"
        uk = classify_utterance(q)
        assert uk.kind == UtteranceKind.STATE_QUESTION and uk.needs_referent
        h, out, results = _run_timed(q, kill_switch=False)
        assert h.client.call_service.await_count == 0
        assert results == [], results
        assert out.answer != "Okay.", out.answer


# ---------------------------------------------------------------------------
# Round 5: the dynamic automation agent never writes for a real question
# ---------------------------------------------------------------------------

from orchestrator.automation_agent import AutomationAgent

AGENT_QUESTION = "did the office light turn off at 6pm"
AGENT_REFERENT_QUESTION = "did they turn off at 6pm"
AGENT_COMMAND = "turn off the office light at 6pm"


def _tool_call(name, arguments, call_id="c1"):
    return {"tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}]}


class _AgentLLM:
    """chat_with_tools returns the scripted tool calls in order, then done."""

    def __init__(self, *responses):
        self.responses = list(responses) + [_tool_call("done", {"message": "All set."}, "c-done")]
        self.chat_with_tools = AsyncMock(side_effect=self._next)

    async def _next(self, **kwargs):
        return self.responses.pop(0)


class _AgentHarness(_Harness):
    """The _Harness runtime routed into the dynamic automation agent
    (automation_mode == "dynamic_agent"), with a real AutomationAgent over
    the same raw HA client and a recording admin client."""

    def __init__(self, *agent_responses, kill_switch):
        super().__init__(n_lights=3, flags=KILL_SWITCH_ON if kill_switch else None)
        self.admin = MagicMock()
        for name in ("create_voice_automation", "archive_voice_automation", "delete_voice_automation"):
            setattr(self.admin, name, AsyncMock(return_value=True))
        self.admin.get_voice_automations = AsyncMock(return_value=[{"id": "voice_1"}])
        self.client.create_automation = AsyncMock(return_value=True)
        self.agent = AutomationAgent(self.client, _AgentLLM(*agent_responses), admin_client=self.admin)

    def _install(self):
        super()._install()
        _runtime.set_automation_agent(self.agent)

    def run_agent(self, query, **state_kw):
        async def _go():
            with self.patched(), \
                    mock.patch("orchestrator.nodes.route_control.get_automation_system_mode",
                               new_callable=AsyncMock, return_value="dynamic_agent"), \
                    mock.patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=True), \
                    mock.patch("orchestrator.automation_agent.build_automation_system_prompt",
                               new_callable=AsyncMock, return_value="SYSTEM"):
                return await self.arun(_state54(query, context_ref_info={}, **state_kw), patch=False)
        return _run(_go())

    def admin_writes(self):
        return [n for n in ("create_voice_automation", "archive_voice_automation", "delete_voice_automation")
                if getattr(self.admin, n).await_count]


HA_OFF = ("ha_service", {"entity_id": "light.office_0", "service": "turn_off"})


class TestAutomationAgentQuestionGuard:
    def test_population_question_and_command(self):
        assert classify_utterance(AGENT_QUESTION).kind == UtteranceKind.STATE_QUESTION
        assert classify_utterance(AGENT_COMMAND).kind == UtteranceKind.IMPERATIVE

    def test_question_under_the_kill_switch_never_calls_a_service(self):
        """codex r5: kill switch on, dynamic_agent, the agent emits the write."""
        h = _AgentHarness(_tool_call(*HA_OFF), kill_switch=True)
        out = h.run_agent(AGENT_QUESTION)
        assert h.client.call_service.await_count == 0, h.client.call_service.await_args_list
        # The refusal went back to the agent as the tool result.
        tool_results = [m["content"] for m in h.agent.llm.chat_with_tools.await_args_list[-1].kwargs["messages"] if m["role"] == "tool"]
        assert tool_results and tool_results[0].startswith("That would turn off 1 light"), tool_results
        assert not tool_results[0].endswith("?")

    def test_question_under_the_kill_switch_never_creates_an_automation(self):
        create = ("create_automation", {"name": "Office off", "trigger": {"type": "time", "time": "18:00"},
                                        "actions": [{"type": "service", "entity_id": "light.office_0", "service": "turn_off"}]})
        h = _AgentHarness(_tool_call(*create), kill_switch=True)
        h.run_agent(AGENT_QUESTION)
        assert h.client.create_automation.await_count == 0
        assert h.admin_writes() == []

    def test_question_under_the_kill_switch_never_deletes_an_automation(self):
        h = _AgentHarness(_tool_call("delete_automation", {"automation_id": "voice_1"}), kill_switch=True)
        h.run_agent(AGENT_QUESTION)
        assert h.admin_writes() == []

    def test_question_under_the_kill_switch_never_notifies(self):
        h = _AgentHarness(_tool_call("notify", {"message": "hi", "target": "all", "room": "office"}), kill_switch=True)
        h.run_agent(AGENT_QUESTION)
        assert h.client.call_service.await_count == 0, h.client.call_service.await_args_list

    def test_command_writes_with_the_kill_switch(self):
        h = _AgentHarness(_tool_call(*HA_OFF), kill_switch=True)
        h.run_agent(AGENT_COMMAND)
        assert [c.args[:2] for c in h.client.call_service.await_args_list] == [("light", "turn_off")]

    def test_command_writes_without_the_kill_switch(self):
        h = _AgentHarness(_tool_call(*HA_OFF), kill_switch=False)
        h.run_agent(AGENT_COMMAND)
        assert [c.args[:2] for c in h.client.call_service.await_args_list] == [("light", "turn_off")]

    def test_command_creates_an_automation(self):
        create = ("create_automation", {"name": "Office off", "trigger": {"type": "time", "time": "18:00"},
                                        "actions": [{"type": "service", "entity_id": "light.office_0", "service": "turn_off"}]})
        h = _AgentHarness(_tool_call(*create), kill_switch=True)
        h.run_agent(AGENT_COMMAND)
        assert h.client.create_automation.await_count == 1
        assert h.admin_writes() == ["create_voice_automation"]

    def test_referent_question_without_the_kill_switch_keeps_the_read_only_refusal(self):
        uk = classify_utterance(AGENT_REFERENT_QUESTION)
        assert uk.kind == UtteranceKind.STATE_QUESTION and uk.needs_referent
        h = _AgentHarness(_tool_call(*HA_OFF), kill_switch=False)
        out = h.run_agent(AGENT_REFERENT_QUESTION)
        assert h.client.call_service.await_count == 0
        assert out.answer == mp.READ_ONLY_REFUSAL, out.answer


class TestQuestionRefusal:
    def _refusal(self, text, **scope_kw):
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance(text), **scope_kw):
            return write_fanout.question_refusal("light", "turn_off", ("light.office_0",))

    def test_real_question_gets_the_rewording(self):
        r = self._refusal("is the office light on")
        assert r and r.startswith("That would turn off 1 light") and not r.endswith("?")

    def test_commands_pass(self):
        assert self._refusal("turn off the office light") is None
        assert self._refusal("office light please") is None

    def test_read_only_scope_is_left_to_the_guard(self):
        assert self._refusal("is the office light on", read_only=True) is None

    def test_no_scope_or_classification_passes(self):
        assert write_fanout.question_refusal("light", "turn_off", ("light.office_0",)) is None
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner"):
            assert write_fanout.question_refusal("light", "turn_off", ("light.office_0",)) is None

    def test_never_leaves_a_block_behind(self):
        with mp.ha_permission_scope({"mode": "owner"}, mode="owner", utterance=classify_utterance("is it on")):
            write_fanout.question_refusal("light", "turn_off", ("light.office_0",))
            assert write_fanout.take_block() is None


# ---------------------------------------------------------------------------
# Round 5: closed-world guard over AutomationAgent's write tools
# ---------------------------------------------------------------------------

AGENT_SRC_PATH = Path("src/orchestrator/automation_agent.py")
# State-mutating calls the agent can make: HA services and automations, and
# the admin backend's voice-automation records.
AGENT_WRITE_ATTRS = frozenset({
    "call_service", "create_automation",
    "create_voice_automation", "archive_voice_automation", "delete_voice_automation",
})
# Tool entry methods that write (directly or through helpers); each binds
# and checks write_fanout.question_refusal before any write. None is
# allowlisted.
AGENT_GUARDED = {"_exec_ha_service", "_create_automation", "_delete_automation", "_send_notification"}
AGENT_ALLOWLIST: dict = {}


def _agent_methods(tree, class_name="AutomationAgent"):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {m.name: m for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return {}


def _write_calls(node):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and c.func.attr in AGENT_WRITE_ATTRS]


def _self_calls(node, names):
    return [c for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and isinstance(c.func.value, ast.Name) and c.func.value.id == "self" and c.func.attr in names]


def _agent_writers(methods):
    """Methods that write directly or through another method (closure)."""
    writers = {n for n, m in methods.items() if _write_calls(m)}
    changed = True
    while changed:
        changed = False
        for n, m in methods.items():
            if n not in writers and _self_calls(m, writers):
                writers.add(n)
                changed = True
    return writers


def _agent_tool_methods(methods):
    """The methods the tool dispatcher (_execute_tool) calls."""
    return {c.func.attr for c in _self_calls(methods["_execute_tool"], set(methods))}


def _is_question_refusal_call(call_node):
    return (
        isinstance(call_node, ast.Call) and isinstance(call_node.func, ast.Attribute)
        and call_node.func.attr == "question_refusal"
        and isinstance(call_node.func.value, ast.Name) and call_node.func.value.id == "write_fanout"
    )


def agent_method_is_guarded(method_node, writers) -> bool:
    """Every write call and every call to another writer in the method comes
    after an `x = write_fanout.question_refusal(...)` / `if x: return x`
    pair in the same statement list."""
    targets = {id(c) for c in _write_calls(method_node)} | {id(c) for c in _self_calls(method_node, writers)}
    if not targets:
        return False
    dominated = set()
    for stmts in _stmt_lists(method_node.body):
        for i, stmt in enumerate(stmts):
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)
                    and _is_question_refusal_call(stmt.value)):
                bound = stmt.targets[0].id
                if i + 1 < len(stmts) and _is_check_and_return(stmts[i + 1], bound):
                    for later in stmts[i + 2:]:
                        dominated.update(id(c) for c in ast.walk(later))
    return targets <= dominated


def _agent_violations(tree):
    methods = _agent_methods(tree)
    writers = _agent_writers(methods)
    write_tools = _agent_tool_methods(methods) & writers
    unguarded = sorted(n for n in write_tools if not agent_method_is_guarded(methods[n], writers))
    # A writer that isn't a tool entry must only be reachable from writers
    # that are guarded tools (helpers like _register_ha_automation).
    helpers = writers - write_tools - {"_execute_tool", "execute"}
    stray = sorted(
        h for h in helpers
        if not all(caller in write_tools for caller, m in methods.items() if _self_calls(m, {h}))
    )
    return write_tools, unguarded, stray


class TestAutomationAgentClosedWorld:
    def test_every_agent_write_tool_is_guarded(self):
        write_tools, unguarded, stray = _agent_violations(ast.parse(AGENT_SRC_PATH.read_text()))
        assert write_tools == AGENT_GUARDED | set(AGENT_ALLOWLIST), write_tools ^ (AGENT_GUARDED | set(AGENT_ALLOWLIST))
        assert unguarded == [], unguarded
        assert stray == [], stray
        assert "_exec_ha_service" in write_tools

    def test_read_tools_are_not_in_the_population(self):
        methods = _agent_methods(ast.parse(AGENT_SRC_PATH.read_text()))
        writers = _agent_writers(methods)
        assert {"_list_automations", "_get_state"} <= _agent_tool_methods(methods)
        assert not ({"_list_automations", "_get_state"} & writers)

    def test_negative_new_unguarded_write_tool_is_caught(self):
        src = """
class AutomationAgent:
    async def _execute_tool(self, name, args, context):
        if name == "ha_service":
            return await self._exec_ha_service(args, context)
        elif name == "unlock_everything":
            return await self._unlock_everything(args)

    async def _exec_ha_service(self, args, context):
        refusal = write_fanout.question_refusal("light", "turn_off", ())
        if refusal:
            return refusal
        await self.ha_client.call_service("light", "turn_off", {})

    async def _unlock_everything(self, args):
        await self.ha_client.call_service("lock", "unlock", {"entity_id": "all"})
"""
        write_tools, unguarded, stray = _agent_violations(ast.parse(src))
        assert write_tools == {"_exec_ha_service", "_unlock_everything"}
        assert unguarded == ["_unlock_everything"]

    def test_negative_helper_reached_from_an_unguarded_method_is_caught(self):
        src = """
class AutomationAgent:
    async def _execute_tool(self, name, args, context):
        return await self._create_automation(args, context)

    async def _create_automation(self, args, context):
        refusal = write_fanout.question_refusal("automation", "create", ())
        if refusal:
            return refusal
        await self._register(args)

    async def _register(self, args):
        await self.ha_client.create_automation("x", {})

    async def _sneaky(self):
        await self._register({})
"""
        write_tools, unguarded, stray = _agent_violations(ast.parse(src))
        assert unguarded == []
        assert stray == ["_register"]
