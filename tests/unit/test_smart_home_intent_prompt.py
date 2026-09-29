"""ATHENA-128 Phase 1 — prompt hardening snapshot test.

Pins the `extract_intent` LLM prompt so any future edit to the smart-home
intent prompt (device_type enum, STATUS QUERIES rule, few-shots) is
reviewed deliberately via the golden file, rather than drifting silently.
"""
from __future__ import annotations

import asyncio
import sys
import unittest.mock as mock
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

for _mod in ("prometheus_client", "langgraph", "langgraph.graph"):
    if _mod not in sys.modules:
        sys.modules[_mod] = mock.MagicMock()

sys.path.insert(0, "src")

import orchestrator.smart_home_controller as shc

GOLDEN_PATH = Path(__file__).parent / "fixtures" / "smart_home_intent_prompt.golden.txt"


def _run(coro):
    return asyncio.run(coro)


class _RecordingLLMRouter:
    def __init__(self):
        self.generate = AsyncMock(side_effect=self._generate)
        self.captured_prompt = None

    async def _generate(self, **kwargs):
        self.captured_prompt = kwargs["prompt"]
        return {
            "response": (
                '{"device_type": "light", "room": "den", "action": "turn_on", '
                '"target_scope": "group", "parameters": {}, "color_description": null}'
            )
        }


class _FakeAdminClient:
    async def get_component_model(self, name):
        return None


def test_extract_intent_prompt_matches_golden(monkeypatch):
    monkeypatch.setattr(shc, "get_admin_client", lambda: _FakeAdminClient())
    llm_router = _RecordingLLMRouter()
    controller = shc.SmartHomeController(entity_manager=MagicMock(), llm_router=llm_router)

    _run(controller.extract_intent("do the thing in the den"))

    llm_router.generate.assert_awaited_once()
    golden = GOLDEN_PATH.read_text()
    assert llm_router.captured_prompt == golden


def test_prompt_contains_never_actions_rule(monkeypatch):
    monkeypatch.setattr(shc, "get_admin_client", lambda: _FakeAdminClient())
    llm_router = _RecordingLLMRouter()
    controller = shc.SmartHomeController(entity_manager=MagicMock(), llm_router=llm_router)

    _run(controller.extract_intent("do the thing in the den"))

    assert "NEVER actions" in llm_router.captured_prompt
    assert '"are the kitchen lights currently on or off"' in llm_router.captured_prompt
