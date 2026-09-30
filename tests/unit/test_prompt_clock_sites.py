"""LLM prompts that carry the current time use the property clock."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from . import _public_audience_harness as h  # noqa: F401  (orchestrator import discipline)
from ._clock_fixture import UTC_POD, clock  # noqa: F401  (fixture)

import orchestrator.automation_agent as automation_agent_module
import orchestrator.smart_home_controller as smart_home_module
from orchestrator.automation_agent import AutomationAgent
from orchestrator.smart_home_controller import SmartHomeController

FROZEN = datetime(2026, 9, 30, 3, 17, tzinfo=timezone.utc)


def test_automation_agent_prompt_time(clock, monkeypatch):
    clock.use(UTC_POD)
    clock.frozen_utc(FROZEN)
    monkeypatch.setattr(automation_agent_module, "build_automation_system_prompt", AsyncMock(return_value="P"))
    agent = AutomationAgent.__new__(AutomationAgent)
    prompt = asyncio.run(agent._build_system_prompt("owner", "kitchen", None))
    assert "Time: 23:17" in prompt
    assert "Date: Tuesday, September 29" in prompt


def test_sequence_intent_prompt_time(clock, monkeypatch):
    clock.use(UTC_POD)
    clock.frozen_utc(FROZEN)
    admin = MagicMock()
    admin.get_component_model = AsyncMock(return_value={"model_name": "m", "enabled": True})
    monkeypatch.setattr(smart_home_module, "get_admin_client", lambda: admin)
    llm = MagicMock()
    llm.generate = AsyncMock(return_value={"response": '{"type": "sequence", "steps": []}'})
    controller = SmartHomeController.__new__(SmartHomeController)
    controller.llm_router = llm
    asyncio.run(controller.extract_sequence_intent("blink the lights", device_room="kitchen"))
    prompt = llm.generate.await_args.kwargs["prompt"]
    assert "Current time: 23:17" in prompt
