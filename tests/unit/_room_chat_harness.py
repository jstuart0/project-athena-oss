"""A multi-turn rig for route_control_node over a fictional house.

Real controller, real entity manager over fictional states, a real area
snapshot (North/South/Lower Bathroom, Study, Galley, each with lamps), the
real directory, and an in-memory conversation store that holds the last
context a turn stored, in the shape classify hands to route_control. Fakes
are at the HA service call, the states, the registry snapshot, the model and
the store.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import unittest.mock as mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()
_SRC = str(Path(__file__).resolve().parents[2] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import orchestrator.smart_home_controller as shc
from orchestrator import room_choice, write_fanout
from orchestrator.nodes import _runtime, route_control
from orchestrator.nodes.route_control import route_control_node
from orchestrator.state import OrchestratorState
from shared import ha_registry as hr

from .test_room_light_area_tier import Rig, _reg, _state, guest, light  # noqa: F401

LAMPS = {"north": 2, "south": 2, "lower": 2, "study": 3, "galley": 2}


def house_raw_and_states(*, big=0):
    areas = [{"area_id": f"area_{k}", "name": n, "aliases": []} for k, n in [
        ("north", "North Bathroom"), ("south", "South Bathroom"), ("lower", "Lower Bathroom"),
        ("study", "Study"), ("galley", "Galley"),
    ]]
    reg, states = [], {}
    for key, count in LAMPS.items():
        for i in range(count):
            eid = f"light.{key}_lamp_{i}"
            reg.append(_reg(eid, area=f"area_{key}"))
            states[eid] = _state(eid)
    if big:
        areas.append({"area_id": "area_big", "name": "Big", "aliases": []})
        for i in range(big):
            eid = f"light.big_{i}"
            reg.append(_reg(eid, area="area_big"))
            states[eid] = _state(eid)
    return {"areas": areas, "devices": [], "entities": reg}, states


class FakeLLM:
    def __init__(self):
        self.reply = {"device_type": "light", "room": None, "action": "get_status",
                      "target_scope": "group", "parameters": {}}
        self.calls = 0

    async def generate(self, **kwargs):
        self.calls += 1
        return {"response": json.dumps(self.reply)}


class _Admin:
    async def get_component_model(self, name):
        return None

    async def resolve_room_group(self, name):
        return None

    async def get_room_groups(self, enabled_only=True):
        return []


class Chat:
    def __init__(self, monkeypatch, *, threshold=50, hard_limit=100, big=0, exclude="[]", registry=True):
        raw, states = house_raw_and_states(big=big)
        self.rig = Rig(monkeypatch, states=states, threshold=threshold, hard_limit=hard_limit,
                       exclude=exclude, registry=registry)
        if registry:
            self.rig.registry._snapshot = hr.RegistrySnapshot.from_raw(raw, fetched_at=2.0)
        self.raw_registry = raw
        self.llm = FakeLLM()
        self.rig.controller.llm_router = self.llm
        monkeypatch.setattr(shc, "get_admin_client", lambda: _Admin())
        self.store = {}
        self.stores = []
        self.monkeypatch = monkeypatch
        route_control._NONCE_FALLBACK.clear()
        _runtime.set_smart_controller(self.rig.controller)
        _runtime.set_entity_manager(self.rig.manager)
        _runtime.set_ha_client(self.rig.raw)
        _runtime.set_sequence_executor(None)
        _runtime.set_automation_agent(None)
        _runtime.set_cache_client(None)
        self.clock = [1_000_000.0]
        monkeypatch.setattr(room_choice, "_now", lambda: self.clock[0])

    @property
    def written(self):
        return self.rig.written

    def reset_writes(self):
        self.rig.raw.call_service.reset_mock()

    async def _store(self, **kw):
        self.stores.append(kw)
        self.store[kw["session_id"]] = {
            k: kw[k] for k in ("intent", "query", "entities", "parameters", "response")
        }
        return True

    def stored_parameters(self, session="s1"):
        return (self.store.get(session) or {}).get("parameters") or {}

    @contextlib.contextmanager
    def patched(self):
        async def feature_config(name):
            return {"enabled": name in ("status_bulk_query", "status_skip_synthesis")}

        patches = [
            mock.patch("orchestrator.nodes.route_control.get_feature_config", new_callable=AsyncMock, side_effect=feature_config),
            mock.patch("orchestrator.nodes.route_control.get_automation_system_mode", new_callable=AsyncMock, return_value="pattern"),
            mock.patch("orchestrator.nodes.route_control.should_use_automation_agent", return_value=False),
            mock.patch("orchestrator.nodes.route_control.store_conversation_context", side_effect=self._store),
        ]
        for p in patches:
            p.start()
        try:
            yield
        finally:
            for p in reversed(patches):
                p.stop()

    def state(self, query, *, fp="fp-a", session="s1", room="study", supports_followup=True,
              perms=None, mode="owner", context_ref_info=None, trusted_voice=True, prev=...):
        state = OrchestratorState(query=query)
        state.mode = mode
        state.permissions = perms or {"mode": mode}
        state.room = room
        state.session_id = session
        state.prev_context = self.store.get(session) if prev is ... else prev
        state.context_ref_info = context_ref_info if context_ref_info is not None else {}
        state.node_timings = {}
        state.supports_followup = supports_followup
        state.caller_fingerprint = fp
        state.trusted_voice = trusted_voice
        return state

    async def aturn(self, query, **kw):
        state = self.state(query, **kw)
        return await route_control_node(state)

    def turn(self, query, **kw):
        with self.patched():
            return asyncio.run(self.aturn(query, **kw))

    def gather(self, *turns):
        """Concurrent turns sharing one set of patches."""
        # Both replies read the same stored context before either writes.
        states = [self.state(q, **kw) for q, kw in turns]

        async def go():
            return await asyncio.gather(*(route_control_node(s) for s in states))

        with self.patched():
            return asyncio.run(go())
